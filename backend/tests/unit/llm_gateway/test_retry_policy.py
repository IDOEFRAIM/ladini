"""Incrément G (2026-09-13, "Production LLM Hardening") — classification
FINE (`LLMFailureKind`, spec §4) + politique de retry same-candidate
(spec §5/§6/§38/§39/§40).

Couvre : `classify_llm_failure_kind` (status codes réels, SDK, réseau),
`should_retry_same_candidate` (matrice spec §5), `retry_after_seconds`
(honoré si le SDK l'expose), `backoff_seconds` (jitter borné, Retry-After
capé), et le comportement BOUT EN BOUT du Gateway (429→retry→succès,
500→retry→fallback, 401→jamais de retry, 413→jamais de retry)."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

from ladini.graphs.agents.market_coach.llm_gateway.circuit_breaker import CircuitBreaker
from ladini.graphs.agents.market_coach.llm_gateway.error_classification import (
    backoff_seconds,
    classify_llm_failure_kind,
    error_class_for_kind,
    retry_after_seconds,
    should_retry_same_candidate,
)
from ladini.graphs.agents.market_coach.llm_gateway.gateway import LLMGateway
from ladini.graphs.agents.market_coach.llm_gateway.health_registry import HealthRegistry
from ladini.graphs.agents.market_coach.llm_gateway.types import (
    ErrorClass,
    LLMFailureKind,
    LLMProfile,
)
from tests.conftest import run
from tests.unit.llm_gateway.conftest import make_fake_redis

# =====================================================================
# A. CLASSIFICATION FINE (spec §4)
# =====================================================================


class TestClassifyLLMFailureKind:
    def test_status_429_is_rate_limit(self):
        exc = Exception("rate limited")
        exc.status_code = 429
        assert classify_llm_failure_kind(exc) == LLMFailureKind.RATE_LIMIT

    def test_status_401_is_auth(self):
        exc = Exception("unauthorized")
        exc.status_code = 401
        assert classify_llm_failure_kind(exc) == LLMFailureKind.AUTH

    def test_status_403_is_auth(self):
        exc = Exception("forbidden")
        exc.status_code = 403
        assert classify_llm_failure_kind(exc) == LLMFailureKind.AUTH

    def test_status_413_is_context_too_large(self):
        exc = Exception("payload too large")
        exc.status_code = 413
        assert classify_llm_failure_kind(exc) == LLMFailureKind.CONTEXT_TOO_LARGE

    def test_status_400_without_config_or_context_hints_is_bad_request(self):
        exc = Exception("missing required field 'foo'")
        exc.status_code = 400
        assert classify_llm_failure_kind(exc) == LLMFailureKind.BAD_REQUEST

    def test_status_400_with_context_length_message_is_context_too_large(self):
        exc = Exception("Error: maximum context length exceeded")
        exc.status_code = 400
        assert classify_llm_failure_kind(exc) == LLMFailureKind.CONTEXT_TOO_LARGE

    def test_status_500_is_provider_5xx(self):
        exc = Exception("internal server error")
        exc.status_code = 500
        assert classify_llm_failure_kind(exc) == LLMFailureKind.PROVIDER_5XX

    def test_status_503_is_provider_5xx(self):
        exc = Exception("service unavailable")
        exc.status_code = 503
        assert classify_llm_failure_kind(exc) == LLMFailureKind.PROVIDER_5XX

    def test_timeout_error_is_timeout(self):
        assert classify_llm_failure_kind(TimeoutError()) == LLMFailureKind.TIMEOUT

    def test_connection_error_is_connection(self):
        assert classify_llm_failure_kind(ConnectionError("reset")) == LLMFailureKind.CONNECTION

    def test_json_decode_error_is_invalid_response(self):
        import json

        try:
            json.loads("not json")
        except json.JSONDecodeError as exc:
            assert classify_llm_failure_kind(exc) == LLMFailureKind.INVALID_RESPONSE
        else:
            raise AssertionError("json.loads devait lever")

    def test_value_error_is_application(self):
        assert classify_llm_failure_kind(ValueError("bug")) == LLMFailureKind.APPLICATION

    def test_unrecognized_exception_is_unknown_not_transient(self):
        # Prudence symétrique à `classify_llm_error` (§15) : une erreur
        # inconnue ne doit jamais être TRAITÉE comme un type retryable —
        # UNKNOWN mappe vers ErrorClass.APPLICATION (n'ouvre pas le circuit).
        kind = classify_llm_failure_kind(RuntimeError("inattendu"))
        assert kind == LLMFailureKind.UNKNOWN
        assert error_class_for_kind(kind) == ErrorClass.APPLICATION


class TestErrorClassForKindMatchesCircuitBreakerSemantics:
    def test_transient_kinds_map_to_transient_error_class(self):
        for kind in (
            LLMFailureKind.RATE_LIMIT,
            LLMFailureKind.TIMEOUT,
            LLMFailureKind.PROVIDER_5XX,
            LLMFailureKind.CONNECTION,
        ):
            assert error_class_for_kind(kind) == ErrorClass.TRANSIENT

    def test_auth_and_config_map_to_config_error_class(self):
        assert error_class_for_kind(LLMFailureKind.AUTH) == ErrorClass.CONFIG
        assert error_class_for_kind(LLMFailureKind.CONFIG) == ErrorClass.CONFIG

    def test_bad_request_and_context_too_large_never_touch_circuit(self):
        # Spec §5 : "pas de retry, pas de fallback aveugle" — ces kinds ne
        # doivent JAMAIS compter comme une panne d'infrastructure.
        assert error_class_for_kind(LLMFailureKind.BAD_REQUEST) == ErrorClass.APPLICATION
        assert error_class_for_kind(LLMFailureKind.CONTEXT_TOO_LARGE) == ErrorClass.APPLICATION


# =====================================================================
# B. MATRICE DE RETRY same-candidate (spec §5)
# =====================================================================


class TestShouldRetrySameCandidate:
    def test_retryable_kinds(self):
        for kind in (
            LLMFailureKind.RATE_LIMIT,
            LLMFailureKind.TIMEOUT,
            LLMFailureKind.PROVIDER_5XX,
            LLMFailureKind.CONNECTION,
        ):
            assert should_retry_same_candidate(kind) is True

    def test_never_retried_kinds(self):
        # "pas de retry, pas de fallback automatique aveugle" (spec §5) pour
        # BAD_REQUEST/AUTH/CONFIG/CONTEXT_TOO_LARGE — un problème de prompt/
        # contrat/credentials ne se résout jamais en réessayant identique.
        for kind in (
            LLMFailureKind.BAD_REQUEST,
            LLMFailureKind.AUTH,
            LLMFailureKind.CONFIG,
            LLMFailureKind.CONTEXT_TOO_LARGE,
            LLMFailureKind.INVALID_RESPONSE,
            LLMFailureKind.APPLICATION,
            LLMFailureKind.UNKNOWN,
        ):
            assert should_retry_same_candidate(kind) is False


# =====================================================================
# C. RETRY-AFTER + BACKOFF (spec §5/§38)
# =====================================================================


class TestRetryAfterAndBackoff:
    def test_retry_after_extracted_from_response_headers(self):
        exc = Exception("rate limited")
        exc.response = SimpleNamespace(headers={"retry-after": "7"})
        assert retry_after_seconds(exc) == 7.0

    def test_retry_after_extracted_from_bare_headers_attribute(self):
        exc = Exception("rate limited")
        exc.headers = {"Retry-After": "3"}
        assert retry_after_seconds(exc) == 3.0

    def test_no_headers_returns_none(self):
        assert retry_after_seconds(Exception("plain")) is None

    def test_malformed_retry_after_returns_none(self):
        exc = Exception("rate limited")
        exc.headers = {"retry-after": "not-a-number"}
        assert retry_after_seconds(exc) is None

    def test_rate_limit_honors_retry_after_capped_at_10s(self):
        assert backoff_seconds(LLMFailureKind.RATE_LIMIT, 1, retry_after=3.0) == 3.0
        assert backoff_seconds(LLMFailureKind.RATE_LIMIT, 1, retry_after=999.0) == 10.0

    def test_timeout_backoff_is_bounded_and_never_negative(self):
        for _ in range(20):
            delay = backoff_seconds(LLMFailureKind.TIMEOUT, 1)
            assert 0.0 <= delay <= 2.0

    def test_backoff_without_retry_after_is_jittered_not_fixed(self):
        # Preuve anti-retry-storm (spec §38) : les délais ne sont pas tous
        # identiques (jitter réel, pas une constante déguisée).
        samples = {backoff_seconds(LLMFailureKind.PROVIDER_5XX, 1) for _ in range(30)}
        assert len(samples) > 1


# =====================================================================
# D. COMPORTEMENT BOUT EN BOUT DU GATEWAY (spec §38/§39/§40)
# =====================================================================


class _FakeCompletion:
    def __init__(self, content="{}", model=None):
        class _Msg:
            def __init__(self, c):
                self.content = c

        class _Choice:
            def __init__(self, c):
                self.message = _Msg(c)

        self.choices = [_Choice(content)]
        self.model = model
        self.usage = None


class _ScriptedClient:
    def __init__(self, behaviors):
        self._behaviors = list(behaviors)
        self.calls: List[dict] = []
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        idx = min(len(self.calls) - 1, len(self._behaviors) - 1)
        behavior = self._behaviors[idx]
        if isinstance(behavior, Exception):
            raise behavior
        return behavior(**kwargs) if callable(behavior) else behavior


def _make_gateway(*, provider_clients: Dict[str, Any], settings=None, store=None):
    from ladini.graphs.agents.market_coach.llm_gateway.alerting import (
        IncidentDeduplicator,
    )
    from ladini.graphs.agents.market_coach.llm_gateway.types import (
        ModelCandidate as _MC,
    )

    settings = settings or SimpleNamespace(
        LLM_FAST_BUDGET_SECONDS=8.0,
        LLM_REASONING_BUDGET_SECONDS=8.0,
        LLM_CIRCUIT_FAILURE_THRESHOLD=3,
        LLM_CIRCUIT_COOLDOWN_SECONDS=30.0,
        LLM_HALF_OPEN_PROBES=1,
        LLM_PROBE_LOCK_SECONDS=10.0,
        ADMIN_ALERT_WEBHOOK_URL="",
        SENTRY_ENVIRONMENT="test",
    )
    store = store if store is not None else {}

    class _StubRegistry:
        def __init__(self):
            self._candidates = [
                _MC(provider="provider_a", model="model-a", profile=LLMProfile.FAST, priority=0, timeout_seconds=2.0),
                _MC(provider="provider_b", model="model-b", profile=LLMProfile.FAST, priority=1, timeout_seconds=2.0),
            ]

        def candidates_for(self, profile):
            return list(self._candidates)

        def primary_model_name(self, profile):
            return self._candidates[0].model

    health = HealthRegistry(redis_client=make_fake_redis(store))
    circuit = CircuitBreaker(
        health,
        failure_threshold=settings.LLM_CIRCUIT_FAILURE_THRESHOLD,
        cooldown_seconds=settings.LLM_CIRCUIT_COOLDOWN_SECONDS,
        half_open_probes=settings.LLM_HALF_OPEN_PROBES,
        probe_lock_seconds=settings.LLM_PROBE_LOCK_SECONDS,
    )
    return LLMGateway(
        registry=_StubRegistry(),
        health_registry=health,
        circuit_breaker=circuit,
        settings=settings,
        incident_dedup=IncidentDeduplicator(redis_client=make_fake_redis(store)),
        client_factory=lambda provider: provider_clients[provider],
    )


class TestRateLimit429ThenSucceeds:
    def test_a_429_is_retried_once_on_the_same_candidate_then_succeeds(self):
        exc_429 = Exception("rate limited")
        exc_429.status_code = 429
        client_a = _ScriptedClient([exc_429, _FakeCompletion(model="model-a")])
        client_b = _ScriptedClient([_FakeCompletion(model="model-b")])
        gw = _make_gateway(provider_clients={"provider_a": client_a, "provider_b": client_b})

        completion = run(gw.complete(profile=LLMProfile.FAST, messages=[{"role": "user", "content": "hi"}]))

        assert completion.model == "model-a"  # resté sur le MÊME candidat
        assert len(client_a.calls) == 2  # 1 essai + 1 retry
        assert len(client_b.calls) == 0  # jamais basculé sur le fallback


class TestProvider500ThenFallback:
    def test_a_persistent_500_retries_once_then_falls_back(self):
        exc_500 = Exception("internal error")
        exc_500.status_code = 500
        client_a = _ScriptedClient([exc_500, exc_500])  # échoue aussi au retry
        client_b = _ScriptedClient([_FakeCompletion(model="model-b")])
        gw = _make_gateway(provider_clients={"provider_a": client_a, "provider_b": client_b})

        completion = run(gw.complete(profile=LLMProfile.FAST, messages=[{"role": "user", "content": "hi"}]))

        assert completion.model == "model-b"
        assert len(client_a.calls) == 2  # 1 essai + 1 retry, jamais plus
        assert len(client_b.calls) == 1


class TestAuthErrorNeverRetriesOrLoops:
    def test_a_401_is_never_retried_and_immediately_falls_back(self):
        exc_401 = Exception("unauthorized")
        exc_401.status_code = 401
        client_a = _ScriptedClient([exc_401])
        client_b = _ScriptedClient([_FakeCompletion(model="model-b")])
        gw = _make_gateway(provider_clients={"provider_a": client_a, "provider_b": client_b})

        completion = run(gw.complete(profile=LLMProfile.FAST, messages=[{"role": "user", "content": "hi"}]))

        assert completion.model == "model-b"
        assert len(client_a.calls) == 1  # AUCUN retry — spec §5


class TestContextTooLargeNeverRetries:
    def test_a_413_is_never_retried(self):
        exc_413 = Exception("payload too large")
        exc_413.status_code = 413
        client_a = _ScriptedClient([exc_413])
        client_b = _ScriptedClient([_FakeCompletion(model="model-b")])
        gw = _make_gateway(provider_clients={"provider_a": client_a, "provider_b": client_b})

        completion = run(gw.complete(profile=LLMProfile.FAST, messages=[{"role": "user", "content": "hi"}]))

        assert completion.model == "model-b"
        assert len(client_a.calls) == 1


class TestRetryDoesNotDistortCircuitBreakerFailureCounting:
    def test_two_network_attempts_for_one_candidate_count_as_a_single_failure(self):
        # Preuve directe que le disjoncteur n'est pas trompé par le retry
        # interne : 3 TOURS en échec (chacun avec 1 retry raté) doivent
        # ouvrir le circuit après EXACTEMENT 3 échecs consécutifs, pas 6.
        exc_500 = Exception("internal error")
        exc_500.status_code = 500
        client_a = _ScriptedClient([exc_500] * 10)
        client_b = _ScriptedClient([_FakeCompletion(model="model-b")] * 10)
        store: Dict[str, Any] = {}
        settings = SimpleNamespace(
            LLM_FAST_BUDGET_SECONDS=8.0,
            LLM_REASONING_BUDGET_SECONDS=8.0,
            LLM_CIRCUIT_FAILURE_THRESHOLD=3,
            LLM_CIRCUIT_COOLDOWN_SECONDS=3600.0,
            LLM_HALF_OPEN_PROBES=1,
            LLM_PROBE_LOCK_SECONDS=10.0,
            ADMIN_ALERT_WEBHOOK_URL="",
            SENTRY_ENVIRONMENT="test",
        )
        gw = _make_gateway(
            provider_clients={"provider_a": client_a, "provider_b": client_b},
            settings=settings,
            store=store,
        )

        for _ in range(3):
            run(gw.complete(profile=LLMProfile.FAST, messages=[{"role": "user", "content": "hi"}]))

        health = gw.health_registry()
        record = health.get("provider_a:model-a")
        assert record.state.value == "OPEN"
        assert record.consecutive_failures == 3  # jamais 6
