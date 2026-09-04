"""LLM Gateway — point d'entrée public unique pour tout appel LLM métier.

`await get_llm_gateway().complete(profile=..., messages=..., response_format=...)`

Orchestre : Model Registry (quels candidats pour ce profil) → Circuit Breaker
(lesquels sont utilisables MAINTENANT, sans appel réseau pour ceux qui ne le
sont pas) → budget global (arrête la chaîne de repli avant de transformer une
requête courte en requête longue, §17) → exécution via les adapters EXISTANTS
de `core/get_llm.py` (aucune réécriture) → Health Registry (rapporte le
résultat) → télémétrie (Langfuse/Prometheus via `core/telemetry.py` étendu) →
alerting (incident ouverture/récupération, dédupliqué).

Ne fait JAMAIS plus d'un appel réseau "en vol" par candidat à la fois côté
CE process (pas de retry en boucle) — la boucle sur `candidates_for(profile)`
EST la stratégie de repli, pas un retry déguisé.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional

from agriconnect.graphs.agents.market_coach.llm_gateway.alerting import (
    IncidentDeduplicator,
    LLMIncidentAlert,
    NotificationService,
    build_notifier,
)
from agriconnect.graphs.agents.market_coach.llm_gateway.circuit_breaker import (
    CircuitBreaker,
    Decision,
)
from agriconnect.graphs.agents.market_coach.llm_gateway.error_classification import (
    classify_llm_error,
)
from agriconnect.graphs.agents.market_coach.llm_gateway.health_registry import (
    HealthRegistry,
)
from agriconnect.graphs.agents.market_coach.llm_gateway.registry import ModelRegistry
from agriconnect.graphs.agents.market_coach.llm_gateway.types import (
    ErrorClass,
    LLMProfile,
    ModelCandidate,
)

logger = logging.getLogger("agriconnect.llm_gateway")

# Marge de sécurité : un candidat n'est tenté que s'il reste au moins ce
# temps sur le budget global APRÈS lui avoir réservé son propre timeout —
# évite de lancer une tentative qui n'a de toute façon pas le temps de finir
# proprement avant `wait_for` du budget.
_MIN_VIABLE_SECONDS = 1.5
_SAFETY_MARGIN_SECONDS = 0.5


class LLMGatewayExhausted(RuntimeError):
    """Tous les candidats éligibles pour ce profil ont échoué, étaient
    indisponibles (circuit OPEN) ou le budget global était épuisé. Les nodes
    métier attrapent cette exception exactement comme ils attrapaient avant
    un timeout/erreur SDK direct — même repli UNKNOWN/dégradé, cause unique."""


class LLMGateway:
    def __init__(
        self,
        *,
        registry: Optional[ModelRegistry] = None,
        health_registry: Optional[HealthRegistry] = None,
        circuit_breaker: Optional[CircuitBreaker] = None,
        notifier: Optional[NotificationService] = None,
        incident_dedup: Optional[IncidentDeduplicator] = None,
        settings: Any = None,
        client_factory: Optional[Any] = None,
    ):
        if settings is None:
            from agriconnect.core.settings import settings as _settings

            settings = _settings
        self._settings = settings

        self._registry = registry or ModelRegistry(settings)
        self._health = health_registry or HealthRegistry()
        self._circuit = circuit_breaker or CircuitBreaker(
            self._health,
            failure_threshold=int(getattr(settings, "LLM_CIRCUIT_FAILURE_THRESHOLD", 3)),
            cooldown_seconds=float(getattr(settings, "LLM_CIRCUIT_COOLDOWN_SECONDS", 30.0)),
            half_open_probes=int(getattr(settings, "LLM_HALF_OPEN_PROBES", 1)),
            probe_lock_seconds=float(getattr(settings, "LLM_PROBE_LOCK_SECONDS", 10.0)),
        )
        self._notifier = notifier or build_notifier(settings)
        self._incident_dedup = incident_dedup or IncidentDeduplicator()
        # Injectable pour les tests (évite de construire de vrais clients SDK) —
        # signature: (provider: str) -> objet exposant .chat.completions.create.
        self._client_factory = client_factory or _default_client_for_provider
        self._client_cache: Dict[str, Any] = {}
        self._environment = str(getattr(settings, "SENTRY_ENVIRONMENT", "development"))

    # ── API publique ─────────────────────────────────────────────────
    async def complete(
        self,
        *,
        profile: LLMProfile,
        messages: Any,
        response_format: Optional[dict] = None,
        request_id: Optional[str] = None,
        agent_node: Optional[str] = None,
        **kwargs: Any,
    ) -> Any:
        structured_required = bool(response_format)
        candidates = [
            c
            for c in self._registry.candidates_for(profile)
            if c.supports(structured_output_required=structured_required)
        ]
        if not candidates:
            raise LLMGatewayExhausted(
                f"Aucun candidat configuré pour profile={profile.value} "
                f"(structured_output_required={structured_required})"
            )

        primary_key = candidates[0].key
        budget_seconds = self._budget_for(profile)
        deadline = time.monotonic() + budget_seconds
        attempt = 0
        last_exc: Optional[BaseException] = None

        for candidate in candidates:
            remaining = deadline - time.monotonic()
            if remaining <= _MIN_VIABLE_SECONDS:
                logger.warning(
                    "LLM_BUDGET_EXHAUSTED | profile=%s budget=%.1fs — arrêt avant "
                    "candidat=%s",
                    profile.value,
                    budget_seconds,
                    candidate.key,
                )
                break

            circuit_decision = self._circuit.decide(candidate)
            if circuit_decision.decision == Decision.SKIP:
                logger.info(
                    "LLM_CALL | profile=%s candidate=%s attempt=skip reason=%s",
                    profile.value,
                    candidate.key,
                    circuit_decision.reason,
                )
                continue

            per_call_timeout = min(
                candidate.timeout_seconds, remaining - _SAFETY_MARGIN_SECONDS
            )
            if per_call_timeout <= 0:
                continue

            attempt += 1
            is_probe = circuit_decision.decision == Decision.PROBE
            t0 = time.perf_counter()
            try:
                completion = await self._call_candidate(
                    candidate,
                    messages=messages,
                    response_format=response_format,
                    timeout=per_call_timeout,
                    **kwargs,
                )
            except Exception as exc:
                latency_ms = (time.perf_counter() - t0) * 1000.0
                self._on_failure(
                    candidate=candidate,
                    profile=profile,
                    exc=exc,
                    latency_ms=latency_ms,
                    attempt=attempt,
                    is_probe=is_probe,
                    request_id=request_id,
                    agent_node=agent_node,
                    messages=messages,
                    structured_required=structured_required,
                )
                last_exc = exc
                continue

            latency_ms = (time.perf_counter() - t0) * 1000.0
            self._on_success(
                candidate=candidate,
                profile=profile,
                completion=completion,
                latency_ms=latency_ms,
                attempt=attempt,
                is_probe=is_probe,
                is_fallback=candidate.key != primary_key,
                request_id=request_id,
                agent_node=agent_node,
                messages=messages,
                structured_required=structured_required,
            )
            return completion

        raise LLMGatewayExhausted(
            f"Tous les candidats du profil {profile.value} ont échoué ou "
            f"étaient indisponibles (budget={budget_seconds}s)."
        ) from last_exc

    def primary_model_name(self, profile: LLMProfile) -> Optional[str]:
        return self._registry.primary_model_name(profile)

    def registry(self) -> ModelRegistry:
        return self._registry

    def health_registry(self) -> HealthRegistry:
        return self._health

    # ── Exécution d'un candidat ──────────────────────────────────────
    async def _call_candidate(
        self,
        candidate: ModelCandidate,
        *,
        messages: Any,
        response_format: Optional[dict],
        timeout: float,
        **kwargs: Any,
    ) -> Any:
        client = self._client_for(candidate.provider)
        call_kwargs: Dict[str, Any] = {"model": candidate.model, "messages": messages}
        if response_format is not None:
            call_kwargs["response_format"] = response_format
        call_kwargs.update(kwargs)

        return await asyncio.wait_for(
            asyncio.to_thread(lambda: client.chat.completions.create(**call_kwargs)),
            timeout=timeout,
        )

    def _client_for(self, provider: str) -> Any:
        cached = self._client_cache.get(provider)
        if cached is not None:
            return cached
        client = self._client_factory(provider)
        self._client_cache[provider] = client
        return client

    def _budget_for(self, profile: LLMProfile) -> float:
        attr = (
            "LLM_FAST_BUDGET_SECONDS"
            if profile == LLMProfile.FAST
            else "LLM_REASONING_BUDGET_SECONDS"
        )
        return float(getattr(self._settings, attr, 15.0))

    # ── Callbacks succès/échec (santé + télémétrie + alerting) ───────
    def _on_success(
        self,
        *,
        candidate: ModelCandidate,
        profile: LLMProfile,
        completion: Any,
        latency_ms: float,
        attempt: int,
        is_probe: bool,
        is_fallback: bool,
        request_id: Optional[str],
        agent_node: Optional[str],
        messages: Any,
        structured_required: bool,
    ) -> None:
        record = self._circuit.report_success(candidate, latency_ms)

        if is_probe and record is not None and record.state.value == "CLOSED":
            logger.info(
                "LLM_CIRCUIT_CLOSE | candidate=%s — probe réussi, trafic restauré",
                candidate.key,
            )
            self._safe_alert(
                candidate,
                lambda: self._incident_dedup.should_notify_recovery(candidate.key),
                LLMIncidentAlert(
                    incident_type="PRIMARY_DEGRADED",
                    severity="RECOVERY",
                    environment=self._environment,
                    profile=profile.value,
                    provider=candidate.provider,
                    model=candidate.model,
                    reason="probe HALF_OPEN réussi",
                ),
            )

        if is_fallback:
            logger.info(
                "LLM_FALLBACK | profile=%s to=%s attempt=%d reason=primary_unavailable",
                profile.value,
                candidate.key,
                attempt,
            )

        self._emit_telemetry(
            candidate=candidate,
            profile=profile,
            attempt=attempt,
            latency_s=latency_ms / 1000.0,
            status="success",
            completion=completion,
            error=None,
            request_id=request_id,
            agent_node=agent_node,
            messages=messages,
            structured_required=structured_required,
            fallback_from=None,
            fallback_reason="primary_unavailable" if is_fallback else None,
        )

    def _on_failure(
        self,
        *,
        candidate: ModelCandidate,
        profile: LLMProfile,
        exc: BaseException,
        latency_ms: float,
        attempt: int,
        is_probe: bool,
        request_id: Optional[str],
        agent_node: Optional[str],
        messages: Any,
        structured_required: bool,
    ) -> None:
        error_class = classify_llm_error(exc)
        is_timeout = isinstance(exc, (asyncio.TimeoutError, TimeoutError))

        if error_class == ErrorClass.CONFIG:
            self._health.mark_config_error(candidate, str(exc))
            self._safe_alert(
                candidate,
                lambda: self._incident_dedup.should_notify_open(candidate.key),
                LLMIncidentAlert(
                    incident_type="MODEL_CONFIG_ERROR",
                    severity="CRITICAL",
                    environment=self._environment,
                    profile=profile.value,
                    provider=candidate.provider,
                    model=candidate.model,
                    reason=str(exc)[:300],
                ),
            )
        elif error_class == ErrorClass.TRANSIENT:
            record = self._circuit.report_failure(candidate, is_timeout=is_timeout)
            just_opened = (
                record is not None
                and record.state.value == "OPEN"
                and (is_probe or record.consecutive_failures >= 1)
            )
            # N'alerter que sur une VRAIE transition vers OPEN, pas à chaque
            # échec supplémentaire pendant que c'est déjà OPEN (§28).
            if just_opened:
                try:
                    from agriconnect.core.telemetry import record_circuit_open

                    record_circuit_open(candidate.provider, candidate.model)
                except Exception:
                    pass
            if just_opened:
                self._safe_alert(
                    candidate,
                    lambda: self._incident_dedup.should_notify_open(candidate.key),
                    LLMIncidentAlert(
                        incident_type="PRIMARY_DEGRADED",
                        severity="CRITICAL" if attempt == 1 else "WARNING",
                        environment=self._environment,
                        profile=profile.value,
                        provider=candidate.provider,
                        model=candidate.model,
                        reason="TIMEOUT" if is_timeout else type(exc).__name__,
                        p95_latency_ms=self._safe_p95(candidate.key),
                    ),
                )
            logger.warning(
                "LLM_CALL | profile=%s candidate=%s attempt=%d status=%s latency_ms=%.0f error=%s",
                profile.value,
                candidate.key,
                attempt,
                "timeout" if is_timeout else "error",
                latency_ms,
                exc,
            )
        else:
            # APPLICATION : ne touche PAS à la santé du candidat (§15) —
            # seulement visible en log/télémétrie pour investiguer le bug.
            logger.error(
                "LLM_CALL | profile=%s candidate=%s attempt=%d status=application_error error=%s",
                profile.value,
                candidate.key,
                attempt,
                exc,
                exc_info=True,
            )

        self._emit_telemetry(
            candidate=candidate,
            profile=profile,
            attempt=attempt,
            latency_s=latency_ms / 1000.0,
            status="error",
            completion=None,
            error=f"{type(exc).__name__}: {exc}",
            request_id=request_id,
            agent_node=agent_node,
            messages=messages,
            structured_required=structured_required,
            fallback_from=None,
            fallback_reason=None,
        )

    def _safe_alert(self, candidate: ModelCandidate, should_notify, alert: LLMIncidentAlert) -> None:
        """Enveloppe TOUT l'aller-retour de dédup+notification — jamais
        bloquant pour le tour utilisateur (même philosophie que
        `core/telemetry.py` : l'observabilité ne doit JAMAIS casser
        l'application). Une panne Redis/webhook au moment même où on essaie
        d'alerter une panne LLM ne doit pas EN PLUS faire échouer la réponse
        qui vient de réussir/échouer proprement."""
        try:
            if should_notify():
                self._notifier.send_incident(alert)
        except Exception as exc:
            logger.debug(
                "[llm_gateway] alerte non envoyée pour %s (%s) — non bloquant.",
                candidate.key,
                exc,
            )

    def _safe_p95(self, candidate_key: str) -> Optional[float]:
        try:
            return self._health.percentiles(candidate_key).get("p95")
        except Exception:
            return None

    def _emit_telemetry(
        self,
        *,
        candidate: ModelCandidate,
        profile: LLMProfile,
        attempt: int,
        latency_s: float,
        status: str,
        completion: Any,
        error: Optional[str],
        request_id: Optional[str],
        agent_node: Optional[str],
        messages: Any,
        structured_required: bool,
        fallback_from: Optional[str],
        fallback_reason: Optional[str],
    ) -> None:
        try:
            from agriconnect.core.telemetry import record_generation

            output = None
            structured_valid: Optional[bool] = None
            usage = None
            if completion is not None:
                try:
                    output = completion.choices[0].message.content
                except Exception:
                    output = None
                usage_obj = getattr(completion, "usage", None)
                if usage_obj is not None:
                    usage = {
                        "prompt_tokens": getattr(usage_obj, "prompt_tokens", None),
                        "completion_tokens": getattr(usage_obj, "completion_tokens", None),
                    }
                if structured_required and output is not None:
                    structured_valid = _looks_like_json(output)

            record_generation(
                model=candidate.model,
                messages=messages,
                output=output,
                latency_s=latency_s,
                usage=usage,
                error=error,
                name="llm_gateway_completion",
                profile=profile.value,
                provider=candidate.provider,
                attempt=attempt,
                fallback_from=fallback_from,
                fallback_reason=fallback_reason,
                structured_output_required=structured_required,
                structured_output_valid=structured_valid,
                request_id=request_id,
                agent_node=agent_node,
            )
        except Exception:
            # Règle d'or telemetry.py : jamais bloquant pour le tour utilisateur.
            pass


def _looks_like_json(text: str) -> bool:
    try:
        import json

        json.loads(text)
        return True
    except Exception:
        return False


def _default_client_for_provider(provider: str) -> Any:
    """Mappe un nom de provider vers un client réel — réutilise TEL QUEL les
    adapters/factories existants de `core/get_llm.py` (aucune réécriture)."""
    from agriconnect.core.get_llm import (
        _BedrockAdapter,
        _GroqAdapter,
        get_bedrock_client,
        get_groq_sdk,
        get_openai_compatible_sdk,
    )

    if provider == "groq":
        return _GroqAdapter(get_groq_sdk())
    if provider == "bedrock_gateway":
        return _GroqAdapter(get_openai_compatible_sdk())
    if provider == "bedrock_native":
        return _BedrockAdapter(get_bedrock_client())
    raise LLMGatewayExhausted(f"Provider inconnu: {provider!r}")


class LegacyOverrideGateway:
    """Compat tests/appelants legacy — `MarketRuntime(llm_client=...)` (voir
    `utils.py::MarketRuntime._llm_override`). Bypasse ENTIÈREMENT le
    registry/health/circuit (donc AUCUN accès réseau/Redis — critique pour
    la philosophie "AUCUN réseau" de la suite de tests, `tests/conftest.py`) :
    appelle le client fourni directement, exactement comme avant
    l'introduction du Gateway. Un seul client fixe, sans repli ni santé
    partagée — jamais utilisé en production (voir `get_llm_gateway()`)."""

    def __init__(self, client: Any, model_resolver: Any):
        self._client = client
        self._model_resolver = model_resolver  # callable() -> Optional[str]

    async def complete(
        self,
        *,
        profile: LLMProfile,
        messages: Any,
        response_format: Optional[dict] = None,
        request_id: Optional[str] = None,
        agent_node: Optional[str] = None,
        **kwargs: Any,
    ) -> Any:
        model = self._model_resolver() or "llama-3.3-70b-versatile"
        call_kwargs: Dict[str, Any] = {"model": model, "messages": messages}
        if response_format is not None:
            call_kwargs["response_format"] = response_format
        call_kwargs.update(kwargs)
        return await asyncio.to_thread(
            lambda: self._client.chat.completions.create(**call_kwargs)
        )

    def primary_model_name(self, profile: LLMProfile) -> Optional[str]:
        return self._model_resolver()


def resolve_gateway(mc_runtime: Any) -> Any:
    """Résout `mc_runtime.llm_gateway` avec repli pour les runtimes minimaux
    qui n'exposent que `.llm`/`.model_answer` (tests ad-hoc — ex:
    `type("RT", (), {"llm": ..., "model_answer": ...})()`, un pattern présent
    dans plusieurs fichiers de tests légers). Même esprit défensif que
    l'ancien `getattr(mc_runtime, "model_answer", "llama-3.3-70b-versatile")`
    que ce Gateway remplace — jamais un `AttributeError` juste parce qu'un
    test n'a pas encore été mis à jour vers la forme complète `MarketRuntime`."""
    gateway = getattr(mc_runtime, "llm_gateway", None)
    if gateway is not None:
        return gateway
    llm = getattr(mc_runtime, "llm", None)
    return LegacyOverrideGateway(llm, lambda: getattr(mc_runtime, "model_answer", None))


def resolve_profile(mc_runtime: Any) -> LLMProfile:
    profile = getattr(mc_runtime, "profile_answer", None)
    if isinstance(profile, LLMProfile):
        return profile
    return LLMProfile.REASONING


# ── Singleton process-wide — §50 "une seule Gateway" ────────────────
_GATEWAY_SINGLETON: Optional[LLMGateway] = None


def get_llm_gateway(force_refresh: bool = False) -> LLMGateway:
    global _GATEWAY_SINGLETON
    if not force_refresh and _GATEWAY_SINGLETON is not None:
        return _GATEWAY_SINGLETON
    _GATEWAY_SINGLETON = LLMGateway()
    return _GATEWAY_SINGLETON
