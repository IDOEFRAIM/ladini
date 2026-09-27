import logging
import os
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from ladini.api.routes.admin import router as admin_router
from ladini.api.routes.analytics_admin import router as analytics_admin_router
from ladini.api.routes.analytics_admin_producers import (
    router as analytics_admin_producers_router,
)
from ladini.api.routes.market import router as market_router
from ladini.api.routes.paydunya_webhook import router as paydunya_router
from ladini.api.routes.twilio_webhook import (
    router as twilio_router,  # repli (MESSAGING_PROVIDER=twilio)
)
from ladini.api.routes.webchat import router as webchat_router
from ladini.api.routes.whatsapp_webhook import (
    router as whatsapp_router,  # provider par défaut
)
from ladini.core import telemetry
from ladini.core.log_redaction import install_log_redaction
from ladini.core.settings import settings

# Le plus tôt possible — AVANT que gunicorn/uvicorn ne produisent leurs
# premiers logs (voir log_redaction.py pour le mécanisme et le pourquoi).
install_log_redaction()

logger = logging.getLogger("Ladini.API")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Démarrage + arrêt de l'API — style `lifespan` moderne (2026-09-11,
    remplace `@app.on_event("startup"/"shutdown")`, dépréciés par FastAPI :
    `on_event` reste fonctionnel aujourd'hui mais n'est plus maintenu et sera
    retiré ; `lifespan` est en plus strictement plus robuste — un seul
    gestionnaire de contexte au lieu de deux callbacks séparés, donc pas de
    risque qu'un futur ajout d'état partagé (ex: une ressource ouverte au
    démarrage) désynchronise l'ordre start/stop. Tout le CODE des deux
    anciens hooks est conservé à l'identique — seule la déclaration change.
    """
    # ── startup (avant `yield` — ex `_startup_telemetry`) ────────────
    # Initialise OTel + Prometheus + Langfuse une fois au démarrage de l'API,
    # puis instrumente FastAPI (spans automatiques par requête).
    telemetry.init_telemetry(service_name="ladini-api")
    telemetry.instrument_fastapi(app)

    # Sentry (bug corrigé 2026-09-16) : `SENTRY_DSN` était configuré en prod
    # (docker-compose.prod.yml) depuis longtemps mais `setup_logging()` —
    # seule fonction qui appelle `sentry_sdk.init(...)` — n'était JAMAIS
    # invoquée nulle part dans le code (uniquement `get_logger(name)`,
    # simple `logging.getLogger`, sans aucun câblage Sentry). On appelle ici
    # `init_sentry()` seul (pas `setup_logging()` complet, qui ferait
    # `logging.basicConfig(...)` + un `FileHandler` par-dessus la config de
    # logging déjà posée par gunicorn/uvicorn) — best-effort, ne doit jamais
    # empêcher l'API de démarrer.
    try:
        from ladini.core.logger import init_sentry

        init_sentry()
    except Exception as exc:
        logger.warning("Initialisation Sentry ignorée (non bloquant) : %s", exc)

    # (2026-09-02) Amorce le pool DB au démarrage — même correctif que
    # `api/tasks.py::init_worker_process` pour le worker Celery (voir
    # [[worker-warm-pool-cold-start]]), jamais appliqué au process API/webhook :
    # `core/database.py::get_sessionmaker()` initialise le moteur PARESSEUSEMENT
    # à la première requête qui le touche. Sans warm-up, la 1ère requête d'un
    # process API tout juste démarré (déploiement, scaling, restart) paie la
    # connexion à froid (handshake SSL DigitalOcean managed DB inclus) — si
    # cette 1ère requête est justement `persist_shared_location` (webhook GPS,
    # `core/location.py`), un simple ralentissement au démarrage peut se
    # traduire par un faux `LOCATION_PERSISTENCE_ERROR` (except Exception large,
    # jamais bloquant pour le webhook lui-même) présenté à l'utilisateur comme
    # un problème permanent, alors que c'était un coût de démarrage à usage
    # unique. Best-effort, jamais bloquant : un échec ici ne doit jamais
    # empêcher l'API de démarrer.
    try:
        from ladini.api.tasks import _warmup_db

        await _warmup_db()
    except Exception as exc:
        logger.warning("Warm-up DB au démarrage de l'API ignoré : %s", exc)

    yield

    # ── shutdown (après `yield` — ex `_shutdown_db`) ─────────────────
    # Ferme proprement le pool SQLAlchemy/asyncpg pendant la fenêtre de
    # graceful shutdown de gunicorn (SIGTERM → drain requêtes → ce hook —
    # voir --graceful-timeout côté Dockerfile.api et stop_grace_period côté
    # compose). Sans ça, les connexions au pooler DB restent ouvertes côté
    # client jusqu'au SIGKILL du process, au lieu d'être libérées à temps.
    from ladini.core.database import close_db

    await close_db()


app = FastAPI(
    title=" LADINI MarketCoach API",
    version="1.1.0",
    lifespan=lifespan,
)

# CORS — piloté par `settings.ALLOWED_ORIGINS` (la valeur était auparavant
# codée en dur à ["*"] ici, ignorant purement et simplement le réglage qui
# existait déjà dans core/settings.py).
#
# SÉCURITÉ : `allow_origins=["*"]` + `allow_credentials=True` est une
# combinaison dangereuse. Starlette, quand un cookie est présent, ne renvoie
# PAS `*` mais RÉFLÉCHIT l'Origin de la requête — n'importe quel site tiers
# peut alors émettre des requêtes AUTHENTIFIÉES cross-origin au nom de
# l'utilisateur. On n'active donc les credentials que si une liste blanche
# EXPLICITE d'origines est configurée. (L'API ne pose aujourd'hui aucun
# cookie de session — les webhooks Twilio/Paydunya sont serveur-à-serveur et
# ne dépendent pas du CORS —, donc ce durcissement ne retire aucune
# fonctionnalité utilisée.)
_allowed_origins = list(settings.ALLOWED_ORIGINS or ["*"])
_allow_all_origins = "*" in _allowed_origins
if _allow_all_origins:
    logger.warning(
        "CORS ouvert à toutes les origines (ALLOWED_ORIGINS=['*']) — credentials "
        "désactivés. Configurez une liste blanche pour autoriser les requêtes "
        "authentifiées cross-origin."
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=not _allow_all_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def trace_and_metrics_middleware(request: Request, call_next):
    """Génère/propage un trace_id unique par requête + mesure latence & codes.

    Le trace_id (format OTel, réutilisé comme id de Trace Langfuse) est :
      - lu depuis l'en-tête entrant `X-Trace-Id` s'il existe (chaînage amont),
      - sinon dérivé du span OTel courant / généré.
    Il est exposé dans la réponse (`X-Trace-Id`) ET stocké dans le contexte pour
    être propagé jusqu'au worker Celery (voir twilio_webhook → tasks).
    """
    incoming = request.headers.get("X-Trace-Id")
    trace_id = incoming or telemetry.new_trace_id()
    telemetry.set_trace_context(trace_id)
    request.state.trace_id = trace_id

    route = request.url.path
    start = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers["X-Trace-Id"] = trace_id
        return response
    finally:
        telemetry.observe_http(
            method=request.method,
            route=route,
            status_code=status_code,
            duration_s=time.perf_counter() - start,
        )


# 2. Inclus tes routers
app.include_router(market_router, prefix="/api")
app.include_router(webchat_router, prefix="/api")
# endpoints réels : /api/webchat/producer, /api/webchat/buyer — canal
# SYNCHRONE serveur-à-serveur pour le site web (voir routes/webchat.py pour
# le raisonnement sécurité complet). Jamais appelé directement par WhatsApp.
# Les deux webhooks WhatsApp restent enregistrés en parallèle : seul
# MESSAGING_PROVIDER (core/settings.py) décide lequel le worker utilise pour
# ENVOYER — recevoir sur les deux endpoints ne coûte rien et permet de
# basculer Meta/Twilio côté configuration webhook sans redéployer l'API.
app.include_router(
    whatsapp_router, prefix="/api"
)  # endpoint réel : /api/webhook/whatsapp
app.include_router(
    twilio_router, prefix="/api"
)  # endpoint réel : /api/webhook/twilio (repli)
app.include_router(
    paydunya_router, prefix="/api"
)  # endpoint réel : /api/webhooks/paydunya-ipn
app.include_router(admin_router)  # endpoint réel : /admin/llm/health (auth token)
app.include_router(analytics_admin_router)  # /internal/analytics/buyers/* (token interne, lecture seule)
app.include_router(analytics_admin_producers_router)  # /internal/analytics/producers/* (token interne, lecture seule)


# ── Métadonnées de release (hardening DevOps 2026-09-10) ─────────────
# Injectées à l'exécution par docker-compose.prod.yml (voir x-release-env) —
# JAMAIS lues depuis le code / git à l'exécution (le conteneur n'a pas le
# dépôt). Permet de répondre "quelle version tourne ici ?" sans SSH ni
# inspection d'image. Aucune valeur secrète.
_RELEASE_INFO = {
    "release": os.getenv("RELEASE_VERSION", "unknown"),
    "git_sha": os.getenv("GIT_SHA", "unknown"),
    "built_at": os.getenv("BUILD_TIMESTAMP", "unknown"),
    "compose_version": os.getenv("COMPOSE_VERSION", "unknown"),
    "service": "ladini-api",
}


@app.get("/version")
async def version_info():
    """Identité exacte de la release en cours d'exécution (release / git_sha /
    built_at). Consommé par `scripts/smoke.sh` et le runbook de déploiement."""
    return _RELEASE_INFO


@app.get("/health")
async def health_check():
    """Liveness — l'API répond. Léger, sans dépendance externe.

    Alias historique de `/health/live` ; les deux sont maintenus (le premier
    est câblé dans tous les HEALTHCHECK Docker, le second est la convention
    explicite liveness ≠ readiness — hardening DevOps 2026-09-10)."""
    return {"status": "healthy", "service": "ladini-api"}


@app.get("/health/live")
async def liveness_check():
    """Liveness explicite : le process répond-il ? AUCUNE dépendance externe
    testée — c'est ce que doit sonder `autoheal` (un blip DB/Redis ne doit
    jamais provoquer de restart storm). Voir `/health/ready` pour la vraie
    disponibilité de service."""
    return {"status": "alive", "service": "ladini-api"}


@app.get("/health/ready")
async def readiness_check():
    """Readiness — probe réelle des dépendances critiques (DB + Redis).

    Retourne 503 si une dépendance est down, pour que l'orchestrateur (compose /
    k8s / load-balancer) ne route pas de trafic vers une instance non prête.
    """
    components: dict[str, str] = {}
    healthy = True

    # DB
    try:
        from sqlalchemy import text as _sql_text

        from ladini.core.database import get_engine

        engine = get_engine()
        if engine is None:
            components["database"] = "unconfigured"
            healthy = False
        else:
            async with engine.connect() as conn:
                await conn.execute(_sql_text("SELECT 1"))
            components["database"] = "ok"
    except Exception as exc:
        components["database"] = f"error: {type(exc).__name__}"
        healthy = False

    # Redis (broker Celery)
    try:
        import redis as _redis

        from ladini.core.settings import settings

        _redis.from_url(settings.REDIS_URL, socket_connect_timeout=2).ping()
        components["redis"] = "ok"
    except Exception as exc:
        components["redis"] = f"error: {type(exc).__name__}"
        healthy = False

    status_code = 200 if healthy else 503
    return Response(
        content=__import__("json").dumps(
            {"status": "ready" if healthy else "degraded", "components": components}
        ),
        media_type="application/json",
        status_code=status_code,
    )


@app.get("/metrics")
async def metrics():
    """Endpoint de scrape Prometheus (format texte OpenMetrics)."""
    out = telemetry.prometheus_asgi_response()
    if out is None:
        return Response(
            content="# prometheus_client indisponible\n", media_type="text/plain"
        )
    body, content_type = out
    return Response(content=body, media_type=content_type)
