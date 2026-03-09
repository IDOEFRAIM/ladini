"""
MCPPermissionHostApp — Intelligence de Risque (Pre-flight)
==========================================================
Sits between agents and the MCPPermissionClient.  Before any tool call
reaches the client it performs:

  1. **Pre-flight SQL injection detection** — regex-based scanning of all
     argument values against known SQL injection patterns.
  2. **Dynamic risk escalation** — bumps risk level to CRITICAL when
     argument values reference sensitive files (``.env``, ``id_rsa``…).
  3. **Descriptive error feedback** — when a call is blocked the host
     explains *why* so the agent can reformulate.
  4. **Prepared-statement enforcement** — rejects any argument that looks
     like raw SQL instead of structured parameters.

Usage::

    host = MCPPermissionHostApp(client)
    result = await host.execute("get_user_profile", {"user_id": "abc"})
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from .constants import (
    SENSITIVE_FILE_PATTERNS,
    SQL_INJECTION_PATTERNS,
    TOOL_RISK_MAP,
    TOOL_SCOPE_MAP,
    PermissionScope,
    RiskLevel,
)
from .client_base import MCPPermissionClient, PermissionDenied

logger = logging.getLogger("MCP.Shield.Host")

# Pre-compile regex patterns for performance
_SQL_RES = [re.compile(p) for p in SQL_INJECTION_PATTERNS]
_FILE_RES = [re.compile(p) for p in SENSITIVE_FILE_PATTERNS]


class PreflightResult:
    """Result of the pre-flight checks."""

    def __init__(self, passed: bool, reason: str = "", risk_override: Optional[RiskLevel] = None):
        self.passed = passed
        self.reason = reason
        self.risk_override = risk_override

    def __bool__(self) -> bool:
        return self.passed


class MCPPermissionHostApp:
    """Host-level risk intelligence layer.

    Parameters
    ----------
    client : MCPPermissionClient
        The secure client to delegate approved calls to.
    block_raw_sql : bool
        When True (default), any argument value containing SQL keywords
        is rejected even if it doesn't match a known injection pattern.
    """

    def __init__(
        self,
        client: MCPPermissionClient,
        block_raw_sql: bool = True,
    ) -> None:
        self._client = client
        self._block_raw_sql = block_raw_sql

    # ────────────── Public API ────────────────────────────────────────────

    async def execute(
        self,
        tool_name: str,
        arguments: Dict[str, Any] | None = None,
    ) -> Any:
        """Run pre-flight checks and delegate to the secure client.

        Raises ``HostBlockedError`` with an agent-friendly explanation
        when the call is blocked.
        """
        arguments = arguments or {}

        # 1. Pre-flight: SQL injection scan
        pf = self._preflight_scan(tool_name, arguments)
        if not pf:
            logger.warning("PRE-FLIGHT BLOCKED | tool=%s | reason=%s", tool_name, pf.reason)
            raise HostBlockedError(
                tool_name=tool_name,
                agent_message=pf.reason,
                suggestion=self._suggest_fix(tool_name, pf.reason),
            )

        # 2. Dynamic risk escalation (may upgrade risk before the client checks scope)
        if pf.risk_override:
            # Temporarily patch the risk map so the client sees the escalated level
            original_risk = TOOL_RISK_MAP.get(tool_name)
            TOOL_RISK_MAP[tool_name] = pf.risk_override
            try:
                result = await self._client.call_tool(tool_name, arguments)
            finally:
                # Restore original risk
                if original_risk is not None:
                    TOOL_RISK_MAP[tool_name] = original_risk
                else:
                    TOOL_RISK_MAP.pop(tool_name, None)
            return result

        # 3. Normal delegation
        try:
            return await self._client.call_tool(tool_name, arguments)
        except PermissionDenied as pd:
            # Enhance the error with a suggestion
            raise HostBlockedError(
                tool_name=pd.tool_name,
                agent_message=pd.reason,
                suggestion=self._suggest_fix(pd.tool_name, pd.reason),
            ) from pd

    def list_tools(self) -> list[dict]:
        """Pass-through to client."""
        return self._client.list_tools()

    # ────────────── Pre-flight scanning ──────────────────────────────────

    def _preflight_scan(self, tool_name: str, arguments: Dict[str, Any]) -> PreflightResult:
        """Scan all argument values for dangerous patterns.

        Returns a ``PreflightResult`` — truthy when safe, falsy when blocked.
        """
        risk_override: Optional[RiskLevel] = None

        # Flatten all string arg values (recursive)
        values = self._flatten_string_values(arguments)

        for val in values:
            # SQL injection check
            for rx in _SQL_RES:
                if rx.search(val):
                    return PreflightResult(
                        passed=False,
                        reason=(
                            f"Pattern SQL suspect détecté dans les arguments de '{tool_name}': "
                            f"'{rx.pattern}'. L'agent ne doit jamais générer de SQL brut — "
                            f"utilisez les outils MCP avec des paramètres structurés."
                        ),
                    )

            # Raw SQL keyword check (optional strict mode)
            if self._block_raw_sql and self._looks_like_raw_sql(val):
                return PreflightResult(
                    passed=False,
                    reason=(
                        f"L'argument semble contenir du SQL brut : '{val[:80]}…'. "
                        f"L'utilisation de SQL brut est interdite. Formulez votre requête "
                        f"via les paramètres structurés de l'outil '{tool_name}'."
                    ),
                )

            # Sensitive file access check → escalate risk
            for rx in _FILE_RES:
                if rx.search(val):
                    risk_override = RiskLevel.CRITICAL
                    logger.warning(
                        "RISK ESCALATED to CRITICAL: arg value '%s' matches sensitive file pattern '%s'",
                        val[:60], rx.pattern,
                    )

        return PreflightResult(passed=True, risk_override=risk_override)

    # ────────────── Helpers ───────────────────────────────────────────────

    @staticmethod
    def _flatten_string_values(obj: Any, _acc: List[str] | None = None) -> List[str]:
        """Recursively extract all string values from a nested dict/list."""
        if _acc is None:
            _acc = []
        if isinstance(obj, str):
            _acc.append(obj)
        elif isinstance(obj, dict):
            for v in obj.values():
                MCPPermissionHostApp._flatten_string_values(v, _acc)
        elif isinstance(obj, (list, tuple)):
            for v in obj:
                MCPPermissionHostApp._flatten_string_values(v, _acc)
        return _acc

    @staticmethod
    def _looks_like_raw_sql(val: str) -> bool:
        """Heuristic: return True if string looks like a raw SQL statement.

        Only triggers on multi-keyword statements (to avoid blocking single
        words like 'SELECT' in product names).
        """
        val_upper = val.upper().strip()
        sql_starters = ("SELECT ", "INSERT ", "UPDATE ", "DELETE ", "DROP ", "ALTER ", "CREATE ", "TRUNCATE ")
        if not any(val_upper.startswith(s) for s in sql_starters):
            return False
        # Must also contain WHERE/FROM/SET/INTO/TABLE to confirm it's a statement
        secondary = ("FROM ", "WHERE ", "SET ", "INTO ", "TABLE ", "VALUES")
        return any(s in val_upper for s in secondary)

    @staticmethod
    def _suggest_fix(tool_name: str, reason: str) -> str:
        """Generate a human-friendly suggestion the agent can use to retry."""
        scope = TOOL_SCOPE_MAP.get(tool_name, PermissionScope.DB_DATA_WRITE)

        if scope == PermissionScope.DB_SCHEMA_MODIFY:
            return (
                "Cette opération nécessite le mode maintenance. "
                "Contactez l'administrateur système pour l'activer."
            )
        if "SQL" in reason or "sql" in reason.lower():
            return (
                "Reformulez votre requête en utilisant uniquement les paramètres "
                f"structurés de l'outil '{tool_name}' (pas de SQL brut)."
            )
        if "confirmation humaine" in reason.lower() or "HITL" in reason:
            return (
                "Présentez un récapitulatif clair à l'utilisateur et attendez "
                "une confirmation explicite (OUI/NON) avant de réessayer."
            )
        return (
            f"Action non autorisée sur '{tool_name}'. Vérifiez les paramètres "
            "et réessayez avec des valeurs valides."
        )


# ────────────────────────────── Exceptions ────────────────────────────────

class HostBlockedError(Exception):
    """Raised when the host blocks a tool call.

    Contains both a technical reason and an agent-friendly suggestion
    so the LLM can reformulate its strategy.
    """

    def __init__(self, tool_name: str, agent_message: str, suggestion: str) -> None:
        self.tool_name = tool_name
        self.agent_message = agent_message
        self.suggestion = suggestion
        super().__init__(f"[HOST] {tool_name}: {agent_message} | Suggestion: {suggestion}")
