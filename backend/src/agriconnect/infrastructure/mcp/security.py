from __future__ import annotations

import asyncio
import os
import contextvars
import hashlib
import importlib
import json
import logging
import re
import time
import uuid
from collections import deque
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, Iterable, List, Optional, Protocol, Tuple

from pydantic import BaseModel, Field

logger = logging.getLogger("MCP.Core.Security")
_REQUEST_ID_CTX: contextvars.ContextVar[str] = contextvars.ContextVar("mcp_request_id", default="")


class PermissionScope(str, Enum):
    DB_READ_ONLY = "DB_READ_ONLY"
    DB_DATA_WRITE = "DB_DATA_WRITE"
    DB_SCHEMA_MODIFY = "DB_SCHEMA_MODIFY"


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


TOOL_SCOPE_MAP: dict[str, PermissionScope] = {
    "get_user_profile": PermissionScope.DB_READ_ONLY,
    "get_user_by_phone": PermissionScope.DB_READ_ONLY,
    "list_products": PermissionScope.DB_READ_ONLY,
    "search_products": PermissionScope.DB_READ_ONLY,
    "get_orders": PermissionScope.DB_READ_ONLY,
    "get_pending_actions": PermissionScope.DB_READ_ONLY,
    "get_open_auctions": PermissionScope.DB_READ_ONLY,
    "get_farm_stocks": PermissionScope.DB_READ_ONLY,
    "get_stocks": PermissionScope.DB_READ_ONLY,
    "get_stock_movements": PermissionScope.DB_READ_ONLY,
    "get_stock_history": PermissionScope.DB_READ_ONLY,
    "normalize_unit": PermissionScope.DB_READ_ONLY,
    "guess_category": PermissionScope.DB_READ_ONLY,
    "get_producer_dashboard": PermissionScope.DB_READ_ONLY,
    "get_clients": PermissionScope.DB_READ_ONLY,
    "get_farms": PermissionScope.DB_READ_ONLY,
    "get_expense_summary": PermissionScope.DB_READ_ONLY,
    "get_expenses": PermissionScope.DB_READ_ONLY,
    "get_user_context_state": PermissionScope.DB_READ_ONLY,
    "list_market_matches": PermissionScope.DB_READ_ONLY,
    "db_status": PermissionScope.DB_READ_ONLY,
    "identify_or_create_user": PermissionScope.DB_DATA_WRITE,
    "create_product": PermissionScope.DB_DATA_WRITE,
    "create_order": PermissionScope.DB_DATA_WRITE,
    "create_auction": PermissionScope.DB_DATA_WRITE,
    "place_bid": PermissionScope.DB_DATA_WRITE,
    "update_stock_with_movement": PermissionScope.DB_DATA_WRITE,
    "prepare_transaction_staging": PermissionScope.DB_DATA_WRITE,
    "commit_staged_transaction": PermissionScope.DB_DATA_WRITE,
    "create_agent_action": PermissionScope.DB_DATA_WRITE,
    "update_action_status": PermissionScope.DB_DATA_WRITE,
    "get_or_create_farm": PermissionScope.DB_DATA_WRITE,
    "add_stock": PermissionScope.DB_DATA_WRITE,
    "remove_stock": PermissionScope.DB_DATA_WRITE,
    "add_expense": PermissionScope.DB_DATA_WRITE,
    "update_order_status": PermissionScope.DB_DATA_WRITE,
    "update_farm": PermissionScope.DB_DATA_WRITE,
    "register_surplus_offer": PermissionScope.DB_DATA_WRITE,
    "upsert_user_context_state": PermissionScope.DB_DATA_WRITE,
    "create_market_match": PermissionScope.DB_DATA_WRITE,
    "migrate_schema": PermissionScope.DB_SCHEMA_MODIFY,
    "drop_table": PermissionScope.DB_SCHEMA_MODIFY,
    "alter_table": PermissionScope.DB_SCHEMA_MODIFY,
    "search_agronomy_docs": PermissionScope.DB_READ_ONLY,
    "search_past_interactions": PermissionScope.DB_READ_ONLY,
    "persist_conversation": PermissionScope.DB_DATA_WRITE,
    # Moderation / anti-abuse
    "get_account_status": PermissionScope.DB_READ_ONLY,
    "get_prohibited_terms": PermissionScope.DB_READ_ONLY,
    "record_moderation_strike": PermissionScope.DB_DATA_WRITE,
    "record_demand_signal": PermissionScope.DB_DATA_WRITE,
}


