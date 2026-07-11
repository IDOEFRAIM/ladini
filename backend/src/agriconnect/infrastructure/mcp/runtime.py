from __future__ import annotations

"""AgriConnect MCP runtime (simple & predictable).

Principes (alignés avec `services/database/database_service.py`):
- Le runtime MCP ne gère pas le cycle de vie DB (pas de `init_db()` ici).
- Il instancie et expose uniquement `AgriDatabaseService`.
- La DB est initialisée **lorsque nécessaire** via `get_sessionmaker()`.

MCP:
- `fastmcp` est instancié **paresseusement** (lazy) via un proxy pour éviter
  les effets de bord à l'import (certains packages font du setup lourd).
"""

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any

try:
    import asyncpg  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    asyncpg = None

from agriconnect.core.database import close_db, get_sessionmaker
from agriconnect.core.settings import settings
from agriconnect.infrastructure.mcp.context import (
    FarmerContext,
    get_mcp_context,
    mcp_context_scope,
)
from agriconnect.infrastructure.mcp.security import (
    HostBlockedError,
    MCPPermissionHostApp,
    PermissionDenied,
    TOOL_SCOPE_MAP,
    ensure_scopes_filled,
    get_execution_policy,
)
from agriconnect.infrastructure.mcp.utils import run_coro_blocking
from agriconnect.services.database import AgriDatabaseService

logger = logging.getLogger(__name__)


