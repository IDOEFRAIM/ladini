"""MCP HTTP daemon — transport persistant pour AgriDBMCPServer.j

Élimine le cold-start du transport stdio (diagnostiqué à ~6-9s/tour : spawn
process + import lourd + pool DB recréé à froid) en gardant un SEUL process
Python vivant, avec un SEUL pool de connexions Postgres chaud, qui répond à
tous les tours via de simples requêtes HTTP locales.

Ce fichier ne contient AUCUNE logique métier nouvelle : il enveloppe
`AgriDBMCPServer` (`infrastructure/mcp/runtime.py`) — la MÊME classe déjà
utilisée par le transport stdio (`db_server.py`) — derrière deux endpoints
REST. `db_server.py` et les agents ne sont pas modifiés.

Contrat HTTP consommé par `HttpMCPAdapter` (infrastructure/mcp/client.py) :
    GET  /tools  -> liste des tools (identique à AgriDBMCPServer.list_tools())
    POST /call   -> {"name": str, "arguments": dict} -> résultat JSON du tool

Lancement en daemon persistant (AVANT de démarrer l'API webhook et les
workers Celery, puisque ceux-ci se connecteront à ce daemon via
MCP_DB_TRANSPORT=http) :

    uvicorn agriconnect.protocols.mcp.servers.http_server:app \\
        --host 0.0.0.0 --port 8003 --workers 1

En systemd (recommandé en production, redémarrage automatique en cas de
crash) — unit minimal :

    [Unit]
    Description=AgriConnect MCP HTTP daemon
    After=network.target

    [Service]
    WorkingDirectory=/opt/agriconnect/backend
    ExecStart=/opt/agriconnect/.venv/bin/uvicorn \\
        agriconnect.protocols.mcp.servers.http_server:app \\
        --host 0.0.0.0 --port 8003 --workers 1
    Restart=on-failure
    RestartSec=2
    User=agriconnect

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
process persistant" tout en augmentant le débit total.
"""
from __future__ import annotations

import hmac
import json
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder

from agriconnect.core.database import close_db
from agriconnect.core.settings import settings
from agriconnect.infrastructure.mcp.runtime import AgriDBMCPServer, runtime
from agriconnect.services.database.errors import sanitize_error_message

logger = logging.getLogger("AgriConnect.MCP.HttpServer")

# Taille maximale du corps JSON accepté sur /call. Les arguments d'outils sont
# de petits objets (identifiants, quantités, texte court) ; au-delà, c'est du
# bruit ou un abus — refuser tôt évite de désérialiser un payload arbitraire
# dans un process partagé par TOUS les tours en cours.
_MAX_BODY_BYTES = 256 * 1024  # 256 Ko


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
    provided = header[len(prefix):].strip() if header.lower().startswith(prefix) else ""
    if not provided:
        provided = str(request.headers.get("x-mcp-token") or "").strip()

    if not provided or not hmac.compare_digest(provided, expected):
        # Ne jamais préciser si c'est le jeton ou son absence qui pose problème.
        logger.warning(
            "MCP_HTTP_AUTH_DENIED | client=%s | path=%s",
            getattr(request.client, "host", "?"), request.url.path,
        )
        raise HTTPException(status_code=401, detail="Unauthorized")

