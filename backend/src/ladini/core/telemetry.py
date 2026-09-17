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
from contextvars import ContextVar
from typing import Any, Dict, Optional

logger = logging.getLogger("ladini.telemetry")

# ─────────────────────────────────────────────────────────────────────
# Contexte de trace partagé (API → Celery → graphe → LLM)
# ─────────────────────────────────────────────────────────────────────
# Le trace_id OTel courant, propagé de bout en bout. Sert d'ID de Trace
# Langfuse pour lier l'infra à l'IA. `None` hors d'une requête tracée.
_current_trace_id: ContextVar[Optional[str]] = ContextVar(
    "current_trace_id", default=None
)
# Contexte métier attaché à la trace courante (phone, role, goal…) pour
# enrichir la Trace Langfuse au 1er appel LLM.
# default=None (pas {}) : un dict par défaut serait UN SEUL objet partagé
# entre tous les contextes n'ayant jamais appelé `.set()` — une mutation en
# place fuiterait entre requêtes concurrentes. Les lecteurs font `.get() or {}`.
_current_trace_meta: ContextVar[Optional[Dict[str, Any]]] = ContextVar(
    "current_trace_meta", default=None
)

# Handles singletons (initialisés une fois au démarrage du process).
_langfuse_client: Optional[Any] = None
_otel_tracer: Optional[Any] = None
_otel_meter_provider: Optional[Any] = None
_initialized = False

# Traces Langfuse déjà créées ce process-ci (évite de recréer la même trace).
_seen_traces: set[str] = set()


# ─────────────────────────────────────────────────────────────────────
# Métriques — double émission Prometheus (scrape, `GET /metrics`) + OTel
# (push OTLP, voir _DualCounter/_DualHistogram ci-dessous).
#
# (2026-09-17, follow-up pre-Hetzner) — GAP CONFIRMÉ ET CORRIGÉ ICI : le
# process Celery WORKER n'a AUCUN port HTTP exposé (voir
# docker-compose.prod.yml, service `worker` — aucun `ports:`), donc
# `GET /metrics` (qui n'existe QUE côté API, api/main.py) ne peut
# STRUCTURELLEMENT jamais scraper les compteurs LLM (`llm_calls`,
# `llm_tokens`, `llm_latency`, `llm_fallback`, `llm_circuit_open`,
# `llm_cost`, `legacy_fallback`) enregistrés PAR le worker — ils existaient
# bien en mémoire (Counter/Histogram Prometheus process-local) mais
# n'atteignaient jamais Grafana. Solution retenue : OpenTelemetry metrics,
# PUSH via le MÊME pipeline OTLP déjà câblé pour les traces (`worker`→
# `alloy:4317`, voir OTEL_EXPORTER_OTLP_ENDPOINT) — aucune nouvelle
# infrastructure, aucun port à ouvrir/firewaller sur le worker, cohérent
# avec le choix déjà fait pour le tracing. Prometheus multiprocess mode
# (alternative envisagée) aurait exigé un serveur HTTP dédié par worker
# (nouveau port, nouvelle surface) pour un modèle scrape/pull qui ne colle
# pas à un process sans serveur web ; Pushgateway explicitement déconseillé
# par le brief sauf nécessité réelle — aucune ici. Cardinalité : mêmes
# labels qu'avant (model/status/provider/reason/prompt_family), JAMAIS
# phone/conversation_id/message_sid/raw prompt (§24, inchangé).
# ─────────────────────────────────────────────────────────────────────
_metrics: Dict[str, Any] = {}


