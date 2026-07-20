"""Télémétrie unifiée — Infrastructure (OpenTelemetry + Prometheus) et LLMOps (Langfuse).

Objectif : un seul module qui relie l'infra à l'IA via un `trace_id` partagé.

    Webhook Twilio ──► middleware OTel (trace_id) ──► header Celery ──► Worker
        └──► graphe LangGraph ──► appel Groq (get_llm) ──► Langfuse Generation
                     (tous rattachés au MÊME trace_id → 1 session = 1 Trace)

RÈGLE D'OR : ce module ne doit JAMAIS casser l'application. Toutes les libs
d'observabilité sont optionnelles et lazy-importées ; si une lib manque ou si
la télémétrie est désactivée (settings), chaque fonction devient un no-op
silencieux. Aucun import lourd au niveau module (sensibilité cold-start
documentée dans ce repo).

Activation via settings/env :
    OTEL_ENABLED, OTEL_EXPORTER_OTLP_ENDPOINT
    PROMETHEUS_ENABLED
    LANGFUSE_ENABLED, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST
"""
from __future__ import annotations

import logging
import time
from contextvars import ContextVar
from typing import Any, Dict, Optional

logger = logging.getLogger("agriconnect.telemetry")

# ─────────────────────────────────────────────────────────────────────
# Contexte de trace partagé (API → Celery → graphe → LLM)
# ─────────────────────────────────────────────────────────────────────
# Le trace_id OTel courant, propagé de bout en bout. Sert d'ID de Trace
# Langfuse pour lier l'infra à l'IA. `None` hors d'une requête tracée.
_current_trace_id: ContextVar[Optional[str]] = ContextVar("current_trace_id", default=None)
# Contexte métier attaché à la trace courante (phone, role, goal…) pour
# enrichir la Trace Langfuse au 1er appel LLM.
_current_trace_meta: ContextVar[Dict[str, Any]] = ContextVar("current_trace_meta", default={})

# Handles singletons (initialisés une fois au démarrage du process).
_langfuse_client: Optional[Any] = None
_otel_tracer: Optional[Any] = None
_initialized = False

# Traces Langfuse déjà créées ce process-ci (évite de recréer la même trace).
_seen_traces: set[str] = set()


# ─────────────────────────────────────────────────────────────────────
# Métriques Prometheus (déclarées à l'import de prometheus_client seulement)
# ─────────────────────────────────────────────────────────────────────
_metrics: Dict[str, Any] = {}


def _init_prometheus() -> None:
    global _metrics
    if _metrics:
        return
    try:
        from prometheus_client import Counter, Histogram
    except Exception:
        logger.info("[telemetry] prometheus_client absent — métriques désactivées.")
        return
    _metrics = {
        "webhooks_received": Counter(
            "agriconnect_webhooks_received_total",
            "Nombre de webhooks Twilio reçus.",
            ["channel"],
        ),
        "http_requests": Counter(
            "agriconnect_http_requests_total",
            "Requêtes HTTP par méthode / route / code.",
            ["method", "route", "status"],
        ),
        "http_5xx": Counter(
            "agriconnect_http_5xx_total",
            "Erreurs serveur 5xx.",
            ["route"],
        ),
        "http_latency": Histogram(
            "agriconnect_http_request_duration_seconds",
            "Latence HTTP globale (secondes).",
            ["method", "route"],
            buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
        ),
        "llm_calls": Counter(
            "agriconnect_llm_calls_total",
            "Appels LLM par modèle / statut.",
            ["model", "status"],
        ),
        "llm_tokens": Counter(
            "agriconnect_llm_tokens_total",
            "Tokens LLM consommés.",
            ["model", "kind"],  # kind=prompt|completion
        ),
        "llm_latency": Histogram(
            "agriconnect_llm_duration_seconds",
            "Latence des appels LLM (secondes).",
            ["model"],
            buckets=(0.1, 0.25, 0.5, 1, 2, 4, 8, 16),
        ),
    }


def _metric(name: str) -> Optional[Any]:
    return _metrics.get(name)


