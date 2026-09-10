"""Orchestrator — point d'entrée métier UNIQUE d'Ladini.

Responsabilités (et rien d'autre) :
    1. charger le Workspace
    2. résoudre le contexte (agent collant)
    3. exécuter MarketCoach (graphe LangGraph)
    4. persister le Workspace

Flux : User → WorkspaceResolver → Orchestrator → Agent → MCP Runtime → Response.
Aucune autre couche métier. Checkpointer = WorkspaceStore (colonne metadata JSONB).
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from typing import Any, Dict, Optional

from ladini.graphs.agents.market_coach.utils import build_runtime, ensure_dict
from ladini.graphs.factory import GraphFactory
from ladini.graphs.roles import normalize_role
from ladini.workspace import Workspace, WorkspaceCheckpointer, WorkspaceResolver
from ladini.workspace.metadata import (
    LANGGRAPH_STATE_KEY,
    build_metadata_from_state,
)

logger = logging.getLogger("Ladini.Orchestrator")

# Clés d'état NON sérialisables / transitoires à exclure du snapshot Workspace.
_SNAPSHOT_EXCLUDE = frozenset(
    {
        "mc_runtime",
        "agent_config",
        "_runtime",
        "llm",
        "llm_client",
        "mcp_session",
        "db_client",
        "checkpointer",
    }
)

_FALLBACK_RESPONSE = (
    "Désolé, une difficulté technique est survenue. Veuillez reessayer.Si cela persiste,"
    "vous pouvez nous contacter au +226 68 81 52 99. L'equipe de LADINI vous presente ses escuses pour ce desagreement"
)
_MAX_AGENT_STEPS = 48
_AGENT_TIMEOUT_SECONDS = 45.0


class AgentCircuitBreaker(RuntimeError):
    """Raised when the LangGraph loop exceeds the allowed number of checkpoints."""


class _WorkspaceRunGuard:
    """Pins a workspace in RAM and enforces step limits."""

    def __init__(
        self,
        workspace: Workspace,
        checkpointer: WorkspaceCheckpointer,
        *,
        max_steps: int,
    ) -> None:
        self._workspace = workspace
        self._checkpointer = checkpointer
        self._max_steps = max_steps
        self._steps = 0
        self._attached = False

    def attach(self) -> None:
        if self._attached:
            return
        self._checkpointer.attach_workspace(
            self._workspace,
            on_checkpoint=self._on_checkpoint,
        )
        self._attached = True

    def detach(self) -> None:
        if not self._attached:
            return
        self._checkpointer.detach_workspace(self._workspace.workspace_id)
        self._attached = False

    def _on_checkpoint(self, metrics: Dict[str, Any]) -> None:
        self._steps += 1
        if self._steps > self._max_steps:
            raise AgentCircuitBreaker(
                f"workspace={self._workspace.workspace_id} exceeded {self._max_steps} checkpoints"
            )


def _json_safe(value: Any) -> Any:
    """Réduit une valeur à un sous-ensemble JSON-sérialisable."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {
            str(k): _json_safe(v)
            for k, v in value.items()
            if k not in _SNAPSHOT_EXCLUDE
        }
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return None


def _snapshot(state: Dict[str, Any]) -> Dict[str, Any]:
    minimal = build_metadata_from_state(state)
    minimal_size = len(json.dumps(minimal).encode("utf-8"))
    if minimal_size > 50_000:
        logger.critical(
            "Workspace snapshot exceeds 50KB even after sanitization (size=%s)",
            minimal_size,
        )
    return minimal


