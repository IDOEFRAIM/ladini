"""MCP HTTP daemon — transport persistant pour AgriDBMCPServer.

Élimine le cold-start du transport stdio (diagnostiqué à ~6-9s/tour : spawn
process + import lourd + pool DB recréé à froid) en gardant un SEUL process
Python vivant, avec un SEUL pool de connexions Postgres chaud, qui répond à
tous les tours via de simples requêtes HTTP locales.

Audit MCP/AGUI 2026-08-27 : ce daemon exposait auparavant une API REST
maison (``GET /tools``, ``POST /call``) étiquetée "MCP" par convention de
nommage seulement — aucun client MCP standard (Claude Desktop, un
Inspector MCP, tout host respectant le spec) ne pouvait s'y connecter.
Il route désormais le trafic via le transport MCP Streamable HTTP officiel
du SDK (``mcp.server.streamable_http_manager.StreamableHTTPSessionManager``,
JSON-RPC 2.0) sur un unique endpoint : ``POST/GET/DELETE /mcp``. Ce fichier
ne contient toujours AUCUNE logique métier propre : il réutilise TEL QUEL
le même ``Server()`` que le transport stdio
(``protocols/mcp/servers/db_server.py::LadiniMCPEntryPoint`` — Tools
ET Resources, voir l'audit Tâche 4), juste sous un second transport.

Contrat consommé par ``HttpMCPAdapter`` (infrastructure/mcp/client.py, via
``fastmcp.Client`` + ``StreamableHttpTransport``) :
    POST /mcp  -> JSON-RPC 2.0 (initialize, tools/list, tools/call, ...)

Lancement en daemon persistant (AVANT de démarrer l'API webhook et les
workers Celery, puisque ceux-ci se connecteront à ce daemon via
MCP_DB_TRANSPORT=http) :

    uvicorn ladini.protocols.mcp.servers.http_server:app \\
        --host 127.0.0.1 --port 8003 --workers 1

⚠️ `--host 127.0.0.1` (boucle locale) par défaut — ce daemon n'a PAS besoin
d'être joignable depuis l'extérieur (webhook API + workers Celery tournent
sur la même machine et s'y connectent en localhost via MCP_DB_TRANSPORT=http).
N'utilisez `--host 0.0.0.0` QUE si un autre process a réellement besoin de
l'atteindre depuis une autre machine — et JAMAIS sans `MCP_HTTP_AUTH_TOKEN`
défini (voir le CRITICAL journalisé au démarrage sinon, et
`.env.example`) : sans token, `POST /mcp` exécute n'importe quel outil DB
pour quiconque atteint le port.

En systemd (recommandé en production, redémarrage automatique en cas de
crash) — unit minimal :

    [Unit]
    Description=Ladini MCP HTTP daemon
    After=network.target

    [Service]
    WorkingDirectory=/opt/ladini/backend
    ExecStart=/opt/ladini/.venv/bin/uvicorn \\
        ladini.protocols.mcp.servers.http_server:app \\
        --host 127.0.0.1 --port 8003 --workers 1
    Restart=on-failure
    RestartSec=2
    User=ladini

    [Install]
    WantedBy=multi-user.target

⚠️ TOUJOURS `--workers 1` pour CE process (voir note ci-dessous) — pour plus
de débit, lancer PLUSIEURS INSTANCES de ce daemon sur des ports différents
derrière un load balancer, pas plusieurs workers uvicorn dans le même process.

Pourquoi un seul worker uvicorn : `AgriDBMCPServer`/`runtime` sont des
singletons process-local (module-level). Avec `--workers N>1`, uvicorn
FORKE N process séparés = N pools DB distincts recréés indépendamment — on
retomberait exactement dans le problème que ce daemon est censé résoudre
(perte du singleton, pool non partagé). Scaler horizontalement (plusieurs
daemons + load balancer) préserve la propriété "un pool chaud unique par
process persistant" tout en augmentant le débit total. C'est aussi pourquoi
``StreamableHTTPSessionManager`` est construit en mode ``stateless=True`` :
cohérent avec la nature déjà sans état du runtime Ladini (aucune
session MCP à faire survivre à un redémarrage/à répartir entre daemons).
"""

from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from ladini.core.database import close_db
from ladini.core.log_redaction import install_log_redaction
from ladini.core.settings import settings
from ladini.infrastructure.mcp.runtime import runtime
from ladini.protocols.mcp.servers.db_server import LadiniMCPEntryPoint

# Voir core/log_redaction.py — même rationale que api/main.py et
# api/celery_app.py : le daemon MCP journalise lui aussi des identifiants
# (MCP_CALL_AUDIT côté appelant, mais aussi ses propres logs de requête).
install_log_redaction()

logger = logging.getLogger("Ladini.MCP.HttpServer")