class _DualCounterBound:
    """Labels déjà appliqués — `.inc()` écrit dans les DEUX backends."""

    __slots__ = ("_prom_bound", "_otel_instrument", "_attributes")

    def __init__(self, prom_bound: Any, otel_instrument: Any, attributes: Dict[str, str]):
        self._prom_bound = prom_bound
        self._otel_instrument = otel_instrument
        self._attributes = attributes

    def inc(self, amount: float = 1) -> None:
        if self._prom_bound is not None:
            self._prom_bound.inc(amount)
        if self._otel_instrument is not None:
            try:
                self._otel_instrument.add(amount, attributes=self._attributes)
            except Exception:
                pass  # jamais casser l'appelant pour une raison d'observabilité

    @property
    def _value(self) -> Any:
        # Passthrough vers l'objet `prometheus_client.Counter` RÉEL en
        # dessous — préserve la compatibilité avec le code/tests existants
        # qui introspectent `.labels(...)._value.get()` (API interne de
        # prometheus_client, pas publique, mais déjà utilisée ailleurs dans
        # ce repo avant l'introduction de ce wrapper — voir
        # test_telemetry_cost_and_legacy_fallback.py).
        return self._prom_bound._value


class _DualHistogramBound:
    __slots__ = ("_prom_bound", "_otel_instrument", "_attributes")

    def __init__(self, prom_bound: Any, otel_instrument: Any, attributes: Dict[str, str]):
        self._prom_bound = prom_bound
        self._otel_instrument = otel_instrument
        self._attributes = attributes

    def observe(self, value: float) -> None:
        if self._prom_bound is not None:
            self._prom_bound.observe(value)
        if self._otel_instrument is not None:
            try:
                self._otel_instrument.record(value, attributes=self._attributes)
            except Exception:
                pass

    @property
    def _value(self) -> Any:
        return self._prom_bound._value  # voir _DualCounterBound._value


class _DualCounter:
    """Même interface `.labels(**kw).inc(n)` qu'un `prometheus_client.
    Counter` — les ~15 sites d'appel existants (`_metric("llm_calls").
    labels(...).inc()`, etc.) n'ont RIEN à changer."""

    __slots__ = ("_prom", "_otel")

    def __init__(self, prom_counter: Any, otel_counter: Any):
        self._prom = prom_counter
        self._otel = otel_counter

    def labels(self, **kwargs: Any) -> _DualCounterBound:
        prom_bound = self._prom.labels(**kwargs) if self._prom is not None else None
        return _DualCounterBound(prom_bound, self._otel, {k: str(v) for k, v in kwargs.items()})


class _DualHistogram:
    __slots__ = ("_prom", "_otel")

    def __init__(self, prom_histogram: Any, otel_histogram: Any):
        self._prom = prom_histogram
        self._otel = otel_histogram

    def labels(self, **kwargs: Any) -> _DualHistogramBound:
        prom_bound = self._prom.labels(**kwargs) if self._prom is not None else None
        return _DualHistogramBound(prom_bound, self._otel, {k: str(v) for k, v in kwargs.items()})


def _init_metrics_meter() -> Optional[Any]:
    """MeterProvider OTel — PUSH périodique vers le même collector Alloy que
    les traces. `None` si OTel désactivé/indisponible (dual-emission
    dégrade alors silencieusement vers Prometheus seul, comportement
    historique inchangé)."""
    global _otel_meter_provider
    if not _otel_enabled():
        return None
    if _otel_meter_provider is not None:
        return _otel_meter_provider.get_meter("ladini")
    try:
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource

        from ladini.core.settings import settings

        endpoint = getattr(settings, "OTEL_EXPORTER_OTLP_ENDPOINT", None)
        if not endpoint:
            return None
        reader = PeriodicExportingMetricReader(
            # `timeout=10` : borne CHAQUE tentative d'export RPC individuelle
            # (indépendant du retry/backoff interne du SDK) — évite qu'une
            # seule tentative reste accrochée indéfiniment sur un réseau
            # dégradé. Ce reader tourne sur son propre thread d'arrière-plan
            # (jamais le thread qui traite les tâches Celery/requêtes API),
            # donc même un export lent ne dégrade pas le chemin critique —
            # voir `flush()` pour pourquoi on ne force JAMAIS cet export
            # depuis le chemin synchrone.
            OTLPMetricExporter(endpoint=endpoint, insecure=True, timeout=10),
            # (worker) un process Celery vit longtemps mais chaque tâche est
            # courte — export fréquent pour que les métriques LLM n'aient
            # pas à attendre la fin d'un long run de tâches avant d'arriver
            # à Grafana. `flush()` (fin de tâche worker, voir plus bas) force
            # aussi un export immédiat, cet intervalle est le filet pour le
            # process API (jamais "fini").
            export_interval_millis=15000,
        )
        _otel_meter_provider = MeterProvider(
            resource=Resource.create({"service.name": "ladini"}), metric_readers=[reader]
        )
        return _otel_meter_provider.get_meter("ladini")
    except Exception as exc:
        logger.warning("[telemetry] OTel metrics indisponibles (%s) — Prometheus seul.", exc)
        return None