class Orchestrator:
    """Seul orchestrateur. Instancié une fois, réutilisable."""

    def __init__(self) -> None:
        self.resolver = WorkspaceResolver()
        self._checkpointer = WorkspaceCheckpointer(store=self.resolver.store)
        self._graph_factory = GraphFactory()

    async def handle(
        self,
        phone: str,
        user_query: str,
        workspace_type: str | None = None,
        force_role: bool = False,
        interactive_id: str | None = None,
        location_shared: bool = False,
        location_outcome: str | None = None,
        location_lat: float | None = None,
        location_lon: float | None = None,
    ) -> Dict[str, Any]:
        workspace_id = (phone or "anonymous").strip()
        ws = await self.resolver.resolve(workspace_id, workspace_type)

        guard = _WorkspaceRunGuard(ws, self._checkpointer, max_steps=_MAX_AGENT_STEPS)
        guard.attach()
        start_ts = time.monotonic()
        try:
            final = await asyncio.wait_for(
                self._run_market(
                    ws,
                    user_query,
                    phone,
                    force_role=force_role,
                    interactive_id=interactive_id,
                    location_shared=location_shared,
                    location_outcome=location_outcome,
                    location_lat=location_lat,
                    location_lon=location_lon,
                ),
                timeout=_AGENT_TIMEOUT_SECONDS,
            )
        except AgentCircuitBreaker as exc:
            elapsed_ms = round((time.monotonic() - start_ts) * 1000, 1)
            logger.error(
                "AGENT_CIRCUIT_BREAKER | workspace=%s | type=%s | max_steps=%s | duration_ms=%s | error=%s",
                workspace_id,
                ws.workspace_type,
                _MAX_AGENT_STEPS,
                elapsed_ms,
                exc,
            )
            await self._flush_workspace(ws, reason="circuit_breaker")
            return self._build_failure_response(ws, workspace_id)
        except asyncio.TimeoutError:
            elapsed_ms = round((time.monotonic() - start_ts) * 1000, 1)
            logger.error(
                "AGENT_TIMEOUT | workspace=%s | type=%s | timeout=%ss | elapsed_ms=%s | query=%r",
                workspace_id,
                ws.workspace_type,
                _AGENT_TIMEOUT_SECONDS,
                elapsed_ms,
                (user_query or "")[:160],
            )
            await self._flush_workspace(ws, reason="timeout")
            return self._build_failure_response(ws, workspace_id)
        except Exception as exc:  # pragma: no cover - safety net
            elapsed_ms = round((time.monotonic() - start_ts) * 1000, 1)
            logger.error(
                "AGENT_ERROR | agent=%s | workspace=%s | type=%s | duration_ms=%s | error=%s",
                ws.active_agent,
                workspace_id,
                ws.workspace_type,
                elapsed_ms,
                exc,
                exc_info=True,
            )
            await self._flush_workspace(ws, reason="agent_error")
            return self._build_failure_response(ws, workspace_id)
        else:
            elapsed_ms = round((time.monotonic() - start_ts) * 1000, 1)
            logger.info(
                "AGENT_COMPLETED | workspace=%s | type=%s | duration_ms=%s | goal=%s | status=%s",
                workspace_id,
                ws.workspace_type,
                elapsed_ms,
                final.get("current_goal"),
                final.get("status"),
            )
            langgraph_blob = (
                copy.deepcopy(ws.agent_state)
                if isinstance(ws.agent_state, dict)
                else None
            )
            self._sync_workspace(ws, final, langgraph_blob)
            await self._flush_workspace(ws, reason="completed")
            return {
                "final_response": final.get("final_response", _FALLBACK_RESPONSE),
                "agent": ws.active_agent,
                "workspace_id": workspace_id,
                # Indice pour la couche d'envoi (tasks.py) : ce tour se termine-t-il
                # sur une décision qui gagnerait à être rendue en boutons/liste ?
                "interactive": self._interactive_hint(final),
            }
        finally:
            guard.detach()

    @staticmethod
    def _interactive_hint(final: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Détecte si la réponse appelle un rendu interactif (bouton/liste).

        Ne construit RIEN de Twilio-spécifique ici (pas de ContentSid) : renvoie
        juste une intention sémantique que tasks.py mappe vers le bon template
        selon la config. `None` = texte simple.

        Source UNIQUE de vérité pour l'UI générative (audit MCP/AGUI) :
        ``ag_ui_component`` (construit par ``nodes/ui_engine.py`` à partir d'un
        ``MenuRequest``) était auparavant écrit dans l'état du graphe puis
        jamais lu par la couche d'envoi — tout menu construit via ce chemin
        restait invisible pour l'utilisateur WhatsApp, qui ne recevait que
        ``final_response`` en texte brut. On le traduit ici en premier ; le
        cas ``CONFIRM`` (indépendant de ``ag_ui_component``) reste géré
        ensuite tel quel.
        """
        if not isinstance(final, dict):
            return None

        # ListMenu (recherche produit, catalogue, stock, sélection
        # producteur...) : les composants interactifs `list_menu`/
        # `list_picker` de Meta/Twilio sont désactivés (2026-08-27) — trop
        # de blocages de template (erreurs 21656, schéma figé côté Console).
        # `ag_ui_component` continue d'être construit par `nodes/ui_engine.py`
        # (nécessaire pour `available_mapping`/`expected_candidates`, la
        # sélection par numéro), mais N'EST PLUS traduit en indice
        # interactif ici : `final_response` porte déjà le texte complet et
        # PAGINÉ (voir `services/text_pagination.py`), donc renvoyer `None`
        # fait retomber ce tour sur le texte brut chunké — jamais tronqué au
        # milieu d'un élément grâce aux marqueurs `PAGE_BREAK` posés par les
        # renderers (`nodes/rendering/success.py`,
        # `services/domain/cart_service.py`). Les confirmations
        # (QuickReplies, ci-dessous) restent interactives : elles ne
        # dépendent d'aucun schéma de liste dynamique côté template.
        ag_ui = final.get("ag_ui_component")

        # QuickReplies (audit UX interactive 2026-08-27) : remplace
        # FormConfirmation (nodes/confirmation_gate.py), un composant
        # orphelin que rien ne lisait jamais — la confirmation Oui/Non
        # retombait sur les flags status/response_strategy ci-dessous,
        # déconnectés du composant que ce nœud construisait pour elle.
        if isinstance(ag_ui, dict) and ag_ui.get("id") == ["ag_ui", "QuickReplies"]:
            kwargs = ag_ui.get("kwargs") or {}
            return {
                "kind": "quick_reply",
                "body": kwargs.get("body", ""),
                "buttons": (kwargs.get("buttons") or [])[:3],
            }

        strategy = str(final.get("response_strategy") or "").upper()
        status = str(final.get("status") or "").upper()
        # (2026-09-02, "no legacy shim") : `waiting_for_confirmation` retiré —
        # les deux écrivains (market_coach/confirmation_gate.py, agents/
        # forms.py) posent TOUJOURS `status`/`response_strategy` en même
        # temps, déjà couverts ci-dessous ; c'était un 3e signal redondant.
        if status == "WAITING_CONFIRMATION" or strategy == "CONFIRMATION":
            # Filet de sécurité : confirmation binaire OUI/NON sans
            # ag_ui_component QuickReplies (ex. checkpoint en vol produit
            # avant ce correctif) → boutons quick-reply génériques.
            return {"kind": "confirm"}
        return None

    # ------------------------------------------------------------------
    # Agent execution
    # ------------------------------------------------------------------
    _ROLE_CHECK_TIMEOUT = 8.0

    async def _run_market(
        self,
        ws: Workspace,
        user_query: str,
        phone: str,
        *,
        force_role: bool = False,
        interactive_id: str | None = None,
        location_shared: bool = False,
        location_outcome: str | None = None,
        location_lat: float | None = None,
        location_lon: float | None = None,
    ) -> Dict[str, Any]:
        config = {"configurable": {"thread_id": ws.workspace_id}}
        agent_metadata = {
            k: v for k, v in (ws.metadata or {}).items() if k != LANGGRAPH_STATE_KEY
        }
        inputs = {
            **agent_metadata,
            "user_query": user_query,
            "user_phone": phone,
            "current_goal": ws.active_goal or None,
            "active_form": ws.active_form,
            # Clic interactif (bouton/liste WhatsApp) : amorce le bypass LLM de
            # input_interpreter. None pour un message texte classique.
            "interactive_selection": interactive_id or None,
            # Position GPS reçue et DÉJÀ persistée ce tour (webhook, appel
            # SYNCHRONE avant l'enqueue Celery depuis 2026-09-02 — voir
            # core/location.py) — signal purement conversationnel pour
            # l'étape finale de l'onboarding (agents/onboarding.py::_step_collect_location).
            "location_shared": bool(location_shared),
            # Issue EXACTE de cette persistance — consommée directement par
            # flows/buyer/gps_delivery_gate.py::resolve_gps_stage au lieu
            # d'une relecture DB (élimine la course webhook/tâche + le repli
            # silencieux sur une position périmée).
            "location_outcome": location_outcome,
            "location_lat": location_lat,
            "location_lon": location_lon,
        }
        runtime = build_runtime()
        async with runtime as live_runtime:
            # Lie le téléphone au runtime AVANT tout appel MCP de ce tour : filet
            # de sécurité global contre les PermissionDenied silencieux quand un
            # site d'appel (gateway ou tool direct) oublie de transmettre `phone`.
            live_runtime.bind_user(phone)
            role = "BUYER" if ws.workspace_type == "buyer" else "PRODUCER"
            profile_res = None

            if not force_role:
                meta_role = (
                    ws.metadata.get("user_role")
                    if isinstance(ws.metadata, dict)
                    else None
                ) or (
                    (ws.metadata.get("transaction_payload") or {}).get("role")
                    if isinstance(ws.metadata, dict)
                    else None
                )
                if isinstance(meta_role, str) and meta_role.strip().upper() in {
                    "BUYER",
                    "ACHETEUR",
                    "ACHETEUSE",
                }:
                    role = "BUYER"

                if role == "PRODUCER" and phone:
                    try:
                        raw = await asyncio.wait_for(
                            live_runtime.call_db(
                                "get_user_by_phone", phone=str(phone).strip()
                            ),
                            timeout=self._ROLE_CHECK_TIMEOUT,
                        )
                        profile_res = ensure_dict(raw)
                        if str(profile_res.get("status") or "").upper() == "SUCCESS":
                            prof = profile_res.get("data") or {}
                            prof_role = str(prof.get("role") or "").upper().strip()
                            if prof_role in {"BUYER", "ACHETEUR", "ACHETEUSE"}:
                                role = "BUYER"
                    except Exception:
                        pass

            role = normalize_role(role)
            inputs["user_role"] = role

            # Forward profile result so input_normalizer skips the redundant MCP call.
            if profile_res is not None:
                profile_status = str(profile_res.get("status") or "").upper()
                if profile_status == "SUCCESS":
                    prof = profile_res.get("data") or {}
                    inputs.update(
                        {
                            "user_context_loaded": True,
                            "is_onboarding": False,
                            "user_name": prof.get("name") or "N/A",
                            "zone_name": (prof.get("zone") or {}).get("name"),
                            "zone_id": (prof.get("zone") or {}).get("id"),
                        }
                    )
                    user_uuid = prof.get("id")
                    if user_uuid:
                        inputs["user_id"] = str(user_uuid)
                elif profile_status == "NEW_USER":
                    inputs.update(
                        {
                            "user_context_loaded": False,
                            "is_onboarding": True,
                            "onboarding_step": "COLLECT_ROLE",
                            "onboarding_internal_step": "COLLECT_ROLE",
                        }
                    )

            logger.info(
                "Orchestrator | phone=%s | resolved role=%s | ws_type=%s",
                phone,
                role,
                ws.workspace_type,
            )

            graph = self._graph_factory.get_graph(
                role,
                mc_runtime=live_runtime,
                checkpointer=self._checkpointer,
            )
            return await graph.ainvoke(inputs, config=config)

    # ------------------------------------------------------------------
    # Workspace sync
    # ------------------------------------------------------------------
    @staticmethod
    def _sync_workspace(
        ws: Workspace, final: Dict[str, Any], langgraph_state: Dict[str, Any] | None
    ) -> None:
        role_raw = final.get("user_role")
        if not role_raw and isinstance(final.get("transaction_payload"), dict):
            role_raw = (final.get("transaction_payload") or {}).get("role")
        if isinstance(role_raw, str):
            role_up = role_raw.strip().upper()
            if role_up in {"BUYER", "ACHETEUR", "ACHETEUSE"}:
                ws.workspace_type = "buyer"
            elif role_up in {"PRODUCER", "PRODUCTEUR", "PRODUCTRICE"}:
                ws.workspace_type = "producer"

        ws.active_goal = (
            str(final.get("current_goal") or "") if final.get("current_goal") else ""
        )
        ws.active_form = final.get("active_form")
        checkpoint_blob = langgraph_state or ws.metadata.get(LANGGRAPH_STATE_KEY)
        if isinstance(checkpoint_blob, dict):
            ws.agent_state = checkpoint_blob
        ws.metadata = _snapshot(final)
        if checkpoint_blob is not None:
            ws.metadata[LANGGRAPH_STATE_KEY] = checkpoint_blob
        success = str(final.get("goal_status") or "").upper() == "COMPLETED"
        success = success or str(final.get("status") or "").upper() in {
            "COMPLETED",
            "SUCCESS",
        }
        if success:
            ws.close_tunnel()
        elif ws.active_goal or ws.active_form:
            ws.locked_agent = ws.active_agent
        else:
            ws.locked_agent = None
        ws.mark_dirty()

    async def _flush_workspace(self, ws: Workspace, *, reason: str) -> None:
        if not ws.is_dirty:
            logger.debug(
                "Workspace flush skipped | workspace=%s | reason=%s (clean state)",
                ws.workspace_id,
                reason,
            )
            return

        # Pruning lourd (deepcopy + json + summary/windows) fait ICI, une seule
        # fois par tour, au lieu d'à chaque nœud (voir checkpointer._persist_state).
        try:
            flush_metrics = self._checkpointer.finalize_for_persistence(ws)
        except Exception:  # pragma: no cover - ne jamais bloquer le flush
            logger.debug(
                "finalize_for_persistence a échoué (non bloquant)", exc_info=True
            )
            flush_metrics = {}

        logger.info(
            "Workspace flush | workspace=%s | reason=%s | bytes=%s | truncated=%s",
            ws.workspace_id,
            reason,
            flush_metrics.get("payload_bytes"),
            flush_metrics.get("truncated", False),
        )
        try:
            persisted = await self.resolver.store.save(ws)
        except Exception as exc:  # pragma: no cover - persistence safety net
            logger.error(
                "Workspace flush failed | workspace=%s | reason=%s | error=%s",
                ws.workspace_id,
                reason,
                exc,
                exc_info=True,
            )
            return

        if persisted:
            ws.reset_dirty()
        else:
            logger.error(
                "Workspace flush unsuccessful (store.save returned False) | workspace=%s | reason=%s",
                ws.workspace_id,
                reason,
            )

    @staticmethod
    def _build_failure_response(ws: Workspace, workspace_id: str) -> Dict[str, Any]:
        return {
            "final_response": _FALLBACK_RESPONSE,
            "agent": ws.active_agent,
            "workspace_id": workspace_id,
        }