def require_auth(request: Request) -> None:
    """Authentifie l'appelant via le secret partagé (si configuré).

    Le daemon exécute des outils qui écrivent en base et débloquent de
    l'argent, en dérivant l'identité de l'appelant depuis le payload — il ne
    doit donc JAMAIS être joignable sans preuve d'appartenance à
    l'infrastructure. Comparaison en temps constant (`hmac.compare_digest`)
    pour ne pas exposer le secret à une attaque temporelle.

    Secret absent : on n'interrompt pas un déploiement existant, mais le
    démarrage a déjà journalisé un CRITICAL (voir `lifespan`).
    """
    expected = str(getattr(settings, "MCP_HTTP_AUTH_TOKEN", "") or "").strip()
    if not expected:
        return

    header = str(request.headers.get("authorization") or "")
    prefix = "bearer "
    provided = (
        header[len(prefix) :].strip() if header.lower().startswith(prefix) else ""
    )
    if not provided:
        provided = str(request.headers.get("x-mcp-token") or "").strip()

    if not provided or not hmac.compare_digest(provided, expected):
        # Ne jamais préciser si c'est le jeton ou son absence qui pose problème.
        logger.warning(
            "MCP_HTTP_AUTH_DENIED | client=%s | path=%s",
            getattr(request.client, "host", "?"),
            request.url.path,
        )
        raise HTTPException(status_code=401, detail="Unauthorized")


class _RequireAuthASGIMiddleware:
    """Applique `require_auth` devant une app ASGI brute.

    `StreamableHTTPSessionManager.handle_request` est un handler ASGI de bas
    niveau (pas une route FastAPI) — `Depends(require_auth)` ne s'y applique
    donc pas directement. Ce middleware reconstruit un `Request` Starlette à
    partir du `scope` ASGI pour réutiliser LA MÊME fonction `require_auth`
    que ``/health`` protégeait déjà, sans dupliquer la logique
    d'authentification entre deux implémentations.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        try:
            require_auth(request)
        except HTTPException as exc:
            response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
            await response(scope, receive, send)
            return

        await self._app(scope, receive, send)


# Taille maximale du corps de requête acceptée sur /mcp. `StreamableHTTPSessionManager`
# n'expose nativement aucune limite de ce type — l'ancien handler REST maison
# en avait une (256 Ko) précisément parce que ce daemon désérialise dans un
# process UNIQUE partagé par tous les tours en cours ; on la restaure ici au
# niveau transport pour ne pas régresser sur cette protection.
_MAX_BODY_BYTES = 256 * 1024


class _MaxBodySizeASGIMiddleware:
    """Rejette tôt (413) un corps de requête trop volumineux déclaré via
    `Content-Length`, avant qu'il n'atteigne le gestionnaire de session MCP.
    """

    def __init__(self, app: ASGIApp, max_bytes: int = _MAX_BODY_BYTES) -> None:
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        raw_length = headers.get(b"content-length")
        if raw_length is not None:
            try:
                declared = int(raw_length)
            except ValueError:
                declared = None
            if declared is not None and declared > self._max_bytes:
                response = JSONResponse(
                    {"detail": "Corps de requête trop volumineux."},
                    status_code=413,
                )
                await response(scope, receive, send)
                return

        await self._app(scope, receive, send)


# ---------------------------------------------------------------------------
# Singletons — instanciés UNE SEULE FOIS au chargement du module. Ce module
# n'est importé qu'une fois par process uvicorn (--workers 1), donc
# `mcp_entry_point`/`session_manager` restent uniques et persistants pour
# toute la durée de vie du daemon.
#
# `LadiniMCPEntryPoint` est LE MÊME point d'entrée que le transport
# stdio (db_server.py) : mêmes Tools, mêmes Resources (_PUBLIC_CATALOG_TOOLS,
# voir Tâche 4) — un seul serveur MCP, deux transports.
# ---------------------------------------------------------------------------
mcp_entry_point = LadiniMCPEntryPoint()

session_manager = StreamableHTTPSessionManager(
    app=mcp_entry_point.server,
    event_store=None,
    stateless=True,
)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Hook de démarrage/arrêt — amorce le pool DB ET le gestionnaire de
    session MCP AVANT d'accepter du trafic.

    Réutilise `runtime.lifespan()` (infrastructure/mcp/runtime.py), qui
    instancie `AgriDatabaseService` et résout le sessionmaker. Point critique :
    ce hook tourne DÉJÀ sur la boucle asyncio d'uvicorn (celle qui servira
    toutes les requêtes HTTP ensuite) — contrairement à
    `runtime.start_with_policy()` (utilisé par le transport stdio via
    `run_coro_blocking`), qui routerait la création du pool sur un AUTRE
    event loop si on l'appelait ici, et casserait l'affinité de loop asyncpg
    (« Future attached to a different loop », voir le commentaire d'alerte
    dans infrastructure/mcp/utils.py). `runtime.lifespan()` est le SEUL des
    deux mécanismes déjà existants qui soit sûr dans un contexte FastAPI.

    En plus de résoudre le sessionmaker, on ouvre une VRAIE connexion
    (`SELECT 1`) pour amorcer le pool + le handshake SSL — exactement le même
    principe que `_warmup_db()` (api/tasks.py, déjà utilisé pour les workers
    Celery), réutilisé ici tel quel plutôt que dupliqué.

    `session_manager.run()` doit lui aussi être actif pendant toute la durée
    de vie de l'app : il crée le task group interne dont
    `handle_request` a besoin — l'omettre lève `RuntimeError("Task group is
    not initialized")` au premier appel.
    """
    import asyncio

    from sqlalchemy import text as _sql_text

    from ladini.core.database import get_engine

    # (2026-08-30) Filtre le bruit asyncio bénin propre à Windows :
    # `ProactorEventLoop` journalise en ERROR un `ConnectionResetError` lors
    # du `socket.shutdown()` de fermeture d'une connexion HTTP déjà servie
    # avec succès (voir les "200 OK"/"202 Accepted" juste avant dans les
    # logs — la requête a déjà abouti, c'est le TEARDOWN qui râle). Bug
    # asyncio connu (cpython #83413), spécifique à `ProactorBasePipeTransport
    # ._call_connection_lost` sous Windows — jamais reproduit hors Windows,
    # jamais corrélé à un vrai échec fonctionnel ce soir. On ne supprime QUE
    # cette signature précise (callback + ConnectionResetError [WinError
    # 10054]) — toute autre exception continue de remonter normalement.
    def _quiet_windows_proactor_noise(
        loop: asyncio.AbstractEventLoop, context: dict
    ) -> None:
        exc = context.get("exception")
        handle_repr = str(context.get("handle") or "")
        if (
            isinstance(exc, ConnectionResetError)
            and "_call_connection_lost" in handle_repr
        ):
            logger.debug(
                "Bénin (fermeture socket Windows post-réponse) : %s", context.get("message")
            )
            return
        loop.default_exception_handler(context)

    try:
        asyncio.get_running_loop().set_exception_handler(_quiet_windows_proactor_noise)
    except Exception:
        pass  # best-effort — ne bloque jamais le démarrage du daemon

    logger.info("🚀 MCP HTTP daemon starting — warming DB pool...")
    if not str(getattr(settings, "MCP_HTTP_AUTH_TOKEN", "") or "").strip():
        logger.critical(
            "MCP HTTP daemon NON AUTHENTIFIÉ (MCP_HTTP_AUTH_TOKEN vide) : "
            "POST /mcp exécute n'importe quel outil DB et dérive l'identité "
            "de l'appelant depuis le payload. N'exposez ce port QUE sur la "
            "boucle locale (--host 127.0.0.1) tant que le secret n'est pas "
            "défini côté daemon ET côté client."
        )
    async with runtime.lifespan():
        try:
            engine = get_engine()
            if engine is not None:
                async with engine.connect() as conn:
                    await conn.execute(_sql_text("SELECT 1"))
                logger.info("🔥 Pool DB amorcé (warm-up) — daemon prêt.")
            else:
                logger.warning(
                    "Warm-up DB ignoré : moteur indisponible (DATABASE_URL manquante ?)."
                )
        except Exception:
            # Ne bloque pas le démarrage du daemon : le premier vrai appel
            # ré-essaiera. Le /health endpoint reflète l'état réel.
            logger.warning(
                "Warm-up DB au démarrage du daemon échoué (non bloquant).",
                exc_info=True,
            )

        async with session_manager.run():
            logger.info("✅ MCP HTTP daemon ready — listening on /mcp.")
            try:
                yield
            finally:
                logger.info("🛑 MCP HTTP daemon stopping — closing DB pool...")
                try:
                    await close_db()
                except Exception:
                    logger.exception("close_db a échoué à l'arrêt du daemon.")


