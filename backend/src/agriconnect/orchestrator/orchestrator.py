"""Orchestrator — point d'entrée métier UNIQUE d'AgriConnect.

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
from typing import Any, Dict

from agriconnect.workspace import Workspace, WorkspaceCheckpointer, WorkspaceResolver
from agriconnect.workspace.metadata import LANGGRAPH_STATE_KEY, build_metadata_from_state
from agriconnect.graphs.factory import GraphFactory
from agriconnect.graphs.roles import normalize_role
from agriconnect.graphs.agents.market_coach.utils import build_runtime, ensure_dict

logger = logging.getLogger("AgriConnect.Orchestrator")

# Clés d'état NON sérialisables / transitoires à exclure du snapshot Workspace.
_SNAPSHOT_EXCLUDE = frozenset({
    "mc_runtime",
    "agent_config",
    "_runtime",
    "llm",
    "llm_client",
    "mcp_session",
    "db_client",
    "checkpointer",
})

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
        return {str(k): _json_safe(v) for k, v in value.items() if k not in _SNAPSHOT_EXCLUDE}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return None


def _snapshot(state: Dict[str, Any]) -> Dict[str, Any]:
    minimal = build_metadata_from_state(state)
    minimal_size = len(json.dumps(minimal).encode("utf-8"))
    if minimal_size > 50_000:
        logger.critical("Workspace snapshot exceeds 50KB even after sanitization (size=%s)", minimal_size)
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
    ) -> Dict[str, Any]:
        workspace_id = (phone or "anonymous").strip()
        ws = await self.resolver.resolve(workspace_id, workspace_type)

        guard = _WorkspaceRunGuard(ws, self._checkpointer, max_steps=_MAX_AGENT_STEPS)
        guard.attach()
        try:
            final = await asyncio.wait_for(
                self._run_market(ws, user_query, phone, force_role=force_role),
                timeout=_AGENT_TIMEOUT_SECONDS,
            )
        except AgentCircuitBreaker as exc:
            logger.error(
                "Agent circuit breaker triggered | workspace=%s | reason=%s",
                workspace_id,
                exc,
            )
            await self._flush_workspace(ws, reason="circuit_breaker")
            return self._build_failure_response(ws, workspace_id)
        except asyncio.TimeoutError:
            logger.error(
                "Agent timeout | workspace=%s | timeout=%ss",
                workspace_id,
                _AGENT_TIMEOUT_SECONDS,
            )
            await self._flush_workspace(ws, reason="timeout")
            return self._build_failure_response(ws, workspace_id)
        except Exception as exc:  # pragma: no cover - safety net
            logger.error("Agent %s failed for %s: %s", ws.active_agent, workspace_id, exc, exc_info=True)
            await self._flush_workspace(ws, reason="agent_error")
            return self._build_failure_response(ws, workspace_id)
        else:
            langgraph_blob = copy.deepcopy(ws.agent_state) if isinstance(ws.agent_state, dict) else None
            self._sync_workspace(ws, final, langgraph_blob)
            await self._flush_workspace(ws, reason="completed")
            return {
                "final_response": final.get("final_response", _FALLBACK_RESPONSE),
                "agent": ws.active_agent,
                "workspace_id": workspace_id,
            }
        finally:
            guard.detach()

    # ------------------------------------------------------------------
    # Agent execution
    # ------------------------------------------------------------------
    async def _run_market(
        self,
        ws: Workspace,
        user_query: str,
        phone: str,
        *,
        force_role: bool = False,
    ) -> Dict[str, Any]:
        config = {"configurable": {"thread_id": ws.workspace_id}}
        agent_metadata = {
            k: v for k, v in (ws.metadata or {}).items() if k != LANGGRAPH_STATE_KEY
        }
        inputs = {
            **agent_metadata,
            "user_query": user_query,
            "user_phone": phone,
            "current_goal": ws.active_goal,
            "active_form": ws.active_form,
        }
        runtime = build_runtime()
        async with runtime as live_runtime:
            role = "BUYER" if ws.workspace_type == "buyer" else "PRODUCER"

            if not force_role:
                meta_role = (
                    (ws.metadata.get("user_role") if isinstance(ws.metadata, dict) else None)
                    or ((ws.metadata.get("transaction_payload") or {}).get("role") if isinstance(ws.metadata, dict) else None)
                )
                if isinstance(meta_role, str) and meta_role.strip().upper() in {"BUYER", "ACHETEUR", "ACHETEUSE"}:
                    role = "BUYER"

                if role == "PRODUCER" and phone:
                    try:
                        raw = await live_runtime.call_db("get_user_by_phone", phone=str(phone).strip())
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
            logger.info("Orchestrator | phone=%s | resolved role=%s | ws_type=%s", phone, role, ws.workspace_type)

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
    def _sync_workspace(ws: Workspace, final: Dict[str, Any], langgraph_state: Dict[str, Any] | None) -> None:
        role_raw = final.get("user_role")
        if not role_raw and isinstance(final.get("transaction_payload"), dict):
            role_raw = (final.get("transaction_payload") or {}).get("role")
        if isinstance(role_raw, str):
            role_up = role_raw.strip().upper()
            if role_up in {"BUYER", "ACHETEUR", "ACHETEUSE"}:
                ws.workspace_type = "buyer"
            elif role_up in {"PRODUCER", "PRODUCTEUR", "PRODUCTRICE"}:
                ws.workspace_type = "producer"

        ws.active_goal = str(final.get("current_goal") or "")
        ws.active_form = final.get("active_form")
        checkpoint_blob = langgraph_state or ws.metadata.get(LANGGRAPH_STATE_KEY)
        if isinstance(checkpoint_blob, dict):
            ws.agent_state = checkpoint_blob
        ws.metadata = _snapshot(final)
        if checkpoint_blob is not None:
            ws.metadata[LANGGRAPH_STATE_KEY] = checkpoint_blob
        success = str(final.get("goal_status") or "").upper() == "COMPLETED"
        success = success or str(final.get("status") or "").upper() in {"COMPLETED", "SUCCESS"}
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

        logger.info(
            "Workspace flush | workspace=%s | reason=%s",
            ws.workspace_id,
            reason,
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
