"""
MCPSessionManager — Session-based Trust & Diff Visualization
=============================================================
Provides:

  1. **Session-based trust** — "Approve for N minutes" on LOW-risk tools
     so users don't suffer validation fatigue.
  2. **Diff builder** — before executing a write tool, build a before/after
     diff dict that can be rendered in Gradio or any UI.
  3. **Approval state tracking** — per-tool, per-session approved windows.

Usage::

    mgr = MCPSessionManager(host=my_host, session_id="user_123")
    diff = await mgr.preview_diff("update_stock_with_movement", args)
    result = await mgr.execute("update_stock_with_movement", args)
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

from .constants import (
    TOOL_RISK_MAP,
    TOOL_SCOPE_MAP,
    PermissionScope,
    RiskLevel,
)
from .host_app import MCPPermissionHostApp

logger = logging.getLogger("MCP.Shield.SessionMgr")

# Default auto-approve window in seconds (5 minutes)
DEFAULT_TRUST_WINDOW_SECONDS: int = 300


class MCPSessionManager:
    """High-level interface for UI-facing code (Gradio, AG-UI, API).

    Wraps the ``MCPPermissionHostApp`` and adds session-level convenience:
    - Auto-approve repeated LOW-risk calls within a trust window.
    - Generate before/after diffs for write operations.
    """

    def __init__(
        self,
        host: MCPPermissionHostApp,
        session_id: str = "unknown",
        trust_window_seconds: int = DEFAULT_TRUST_WINDOW_SECONDS,
    ) -> None:
        self._host = host
        self.session_id = session_id
        self._trust_window = trust_window_seconds
        # {tool_name: expiry_timestamp}
        self._trusted_tools: Dict[str, float] = {}

    # ────────────── Session Trust ─────────────────────────────────────────

    def grant_trust(self, tool_name: str, duration_seconds: int | None = None) -> None:
        """Grant auto-approve on *tool_name* for *duration_seconds*.

        Only allowed for tools with risk <= MEDIUM.  HIGH/CRITICAL tools
        always require per-call approval.
        """
        risk = TOOL_RISK_MAP.get(tool_name, RiskLevel.MEDIUM)
        if risk in (RiskLevel.HIGH, RiskLevel.CRITICAL):
            logger.warning(
                "Cannot grant session trust for HIGH/CRITICAL tool '%s'", tool_name
            )
            return
        window = duration_seconds if duration_seconds else self._trust_window
        expiry = time.time() + window
        self._trusted_tools[tool_name] = expiry
        logger.info(
            "Session trust granted: tool=%s, window=%ds, session=%s",
            tool_name, window, self.session_id,
        )

    def revoke_trust(self, tool_name: str | None = None) -> None:
        """Revoke session trust for a specific tool or all tools."""
        if tool_name:
            self._trusted_tools.pop(tool_name, None)
        else:
            self._trusted_tools.clear()

    def is_trusted(self, tool_name: str) -> bool:
        """Check whether *tool_name* is currently auto-approved."""
        expiry = self._trusted_tools.get(tool_name)
        if expiry is None:
            return False
        if time.time() > expiry:
            del self._trusted_tools[tool_name]
            return False
        return True

    # ────────────── Execute ───────────────────────────────────────────────

    async def execute(
        self,
        tool_name: str,
        arguments: Dict[str, Any] | None = None,
    ) -> Any:
        """Execute a tool through the full Shield pipeline.

        If the tool is currently trusted via ``grant_trust()``, the HITL
        callback in the underlying client will be skipped for LOW/MEDIUM
        risk levels.
        """
        return await self._host.execute(tool_name, arguments)

    # ────────────── Diff Preview ──────────────────────────────────────────

    async def preview_diff(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        fetch_current_fn: Optional[Callable[..., Coroutine]] = None,
    ) -> Dict[str, Any]:
        """Build a before/after diff for a write operation.

        Parameters
        ----------
        tool_name : str
            The tool to preview.
        arguments : dict
            Arguments that will be passed to the tool.
        fetch_current_fn : callable, optional
            An async function ``(tool_name, args) -> current_state`` that
            fetches the current DB state.  If None, "before" will be empty.

        Returns
        -------
        dict with keys ``tool``, ``before``, ``after`` (proposed), ``risk``,
        ``scope``, and ``requires_approval``.
        """
        scope = TOOL_SCOPE_MAP.get(tool_name, PermissionScope.DB_DATA_WRITE)
        risk = TOOL_RISK_MAP.get(tool_name, RiskLevel.MEDIUM)

        before: Any = None
        if fetch_current_fn:
            try:
                before = await fetch_current_fn(tool_name, arguments)
            except Exception as exc:
                logger.warning("Could not fetch 'before' state for diff: %s", exc)

        # "after" is the proposed change (the arguments themselves)
        after = arguments

        requires_approval = (
            scope != PermissionScope.DB_READ_ONLY
            and risk in (RiskLevel.HIGH, RiskLevel.CRITICAL)
            and not self.is_trusted(tool_name)
        )

        return {
            "tool": tool_name,
            "before": before,
            "after": after,
            "risk": risk.value,
            "scope": scope.value,
            "requires_approval": requires_approval,
            "trusted_until": self._trusted_tools.get(tool_name),
        }

    # ────────────── Convenience: Batch read ───────────────────────────────

    async def safe_read(self, tool_name: str, arguments: Dict[str, Any] | None = None) -> Any:
        """Shortcut for read-only calls — identical to execute but clearer intent."""
        scope = TOOL_SCOPE_MAP.get(tool_name, PermissionScope.DB_DATA_WRITE)
        if scope != PermissionScope.DB_READ_ONLY:
            logger.warning("safe_read called on non-read tool '%s' — proceeding anyway.", tool_name)
        return await self._host.execute(tool_name, arguments)

    # ────────────── Info ──────────────────────────────────────────────────

    def get_trusted_tools(self) -> Dict[str, float]:
        """Return dict of {tool_name: remaining_seconds} for currently trusted tools."""
        now = time.time()
        result = {}
        expired = []
        for tool, expiry in self._trusted_tools.items():
            remaining = expiry - now
            if remaining > 0:
                result[tool] = round(remaining, 1)
            else:
                expired.append(tool)
        for t in expired:
            del self._trusted_tools[t]
        return result


if __name__ == "__main__":
    # Minimal CLI demo for MCPSessionManager.preview_diff
    import argparse
    import asyncio
    import json

    parser = argparse.ArgumentParser(description="MCPSessionManager demo (preview_diff)")
    parser.add_argument("--demo", action="store_true", help="Run demo preview_diff")
    parser.add_argument("--tool", type=str, default="update_stock_with_movement", help="Tool name to preview")
    parser.add_argument("--args", type=str, default='{"product_id": "P123", "delta": -5}', help="JSON string of arguments")
    ns = parser.parse_args()

    if not ns.demo:
        print("No action specified. Use --demo to run a preview_diff example.")
    else:
        try:
            arguments = json.loads(ns.args)
        except Exception as e:
            print("Could not parse --args as JSON:", e)
            arguments = {}

        mgr = MCPSessionManager(host=None, session_id="cli_demo")

        async def _run_demo():
            diff = await mgr.preview_diff(ns.tool, arguments)
            print(json.dumps(diff, ensure_ascii=False, indent=2))

        asyncio.run(_run_demo())