# Préfixes de LECTURE explicites. Tout ce qui ne commence pas par l'un d'eux
# (et n'est pas déjà mappé) est considéré comme une ÉCRITURE par défaut :
# fail-safe vers le chemin de contrôle plutôt que d'auto-autoriser un outil
# inconnu potentiellement destructeur en READ_ONLY.
_READ_PREFIXES = (
    "get", "list", "search", "fetch", "read", "guess", "normalize",
    "check", "validate", "count", "find", "resolve", "db_status",
)


def _guess_scope(tool_name: str) -> PermissionScope:
    name = (tool_name or "").lower()
    if name.startswith(("migrate", "drop", "alter", "truncate")):
        return PermissionScope.DB_SCHEMA_MODIFY
    if name.startswith(_READ_PREFIXES):
        return PermissionScope.DB_READ_ONLY
    # Défaut fail-safe : écriture (scrutiny), jamais lecture auto-autorisée.
    return PermissionScope.DB_DATA_WRITE


_scopes_filled = False


def _autofill_tool_scopes() -> None:
    """Ensure tools exposed by handlers are assigned a scope.

    Called lazily on first use (via ``ensure_scopes_filled``) instead of at
    import time to avoid cascading imports (h.py → AgriDatabaseService → all
    mixins) when security.py is merely imported.
    """
    global _scopes_filled
    if _scopes_filled:
        return
    _scopes_filled = True

    handlers = None
    last_error: Exception | None = None
    for module_path in (
        "agriconnect.protocols.mcp.servers.h",
        "agriconnect.protocols.mcp.handlers",
    ):
        try:
            module = importlib.import_module(module_path)
            handlers = getattr(module, "TOOL_HANDLERS", None)
        except Exception as exc:  # pragma: no cover - defensive
            last_error = exc
            continue
        if handlers:
            break

    if not handlers:
        logger.debug("TOOL_HANDLERS import failed; cannot autofill scopes: %s", last_error)
        return

    for tool in handlers.keys():
        TOOL_SCOPE_MAP.setdefault(tool, _guess_scope(tool))


def ensure_scopes_filled() -> None:
    """Public trigger for lazy scope initialization."""
    if not _scopes_filled:
        _autofill_tool_scopes()


TOOL_RISK_MAP: dict[str, RiskLevel] = {
    "create_order": RiskLevel.HIGH,
    "prepare_transaction_staging": RiskLevel.HIGH,
    "commit_staged_transaction": RiskLevel.HIGH,
    "migrate_schema": RiskLevel.CRITICAL,
    "drop_table": RiskLevel.CRITICAL,
    "alter_table": RiskLevel.CRITICAL,
    "search_agronomy_docs": RiskLevel.LOW,
    "search_past_interactions": RiskLevel.LOW,
}

SENSITIVE_COLUMNS = frozenset(
    {
        "password_hash",
        "password",
        "hashed_password",
        "email",
        "token",
        "secret",
        "api_key",
        "id_rsa",
        "private_key",
        "refresh_token",
        "access_token",
        "credit_card",
        "ssn",
        "otp",
    }
)

SQL_INJECTION_PATTERNS = [
    r"(?i)\\bDROP\\s+TABLE\\b",
    r"(?i)\\bDELETE\\s+FROM\\b",
    r"(?i)\\bTRUNCATE\\b",
    r"(?i)\\bALTER\\s+TABLE\\b",
    r"(?i)\\bOR\\s+1\\s*=\\s*1",
    r"(?i)\\bUNION\\s+(ALL\\s+)?SELECT\\b",
]