# ─────────────────────────────────────────────────────────────────────
# Initialisation (appelée une fois : startup FastAPI ET init worker Celery)
# ─────────────────────────────────────────────────────────────────────
def init_telemetry(service_name: str = "agriconnect") -> None:
    """Initialise OTel + Prometheus + Langfuse selon les settings. Idempotent."""
    global _initialized, _otel_tracer, _langfuse_client
    if _initialized:
        return
    _initialized = True

    try:
        from agriconnect.core.settings import settings
    except Exception:
        settings = None  # type: ignore

    def _flag(name: str, default: bool = False) -> bool:
        return bool(getattr(settings, name, default)) if settings else default

    # 1. Prometheus (déclare les métriques).
    if _flag("PROMETHEUS_ENABLED", True):
        _init_prometheus()

    # 2. OpenTelemetry (TracerProvider + exporteur OTLP).
    if _flag("OTEL_ENABLED", False):
        try:
            from opentelemetry import trace
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )

            endpoint = getattr(settings, "OTEL_EXPORTER_OTLP_ENDPOINT", None) if settings else None
            provider = TracerProvider(
                resource=Resource.create({"service.name": service_name})
            )
            if endpoint:
                provider.add_span_processor(
                    BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True))
                )
            trace.set_tracer_provider(provider)
            _otel_tracer = trace.get_tracer(service_name)
            logger.info("[telemetry] OpenTelemetry initialisé (endpoint=%s).", endpoint)
        except Exception as exc:
            logger.warning("[telemetry] OTel indisponible (%s) — désactivé.", exc)

    # 3. Langfuse (LLMOps).
    if _flag("LANGFUSE_ENABLED", False):
        try:
            from langfuse import Langfuse

            _langfuse_client = Langfuse(
                public_key=getattr(settings, "LANGFUSE_PUBLIC_KEY", None),
                secret_key=getattr(settings, "LANGFUSE_SECRET_KEY", None),
                host=getattr(settings, "LANGFUSE_HOST", None),
            )
            logger.info(
                "[telemetry] Langfuse initialisé (host=%s).",
                getattr(settings, "LANGFUSE_HOST", "default"),
            )
        except Exception as exc:
            logger.warning("[telemetry] Langfuse indisponible (%s) — désactivé.", exc)


def instrument_fastapi(app: Any) -> None:
    """Instrumente automatiquement FastAPI via OTel si disponible (best-effort)."""
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        FastAPIInstrumentor.instrument_app(app)
        logger.info("[telemetry] FastAPI instrumenté (OTel).")
    except Exception as exc:
        logger.debug("[telemetry] instrument_fastapi ignoré: %s", exc)


def instrument_celery() -> None:
    """Instrumente Celery via OTel si disponible (best-effort)."""
    try:
        from opentelemetry.instrumentation.celery import CeleryInstrumentor
        CeleryInstrumentor().instrument()
        logger.info("[telemetry] Celery instrumenté (OTel).")
    except Exception as exc:
        logger.debug("[telemetry] instrument_celery ignoré: %s", exc)


# ─────────────────────────────────────────────────────────────────────
# Gestion du trace_id partagé
# ─────────────────────────────────────────────────────────────────────
def new_trace_id() -> str:
    """Génère un trace_id (hex 32) — format OTel, réutilisable comme id Langfuse."""
    try:
        from opentelemetry import trace
        span = trace.get_current_span()
        ctx = span.get_span_context() if span else None
        if ctx and getattr(ctx, "trace_id", 0):
            return format(ctx.trace_id, "032x")
    except Exception:
        pass
    import uuid
    return uuid.uuid4().hex


def set_trace_context(trace_id: Optional[str], **meta: Any) -> None:
    """Fixe le trace_id courant + métadonnées métier (phone, role, goal…)."""
    _current_trace_id.set(trace_id or None)
    if meta:
        _current_trace_meta.set({k: v for k, v in meta.items() if v is not None})


def get_trace_id() -> Optional[str]:
    return _current_trace_id.get()


def _ensure_langfuse_trace(trace_id: str) -> None:
    """Crée la Trace Langfuse (une fois) pour ce trace_id, avec le contexte métier."""
    if _langfuse_client is None or not trace_id or trace_id in _seen_traces:
        return
    try:
        meta = _current_trace_meta.get() or {}
        _langfuse_client.trace(
            id=trace_id,
            name="whatsapp_session_turn",
            user_id=meta.get("user_phone"),
            session_id=meta.get("user_phone"),
            metadata={k: v for k, v in meta.items() if k != "user_phone"},
        )
        _seen_traces.add(trace_id)
    except Exception as exc:
        logger.debug("[telemetry] ensure trace langfuse échoué: %s", exc)