def _derive_context_identity(payload: dict[str, Any]) -> FarmerContext | None:
    """Reconstruct a FarmerContext from tool payload when no context is scoped.

    Searches both top-level keys and one level of nested dicts (e.g.
    ``data.phone``) so tools receiving a ``data={...}`` envelope still
    resolve an identity.
    """
    sources = [payload]
    for key in ("data", "payload", "args"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            sources.append(nested)

    def _pick(*keys: str) -> str:
        for src in sources:
            for key in keys:
                value = src.get(key)
                if value not in (None, "", [], {}):
                    return str(value).strip()
        return ""

    user_id = _pick("user_id", "producer_id", "buyer_id", "farmer_id")
    phone = _pick("phone", "user_phone", "phone_number", "buyer_phone", "customer_phone")
    session_id = _pick("session_id", "request_id")
    if not user_id and not phone:
        return None
    return FarmerContext(
        user_id=user_id or phone,
        phone_number=phone or "unknown",
        session_id=session_id or str(uuid.uuid4()),
    )


# ---------------------------------------------------------------------------
# FastMCP lazy proxy
# ---------------------------------------------------------------------------

_mcp_instance: Any | None = None


def _create_mcp_instance() -> Any:
    from fastmcp import FastMCP

    return FastMCP("AgriConnect Database MCP Server")


def get_mcp() -> Any:
    """Get (and lazily create) the underlying FastMCP instance."""
    global _mcp_instance
    if _mcp_instance is None:
        _mcp_instance = _create_mcp_instance()
    return _mcp_instance


class _LazyMCPProxy:
    """Lightweight proxy that defers FastMCP creation until first use."""

    def __getattr__(self, item: str):
        return getattr(get_mcp(), item)

    def __repr__(self) -> str:  # pragma: no cover
        return "<LazyMCPProxy>"


# Exported symbol (kept for backwards compatibility)
mcp = _LazyMCPProxy()


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


class MCPRuntime:
    """Gère l'accès aux ressources nécessaires aux tools MCP.

    NOTE: la DB est gérée via `AgriDatabaseService` et `get_sessionmaker()`.
    Le runtime ne fait pas de healthcheck réseau ni de retry ici — on garde
    la même philosophie que `database_service.py` (les erreurs DB émergent
    lors des appels métiers et sont gérées au bon niveau).
    """

    def __init__(self) -> None:
        self.db: AgriDatabaseService | None = None
        self.is_ready: bool = False
        self.last_start_error: str = ""
        self._service_lock = asyncio.Lock()

    async def _ensure_db_service(self) -> None:
        async with self._service_lock:
            if self.db is None:
                self.db = AgriDatabaseService()

    def _refresh_ready_state(self) -> None:
        try:
            sm = get_sessionmaker()
            self.is_ready = sm is not None
            self.last_start_error = "" if self.is_ready else "sessionmaker_unavailable"
        except Exception as exc:
            self.is_ready = False
            self.last_start_error = str(exc)

    def _build_failure_hint(self, raw_reason: str) -> str:
        reason = raw_reason or "unknown"
        if "CERTIFICATE_VERIFY_FAILED" in reason or "self-signed certificate" in reason:
            return (
                "SSL certificate verification failed. "
                f"Current DB_SSL_MODE={settings.DB_SSL_MODE!r}, DB_CA_PATH={settings.DB_CA_PATH!r}. "
                "If this is a local diagnostic run, set DB_SSL_MODE=require temporarily; "
                "for production, fix the CA chain and keep verify-full."
            )
        return reason

    @asynccontextmanager
    async def lifespan(self):
        """Context manager for server startup/shutdown."""
        logger.info("MCP runtime startup: ensuring DB service")
        await self._ensure_db_service()
        self._refresh_ready_state()
        try:
            yield self
        finally:
            # Do not close DB by default (host responsibility).
            if (os.getenv("MCP_CLOSE_DB_ON_STOP", "0") or "0").strip() in {"1", "true", "True"}:
                try:
                    await close_db()
                except Exception:
                    logger.exception("DB close failed during lifespan shutdown")

    def start(self) -> None:
        self.start_with_policy()

    def start_with_policy(
        self,
        require_connection: bool | None = None,
        retries: int | None = None,
        retry_delay_s: float | None = None,
    ) -> bool:
        """Compat API: start runtime.

        `retries` and `retry_delay_s` are accepted for backwards compatibility
        but intentionally unused to keep runtime simple.
        """
        require = (not settings.MCP_ALLOW_DEGRADED_START) if require_connection is None else require_connection

        run_coro_blocking(self._ensure_db_service())
        self._refresh_ready_state()
        if self.is_ready:
            logger.info("MCP runtime ready (sessionmaker available)")
            return True

        hint = self._build_failure_hint(self.last_start_error)
        if require:
            raise RuntimeError(f"Database unavailable ({hint})")
        logger.warning("MCP runtime degraded: %s", hint)
        return False

    def stop(self) -> None:
        """Compat API: stop runtime.

        By default, does nothing. Set `MCP_CLOSE_DB_ON_STOP=1` for local runs.
        """
        if (os.getenv("MCP_CLOSE_DB_ON_STOP", "0") or "0").strip() not in {"1", "true", "True"}:
            return
        try:
            run_coro_blocking(close_db())
        except Exception:
            logger.exception("Synchronous runtime stop failed")

    async def ensure_initialized(self) -> None:
        """Ensure service exists and sessionmaker can be resolved."""
        await self._ensure_db_service()
        self._refresh_ready_state()
        if not self.is_ready:
            raise RuntimeError(self._build_failure_hint(self.last_start_error))


# --- Instance Globale ---
runtime = MCPRuntime()


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------


def _log_call(tool_name: str, params: dict[str, Any], response: str) -> None:
    """Centralise le logging des appels de tools pour le debugging et l'audit."""
    try:
        res_preview = (response[:250] + "...") if len(response) > 250 else response
        logger.info("MCP_CALL [%s] | PARAMS: %s | RES: %s", tool_name, params, res_preview)
    except Exception as exc:
        logger.warning("Erreur lors du logging de l'appel %s: %s", tool_name, exc)


# ---------------------------------------------------------------------------
# MCP DB server wrapper (security / tool resolution)
# ---------------------------------------------------------------------------


_PUBLIC_CATALOG_TOOLS: frozenset[str] = frozenset({
    "get_zone_by_name",
    "get_available_zones",
})


class AgriDBMCPServer:
    """Lightweight in-process MCP backend — **Fail-Closed** by default."""

    name = "agri_db_full_access"
    FAIL_CLOSED: bool = True

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    @staticmethod
    def list_tools() -> list[dict]:
        """
        Retourne la liste des outils formatée pour le protocole MCP.
        Inclut le nom, la description et le JSON Schema des arguments.
        """
        # Imports locaux pour éviter les cycles au démarrage
        from agriconnect.protocols.mcp.servers.h import TOOL_DESCRIPTIONS, TOOL_SCHEMAS

        tools = []
        for name, description in TOOL_DESCRIPTIONS.items():
            # Récupération du schéma généré par l'introspection
            # Si le schéma n'existe pas, on renvoie un objet vide par défaut
            schema = TOOL_SCHEMAS.get(name, {
                "type": "object", 
                "properties": {}, 
                "required": []
            })

            tools.append({
                "name": name,
                "description": description,
                "inputSchema": schema  # CRITIQUE : C'est ce champ qui active les inputs dans l'UI
            })
            
        return tools
        
    async def call_tool(self, name: str, arguments: dict | None = None, **kwargs):
        """Backend tool execution: context → scope check → preflight → execute → audit."""
        full_args = {**(arguments or {}), **kwargs}
        logger.info("Backend call_tool: tool=%s, args=%s", name, full_args)

        # 1. Resolve identity (scoped OR derived from payload)
        context_identity = get_mcp_context() or _derive_context_identity(full_args)
        if context_identity is None and name not in _PUBLIC_CATALOG_TOOLS:
            raise PermissionDenied(name, "missing_context_identity")

        if context_identity is None:
            context_identity = FarmerContext(
                user_id="catalog",
                phone_number="catalog",
                session_id=str(uuid.uuid4()),
            )

        with mcp_context_scope(context_identity):
            # 2. Static scope check (fail-closed on unknown tools)
            ensure_scopes_filled()
            if name not in TOOL_SCOPE_MAP:
                reason = f"L'outil '{name}' n'est pas autorisé (absent de TOOL_SCOPE_MAP)."
                logger.error("DENY | tool=%s | reason=not_in_scope", name)
                await self._persist_audit(name, full_args, "DENY", reason)
                raise PermissionDenied(name, reason)

            policy = get_execution_policy()
            sanitized_args = policy.sanitize_arguments(full_args)

            # 3. Preflight security scan
            await self._run_preflight(name, sanitized_args)

            # 4. Execute under policy (timeout / audit envelope)
            fn = self._resolve_tool_fn(name)
            envelope = await policy.execute(
                name,
                fn,
                sanitized_args,
                user_id=context_identity.user_id,
                request_id=str(uuid.uuid4()),
            )
            await self._persist_audit(name, sanitized_args, "ALLOW", None)
            return envelope.get("data", envelope)

    async def _run_preflight(self, tool_name: str, sanitized_args: dict) -> None:
        try:
            host = MCPPermissionHostApp(client=None)
            preflight = host._preflight_scan(tool_name, sanitized_args)
            if hasattr(preflight, "allowed") and not preflight.allowed:
                reason = getattr(preflight, "reason", "Scan de sécurité échoué")
                await self._persist_audit(tool_name, sanitized_args, "DENY", reason)
                raise HostBlockedError(
                    tool_name=tool_name,
                    agent_message=reason,
                    suggestion=host._suggest_fix(tool_name, reason),
                )
        except (HostBlockedError, PermissionDenied):
            raise
        except Exception as exc:
            reason = f"Erreur fatale lors du contrôle de sécurité : {exc}"
            logger.exception("SECURITY_EXCEPTION | tool=%s", tool_name)
            await self._persist_audit(tool_name, sanitized_args, "DENY", reason)
            raise PermissionDenied(tool_name, reason)

    def _resolve_tool_fn(self, name: str):
        from agriconnect.protocols.mcp.servers.h import TOOL_HANDLERS

        fn = TOOL_HANDLERS.get(name)
        if fn is None or not callable(fn):
            raise ValueError(f"Unknown DB tool: {name}")
        return fn

    @staticmethod
    def _normalize_output(raw: Any) -> Any:
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return {"result": raw}
        if isinstance(raw, (list, tuple)):
            return {"items": list(raw)}
        return {"result": str(raw)}

    async def _persist_audit(
        self, tool_name: str, arguments: dict, decision: str, reason: str | None
    ) -> None:
        import hashlib
        import time

        try:
            args_hash = hashlib.sha256(
                json.dumps(arguments, sort_keys=True, default=str).encode()
            ).hexdigest()[:16]
            entry = {
                "timestamp": time.time(),
                "session_id": "server_proxy",
                "tool_name": tool_name,
                "arguments_hash": args_hash,
                "decision": decision,
                "reason": reason or "",
            }
            logger.info("AUDIT|%s", json.dumps(entry, ensure_ascii=False))
            if runtime.is_ready and runtime.db is not None and hasattr(runtime.db, "log_conversation"):
                await _log_audit_entry(tool_name, args_hash, entry)
        except Exception:
            logger.debug("Audit persistence failed (non-blocking)", exc_info=True)


__all__ = ["mcp", "runtime", "_log_call", "AgriDBMCPServer", "get_mcp"]


def _load_backend_env() -> None:
    """Load backend/.env when running the module directly."""
    try:
        from dotenv import load_dotenv  # type: ignore
    except Exception:
        return

    here = os.path.abspath(__file__)
    backend_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(here))))
    env_file = os.path.join(backend_root, ".env")
    try:
        if os.path.exists(env_file):
            load_dotenv(env_file, override=False)
    except Exception:
        pass


def _should_reset_db(exc: Exception) -> bool:
    if asyncpg and isinstance(exc, asyncpg.exceptions.ConnectionDoesNotExistError):
        return True
    msg = str(exc).lower()
    return "connection was closed" in msg or "connection does not exist" in msg


async def _log_audit_entry(tool_name: str, args_hash: str, entry: dict[str, Any]) -> None:
    for attempt in range(2):
        try:
            await runtime.ensure_initialized()
            if runtime.db is None:
                return
            import uuid

            system_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, "agriconnect.system"))
        except Exception:
            import uuid as _uuid

            system_uuid = str(_uuid.uuid4())

        try:
            await runtime.db.log_conversation(
                system_uuid,
                json.dumps({"tool": tool_name, "args_hash": args_hash}),
                json.dumps(entry),
                agent_type="mcp_shield_audit",
            )
            return
        except Exception as exc:
            if attempt == 1 or not _should_reset_db(exc):
                logger.debug("Audit log_conversation failed", exc_info=True)
                return
            logger.warning("Audit log_conversation lost DB connection; reinitializing", exc_info=True)
            runtime.db = None