SQL_INJECTION_REGEX = [re.compile(pattern) for pattern in SQL_INJECTION_PATTERNS]

SENSITIVE_FILE_PATTERNS = [r"\\.env", r"id_rsa", r"\\.pem$", r"\\.key$", r"credentials"]


class MCPServerKind(str, Enum):
    DB = "db"


class MCPToolMeta(BaseModel):
    name: str
    server: MCPServerKind
    scope: PermissionScope
    risk: RiskLevel
    description: str = ""
    timeout_seconds: float = Field(default=15.0, ge=0.1)
    retries: int = Field(default=1, ge=0, le=5)


class ToolExecutionMeta(BaseModel):
    request_id: str
    user_id: str
    tool_name: str
    duration_ms: float
    timed_out: bool = False
    token_estimate: int = 0


class ToolExecutionEnvelope(BaseModel):
    ok: bool
    data: dict[str, Any] = Field(default_factory=dict)
    error: str = ""
    meta: ToolExecutionMeta


class ToolRateLimiter:
    """In-memory per-user per-tool rate limiter (sliding window with eviction)."""

    def __init__(self, max_calls: int = 60, window_seconds: float = 60.0, max_keys: int = 2048) -> None:
        self.max_calls = max(1, int(max_calls))
        self.window_seconds = max(1.0, float(window_seconds))
        self.max_keys = max(128, int(max_keys))
        self._events: dict[str, deque[float]] = {}

    def check(self, key: str) -> tuple[bool, str]:
        now = time.monotonic()
        events = self._events.setdefault(key, deque())
        cutoff = now - self.window_seconds
        while events and events[0] < cutoff:
            events.popleft()
        if len(events) >= self.max_calls:
            return False, f"rate_limit_exceeded:{self.max_calls}/{int(self.window_seconds)}s"
        events.append(now)
        if len(self._events) > self.max_keys:
            self._prune_stale(now)
        return True, "ok"

    def _prune_stale(self, now: float) -> None:
        stale_keys = [k for k, v in self._events.items() if not v or v[-1] < now - (self.window_seconds * 5)]
        for key in stale_keys:
            self._events.pop(key, None)