def _init_prometheus() -> None:
    global _metrics
    if _metrics:
        return
    try:
        from prometheus_client import Counter, Histogram
    except Exception:
        logger.info("[telemetry] prometheus_client absent — métriques désactivées.")
        return

    meter = _init_metrics_meter()

    def _counter(name: str, description: str, labelnames: list) -> _DualCounter:
        prom = Counter(name, description, labelnames)
        otel = meter.create_counter(name, description=description) if meter else None
        return _DualCounter(prom, otel)

    def _histogram(name: str, description: str, labelnames: list, buckets: tuple) -> _DualHistogram:
        prom = Histogram(name, description, labelnames, buckets=buckets)
        otel = meter.create_histogram(name, description=description) if meter else None
        return _DualHistogram(prom, otel)

    _metrics = {
        "webhooks_received": _counter(
            "ladini_webhooks_received_total",
            "Nombre de webhooks Twilio reçus.",
            ["channel"],
        ),
        "http_requests": _counter(
            "ladini_http_requests_total",
            "Requêtes HTTP par méthode / route / code.",
            ["method", "route", "status"],
        ),
        "http_5xx": _counter(
            "ladini_http_5xx_total",
            "Erreurs serveur 5xx.",
            ["route"],
        ),
        "http_latency": _histogram(
            "ladini_http_request_duration_seconds",
            "Latence HTTP globale (secondes).",
            ["method", "route"],
            (0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
        ),
        # ── LLM (2026-09-17) — désormais RÉELLEMENT exposées côté worker
        # (voir le commentaire en tête de section) : `_counter`/`_histogram`
        # émettent en double Prometheus (scrape API) ET OTel/OTLP (push
        # worker+api → Alloy → Grafana Cloud).
        "llm_calls": _counter(
            "ladini_llm_calls_total",
            "Appels LLM par modèle / statut.",
            ["model", "status"],
        ),
        "llm_tokens": _counter(
            "ladini_llm_tokens_total",
            "Tokens LLM consommés.",
            ["model", "kind"],  # kind=prompt|completion
        ),
        "llm_latency": _histogram(
            "ladini_llm_duration_seconds",
            "Latence des appels LLM (secondes).",
            ["model"],
            (0.1, 0.25, 0.5, 1, 2, 4, 8, 16),
        ),
        # ── LLM Gateway (2026-09-02) — cardinalité bornée à dessein : jamais
        # de label user_id/phone/conversation_id (§24 du brief anti-cardinalité).
        "llm_fallback": _counter(
            "ladini_llm_fallback_total",
            "Bascules de repli du LLM Gateway (candidat primaire indisponible).",
            ["profile", "provider", "reason"],
        ),
        "llm_circuit_open": _counter(
            "ladini_llm_circuit_open_total",
            "Ouvertures du disjoncteur LLM Gateway par candidat.",
            ["provider", "model"],
        ),
        # (2026-09-13, Incrément G, spec §35) : mesure l'usage de l'ancien
        # interpréteur unifié (repli explicite sur échec infrastructurel des
        # micro-prompts A-F) — objectif affiché : tendre vers 0.
        "legacy_fallback": _counter(
            "ladini_legacy_fallback_total",
            "Replis vers l'interpréteur unifié legacy, par famille de prompt.",
            ["prompt_family"],
        ),
        # (2026-09-13, Incrément G, spec §25-28) : coût estimé par famille de
        # prompt/modèle — jamais par user_id/phone/conversation_id (même
        # discipline anti-cardinalité que `llm_fallback` ci-dessus).
        "llm_cost": _counter(
            "ladini_llm_cost_usd_total",
            "Coût USD estimé des appels LLM (prix venant de config, spec §23/§24).",
            ["prompt_family", "model"],
        ),
    }


def _metric(name: str) -> Optional[Any]:
    return _metrics.get(name)


# ─────────────────────────────────────────────────────────────────────
# Initialisation (appelée une fois : startup FastAPI ET init worker Celery)
# ─────────────────────────────────────────────────────────────────────
def init_telemetry(service_name: str = "ladini") -> None:
    """Initialise OTel + Prometheus + Langfuse selon les settings. Idempotent."""
    global _initialized, _otel_tracer, _langfuse_client
    if _initialized:
        return
    _initialized = True

    try:
        from ladini.core.settings import settings
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
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            endpoint = (
                getattr(settings, "OTEL_EXPORTER_OTLP_ENDPOINT", None)
                if settings
                else None
            )
            provider = TracerProvider(
                resource=Resource.create({"service.name": service_name})
            )
            if endpoint:
                provider.add_span_processor(
                    BatchSpanProcessor(
                        OTLPSpanExporter(endpoint=endpoint, insecure=True)
                    )
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


def _otel_enabled() -> bool:
    try:
        from ladini.core.settings import settings

        return bool(getattr(settings, "OTEL_ENABLED", False))
    except Exception:
        return False


def instrument_fastapi(app: Any) -> None:
    """Instrumente automatiquement FastAPI via OTel si disponible (best-effort)."""
    if not _otel_enabled():
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
        logger.info("[telemetry] FastAPI instrumenté (OTel).")
    except Exception as exc:
        logger.debug("[telemetry] instrument_fastapi ignoré: %s", exc)


def instrument_celery() -> None:
    """Instrumente Celery via OTel si disponible (best-effort).

    Gardé derrière OTEL_ENABLED : sans TracerProvider actif (voir
    init_telemetry), le CeleryInstrumentor pousse/pop quand même un
    contexte OTel à chaque tâche — avec les retries (`autoretry_for`), les
    signaux start/retry/failure se désynchronisent et un detach sans
    attach correspondant lève `TypeError: expected an instance of Token,
    got None` dans le worker.
    """
    if not _otel_enabled():
        return
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
    # ── LLM Gateway (2026-09-02) — tous optionnels, rétrocompatibles avec
    # les 2 sites d'appel existants (_GroqAdapter/_BedrockAdapter dans
    # get_llm.py) qui n'en passent aucun. Jamais de secret/credential dedans
    # (§21/§52 : vérifié — aucun champ ci-dessous ne porte de clé/token).
    profile: Optional[str] = None,
    provider: Optional[str] = None,
    attempt: Optional[int] = None,
    fallback_from: Optional[str] = None,
    fallback_reason: Optional[str] = None,
    structured_output_required: Optional[bool] = None,
    structured_output_valid: Optional[bool] = None,
    request_id: Optional[str] = None,
    agent_node: Optional[str] = None,
    # (2026-09-12) Sac libre de dimensions d'observabilité SUPPLÉMENTAIRES,
    # propre à l'appelant — ex: `message_sid`, `prompt_version`,
    # `cache_hit`, `llm_call_index`, `current_goal`, `expected_input` pour
    # `input_interpreter` (voir `interpreter/routing.py`). Ajouté en un seul
    # paramètre extensible plutôt qu'un nouveau paramètre nommé par
    # dimension : évite de retoucher cette signature (et les 4 signatures
    # en amont dans `llm_gateway/gateway.py`) à chaque nouveau besoin
    # d'observabilité. AUCUN secret/credential ne doit y transiter — même
    # règle que les autres champs de cette fonction (§21/§52).
    extra_metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Enregistre un appel LLM : Langfuse Generation + métriques Prometheus.

    Capture TOUT ce qui est exigé côté LLMOps :
      - le modèle utilisé (ex: llama-3.3-70b-versatile)
      - le prompt exact (messages interpolés) et la réponse brute
      - la latence précise de l'appel
      - l'usage tokens (prompt_tokens / completion_tokens) → coût
      - les erreurs API (rate limit / timeout / …)
      - depuis le LLM Gateway : profile/provider/attempt/fallback/structured
        validity — voir `graphs/agents/market_coach/llm_gateway/gateway.py`.
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
                _metric("llm_tokens").labels(model=model, kind="prompt").inc(
                    prompt_tokens
                )
            if completion_tokens:
                _metric("llm_tokens").labels(model=model, kind="completion").inc(
                    completion_tokens
                )
        if fallback_from and _metric("llm_fallback"):
            _metric("llm_fallback").labels(
                profile=profile or "unknown",
                provider=provider or "unknown",
                reason=fallback_reason or "unknown",
            ).inc()
        # (2026-09-13, Incrément G, spec §35) : `legacy_fallback`/
        # `prompt_family`/`estimated_cost_usd` voyagent dans `extra_metadata`
        # (sac libre déjà établi, pas un nouveau paramètre nommé — voir la
        # docstring du paramètre `extra_metadata` plus haut).
        _extra = extra_metadata or {}
        if _extra.get("legacy_fallback") and _metric("legacy_fallback"):
            _metric("legacy_fallback").labels(
                prompt_family=str(_extra.get("prompt_family") or "unknown")
            ).inc()
        _cost = _extra.get("estimated_cost_usd")
        if _cost is not None and _metric("llm_cost"):
            _metric("llm_cost").labels(
                prompt_family=str(_extra.get("prompt_family") or "unknown"),
                model=model,
            ).inc(float(_cost))
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
        gen_metadata: Dict[str, Any] = {
            "goal": meta.get("goal"),
            "latency_s": round(latency_s, 3),
        }
        # N'ajouter les champs Gateway QUE quand fournis — garde les
        # Generations des 2 sites d'appel historiques (get_llm.py) inchangées.
        for key, value in (
            ("profile", profile),
            ("provider", provider),
            ("attempt", attempt),
            ("fallback_from", fallback_from),
            ("fallback_reason", fallback_reason),
            ("structured_output_required", structured_output_required),
            ("structured_output_valid", structured_output_valid),
            ("agent_node", agent_node),
            ("request_id", request_id),
        ):
            if value is not None:
                gen_metadata[key] = value
        # `extra_metadata` fusionné EN DERNIER — un appelant qui fournirait
        # explicitement une clé déjà posée ci-dessus (ex: `agent_node`) la
        # remplace volontairement, jamais l'inverse (les valeurs nommées
        # ci-dessus restent la source de vérité par défaut).
        if extra_metadata:
            gen_metadata.update(extra_metadata)

        gen_kwargs: Dict[str, Any] = {
            "name": name,
            "model": model,
            "input": messages,
            "output": output,
            "metadata": gen_metadata,
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


def record_state_transition(
    *,
    previous_interaction: Optional[str],
    input_event: Optional[str],
    interpretation: Optional[str],
    unknown_reason: Optional[str],
    action: Optional[str],
    next_interaction: Optional[str],
    outcome: Optional[str],
    goal: Optional[str] = None,
) -> None:
    """Événement `CONVERSATION_STATE_TRANSITION` (refonte 2026-09-02, mandat
    §35) — un événement Langfuse PAR TOUR, rattaché à la Trace du trace_id
    courant, capturant exactement les 6 champs minimum demandés : interaction
    précédente/suivante, message interprété, action, issue. Aucune donnée
    utilisateur brute (texte du message, téléphone) — uniquement les
    DISCRIMINANTS (kinds/goals/strategies), déjà non sensibles par nature.
    Même discipline défensive que `record_generation` : ne casse jamais le
    tour si Langfuse est absent/indisponible.
    """
    if _langfuse_client is None:
        return
    trace_id = _current_trace_id.get()
    try:
        if trace_id:
            _ensure_langfuse_trace(trace_id)
        kwargs: Dict[str, Any] = {
            "name": "CONVERSATION_STATE_TRANSITION",
            "input": {
                "previous_interaction": previous_interaction,
                "event": input_event,
                "interpretation": interpretation,
                "unknown_reason": unknown_reason,
                "goal": goal,
            },
            "output": {
                "action": action,
                "next_interaction": next_interaction,
                "outcome": outcome,
            },
        }
        if trace_id:
            kwargs["trace_id"] = trace_id
        _langfuse_client.event(**kwargs)
    except Exception as exc:
        logger.debug("[telemetry] record_state_transition échoué: %s", exc)


def record_procurement_transaction_event(
    *,
    event_id: str,
    conversation_id: Optional[str],
    message_id: Optional[str] = None,
    draft_id: Optional[str],
    draft_version_before: Optional[int],
    draft_version_after: Optional[int],
    pending_kind: Optional[str],
    confirmation_target: Optional[Dict[str, Any]],
    interpreter_event: Optional[str],
    domain_action: Optional[str],
    state_before: Optional[str],
    state_after: Optional[str],
    execution_key: Optional[str] = None,
    external_request_id: Optional[str] = None,
    mcp_status: Optional[str] = None,
    execution_result: Optional[str] = None,
    payment_status: Optional[str] = None,
    outcome: str,
    error: Optional[str] = None,
) -> None:
    """Événement `PROCUREMENT_TRANSACTION_STEP` — un événement Langfuse PAR
    APPEL à `apply_domain_action`/`finalize_after_execution`, rattaché à la
    Trace du `trace_id` courant (2026-09-03, clôture de l'observabilité
    corrélée demandée : remplace/complète le `logger.info("PROCUREMENT_TRACE"...)`
    qui n'était PAS de l'instrumentation Langfuse — un `logger.info` reste
    utile pour le grep local, mais ce n'est PAS ce que ce mandat exige).

    Mêmes garanties que `record_state_transition` (précédent direct, mandat
    §35) : même client, même mécanisme de Trace, même discipline défensive
    (`_langfuse_client is None` → no-op, `except Exception` → jamais un tour
    cassé par une panne d'observabilité). Capture exactement les champs
    requis : identité de l'événement/conversation/message, identité et
    version du draft (avant/après), interaction en attente et cible de
    confirmation, action métier décidée, statut transactionnel avant/après,
    clé d'idempotence d'exécution et référence externe MCP le cas échéant,
    issue et erreur. AUCUNE donnée utilisateur brute (texte, téléphone) —
    uniquement des discriminants, comme `record_state_transition`.

    `payment_status` (2026-09-03, clôture escrow/IPN PREORDER) : optionnel,
    réutilise ce MÊME événement plutôt que d'en créer un nouveau pour la
    couche paiement — porte le statut Paydunya déjà adapté par
    `adapt_payment_outcome` (jamais relu ici, uniquement journalisé) quand
    l'appelant est `flows/buyer/preorder_payment.py`. `None` pour les
    appelants PROCUREMENT existants (rétrocompatible)."""
    if _langfuse_client is None:
        return
    trace_id = _current_trace_id.get()
    try:
        if trace_id:
            _ensure_langfuse_trace(trace_id)
        kwargs: Dict[str, Any] = {
            "name": "PROCUREMENT_TRANSACTION_STEP",
            "input": {
                "event_id": event_id,
                "conversation_id": conversation_id,
                "message_id": message_id,
                "draft_id": draft_id,
                "draft_version_before": draft_version_before,
                "pending_kind": pending_kind,
                "confirmation_target": confirmation_target,
                "interpreter_event": interpreter_event,
                "state_before": state_before,
            },
            "output": {
                "domain_action": domain_action,
                "draft_version_after": draft_version_after,
                "state_after": state_after,
                "execution_key": execution_key,
                "external_request_id": external_request_id,
                "mcp_status": mcp_status,
                "execution_result": execution_result,
                "payment_status": payment_status,
                "outcome": outcome,
                "error": error,
            },
        }
        if trace_id:
            kwargs["trace_id"] = trace_id
        _langfuse_client.event(**kwargs)
    except Exception as exc:
        logger.debug("[telemetry] record_procurement_transaction_event échoué: %s", exc)


def record_procurement_reconciliation_event(
    *,
    event_name: str,
    draft_id: str,
    draft_version: Optional[int],
    execution_key: Optional[str],
    state_before: Optional[str],
    state_after: Optional[str],
    reconciliation_reason: str,
    external_lookup: Optional[str],
    external_result: Optional[Any] = None,
    attempt: Optional[int] = None,
    outcome: str,
) -> None:
    """Événement Langfuse dédié à la RÉCONCILIATION (mandat recovery phase
    1/6, distinct de `record_procurement_transaction_event` — un tour
    utilisateur normal n'est PAS une réconciliation, et un worker de fond
    n'a pas de `trace_id` de tour à rattacher, donc pas de
    `conversation_id`/`message_id` significatifs ici). `event_name` porte
    le type d'événement demandé (`STALE_EXECUTING_DETECTED`,
    `RECONCILIATION_FOUND_EXTERNAL_EFFECT`, `RECONCILIATION_RETRY`,
    `RECONCILIATION_UNKNOWN`...) plutôt qu'un nom fixe — chaque appel
    produit un événement Langfuse nommément identifiable, pas une trace
    générique à décoder après coup. Même discipline défensive que les
    autres fonctions de ce module (no-op si Langfuse absent, jamais un
    crash du worker de réconciliation pour une panne d'observabilité)."""
    if _langfuse_client is None:
        return
    try:
        _langfuse_client.event(
            name=event_name,
            input={
                "draft_id": draft_id,
                "draft_version": draft_version,
                "execution_key": execution_key,
                "state_before": state_before,
                "reconciliation_reason": reconciliation_reason,
                "attempt": attempt,
            },
            output={
                "state_after": state_after,
                "external_lookup": external_lookup,
                "external_result": external_result,
                "outcome": outcome,
            },
        )
    except Exception as exc:
        logger.debug("[telemetry] record_procurement_reconciliation_event échoué: %s", exc)


def record_circuit_open(provider: str, model: str) -> None:
    """Métrique dédiée (§24) — appelée par le LLM Gateway à chaque ouverture
    RÉELLE du disjoncteur (pas à chaque échec pendant qu'il est déjà ouvert)."""
    try:
        if _metric("llm_circuit_open"):
            _metric("llm_circuit_open").labels(provider=provider, model=model).inc()
    except Exception:
        pass


def flush() -> None:
    """Force l'envoi des évènements Langfuse bufferisés (fin de tâche worker).

    (2026-09-17) : NE fait PAS de `force_flush()` sur le MeterProvider OTel,
    délibérément — testé en direct : `force_flush(timeout_millis=...)` ne
    borne PAS l'attente réelle quand le collector est injoignable (l'export
    OTLP retente en interne avec backoff exponentiel, 1s/2s/4s/8s/16s/...,
    INDÉPENDAMMENT du timeout demandé au MeterProvider — mesuré : toujours
    bloqué après 20s dans ce test). Appeler ça à CHAQUE fin de tâche worker
    aurait réintroduit exactement la même classe de bug que le blocage Redis
    synchrone déjà corrigé cette session (un appel d'observabilité qui peut
    geler le chemin critique). Les métriques partent via l'export PÉRIODIQUE
    en arrière-plan du `PeriodicExportingMetricReader` (15s, non-bloquant
    pour le traitement des tâches) — largement suffisant pour des compteurs
    agrégés (contrairement aux traces/événements Langfuse, où un flush par
    tâche a un vrai intérêt de corrélation, jamais appliqué ici)."""
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
            _metric("http_requests").labels(
                method=method, route=route, status=str(status_code)
            ).inc()
        if _metric("http_latency"):
            _metric("http_latency").labels(method=method, route=route).observe(
                duration_s
            )
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
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

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
    "record_state_transition",
    "record_procurement_transaction_event",
    "record_procurement_reconciliation_event",
    "record_circuit_open",
    "observe_http",
    "count_webhook",
    "prometheus_asgi_response",
    "flush",
]