# ─────────────────────────────────────────────────────────────────────
# Capture d'une Generation LLM (appelée depuis le choke point get_llm.py)
# ─────────────────────────────────────────────────────────────────────
def record_generation(
    *,
    model: str,
    messages: Any,
    output: Optional[str],
    latency_s: float,
    usage: Optional[Dict[str, int]] = None,
    error: Optional[str] = None,
    name: str = "groq_completion",
) -> None:
    """Enregistre un appel LLM : Langfuse Generation + métriques Prometheus.

    Capture TOUT ce qui est exigé côté LLMOps :
      - le modèle utilisé (ex: llama-3.3-70b-versatile)
      - le prompt exact (messages interpolés) et la réponse brute
      - la latence précise de l'appel
      - l'usage tokens (prompt_tokens / completion_tokens) → coût
      - les erreurs API (rate limit / timeout / …)
    Défensif : n'échoue jamais, quel que soit l'état des backends.
    """
    status = "error" if error else "success"
    prompt_tokens = int((usage or {}).get("prompt_tokens") or 0)
    completion_tokens = int((usage or {}).get("completion_tokens") or 0)

    # 1. Prometheus.
    try:
        if _metric("llm_calls"):
            _metric("llm_calls").labels(model=model, status=status).inc()
        if _metric("llm_latency"):
            _metric("llm_latency").labels(model=model).observe(latency_s)
        if _metric("llm_tokens"):
            if prompt_tokens:
                _metric("llm_tokens").labels(model=model, kind="prompt").inc(prompt_tokens)
            if completion_tokens:
                _metric("llm_tokens").labels(model=model, kind="completion").inc(completion_tokens)
    except Exception:
        pass

    # 2. Langfuse Generation, rattachée à la Trace du trace_id courant.
    if _langfuse_client is None:
        return
    trace_id = _current_trace_id.get()
    try:
        if trace_id:
            _ensure_langfuse_trace(trace_id)
        meta = _current_trace_meta.get() or {}
        gen_kwargs: Dict[str, Any] = {
            "name": name,
            "model": model,
            "input": messages,
            "output": output,
            "metadata": {"goal": meta.get("goal"), "latency_s": round(latency_s, 3)},
            "usage": {
                "input": prompt_tokens or None,
                "output": completion_tokens or None,
                "unit": "TOKENS",
            },
        }
        if trace_id:
            gen_kwargs["trace_id"] = trace_id
        if error:
            gen_kwargs["level"] = "ERROR"
            gen_kwargs["status_message"] = error[:500]
        _langfuse_client.generation(**gen_kwargs)
    except Exception as exc:
        logger.debug("[telemetry] record_generation langfuse échoué: %s", exc)


def flush() -> None:
    """Force l'envoi des évènements Langfuse bufferisés (fin de tâche worker)."""
    if _langfuse_client is not None:
        try:
            _langfuse_client.flush()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────
# Métriques HTTP (utilisées par le middleware FastAPI)
# ─────────────────────────────────────────────────────────────────────
def observe_http(method: str, route: str, status_code: int, duration_s: float) -> None:
    try:
        if _metric("http_requests"):
            _metric("http_requests").labels(method=method, route=route, status=str(status_code)).inc()
        if _metric("http_latency"):
            _metric("http_latency").labels(method=method, route=route).observe(duration_s)
        if status_code >= 500 and _metric("http_5xx"):
            _metric("http_5xx").labels(route=route).inc()
    except Exception:
        pass


def count_webhook(channel: str = "twilio") -> None:
    try:
        if _metric("webhooks_received"):
            _metric("webhooks_received").labels(channel=channel).inc()
    except Exception:
        pass


def prometheus_asgi_response():
    """Retourne (body, content_type) pour l'endpoint /metrics, ou None si absent."""
    try:
        from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
        return generate_latest(), CONTENT_TYPE_LATEST
    except Exception:
        return None


__all__ = [
    "init_telemetry",
    "instrument_fastapi",
    "instrument_celery",
    "new_trace_id",
    "set_trace_context",
    "get_trace_id",
    "record_generation",
    "observe_http",
    "count_webhook",
    "prometheus_asgi_response",
    "flush",
]