class ToolExecutionPolicy:
    """Central policy for sanitation, timeout, rate-limit and audit metadata."""

    def __init__(
        self,
        registry: Optional[MCPToolRegistry] = None,
        max_calls_per_minute: int = 60,
        default_timeout_seconds: float = 30.0,
    ) -> None:
        self._registry = registry or get_registry()
        self._limiter = ToolRateLimiter(max_calls=max_calls_per_minute, window_seconds=60.0)
        self._default_timeout = max(0.1, float(default_timeout_seconds))
        self._overrides: dict[str, float] = {}

    async def execute(
        self,
        tool_name: str,
        handler: Callable[..., Coroutine[Any, Any, Any]],
        arguments: Optional[Dict[str, Any]] = None,
        *,
        user_id: str = "anonymous",
        request_id: str = "",
        timeout_seconds: Optional[float] = None,
    ) -> dict[str, Any]:
        arguments = self.sanitize_arguments(arguments or {})
        rid = request_id or _REQUEST_ID_CTX.get() or str(uuid.uuid4())
        _REQUEST_ID_CTX.set(rid)

        logger.info(f"TOOL_NAME:{tool_name} - TIMEOUT_SECONDS:{timeout_seconds}")
        limiter_key = f"{user_id}:{tool_name}"
        allowed, reason = self._limiter.check(limiter_key)
        if not allowed:
            raise PermissionDenied(tool_name, reason)

        meta = self._registry.get_tool(tool_name)
        # per-call override > env overrides > registry meta > default
        if timeout_seconds is not None:
            timeout = float(timeout_seconds)
        elif tool_name in getattr(self, "_overrides", {}):
            timeout = float(self._overrides[tool_name])
        else:
            timeout = float(meta.timeout_seconds if meta else self._default_timeout)
        start = time.monotonic()

        try:
            raw = await asyncio.wait_for(handler(**arguments), timeout=timeout)
            
            
            normalized = MCPPermissionClient._normalize_output(raw)
            elapsed = round((time.monotonic() - start) * 1000, 1)
            envelope = ToolExecutionEnvelope(
                ok=True,
                data=normalized,
                meta=ToolExecutionMeta(
                    request_id=rid,
                    user_id=user_id,
                    tool_name=tool_name,
                    duration_ms=elapsed,
                    timed_out=False,
                    token_estimate=self._estimate_tokens(normalized),
                ),
            )
            logger.info(
                "TOOL_AUDIT|%s",
                json.dumps(envelope.model_dump(mode="json"), ensure_ascii=False),
            )
            return envelope.model_dump()
        except asyncio.TimeoutError as exc:
            elapsed = round((time.monotonic() - start) * 1000, 1)
            envelope = ToolExecutionEnvelope(
                ok=False,
                data={},
                error=f"timeout_after_{timeout:.1f}s",
                meta=ToolExecutionMeta(
                    request_id=rid,
                    user_id=user_id,
                    tool_name=tool_name,
                    duration_ms=elapsed,
                    timed_out=True,
                    token_estimate=0,
                ),
            )
            logger.warning(
                "TOOL_TIMEOUT|%s",
                json.dumps(envelope.model_dump(mode="json"), ensure_ascii=False),
            )
            raise ToolExecutionTimeout(tool_name, timeout) from exc

    @staticmethod
    def sanitize_arguments(arguments: Dict[str, Any]) -> Dict[str, Any]:
        def _clean(v: Any) -> Any:
            if isinstance(v, str):
                cleaned = v.replace("\x00", "").strip()
                if len(cleaned) > 4000:
                    cleaned = cleaned[:4000]
                return cleaned
            if isinstance(v, dict):
                return {str(k): _clean(val) for k, val in v.items()}
            if isinstance(v, list):
                return [_clean(x) for x in v]
            return v

        return _clean(arguments)

    @staticmethod
    def _estimate_tokens(payload: dict[str, Any]) -> int:
        try:
            s = json.dumps(payload, ensure_ascii=False)
            return max(1, len(s) // 4)
        except Exception:
            return 0


_GLOBAL_EXECUTION_POLICY: Optional[ToolExecutionPolicy] = None


def get_execution_policy() -> ToolExecutionPolicy:
    global _GLOBAL_EXECUTION_POLICY
    ensure_scopes_filled()
    if _GLOBAL_EXECUTION_POLICY is None:
        default_env = os.getenv("MCP_DEFAULT_TOOL_TIMEOUT")
        try:
            default_timeout = float(default_env) if default_env is not None else 30.0
        except Exception:
            default_timeout = 30.0
        _GLOBAL_EXECUTION_POLICY = ToolExecutionPolicy(default_timeout_seconds=default_timeout)
        # Load optional per-tool overrides from env var JSON: {"search_agronomy_docs": 60}
        overrides_raw = os.getenv("MCP_TOOL_TIMEOUT_OVERRIDES")
        if overrides_raw:
            try:
                parsed = json.loads(overrides_raw)
                if isinstance(parsed, dict):
                    for k, v in parsed.items():
                        try:
                            _GLOBAL_EXECUTION_POLICY._overrides[str(k)] = float(v)
                        except Exception:
                            pass
            except Exception:
                logger.warning("Invalid MCP_TOOL_TIMEOUT_OVERRIDES; must be JSON mapping tool->seconds")
    return _GLOBAL_EXECUTION_POLICY


class MCPToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, MCPToolMeta] = {}
        self.register_defaults()

    def register_defaults(self) -> None:
        for name, scope in TOOL_SCOPE_MAP.items():
            self._tools[name] = MCPToolMeta(
                name=name,
                server= MCPServerKind.DB,
                scope=scope,
                risk=TOOL_RISK_MAP.get(name, RiskLevel.MEDIUM),
                timeout_seconds= 90.0,
                retries=1,
            )

    def has_tool(self, tool_name: str) -> bool:
        return tool_name in self._tools

    def get_tool(self, tool_name: str) -> Optional[MCPToolMeta]:
        return self._tools.get(tool_name)

    def list_tools(self, server: Optional[MCPServerKind] = None) -> list[dict[str, Any]]:
        items = []
        for meta in self._tools.values():
            if server and meta.server != server:
                continue
            items.append(meta.model_dump())
        items.sort(key=lambda x: x["name"])
        return items

    def sync_discovered_tools(self, server: MCPServerKind, discovered: Iterable[dict[str, Any]]) -> None:
        for item in discovered:
            name = str(item.get("name") or "").strip()
            if not name or name in self._tools:
                continue
            self._tools[name] = MCPToolMeta(
                name=name,
                server=server,
                scope=PermissionScope.DB_DATA_WRITE,
                risk= RiskLevel.MEDIUM,
                description=str(item.get("description") or ""),
                timeout_seconds= 30.0,
                retries=1,
            )


