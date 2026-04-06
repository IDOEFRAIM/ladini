from __future__ import annotations

import logging
import json
import asyncio
import time
import concurrent.futures
from contextlib import asynccontextmanager
from typing import Any
import os
from fastmcp import FastMCP

# Imports depuis le cœur de l'application
from agriconnect.core.database import (
    init_db,
    close_db,
    check_connection,
    check_connection_detailed,
    check_connection_aggressive,
)
from agriconnect.core.settings import settings
from agriconnect.services.database.database_service import AgriDatabaseService

mcp = FastMCP("AgriConnect Database MCP Server")

# Configuration du logger pour l'infrastructure MCP
logger = logging.getLogger(__name__)

class MCPRuntime:
    """
    Gère le cycle de vie du serveur MCP AgriConnect.
    Centralise l'instance du service de base de données et gère la connexion.
    """
    def __init__(self):
        # Service DB instancié à la demande uniquement quand la connexion
        # est validée; cela évite les appels outils en mode dégradé.
        self.db: AgriDatabaseService | None = None
        self.is_ready = False
        self.last_start_error: str = ""

    def _ensure_db_service(self) -> None:
        if self.db is None:
            self.db = AgriDatabaseService()

    @staticmethod
    def _run_async_blocking(coro):
        """Run coroutine from sync code, even if an event loop already exists."""
        try:
            asyncio.get_running_loop()
            in_running_loop = True
        except RuntimeError:
            in_running_loop = False

        if not in_running_loop:
            return asyncio.run(coro)

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result(timeout=20)

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
        """
        Gestionnaire de contexte pour le démarrage et l'arrêt du serveur.
        Garantit que la DB est initialisée avant l'usage des tools.
        """
        logger.info("Démarrage du Runtime MCP : Initialisation de la base de données...")
        try:
            # 1. Initialisation du moteur SQLAlchemy (Engine)
            init_db()
            
            # 2. Vérification de la santé de la connexion
            self.is_ready = await check_connection()
            if not self.is_ready:
                logger.error("Échec de la connexion à la base de données lors du check initial.")
                raise RuntimeError("Base de données indisponible. Vérifiez les variables d'environnement.")

            self._ensure_db_service()
            
            logger.info("Runtime MCP opérationnel. Connexion DB établie.")
            
            # On cède le contrôle au serveur MCP (yield)
            yield self
            
        except Exception as e:
            logger.exception(f"Erreur critique lors du démarrage du Runtime : {str(e)}")
            raise
        finally:
            # 3. Fermeture propre des ressources à l'arrêt du serveur
            logger.info("Arrêt du Runtime MCP : Fermeture des connexions DB...")
            await close_db()
            logger.info("Ressources nettoyées avec succès.")

    def start(self) -> None:
        self.start_with_policy()

    def start_with_policy(
        self,
        require_connection: bool | None = None,
        retries: int | None = None,
        retry_delay_s: float | None = None,
    ) -> bool:
        """Start runtime with retry and optional degraded mode.

        Returns:
            True when DB is connected, False when degraded mode is used.
        """
        require = (not settings.MCP_ALLOW_DEGRADED_START) if require_connection is None else require_connection
        max_retries = settings.MCP_DB_STARTUP_RETRIES if retries is None else max(0, retries)
        delay_s = settings.MCP_DB_STARTUP_RETRY_DELAY_SEC if retry_delay_s is None else max(0.0, retry_delay_s)

        self.is_ready = False
        self.last_start_error = ""

        try:
            init_db()
        except Exception as exc:
            self.last_start_error = str(exc)
            hint = self._build_failure_hint(self.last_start_error)
            if require:
                logger.exception("Runtime start failed during init_db: %s", hint)
                raise RuntimeError(f"Database unavailable ({hint})") from exc
            logger.error("Runtime starting in degraded mode (init_db failed): %s", hint)
            return False

        for attempt in range(max_retries + 1):
            ok = False
            reason = "health_check_failed"
            try:
                ok, reason = self._run_async_blocking(check_connection_detailed())
            except Exception as exc:
                reason = str(exc)

            if ok:
                strict_health = (os.getenv("MCP_STRICT_DB_HEALTHCHECK", "1") or "1").strip() not in {"0", "false", "False"}
                if strict_health:
                    aok, areason = self._run_async_blocking(check_connection_aggressive())
                    if not aok:
                        self.last_start_error = f"aggressive_check_failed:{areason}"
                        logger.error("Aggressive DB startup check failed: %s", areason)
                        if attempt < max_retries and delay_s > 0:
                            time.sleep(delay_s)
                            continue
                        break

                self.is_ready = True
                self.last_start_error = ""
                self._ensure_db_service()
                logger.info("Runtime MCP opérationnel. Connexion DB établie.")
                return True

            self.last_start_error = reason
            logger.warning(
                "DB startup check failed (attempt %s/%s): %s",
                attempt + 1,
                max_retries + 1,
                reason,
            )
            if attempt < max_retries and delay_s > 0:
                time.sleep(delay_s)

        hint = self._build_failure_hint(self.last_start_error)
        if require:
            logger.error("Synchronous runtime start failed after retries: %s", hint)
            raise RuntimeError(f"Database unavailable (sync startup check failed): {hint}")

        logger.error("Runtime MCP starting in degraded mode: %s", hint)
        return False

    def stop(self) -> None:
        """Synchronous shutdown helper for non-async entrypoints."""
        try:
            self._run_async_blocking(close_db())
        except Exception:
            logger.exception("Synchronous runtime stop failed")

    async def ensure_initialized(self) -> None:
        """Ensure DB engine/session are initialized when called from an
        existing event loop. Safe to call multiple times.
        """
        try:
            # if check_connection already returns True, nothing to do
            if await check_connection():
                self.is_ready = True
                return
        except Exception:
            pass

        # attempt to initialize engine then recheck
        try:
            init_db()
        except Exception as e:
            logger.exception("Failed to init_db during ensure_initialized: %s", e)
            raise

        ok = await check_connection()
        if not ok:
            logger.error("DB still unavailable after init_db()")
            raise RuntimeError("Database unavailable")
        self.is_ready = True
        self._ensure_db_service()

