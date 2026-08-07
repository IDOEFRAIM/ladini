import logging
import time

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from agriconnect.api.routes.market import router as market_router
from agriconnect.api.routes.twilio_webhook import router as twilio_router  # repli (MESSAGING_PROVIDER=twilio)
from agriconnect.api.routes.whatsapp_webhook import router as whatsapp_router  # provider par défaut
from agriconnect.api.routes.paydunya_webhook import router as paydunya_router
from agriconnect.core import telemetry

logger = logging.getLogger("AgriConnect.API")

app = FastAPI(
    title=" LADINI MarketCoach API",
    version="1.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _startup_telemetry() -> None:
    # Initialise OTel + Prometheus + Langfuse une fois au démarrage de l'API,
    # puis instrumente FastAPI (spans automatiques par requête).
    telemetry.init_telemetry(service_name="agriconnect-api")
    telemetry.instrument_fastapi(app)


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
# Les deux webhooks WhatsApp restent enregistrés en parallèle : seul
# MESSAGING_PROVIDER (core/settings.py) décide lequel le worker utilise pour
# ENVOYER — recevoir sur les deux endpoints ne coûte rien et permet de
# basculer Meta/Twilio côté configuration webhook sans redéployer l'API.
app.include_router(whatsapp_router, prefix="/api")  # endpoint réel : /api/webhook/whatsapp
app.include_router(twilio_router, prefix="/api")  # endpoint réel : /api/webhook/twilio (repli)
app.include_router(paydunya_router, prefix="/api")  # endpoint réel : /api/webhooks/paydunya-ipn


@app.get("/health")
async def health_check():
    """Liveness — l'API répond. Léger, sans dépendance externe."""
    return {"status": "healthy", "service": "agriconnect-api"}


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
        from agriconnect.core.database import get_engine
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
        from agriconnect.core.settings import settings
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
        return Response(content="# prometheus_client indisponible\n", media_type="text/plain")
    body, content_type = out
    return Response(content=body, media_type=content_type)