_GLOBAL_REGISTRY: Optional[MCPToolRegistry] = None


def get_registry() -> MCPToolRegistry:
    global _GLOBAL_REGISTRY
    if _GLOBAL_REGISTRY is None:
        _GLOBAL_REGISTRY = MCPToolRegistry()
    return _GLOBAL_REGISTRY


class PermissionDenied(Exception):
    def __init__(self, tool_name: str, reason: str) -> None:
        self.tool_name = tool_name
        self.reason = reason
        super().__init__(f"[SHIELD] {tool_name}: {reason}")


class ToolExecutionTimeout(Exception):
    """Un outil a dépassé son délai. Distinct de PermissionDenied : c'est un
    problème de latence/DB, pas d'autorisation — les appelants ne doivent pas
    l'afficher comme un refus d'accès."""

    def __init__(self, tool_name: str, timeout_seconds: float) -> None:
        self.tool_name = tool_name
        self.timeout_seconds = timeout_seconds
        super().__init__(f"[TIMEOUT] {tool_name}: dépassé {timeout_seconds:.1f}s")


class HostBlockedError(Exception):
    def __init__(self, tool_name: str, agent_message: str, suggestion: str) -> None:
        self.tool_name = tool_name
        self.agent_message = agent_message
        self.suggestion = suggestion
        super().__init__(f"[HOST] {tool_name}: {agent_message} | Suggestion: {suggestion}")


class PermissionDecision(BaseModel):
    allowed: bool
    decision: str
    reason: str
    scope: PermissionScope
    risk: RiskLevel


