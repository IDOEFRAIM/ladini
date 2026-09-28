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

from ladini.core.conversation_lock import conversation_turn_lock
from ladini.core.idempotency import get_cached as _get_role_hint
from ladini.core.idempotency import release as _release_role_hint
from ladini.graphs.agents.market_coach.core import turn_trace
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

def _tt_final(final: Any) -> None:
    try:
        from ladini.core import turn_telemetry

        turn_telemetry.note_final(final)
    except Exception:  # pragma: no cover
        pass


def _tt_error(code: str, category: str) -> None:
    try:
        from ladini.core import turn_telemetry

        turn_telemetry.note_error(code, category)
    except Exception:  # pragma: no cover
        pass


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
        message_sid: str | None = None,
        channel: str = "WHATSAPP",
    ) -> Dict[str, Any]:
        workspace_id = (phone or "anonymous").strip()

        # (Phase 2 hardening, commit 9, mandat décision E) : sérialise TOUT le tour — de la
        # résolution du Workspace à sa persistance finale — par conversation, avec une attente
        # bornée. Sans ceci, deux messages quasi simultanés (deux requêtes webchat, deux
        # workers Celery) sur le MÊME numéro pouvaient lire le même état de départ et écrire
        # chacun leur patch sans jamais voir celui de l'autre (lost update). Point d'entrée
        # UNIQUE (`Orchestrator.handle`) — webchat ET WhatsApp en bénéficient tous les deux
        # sans code dupliqué par canal.
        async with conversation_turn_lock(workspace_id, timeout_seconds=_AGENT_TIMEOUT_SECONDS):
            ws = await self.resolver.resolve(workspace_id, workspace_type)
            guard = _WorkspaceRunGuard(ws, self._checkpointer, max_steps=_MAX_AGENT_STEPS)
            guard.attach()
            # (Phase 2 hardening, commit 11) : ouvre la capture `TurnTrace` avec l'état TEL
            # QUE LE TOUR PRÉCÉDENT L'A LAISSÉ — le seul endroit où lire "goal AVANT ce
            # tour"/"pending AVANT ce tour" sans le confondre avec ce que CE tour va
            # produire. `ws.metadata` n'est qu'un snapshot OPS minimal
            # (`build_metadata_from_state`, ex. juste `pending_interaction_kind`, pas le
            # dict complet — voir sa docstring) : la source DURABLE et complète de l'état
            # métier plat d'un tour est le checkpoint LangGraph lui-même
            # (`checkpoint["channel_values"]`), la même chose que LangGraph décode en
            # interne pour reprendre le thread — jamais une reconstruction parallèle qui
            # pourrait diverger. `_load_before_state` réutilise donc le checkpointer déjà
            # attaché (`guard.attach()` ci-dessus vient de le pinner en RAM) au lieu d'une
            # 2e lecture DB. Voir `core/turn_trace.py` pour le reste de la capture (avant
            # nettoyage) et la fermeture (tous les chemins de sortie, ci-dessous).
            before_state = await self._load_before_state(workspace_id)
            turn_trace.start(
                conversation_phone=phone,
                message_id=message_sid,
                channel=channel,
                before_state=before_state,
            )
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
                        message_sid=message_sid,
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
                _tt_error("AGENT_CIRCUIT_BREAKER", "INTERNAL")
                turn_trace.finish(error_class="AgentCircuitBreaker")
                await self._reconcile_workspace_after_failure(ws, workspace_id)
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
                _tt_error("AGENT_TIMEOUT", "TIMEOUT")
                turn_trace.finish(error_class="TimeoutError")
                await self._reconcile_workspace_after_failure(ws, workspace_id)
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
                _tt_error(type(exc).__name__, "INTERNAL")
                turn_trace.finish(error_class=type(exc).__name__)
                await self._reconcile_workspace_after_failure(ws, workspace_id)
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
                _tt_final(final)  # télémétrie du tour (best-effort, sans effet sur le métier)
                turn_trace.finish()
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
        message_sid: str | None = None,
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
            # (2026-09-12) Identifiant STABLE de l'événement entrant — voir
            # `api/tasks.py::process_agent_task` docstring. Clé du cache
            # d'interprétation LLM (`interpreter/routing.py::
            # _cached_llm_completion`) : un retry Celery de la tâche
            # (même MessageSid) ne doit jamais repayer l'appel LLM déjà
            # réussi de ce tour.
            "message_sid": message_sid,
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

            # (2026-09-13, incident WhatsApp #3 — utilisateur double-rôle) :
            # `ws.workspace_type` est STICKY sur le dernier rôle utilisé dans
            # CE workspace (même clé pour buyer/producer, un seul numéro de
            # téléphone). Un producteur qui répond "confirmer"/"annuler" à
            # une notification de commande reçue, alors que son workspace
            # est resté en mode BUYER (session buyer antérieure jamais
            # nettoyée), voit son message classifié contre le catalogue
            # BUYER — qui ne contient même pas PRODUCER_CONFIRM_ORDER/
            # PRODUCER_CANCEL_ORDER. Aucun réglage du prompt NEW_TASK ne
            # peut compenser un mauvais catalogue de départ.
            #
            # `flows/buyer/preorder_confirmation.py` pose un indice d'ÉTAT
            # (jamais un mot-clé du texte reçu ici) juste après l'appel MCP
            # qui a mis CETTE notification producteur en file — lu et
            # consommé (relâché) ici, donc sans effet sur les tours
            # suivants de ce même numéro. Fail-open comme tout
            # `core/idempotency.py` : une panne Redis dégrade simplement
            # vers le rôle sticky actuel.
            role_hint_applied = False
            if not force_role and phone:
                role_hint = _get_role_hint(f"pending_role_hint:{phone}")
                if role_hint:
                    _release_role_hint(f"pending_role_hint:{phone}")
                    hint_up = str(role_hint).strip().upper()
                    if hint_up in {"PRODUCER", "PRODUCTEUR", "PRODUCTRICE"}:
                        role = "PRODUCER"
                        role_hint_applied = True
                    elif hint_up in {"BUYER", "ACHETEUR", "ACHETEUSE"}:
                        role = "BUYER"
                        role_hint_applied = True

            # L'indice ci-dessus reflète un état RÉEL et plus récent que
            # `ws.metadata`/le profil (qui datent tous deux du dernier tour
            # BUYER de ce même numéro, potentiellement périmé) — une fois
            # posé, il prime sur les deux heuristiques suivantes plutôt que
            # d'être aussitôt écrasé par elles.
            if not force_role and not role_hint_applied:
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

    async def _reconcile_workspace_after_failure(self, ws: Workspace, workspace_id: str) -> None:
        """Réconciliation best-effort de `ws.active_goal`/`ws.active_form`/... sur les 3
        chemins d'échec (`AgentCircuitBreaker`, `TimeoutError`, `Exception` générique).

        `_sync_workspace` est la SEULE fonction qui dérive ces champs d'un résultat de
        tour RÉEL — elle n'était appelée que sur le chemin succès. Or chaque nœud qui a
        RÉELLEMENT terminé avant l'échec a déjà persisté sa progression dans le
        checkpoint LangGraph (`workspace/checkpointer.py::_persist_state`, après CHAQUE
        nœud) — ce checkpoint est donc une vérité fiable même quand le tour entier
        échoue plus loin. Sans cette réconciliation, `ws.active_goal` reste figé à sa
        valeur D'AVANT ce tour, et `_run_market` la réinjecte comme INPUT explicite du
        graphe au tour suivant (`MarketAgentState.current_goal` est `replace_value`),
        ressuscitant un but déjà invalide/abandonné.

        Best-effort à dessein (même esprit que `_load_before_state`) : une erreur ici
        ne doit jamais faire remonter une 2e exception — dans le pire cas, `ws` garde
        sa valeur pré-tour, dégradation silencieuse plutôt qu'un crash du tour.
        """
        try:
            config = {"configurable": {"thread_id": workspace_id}}
            tup = await self._checkpointer.aget_tuple(config)
            if tup is None:
                return
            channel_values = tup.checkpoint.get("channel_values")
            if not isinstance(channel_values, dict) or not channel_values:
                return
            langgraph_blob = (
                copy.deepcopy(ws.agent_state) if isinstance(ws.agent_state, dict) else None
            )
            goal_before = ws.active_goal
            self._sync_workspace(ws, channel_values, langgraph_blob)
            # (2026-09-28, observability P1-C) : `workspace_reconciled`, sans texte
            # de message ni contenu métier — seulement les identifiants de
            # corrélation et les 2 valeurs de `active_goal` en jeu, utiles pour
            # distinguer un stale goal réellement effacé d'un no-op silencieux.
            logger.info(
                "workspace_reconciled | workspace=%s | goal_before=%s | goal_after=%s",
                workspace_id,
                goal_before or None,
                ws.active_goal or None,
            )
        except Exception:  # pragma: no cover - défensif, jamais vers l'appelant
            logger.debug(
                "workspace reconciliation ignorée après échec | workspace=%s",
                workspace_id,
                exc_info=True,
            )

    async def _load_before_state(self, workspace_id: str) -> Dict[str, Any]:
        """État métier plat du DERNIER checkpoint LangGraph de ce thread — best-effort,
        UNIQUEMENT pour `TurnTrace` (Phase 2 hardening, commit 11) : une erreur ici ne
        doit jamais faire échouer le tour réel. `{}` sur un thread neuf (aucun
        checkpoint) — comportement attendu, pas une erreur."""
        try:
            config = {"configurable": {"thread_id": workspace_id}}
            tup = await self._checkpointer.aget_tuple(config)
            if tup is None:
                return {}
            channel_values = tup.checkpoint.get("channel_values")
            return channel_values if isinstance(channel_values, dict) else {}
        except Exception:  # pragma: no cover - défensif, jamais vers l'appelant
            logger.debug(
                "turn_trace before_state ignoré | workspace=%s", workspace_id, exc_info=True
            )
            return {}

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