# --- Instance Globale ---
# Importez cet objet 'runtime' dans vos fichiers tools.py et main.py
runtime = MCPRuntime()

# --- Utilitaires de Logging ---
def _log_call(tool_name: str, params: dict[str, Any], response: str) -> None:
    """
    Centralise le logging des appels de tools pour le debugging et l'audit.
    """
    try:
        # On limite la taille de la réponse dans les logs pour la lisibilité
        res_preview = (response[:250] + '...') if len(response) > 250 else response
        logger.info(f"MCP_CALL [{tool_name}] | PARAMS: {params} | RES: {res_preview}")
    except Exception as e:
        logger.warning(f"Erreur lors du logging de l'appel {tool_name}: {e}")

# --- Fonctions de compatibilité (Legacy) ---
# legacy accessor removed — use `runtime.db` directly

class AgriDBMCPServer:
    """Lightweight in-process MCP backend — **Fail-Closed** by default.

    Security policy:
    - Unknown tools (not in TOOL_SCOPE_MAP) → DENY.
    - Pre-flight check failure → DENY (never fail-open).
    - Permission check failure → DENY.
    - All calls are audited to ``runtime.db`` when available.
    """

    name = "agri_db_full_access"

    # ── Configurable policy ──────────────────────────────────────────
    FAIL_CLOSED: bool = True  # Set to False only for debugging

    def __init__(self, *_args, **_kwargs) -> None:
        # Compatibility: legacy callers pass session_factory/extra args.
        pass

    @staticmethod
    def list_tools() -> list[dict]:
        try:
            import agriconnect.protocols.mcp.tools.db_handler as db_tools
            tm = getattr(db_tools, "TOOL_MAP", None) or getattr(db_tools, "TOOLS", None)
            if isinstance(tm, dict):
                return [{"name": n, "description": (fn.__doc__ or "").strip().split("\n")[0]} for n, fn in tm.items()]
        except Exception:
            pass

        tools_attr = getattr(mcp, "_tools", None) or getattr(mcp, "tools", None)
        if isinstance(tools_attr, dict):
            return [{"name": n, "description": (getattr(fn, "__doc__", "") or "").strip().split("\n")[0]} for n, fn in tools_attr.items()]
        return []

    async def call_tool(self, name: str, arguments: dict | None = None):
        """Execute a tool after fail-closed security checks.

        Raises ValueError, HostBlockedError, or PermissionDenied on any
        security violation.  ALL outcomes are persisted to the audit log.
        """
        arguments = arguments or {}
        fn = self._resolve_tool_fn(name)

        # ── Server-side Fail-Closed pre-flight ───────────────────────
        from agriconnect.infrastructure.mcp.security import (
            HostBlockedError,
            MCPPermissionClient,
            MCPPermissionHostApp,
            PermissionDenied,
            TOOL_SCOPE_MAP,
        )

        # Block unknown tools immediately
        if name not in TOOL_SCOPE_MAP:
            reason = f"Tool '{name}' is not registered in TOOL_SCOPE_MAP — DENIED by fail-closed policy."
            logger.warning("FAIL-CLOSED DENY | tool=%s | reason=unknown_tool", name)
            await self._persist_audit(name, arguments, "DENY", reason)
            raise PermissionDenied(name, reason)

        try:
            host = MCPPermissionHostApp(client=None)
            preflight = host._preflight_scan(name, arguments)
            if not preflight:
                await self._persist_audit(name, arguments, "DENY", preflight.reason)
                raise HostBlockedError(
                    tool_name=name,
                    agent_message=preflight.reason,
                    suggestion=host._suggest_fix(name, preflight.reason),
                )

            # Scope / risk check (stateless — no backend call)
            tmp_client = MCPPermissionClient(backend=self, session_id="mcp_server_proxy")
            decision = tmp_client._check_permission(name, arguments)
            if not decision.allowed:
                await self._persist_audit(name, arguments, decision.decision, decision.reason)
                raise PermissionDenied(name, decision.reason)

        except (HostBlockedError, PermissionDenied):
            raise
        except Exception as exc:
            # *** FAIL-CLOSED: any unexpected error during checks → DENY ***
            reason = f"Unexpected security check error: {exc}"
            logger.exception("FAIL-CLOSED DENY | tool=%s | %s", name, reason)
            await self._persist_audit(name, arguments, "DENY", reason)
            if self.FAIL_CLOSED:
                raise PermissionDenied(name, reason) from exc
            # Only reachable when FAIL_CLOSED is explicitly disabled

        # ── Execute ──────────────────────────────────────────────────
        raw = await fn(**arguments)
        result = self._normalize_output(raw)
        await self._persist_audit(name, arguments, "ALLOW", None)
        return result

    def call_tool_sync(self, name: str, arguments: dict | None = None, timeout: float = 10.0) -> dict:
        new_loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(new_loop)
            result = new_loop.run_until_complete(self.call_tool(name, arguments or {}))
        finally:
            try:
                new_loop.run_until_complete(new_loop.shutdown_asyncgens())
            except Exception:
                pass
            new_loop.close()
            try:
                asyncio.set_event_loop(None)
            except Exception:
                pass
        return {"ok": True, "data": result}

    # ── Internal helpers ─────────────────────────────────────────────

    def _resolve_tool_fn(self, name: str):
        """Resolve tool function from registry — raises ValueError if not found."""
        fn = None
        try:
            import agriconnect.protocols.mcp.tools.db_handler as db_tools
            tm = getattr(db_tools, "TOOL_MAP", None) or getattr(db_tools, "TOOLS", None)
            if isinstance(tm, dict) and name in tm:
                fn = tm[name]
        except Exception:
            pass
        if fn is None:
            tools_attr = getattr(mcp, "_tools", None) or getattr(mcp, "tools", None)
            if isinstance(tools_attr, dict) and name in tools_attr:
                fn = tools_attr[name]
        if fn is None or not callable(fn):
            raise ValueError(f"Unknown DB tool: {name}")
        return fn

    @staticmethod
    def _normalize_output(raw: Any) -> Any:
        """Force all tool outputs to valid JSON dicts. No raw text allowed."""
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                return parsed
            except (json.JSONDecodeError, TypeError):
                return {"result": raw}
        if isinstance(raw, (list, tuple)):
            return {"items": list(raw)}
        return {"result": str(raw)}

    async def _persist_audit(
        self, tool_name: str, arguments: dict, decision: str, reason: str | None
    ) -> None:
        """Persist an audit row to DB via ``runtime.db`` when available."""
        import time
        import hashlib
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
            # Persist to DB if runtime is ready
            if runtime.is_ready and hasattr(runtime.db, "log_conversation"):
                # Use a deterministic UUID for system/audit events so the
                # DB receives a valid UUID and records can be correlated.
                try:
                    import uuid

                    system_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, "agriconnect.system"))
                except Exception:
                    import uuid as _uuid

                    system_uuid = str(_uuid.uuid4())

                await runtime.db.log_conversation(
                    system_uuid,
                    json.dumps({"tool": tool_name, "args_hash": args_hash}),
                    json.dumps(entry),
                    agent_type="mcp_shield_audit",
                )
        except Exception:
            logger.debug("Audit persistence failed (non-blocking)", exc_info=True)


# Exports explicites
__all__ = ["mcp", "runtime", "_log_call", "AgriDBMCPServer"]