class MCPPermissionClient:
    def __init__(
        self,
        backend: Any,
        session_id: str = "unknown",
        maintenance_mode: bool = False,
        hitl_callback: Optional[Callable[..., Coroutine[Any, Any, bool]]] = None,
        registry: Optional[MCPToolRegistry] = None,
    ) -> None:
        self._backend = backend
        self.session_id = session_id
        self.maintenance_mode = maintenance_mode
        self._hitl_callback = hitl_callback
        self._registry = registry or get_registry()

    async def call_tool(self, tool_name: str, arguments) -> Any:
        arguments = arguments or {}
        args_hash = self._hash_args(arguments)
        t0 = time.monotonic()

        if not self._registry.has_tool(tool_name):
            raise PermissionDenied(tool_name, "Tool not registered in registry")

        decision = self._check_permission(tool_name, arguments)
        if not decision.allowed:
            if decision.decision == "HITL_REQUIRED":
                approved = await self._request_hitl(tool_name, arguments, decision.reason)
                if not approved:
                    raise PermissionDenied(tool_name, decision.reason)
            else:
                raise PermissionDenied(tool_name, decision.reason)

        result = await self._backend.call_tool(tool_name, arguments)
        result = self._normalize_output(self._mask_sensitive(result))
        logger.info(
            "AUDIT|%s",
            json.dumps(
                {
                    "timestamp": time.time(),
                    "session_id": self.session_id,
                    "tool_name": tool_name,
                    "arguments_hash": args_hash,
                    "decision": "ALLOW",
                    "duration_ms": round((time.monotonic() - t0) * 1000, 1),
                },
                ensure_ascii=False,
            ),
        )
        return result

    async def execute(self, tool_name: str, arguments: Dict[str, Any] | None = None) -> Any:
        return await self.call_tool(tool_name, arguments)

    def list_tools(self) -> list[dict]:
        return self._registry.list_tools()

    def _check_permission(self, tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> PermissionDecision:
        meta = self._registry.get_tool(tool_name)
        scope = meta.scope if meta else TOOL_SCOPE_MAP.get(tool_name, PermissionScope.DB_DATA_WRITE)
        risk = meta.risk if meta else TOOL_RISK_MAP.get(tool_name, RiskLevel.MEDIUM)

        if arguments:
            suspicious, reason = self._scan_arguments_for_risk(arguments)
            if suspicious:
                return PermissionDecision(allowed=False, decision="HITL_REQUIRED", reason=reason, scope=scope, risk=RiskLevel.CRITICAL)

        if scope == PermissionScope.DB_SCHEMA_MODIFY:
            if self.maintenance_mode:
                return PermissionDecision(allowed=True, decision="ALLOW", reason="maintenance", scope=scope, risk=risk)
            return PermissionDecision(allowed=False, decision="DENY", reason="Schema modification denied", scope=scope, risk=risk)

        if scope == PermissionScope.DB_READ_ONLY:
            return PermissionDecision(allowed=True, decision="ALLOW", reason="read-only", scope=scope, risk=risk)

        if risk in (RiskLevel.HIGH, RiskLevel.CRITICAL):
            return PermissionDecision(allowed=False, decision="HITL_REQUIRED", reason="Human confirmation required", scope=scope, risk=risk)

        return PermissionDecision(allowed=True, decision="ALLOW", reason="write-allowed", scope=scope, risk=risk)

    async def _request_hitl(self, tool_name: str, args: Dict[str, Any], reason: str) -> bool:
        if self._hitl_callback is None:
            return False
        try:
            return await self._hitl_callback(tool_name, args, reason)
        except Exception:
            return False

    def _mask_sensitive(self, data: Any, seen: Optional[set[int]] = None) -> Any:
        if seen is None:
            seen = set()
        obj_id = id(data)
        if obj_id in seen:
            return "***RECURSION***"
        seen.add(obj_id)
        if isinstance(data, dict):
            return {k: ("***MASKED***" if k.lower() in SENSITIVE_COLUMNS else self._mask_sensitive(v, seen)) for k, v in data.items()}
        if isinstance(data, list):
            return [self._mask_sensitive(x, seen) for x in data]
        if isinstance(data, str):
            try:
                parsed = json.loads(data)
                if isinstance(parsed, (dict, list)):
                    return self._mask_sensitive(parsed, seen)
            except Exception:
                pass
        return data

    def _scan_arguments_for_risk(self, arguments: Dict[str, Any]) -> Tuple[bool, str]:
        json_blob = json.dumps(arguments, default=str, ensure_ascii=False)
        if any(pattern.search(json_blob) for pattern in SQL_INJECTION_REGEX):
            return True, "sql_pattern_detected"
        for key, value in arguments.items():
            if isinstance(value, str) and len(value) > 4000:
                return True, f"arg_{key}_too_large"
            if "sql" in key.lower():
                return True, f"arg_{key}_sql_key"
        return False, "ok"

    @staticmethod
    def _normalize_output(data: Any) -> dict:
        if isinstance(data, dict):
            return data
        if isinstance(data, str):
            try:
                parsed = json.loads(data)
                return parsed if isinstance(parsed, dict) else {"result": parsed}
            except Exception:
                return {"result": data}
        if isinstance(data, (list, tuple)):
            return {"items": list(data)}
        return {"result": str(data)}

    @staticmethod
    def _hash_args(arguments: Dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(arguments, sort_keys=True, default=str).encode()).hexdigest()[:16]


class PreflightResult:
    def __init__(self, passed: bool, reason: str = "", risk_override: Optional[RiskLevel] = None):
        self.passed = passed
        self.reason = reason
        self.risk_override = risk_override

    def __bool__(self) -> bool:
        return self.passed


class MCPPermissionHostApp:
    def __init__(self, client: MCPPermissionClient, block_raw_sql: bool = True) -> None:
        self._client = client
        self._block_raw_sql = block_raw_sql
        self._sql_re = SQL_INJECTION_REGEX
        self._file_re = [re.compile(p) for p in SENSITIVE_FILE_PATTERNS]

    async def execute(self, tool_name: str, arguments: Dict[str, Any] | None = None) -> Any:
        arguments = arguments or {}
        pf = self._preflight_scan(tool_name, arguments)
        if not pf:
            raise HostBlockedError(tool_name, pf.reason, self._suggest_fix(tool_name, pf.reason))
        try:
            return await self._client.call_tool(tool_name, arguments)
        except PermissionDenied as pd:
            raise HostBlockedError(pd.tool_name, pd.reason, self._suggest_fix(pd.tool_name, pd.reason)) from pd

    def list_tools(self) -> list[dict]:
        return self._client.list_tools()

    def _preflight_scan(self, tool_name: str, arguments: Dict[str, Any]) -> PreflightResult:
        values = self._flatten_strings(arguments)
        for val in values:
            for rx in self._sql_re:
                if rx.search(val):
                    return PreflightResult(False, f"SQL suspect pattern: {rx.pattern}")
            if self._block_raw_sql and self._looks_like_raw_sql(val):
                return PreflightResult(False, "Raw SQL is forbidden")
            for rx in self._file_re:
                if rx.search(val):
                    return PreflightResult(True, risk_override=RiskLevel.CRITICAL)
        return PreflightResult(True)

    def _suggest_fix(self, tool_name: str, reason: str) -> str:
        scope = TOOL_SCOPE_MAP.get(tool_name, PermissionScope.DB_DATA_WRITE)
        if scope == PermissionScope.DB_SCHEMA_MODIFY:
            return "Enable maintenance mode for schema changes."
        if "SQL" in reason:
            return f"Use structured arguments for tool '{tool_name}', not raw SQL."
        return f"Check arguments and retry '{tool_name}'."

    @staticmethod
    def _flatten_strings(obj: Any, acc: Optional[List[str]] = None) -> List[str]:
        if acc is None:
            acc = []
        if isinstance(obj, str):
            acc.append(obj)
        elif isinstance(obj, dict):
            for v in obj.values():
                MCPPermissionHostApp._flatten_strings(v, acc)
        elif isinstance(obj, (list, tuple)):
            for v in obj:
                MCPPermissionHostApp._flatten_strings(v, acc)
        return acc

    @staticmethod
    def _looks_like_raw_sql(val: str) -> bool:
        vu = val.upper().strip()
        starters = ("SELECT ", "INSERT ", "UPDATE ", "DELETE ", "DROP ", "ALTER ", "CREATE ", "TRUNCATE ")
        if not any(vu.startswith(s) for s in starters):
            return False
        secondary = ("FROM ", "WHERE ", "SET ", "INTO ", "TABLE ", "VALUES")
        return any(s in vu for s in secondary)


class MCPSessionManager:
    def __init__(self, host: MCPPermissionHostApp, session_id: str = "unknown", trust_window_seconds: int = 300) -> None:
        self._host = host
        self.session_id = session_id
        self._trust_window = trust_window_seconds
        self._trusted_tools: Dict[str, float] = {}

    async def execute(self, tool_name: str, arguments: Dict[str, Any] | None = None) -> Any:
        return await self._host.execute(tool_name, arguments)

    async def safe_read(self, tool_name: str, arguments: Dict[str, Any] | None = None) -> Any:
        return await self._host.execute(tool_name, arguments)


class AsyncMCPBackend(Protocol):
    async def call_tool(self, name: str, arguments) -> Any:
        ...

    async def list_tools(self) -> list[dict[str, Any]]:
        ...



class DBInProcessBackend:
    async def call_tool(self, name: str, arguments) -> Any:
        from agriconnect.protocols.mcp.servers.h import TOOL_HANDLERS

        fn = TOOL_HANDLERS.get(name)
        if fn is None:
            raise ValueError(f"Unknown DB tool: {name}")
        raw = await fn(**arguments)
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except Exception:
                return {"result": raw}
        return raw

    async def list_tools(self) -> list[dict[str, Any]]:
        from agriconnect.protocols.mcp.servers.h import TOOL_DESCRIPTIONS

        return [{"name": n, "description": d} for n, d in TOOL_DESCRIPTIONS.items()]


class MCPManager:
    def __init__(self, registry: MCPToolRegistry | None = None) -> None:
        self.registry = registry or get_registry()

        self._backends: dict[MCPServerKind, AsyncMCPBackend] = {
            MCPServerKind.DB: DBInProcessBackend(),
        }
        self._ready = False

    async def ensure_ready(self) -> None:
        if self._ready:
            return
        
        # Synchronisation uniquement pour le backend DB
        db_tools = await self._backends[MCPServerKind.DB].list_tools()
        self.registry.sync_discovered_tools(MCPServerKind.DB, db_tools)
        
        self._ready = True

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any] | None = None) -> Any:
        await self.ensure_ready()
        arguments = arguments or {}
        meta = self.registry.get_tool(tool_name)
        if meta is None:
            raise ValueError(f"Unknown tool '{tool_name}'")

        backend = self._backends[meta.server]
        attempts = 1 + max(0, int(meta.retries))
        last_error: Optional[Exception] = None
        
        for attempt in range(1, attempts + 1):
            try:
                return await backend.call_tool(tool_name, arguments)
            except Exception as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                    
        raise RuntimeError(f"MCP call failed for '{tool_name}': {last_error}")

    async def list_tools(self) -> dict[str, list[dict[str, Any]]]:
        await self.ensure_ready()
        # Exposition uniquement des outils DB
        return {
            "db_tools": self.registry.list_tools(MCPServerKind.DB),
        }


