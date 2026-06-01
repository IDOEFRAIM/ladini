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
import concurrent.futures
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from agriconnect.core.database import close_db, get_sessionmaker
from agriconnect.core.settings import settings
from agriconnect.services.database.database_service import AgriDatabaseService

logger = logging.getLogger(__name__)


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

    def _ensure_db_service(self) -> None:
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
        """Context manager for server startup/shutdown."""
        logger.info("MCP runtime startup: ensuring DB service")
        self._ensure_db_service()
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

        self._ensure_db_service()
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
            self._run_async_blocking(close_db())
        except Exception:
            logger.exception("Synchronous runtime stop failed")

    async def ensure_initialized(self) -> None:
        """Ensure service exists and sessionmaker can be resolved."""
        self._ensure_db_service()
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
        from agriconnect.protocols.mcp.h import TOOL_DESCRIPTIONS, TOOL_SCHEMAS

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
        """
        Point d'entrée principal pour l'exécution des outils dans le Backend.
        Gère la fusion des arguments, la sécurité (Preflight/Permissions) et l'audit.
        """
        # 1. FUSION ET NETTOYAGE DES ARGUMENTS
        # On combine le dictionnaire MCP ('arguments') et les éventuels kwargs
        full_args = {**(arguments or {}), **kwargs}
        
        # CRITIQUE : On retire 'name' pour éviter l'erreur "multiple values for argument 'name'"
        # car 'name' est déjà passé en premier paramètre positionnel.
        tool_identity = name

        logger.info("Backend call_tool: tool=%s, args=%s", tool_identity, full_args)
        # 2. RÉSOLUTION DU HANDLER
        # Récupère la fonction métier (ex: get_user_profile) définie dans handlers.py
        fn = self._resolve_tool_fn(tool_identity)

        # Imports locaux pour la couche de sécurité
        from agriconnect.infrastructure.mcp.security import (
            HostBlockedError,
            MCPPermissionClient,
            MCPPermissionHostApp,
            PermissionDenied,
            TOOL_SCOPE_MAP,
        )

        # 3. VÉRIFICATION DU SCOPE (Fail-Closed)
        if tool_identity not in TOOL_SCOPE_MAP:
            reason = f"L'outil '{tool_identity}' n'est pas autorisé (absent de TOOL_SCOPE_MAP)."
            logger.error("DENY | tool=%s | reason=not_in_scope", tool_identity)
            await self._persist_audit(tool_identity, full_args, "DENY", reason)
            raise PermissionDenied(tool_identity, reason)

        try:
            # 4. SÉCURITÉ HÔTE (Preflight Scan)
            host = MCPPermissionHostApp(client=None)
            preflight = host._preflight_scan(tool_identity, full_args)
            
            # Vérification robuste de l'autorisation du scan
            if hasattr(preflight, "allowed") and not preflight.allowed:
                reason = getattr(preflight, "reason", "Scan de sécurité échoué")
                await self._persist_audit(tool_identity, full_args, "DENY", reason)
                raise HostBlockedError(
                    tool_name=tool_identity,
                    agent_message=reason,
                    suggestion=host._suggest_fix(tool_identity, reason),
                )

            # 5. VÉRIFICATION DES PERMISSIONS CLIENT
            tmp_client = MCPPermissionClient(backend=self, session_id="mcp_server_proxy")
            decision = tmp_client._check_permission(tool_identity, full_args)
            
            if not decision.allowed:
                await self._persist_audit(tool_identity, full_args, "DENY", decision.reason)
                raise PermissionDenied(tool_identity, decision.reason)

        except (HostBlockedError, PermissionDenied):
            # On laisse remonter ces erreurs spécifiques pour le traitement par le serveur MCP
            raise
        except Exception as exc:
            reason = f"Erreur fatale lors du contrôle de sécurité : {exc}"
            logger.exception("SECURITY_EXCEPTION | tool=%s", tool_identity)
            await self._persist_audit(tool_identity, full_args, "DENY", reason)
            raise PermissionDenied(tool_identity, reason)

        # 6. EXÉCUTION MÉTIER
        try:
            # On injecte les arguments nettoyés dans la fonction cible via **
            # Grâce au @wraps(fn) dans handlers.py, la signature est préservée.
            raw = await fn(**full_args)
            
            # 7. NORMALISATION ET AUDIT FINAL
            result = self._normalize_output(raw)
            await self._persist_audit(tool_identity, full_args, "ALLOW", None)
            return result

        except TypeError as te:
            # Capture les erreurs de paramètres (ex: paramètre manquant ou inconnu)
            logger.error("SIGNATURE_MISMATCH | tool=%s | %s", tool_identity, te)
            raise ValueError(f"Arguments invalides pour l'outil '{tool_identity}' : {te}")
        except Exception as e:
            # Erreurs internes au handler (Base de données, etc.)
            logger.exception("EXECUTION_ERROR | tool=%s", tool_identity)
            raise RuntimeError(f"Erreur lors de l'exécution de l'outil '{tool_identity}' : {e}")

    def call_tool_sync(self, name: str, arguments: dict , timeout: float = 10.0) -> dict:
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

    def _resolve_tool_fn(self, name: str):
        from agriconnect.protocols.mcp.h import TOOL_HANDLERS

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

async def _main() -> None:
    """
    Point d'entrée de test pour AgriDBMCPServer.
    Vérifie la chaîne complète : Runtime -> Security Shield -> DB Service.
    """
    _load_backend_env()
    
    # Configuration des logs pour voir les sorties "AUDIT|" et "FAIL-CLOSED"
    level = os.getenv("AGRICONNECT_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(levelname)s | %(name)s | %(message)s"
    )

    logger.info("🧪 Démarrage du test unitaire du serveur MCP...")

    try:
        # 1. Initialisation du runtime (nécessaire pour runtime.db utilisé dans _persist_audit)
        ok = runtime.start_with_policy()
        if not ok or not runtime.is_ready:
            logger.error("❌ Impossible de démarrer le runtime : %s", runtime.last_start_error)
            return

        # 2. Instanciation du serveur de sécurité
        server = AgriDBMCPServer()

        # 3. Paramètres de test
        test_tool = "get_producer_dashboard"
        test_args = {"producer_id": "9e37a423-45cf-4e63-a563-075a46d104bf"}

        logger.info("📡 Appel de l'outil : %s", test_tool)

        # 4. Exécution via call_tool (déclenche le scan de sécurité et l'audit)
        response = await server.call_tool(name=test_tool, arguments=test_args)
        
        # 5. Validation du résultat
        logger.info("✅ RÉPONSE REÇUE : %s", json.dumps(response, indent=2, ensure_ascii=False))

    except Exception as exc:
        # Ici on attrape notamment les PermissionDenied si la sécurité bloque
        logger.error("❌ ÉCHEC DU TEST : %s", str(exc))
        if hasattr(exc, 'suggestion'):
             logger.info("💡 Suggestion du shield : %s", exc.suggestion)

    finally:
        # 6. Arrêt propre
        logger.info("Cleaning up runtime...")
        try:
            runtime.stop()
        except Exception:
            pass

if __name__ == "__main__":
    import asyncio
    import json
    import os
    import logging
    
    # On lance le test
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        logger.info("Test interrompu par l'utilisateur.")