# ---------------------------------------------------------------------------
# Singleton — instancié UNE SEULE FOIS au chargement du module, comme
# `AgriConnectMCPEntryPoint.__init__` (db_server.py) instancie
# `self.backend = AgriDBMCPServer()`. Ce module n'est importé qu'une fois par
# process uvicorn (--workers 1), donc `backend` reste unique et persistant
# pour toute la durée de vie du daemon.
# ---------------------------------------------------------------------------
backend = AgriDBMCPServer()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Hook de démarrage/arrêt — amorce le pool DB AVANT d'accepter du trafic.

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
    """
    from sqlalchemy import text as _sql_text
    from agriconnect.core.database import get_engine

    logger.info("🚀 MCP HTTP daemon starting — warming DB pool...")
    if not str(getattr(settings, "MCP_HTTP_AUTH_TOKEN", "") or "").strip():
        logger.critical(
            "MCP HTTP daemon NON AUTHENTIFIÉ (MCP_HTTP_AUTH_TOKEN vide) : "
            "POST /call exécute n'importe quel outil DB et dérive l'identité "
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
            logger.warning("Warm-up DB au démarrage du daemon échoué (non bloquant).", exc_info=True)

        logger.info("✅ MCP HTTP daemon ready — listening.")
        try:
            yield
        finally:
            logger.info("🛑 MCP HTTP daemon stopping — closing DB pool...")
            try:
                await close_db()
            except Exception:
                logger.exception("close_db a échoué à l'arrêt du daemon.")


app = FastAPI(
    title="AgriConnect MCP HTTP Daemon",
    description=(
        "Transport HTTP persistant pour AgriDBMCPServer — remplace le "
        "spawn stdio par tour par un process unique à pool DB chaud."
    ),
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict[str, Any]:
    """Health-check (systemd, load balancer, supervision)."""
    return {"status": "ok" if runtime.is_ready else "degraded", "ready": runtime.is_ready}


@app.get("/tools", dependencies=[Depends(require_auth)])
async def list_tools() -> list[dict[str, Any]]:
    """Catalogue des tools — identique à `AgriDBMCPServer.list_tools()` côté stdio.

    Authentifié au même titre que /call : le catalogue révèle l'intégralité de
    la surface d'attaque (noms d'outils + schémas d'arguments).
    """
    return backend.list_tools()


@app.post("/call", dependencies=[Depends(require_auth)])
async def call_tool(request: Request) -> JSONResponse:
    """Exécute un tool — même point d'entrée métier que le handler stdio de
    `db_server.py` (`self.backend.call_tool(name=name, arguments=arguments)`),
    juste ré-exposé en HTTP. Renvoie un JSON natif (pas de double-encodage en
    string comme le TextContent MCP du transport stdio) : `HttpMCPAdapter`
    (infrastructure/mcp/client.py) appelle déjà `resp.json()` quand
    Content-Type est application/json.
    """
    raw_body = await request.body()
    if len(raw_body) > _MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Corps de requête trop volumineux.")

    # Un JSON malformé ne doit pas produire une 500 avec traceback : ce daemon
    # est partagé par tous les tours en cours et sa surface d'erreur ne doit
    # rien révéler de son implémentation.
    try:
        body = json.loads(raw_body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Corps JSON invalide.")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Le corps doit être un objet JSON.")

    name = body.get("name")
    if not name or not isinstance(name, str):
        raise HTTPException(status_code=400, detail="Champ 'name' manquant ou invalide.")

    # `arguments` est déballé en **kwargs plus bas dans la chaîne : une valeur
    # non-dict (liste, chaîne...) provoquerait un TypeError opaque au lieu
    # d'un refus net et traçable.
    arguments = body.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise HTTPException(status_code=400, detail="Champ 'arguments' doit être un objet JSON.")

    try:
        result = await backend.call_tool(name=name, arguments=arguments)
    except Exception as exc:
        # Même garde-fou que db_server.py : ne jamais laisser une exception
        # de tool faire tomber le process — ce daemon est PARTAGÉ par tous
        # les tours en cours, contrairement au stdio (1 process par tour).
        #
        # `str(exc)` renvoyait la trace technique BRUTE (contrainte SQL, nom de
        # table, dialecte...) au client HTTP, contournant complètement la
        # barrière anti-fuite de `services/database/errors.py`. On applique
        # désormais la MÊME sanitisation que le reste du backend : les erreurs
        # métier passent intactes, les erreurs techniques deviennent génériques
        # (la cause réelle restant dans le log serveur ci-dessus).
        logger.exception("Tool failure via HTTP: %s", name)
        return JSONResponse(
            status_code=200,
            content={
                "ok": False,
                "error": sanitize_error_message(exc, context=f"mcp_http:{name}"),
                "tool": name,
            },
        )

    payload = result if isinstance(result, dict) else {"data": result}

    return JSONResponse(content=jsonable_encoder(payload))