class ShieldHub:
    def __init__(self, session_id: str = "unknown") -> None:
        self.registry = get_registry()
        self.manager = MCPManager(registry=self.registry)
        self._shield = MCPPermissionClient(backend=self.manager, session_id=session_id, registry=self.registry)
        self.session = MCPSessionManager(host=MCPPermissionHostApp(client=self._shield), session_id=session_id)

    async def call(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        await self.manager.ensure_ready()
        meta = self.registry.get_tool(tool_name)
        if meta is None:
            raise ValueError(f"Unknown MCP tool: {tool_name}")
        if meta.scope == PermissionScope.DB_READ_ONLY:
            return await self.session.safe_read(tool_name, arguments)
        return await self.session.execute(tool_name, arguments)

    async def list_tools(self) -> dict[str, list[dict]]:
        return await self.manager.list_tools()


class MCPShield:
    def __init__(self, session_id: str = "unknown") -> None:
        self._hub = ShieldHub(session_id=session_id)

    async def authorize_and_call(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        return await self._hub.call(tool_name, arguments)

    async def list_allowed_tools(self) -> dict[str, list[dict]]:
        return await self._hub.list_tools()


class UnifiedMCPClient:
    def __init__(self, session_id: str = "unknown") -> None:
        self._hub = ShieldHub(session_id=session_id)

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any] | None = None) -> Any:
        return await self._hub.call(tool_name, arguments or {})

    async def list_tools(self) -> dict[str, list[dict]]:
        return await self._hub.list_tools()
