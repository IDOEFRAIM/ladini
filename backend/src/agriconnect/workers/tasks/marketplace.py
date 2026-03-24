"""
Marketplace background tasks.

Consumes pending agent actions for MarketplaceBackgroundAgent and executes
matching asynchronously through MCP tools.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Dict, List

from agriconnect.graphs.nodes.marketplace_background import MarketplaceBackgroundAgent
from agriconnect.protocols.mcp.infrastructure import AgriDBMCPServer, runtime
from agriconnect.workers.celery_app import celery_app
from agriconnect.workers.celery_config import TIME_LIMITS
from agriconnect.workers.task_base import AgriTask, error_result, success_result

logger = logging.getLogger("AgriConnect.tasks.marketplace")
_MARKET_LIMITS = TIME_LIMITS["marketplace"]

_SERVER: AgriDBMCPServer | None = None
_SERVER_LOCK = threading.Lock()


def _get_server() -> AgriDBMCPServer:
    global _SERVER
    if _SERVER is not None:
        return _SERVER

    with _SERVER_LOCK:
        if _SERVER is not None:
            return _SERVER
        runtime.start()
        _SERVER = AgriDBMCPServer()
        return _SERVER


def _extract_tool_data(tool_result: Any) -> Any:
    """Normalize MCP sync result payloads into raw data objects."""
    if isinstance(tool_result, dict) and "data" in tool_result:
        payload = tool_result.get("data")
        if isinstance(payload, dict) and "data" in payload:
            return payload.get("data")
        return payload
    return tool_result


def _safe_action_status_update(server: AgriDBMCPServer, action_id: str, status: str, note: str) -> None:
    try:
        server.call_tool_sync(
            "update_action_status",
            {
                "action_id": action_id,
                "new_status": status,
                "admin_notes": note,
            },
        )
    except Exception:
        logger.exception("Failed to update action status id=%s status=%s", action_id, status)


@celery_app.task(
    base=AgriTask,
    name="agriconnect.workers.tasks.marketplace.process_pending_actions",
    bind=True,
    max_retries=1,
    soft_time_limit=_MARKET_LIMITS["soft"],
    time_limit=_MARKET_LIMITS["hard"],
    acks_late=True,
    track_started=True,
)
def process_pending_actions(self, limit: int = 20) -> Dict[str, Any]:
    """Process queued RUN_MATCHING actions for MarketplaceBackgroundAgent."""
    try:
        server = _get_server()
        pending_raw = server.call_tool_sync(
            "get_pending_actions",
            {"agent_name": "MarketplaceBackgroundAgent", "limit": int(limit)},
        )
        pending_actions = _extract_tool_data(pending_raw) or []
        if not isinstance(pending_actions, list):
            pending_actions = []

        processed = 0
        executed = 0
        failed = 0

        background_agent = MarketplaceBackgroundAgent(mcp_session=server)

        for action in pending_actions:
            if not isinstance(action, dict):
                continue
            processed += 1
            action_id = action.get("id")
            action_type = str(action.get("action_type") or "")
            payload = action.get("payload") or {}

            if not action_id:
                failed += 1
                continue

            if action_type != "RUN_MATCHING":
                _safe_action_status_update(server, action_id, "FAILED", f"Unsupported action_type: {action_type}")
                failed += 1
                continue

            if not isinstance(payload, dict) or not payload.get("product_name"):
                _safe_action_status_update(server, action_id, "FAILED", "Missing payload.product_name")
                failed += 1
                continue

            try:
                created_matches: List[Dict[str, Any]] = asyncio.run(
                    background_agent.generate_matches_for_product(
                        product_name=str(payload.get("product_name")),
                        zone_id=payload.get("location"),
                        limit=10,
                    )
                )
                created_count = len(created_matches) if isinstance(created_matches, list) else 0
                _safe_action_status_update(
                    server,
                    action_id,
                    "EXECUTED",
                    f"Matching done. created_matches={created_count}",
                )
                executed += 1
            except Exception as exc:
                _safe_action_status_update(server, action_id, "FAILED", f"Matching error: {exc}")
                failed += 1

        return success_result(
            data={
                "processed": processed,
                "executed": executed,
                "failed": failed,
                "limit": int(limit),
            },
            task_name=self.name,
        )
    except Exception as exc:
        logger.exception("Marketplace action processor failed")
        return error_result(
            error=str(exc),
            task_name=self.name,
            retryable=True,
        )
