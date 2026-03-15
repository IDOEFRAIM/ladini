"""
MCPPermissionClient — The Security Shield
==========================================
Zero-Trust client that wraps any MCP tool-calling backend (in-process
AgriDBMCPServer or remote AgriMCPClient) and enforces:

  1. **Pydantic schema validation** on every call (no raw JSON trust).
  2. **Permission scopes** — DB_READ_ONLY / DB_DATA_WRITE / DB_SCHEMA_MODIFY.
  3. **Data masking** — strips sensitive columns before returning to LLM.
  4. **Structured JSON audit** — every call is logged with argument hash,
     session id, decision, and latency.

Usage::

    from agriconnect.protocols.mcp.security import MCPPermissionClient
    client = MCPPermissionClient(backend=my_server, session_id="user123")
    result = await client.call_tool("list_products", {"producer_id": "..."})
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from typing import Any, Callable, Coroutine, Dict, List, Optional

from pydantic import BaseModel, ValidationError, create_model

from .constants import (
    SENSITIVE_COLUMNS,
    TOOL_SCOPE_MAP,
    PermissionScope,
    RiskLevel,
)
from .mcp_registry import MCPToolRegistry, get_registry

logger = logging.getLogger("MCP.Shield.Client")


# ────────────────────────────── Pydantic schema registry ──────────────────

# Pre-defined schemas for known tools (imported from core.models when possible).
# If a tool is NOT listed here, a permissive baseline schema is used.
try:
    from agriconnect.core.models import (
        CreateProductInput,
        PrepareTransactionInput,
        UpdateStockInput,
        CreateAgentActionInput,
        CreateAuctionInput,
    )
except ImportError:
    CreateProductInput = None
    PrepareTransactionInput = None
    UpdateStockInput = None
    CreateAgentActionInput = None
    CreateAuctionInput = None


# Maps tool name → Pydantic model class used to validate arguments.
_SCHEMA_REGISTRY: Dict[str, type[BaseModel] | None] = {
    "create_product": CreateProductInput,
    "prepare_transaction_staging": PrepareTransactionInput,
    "update_stock_with_movement": UpdateStockInput,
    "create_agent_action": CreateAgentActionInput,
    "create_auction": CreateAuctionInput,
}


# ───────────────────── Lightweight schemas for common read tools ──────────

class _UserIdSchema(BaseModel):
    user_id: str

class _PhoneSchema(BaseModel):
    phone: str

class _ProducerIdSchema(BaseModel):
    producer_id: str

class _FarmIdSchema(BaseModel):
    farm_id: str

class _TransactionIdSchema(BaseModel):
    transaction_id: str
    approved: bool = True

class _OrderLookupSchema(BaseModel):
    buyer_id: Optional[str] = None
    buyer_phone: Optional[str] = None
    status: Optional[str] = None
    limit: int = 20

class _SearchProductSchema(BaseModel):
    product_name: str
    zone_id: Optional[str] = None
    limit: int = 10


class _RAGSearchSchema(BaseModel):
    query: str
    level: str = "debutant"
    top_k: int = 4


class _MemorySearchSchema(BaseModel):
    user_id: str
    query: str
    top_k: int = 3


_SCHEMA_REGISTRY.update({
    "get_user_profile":          _UserIdSchema,
    "get_user_by_phone":         _PhoneSchema,
    "list_products":             _ProducerIdSchema,
    "search_products":           _SearchProductSchema,
    "get_orders":                _OrderLookupSchema,
    "get_farm_stocks":           _FarmIdSchema,
    "get_stocks":                _FarmIdSchema,
    "commit_staged_transaction": _TransactionIdSchema,
    # RAG related tools
    "search_agronomy_docs":      _RAGSearchSchema,
    "search_past_interactions":  _MemorySearchSchema,
})


# ────────────────────────────── Audit entry model ─────────────────────────

class AuditEntry(BaseModel):
    """Structured JSON audit log entry."""
    timestamp: float
    session_id: str
    tool_name: str
    arguments_hash: str
    scope: str
    risk: str
    decision: str          # ALLOW, DENY, HITL_REQUIRED
    reason: Optional[str] = None
    duration_ms: Optional[float] = None
    error: Optional[str] = None


# ────────────────────────────── Permission Result ─────────────────────────

class PermissionDecision(BaseModel):
    allowed: bool
    decision: str   # ALLOW / DENY / HITL_REQUIRED
    reason: str
    scope: PermissionScope
    risk: RiskLevel


# ────────────────────────────── Main Client ───────────────────────────────

class MCPPermissionClient:
    """Security-first MCP client wrapper.

    Parameters
    ----------
    backend : object
        Any object with an async ``call_tool(name, arguments)`` method —
        typically ``AgriDBMCPServer`` or ``AgriMCPClient``.
    session_id : str
        Unique identifier for the current user conversation/session.
    maintenance_mode : bool
        When True, DB_SCHEMA_MODIFY tools are allowed (default: False).
    hitl_callback : callable, optional
        An async callback ``(tool_name, args, reason) -> bool`` invoked
        when a HITL confirmation is needed.  If None, HITL requests are
        auto-denied with a descriptive error.
    audit_sink : callable, optional
        An async callable ``(AuditEntry) -> None`` to persist audit entries
        (e.g. to file, DB, external SIEM).  Defaults to logger.info.
    """

    def __init__(
        self,
        backend: Any,
        session_id: str = "unknown",
        maintenance_mode: bool = False,
        hitl_callback: Optional[Callable[..., Coroutine[Any, Any, bool]]] = None,
        audit_sink: Optional[Callable[..., Coroutine[Any, Any, None]]] = None,
        registry: Optional[MCPToolRegistry] = None,
    ) -> None:
        self._backend = backend
        self.session_id = session_id
        self.maintenance_mode = maintenance_mode
        self._hitl_callback = hitl_callback
        self._audit_sink = audit_sink
        self._registry = registry or get_registry()

    # ────────────── Public API ────────────────────────────────────────────

    async def call_tool(
        self,
        tool_name: str,
        arguments: Dict[str, Any] | None = None,
    ) -> Any:
        """Validate, authorize, execute, mask, and audit a tool call.

        **Fail-Closed policy**: unknown tools (not in TOOL_SCOPE_MAP) are
        denied immediately.  Any unexpected error during validation also
        results in a DENY.
        """
        arguments = arguments or {}
        t0 = time.monotonic()
        args_hash = self._hash_args(arguments)

        # 0. Fail-closed: reject tools missing from the central registry
        if not self._registry.has_tool(tool_name):
            reason = f"Tool '{tool_name}' not registered in MCP registry — DENIED (fail-closed)."
            await self._emit_audit(tool_name, args_hash, "DENY", reason=reason, t0=t0)
            raise PermissionDenied(tool_name, reason)

        # 1. Pydantic validation
        try:
            validated_args = self._validate_arguments(tool_name, arguments)
        except ValidationError as ve:
            await self._emit_audit(tool_name, args_hash, "DENY", reason=f"Schema validation error: {ve}", t0=t0)
            raise PermissionDenied(
                tool_name,
                f"Arguments invalides pour l'outil '{tool_name}': {ve.error_count()} erreur(s). "
                f"Vérifiez les types et champs requis.",
            )
        except Exception as exc:
            reason = f"Unexpected validation error: {exc}"
            await self._emit_audit(tool_name, args_hash, "DENY", reason=reason, t0=t0)
            raise PermissionDenied(tool_name, reason) from exc

        # 2. Permission check
        decision = self._check_permission(tool_name, validated_args)

        if not decision.allowed:
            await self._emit_audit(tool_name, args_hash, decision.decision, reason=decision.reason, t0=t0)
            if decision.decision == "HITL_REQUIRED":
                # Ask human via callback
                approved = await self._request_hitl(tool_name, validated_args, decision.reason)
                if not approved:
                    raise PermissionDenied(
                        tool_name,
                        f"Action sur '{tool_name}' nécessite une confirmation humaine : {decision.reason}",
                    )
                # Human approved — proceed
                await self._emit_audit(tool_name, args_hash, "ALLOW", reason="HITL_APPROVED", t0=t0)
            else:
                raise PermissionDenied(tool_name, decision.reason)

        # 3. Execute
        error_msg: Optional[str] = None
        try:
            raw_result = await self._backend.call_tool(tool_name, validated_args)
        except Exception as exc:
            error_msg = str(exc)
            await self._emit_audit(tool_name, args_hash, "ERROR", reason="executed_with_error", t0=t0, error=error_msg)
            raise

        # 4. Mask sensitive data (recursive)
        masked_result = self._mask_sensitive(raw_result)

        # 5. Force JSON dict output — no raw text
        masked_result = self._normalize_output(masked_result)

        # 6. Audit
        await self._emit_audit(tool_name, args_hash, "ALLOW", t0=t0)

        return masked_result

    # Backwards-compatible alias expected by MCPSessionManager / Session API
    async def execute(self, tool_name: str, arguments: Dict[str, Any] | None = None) -> Any:
        """Compatibility shim: delegate to `call_tool`.

        Some session helpers expect a host exposing `execute(tool, args)`;
        historically hosts implemented that method. Provide a thin wrapper
        to keep the surface stable.
        """
        return await self.call_tool(tool_name, arguments)

    def list_tools(self) -> list[dict]:
        """Return tools visible in the central registry."""
        return self._registry.list_tools()

    # ────────────── Validation ────────────────────────────────────────────

    def _validate_arguments(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Validate arguments against the Pydantic schema for $tool_name.

        Returns the validated dict (with defaults applied).
        Raises ``ValidationError`` on mismatch.
        """
        schema_cls = _SCHEMA_REGISTRY.get(tool_name)
        if schema_cls is None:
            # No schema registered — allow args through but still parse as JSON
            # to reject non-serializable garbage.
            _round_trip = json.loads(json.dumps(arguments, default=str))
            return _round_trip

        instance = schema_cls(**arguments)
        return instance.model_dump()

    # ────────────── Permission ────────────────────────────────────────────

    def _check_permission(self, tool_name: str, arguments: Dict[str, Any]) -> PermissionDecision:
        """Evaluate scope + risk and return a decision."""
        meta = self._registry.get_tool(tool_name)
        scope = meta.scope if meta else TOOL_SCOPE_MAP.get(tool_name, PermissionScope.DB_DATA_WRITE)
        from .constants import TOOL_RISK_MAP
        risk = meta.risk if meta else TOOL_RISK_MAP.get(tool_name, RiskLevel.MEDIUM)

        # Schema modify — always DENY unless maintenance mode
        if scope == PermissionScope.DB_SCHEMA_MODIFY:
            if self.maintenance_mode:
                return PermissionDecision(
                    allowed=True, decision="ALLOW", reason="Maintenance mode active",
                    scope=scope, risk=risk,
                )
            return PermissionDecision(
                allowed=False, decision="DENY",
                reason=f"L'outil '{tool_name}' modifie le schéma DB. Opération interdite en mode normal.",
                scope=scope, risk=risk,
            )

        # Read-only — auto-approve
        if scope == PermissionScope.DB_READ_ONLY:
            return PermissionDecision(
                allowed=True, decision="ALLOW", reason="Read-only scope",
                scope=scope, risk=risk,
            )

        # Data write — require HITL for HIGH/CRITICAL risk
        if risk in (RiskLevel.HIGH, RiskLevel.CRITICAL):
            return PermissionDecision(
                allowed=False, decision="HITL_REQUIRED",
                reason=f"L'outil '{tool_name}' écrit en DB avec risque {risk.value}. Confirmation humaine requise.",
                scope=scope, risk=risk,
            )

        # Data write, low/medium risk — allow
        return PermissionDecision(
            allowed=True, decision="ALLOW", reason=f"Write scope, risk={risk.value}",
            scope=scope, risk=risk,
        )

    # ────────────── HITL ──────────────────────────────────────────────────

    async def _request_hitl(self, tool_name: str, args: Dict[str, Any], reason: str) -> bool:
        """Request human-in-the-loop approval.

        Returns True if approved, False otherwise.
        """
        if self._hitl_callback is not None:
            try:
                return await self._hitl_callback(tool_name, args, reason)
            except Exception as exc:
                logger.warning("HITL callback error: %s — defaulting to DENY", exc)
                return False
        logger.warning("HITL required for '%s' but no callback registered — denying.", tool_name)
        return False

    # ────────────── Data Masking ──────────────────────────────────────────

    def _mask_sensitive(self, data: Any) -> Any:
        """Recursively mask values of sensitive keys before returning to LLM."""
        if isinstance(data, dict):
            return {
                k: ("***MASKED***" if k.lower() in SENSITIVE_COLUMNS else self._mask_sensitive(v))
                for k, v in data.items()
            }
        if isinstance(data, list):
            return [self._mask_sensitive(item) for item in data]
        if isinstance(data, str):
            # Try parsing JSON strings (common in MCP tool responses)
            try:
                parsed = json.loads(data)
                if isinstance(parsed, (dict, list)):
                    masked = self._mask_sensitive(parsed)
                    return json.dumps(masked, ensure_ascii=False)
            except (json.JSONDecodeError, TypeError):
                pass
        return data

    # ────────────── Audit ─────────────────────────────────────────────────

    async def _emit_audit(
        self,
        tool_name: str,
        args_hash: str,
        decision: str,
        reason: Optional[str] = None,
        t0: Optional[float] = None,
        error: Optional[str] = None,
    ) -> None:
        duration_ms = round((time.monotonic() - t0) * 1000, 1) if t0 else None
        meta = self._registry.get_tool(tool_name)
        risk = meta.risk if meta else RiskLevel.MEDIUM
        scope = meta.scope if meta else TOOL_SCOPE_MAP.get(tool_name, PermissionScope.DB_DATA_WRITE)
        entry = AuditEntry(
            timestamp=time.time(),
            session_id=self.session_id,
            tool_name=tool_name,
            arguments_hash=args_hash,
            scope=scope.value,
            risk=risk.value,
            decision=decision,
            reason=reason,
            duration_ms=duration_ms,
            error=error,
        )
        log_line = entry.model_dump_json()
        logger.info("AUDIT|%s", log_line)

        # Custom sink (SIEM, file, etc.)
        if self._audit_sink is not None:
            try:
                await self._audit_sink(entry)
            except Exception as exc:
                logger.warning("Audit sink error: %s", exc)

        # Persist to runtime.db for production audit trail.
        # Prefer routing via the MCP backend (call_tool -> persist_conversation)
        # so the server-side runtime/db handles the write. Fall back to
        # direct runtime.db logging only if backend doesn't support call_tool
        # or the routed call fails.
        try:
            # Ensure we persist a valid UUID for `user_id` — map session ids
            # (like 'test-session' or similar) to a generated UUID when needed.
            try:
                # Accept already-valid UUID strings
                uuid.UUID(str(self.session_id))
                user_uuid = str(self.session_id)
            except Exception:
                user_uuid = str(uuid.uuid4())

            payload = {
                "user_id": user_uuid,
                "query_json": json.dumps({"tool": tool_name, "args_hash": args_hash}),
                "response_json": log_line,
                "agent_type": "mcp_shield_audit",
            }

            routed = False
            if hasattr(self._backend, "call_tool"):
                try:
                    # Try routing the audit through the backend MCP server/tool
                    await self._backend.call_tool("persist_conversation", payload)
                    routed = True
                except Exception:
                    logger.debug("Routing audit via backend.call_tool failed, will try runtime.db fallback", exc_info=True)

            if not routed:
                from agriconnect.protocols.mcp.infrastructure import runtime
                if runtime.is_ready and hasattr(runtime.db, "log_conversation"):
                    # AgriDatabaseService.log_conversation expects positional
                    # args (user_id, query, response, ...) — avoid passing
                    # the HTTP payload keys as unexpected keywords like
                    # `query_json` / `response_json` which cause TypeError.
                    await runtime.db.log_conversation(
                        payload["user_id"],
                        payload["query_json"],
                        payload["response_json"],
                        payload.get("agent_type"),
                    )
        except Exception:
            logger.debug("DB audit persistence skipped (non-blocking)", exc_info=True)

    # ────────────── Output normalizer ─────────────────────────────────────

    @staticmethod
    def _normalize_output(data: Any) -> dict:
        """Force all tool outputs to be valid JSON dicts. No raw text."""
        if isinstance(data, dict):
            return data
        if isinstance(data, str):
            try:
                parsed = json.loads(data)
                if isinstance(parsed, dict):
                    return parsed
                return {"result": parsed}
            except (json.JSONDecodeError, TypeError):
                return {"result": data}
        if isinstance(data, (list, tuple)):
            return {"items": list(data)}
        return {"result": str(data)}

    # ────────────── Helpers ───────────────────────────────────────────────

    @staticmethod
    def _hash_args(arguments: Dict[str, Any]) -> str:
        payload = json.dumps(arguments, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


# ────────────────────────────── Exceptions ────────────────────────────────

class PermissionDenied(Exception):
    """Raised when a tool call is blocked by the Shield."""

    def __init__(self, tool_name: str, reason: str) -> None:
        self.tool_name = tool_name
        self.reason = reason
        super().__init__(f"[SHIELD] {tool_name}: {reason}")