app = FastAPI(
    title="Ladini MCP HTTP Daemon",
    description=(
        "Transport HTTP persistant pour AgriDBMCPServer — remplace le "
        "spawn stdio par tour par un process unique à pool DB chaud. "
        "Conforme au spec MCP (Streamable HTTP, JSON-RPC 2.0) sur /mcp."
    ),
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict[str, Any]:
    """Health-check (systemd, load balancer, supervision) — reste ouvert
    sans authentification : ne révèle aucune donnée métier."""
    return {
        "status": "ok" if runtime.is_ready else "degraded",
        "ready": runtime.is_ready,
    }


@app.get("/version")
async def version_info() -> dict[str, Any]:
    """Identité de la release (hardening DevOps 2026-09-10). Injectée par
    l'environnement du conteneur ; aucune valeur secrète."""
    import os as _os

    return {
        "release": _os.getenv("RELEASE_VERSION", "unknown"),
        "git_sha": _os.getenv("GIT_SHA", "unknown"),
        "built_at": _os.getenv("BUILD_TIMESTAMP", "unknown"),
        "service": "ladini-mcp",
        "ready": runtime.is_ready,
    }


app.mount(
    "/mcp",
    _MaxBodySizeASGIMiddleware(
        _RequireAuthASGIMiddleware(session_manager.handle_request)
    ),
)
