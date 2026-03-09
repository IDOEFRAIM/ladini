from __future__ import annotations

import logging
import json
import asyncio
from contextlib import asynccontextmanager
from typing import Any

# Imports depuis le cœur de l'application
from agriconnect.core.database import init_db, close_db, check_connection
from agriconnect.services.database.database_service import AgriDatabaseService
from agriconnect.protocols.mcp.app import mcp

# Configuration du logger pour l'infrastructure MCP
logger = logging.getLogger(__name__)

class MCPRuntime:
    """
    Gère le cycle de vie du serveur MCP AgriConnect.
    Centralise l'instance du service de base de données et gère la connexion.
    """
    def __init__(self):
        # L'instance du service est créée une seule fois (Singleton)
        # Elle sera utilisée par tous les tools via runtime.db
        self.db = AgriDatabaseService()
        self.is_ready = False

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
        """Synchronous startup helper for non-async entrypoints.

        Initializes DB engine and performs a connectivity check using
        a short-lived event loop so blocking entrypoints (like `mcp.run()`)
        can be executed without nested asyncio loops.
        """
        init_db()
        try:
            # run the async check_connection in a fresh event loop
            import asyncio as _asyncio

            self.is_ready = _asyncio.run(check_connection())
            if not self.is_ready:
                raise RuntimeError("Database unavailable (sync startup check failed)")
        except Exception as e:
            logger.exception("Synchronous runtime start failed: %s", e)
            raise

    def stop(self) -> None:
        """Synchronous shutdown helper for non-async entrypoints."""
        try:
            import asyncio as _asyncio

            _asyncio.run(close_db())
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

    @staticmethod
    def list_tools() -> list[dict]:
        try:
            import agriconnect.protocols.mcp.tools.db_tools as db_tools
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
        from agriconnect.protocols.mcp.security.host_app import MCPPermissionHostApp, HostBlockedError
        from agriconnect.protocols.mcp.security.client_base import MCPPermissionClient, PermissionDenied
        from agriconnect.protocols.mcp.security.constants import TOOL_SCOPE_MAP

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
            import agriconnect.protocols.mcp.tools.db_tools as db_tools
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
                await runtime.db.log_conversation(
                    user_id="system",
                    query_json=json.dumps({"tool": tool_name, "args_hash": args_hash}),
                    response_json=json.dumps(entry),
                    agent_type="mcp_shield_audit",
                )
        except Exception:
            logger.debug("Audit persistence failed (non-blocking)", exc_info=True)


# Exports explicites
__all__ = ["runtime", "_log_call", "AgriDBMCPServer"]