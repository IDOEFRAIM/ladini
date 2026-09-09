"""`llm_gateway/circuit_breaker.py::CircuitBreaker` — §13/§14 du brief.

Verrouille la décision ATTEMPT/PROBE/SKIP pour un candidat, et le cycle
CLOSED → OPEN → (cooldown) → HALF_OPEN (probe unique) → CLOSED/OPEN — voir
aussi `tests/integration/test_llm_gateway_outage_scenario.py` pour le
scénario complet bout-en-bout avec la Gateway."""

from __future__ import annotations

import json
import time

from tests.unit.llm_gateway.conftest import make_fake_redis

from agriconnect.graphs.agents.market_coach.llm_gateway.circuit_breaker import (
    CircuitBreaker,
    Decision,
)
from agriconnect.graphs.agents.market_coach.llm_gateway.health_registry import (
    HealthRegistry,
    _key,
)
from agriconnect.graphs.agents.market_coach.llm_gateway.types import (
    CircuitState,
    HealthRecord,
    LLMProfile,
    ModelCandidate,
)


def _candidate() -> ModelCandidate:
    return ModelCandidate(
        provider="bedrock_gateway",
        model="deepseek.v3.2",
        profile=LLMProfile.REASONING,
        priority=0,
        timeout_seconds=12.0,
    )


def _breaker(store=None, **overrides) -> CircuitBreaker:
    health = HealthRegistry(redis_client=make_fake_redis(store))
    kwargs = dict(
        failure_threshold=3,
        cooldown_seconds=30.0,
        half_open_probes=1,
        probe_lock_seconds=10.0,
    )
    kwargs.update(overrides)
    return CircuitBreaker(health, **kwargs)


class TestClosedCircuit:
    def test_a_healthy_candidate_is_attempted_normally(self):
        breaker = _breaker()
        decision = breaker.decide(_candidate())
        assert decision.decision == Decision.ATTEMPT


class TestOpenCircuitSkipsWithoutNetworkCall:
    def test_an_open_circuit_within_cooldown_is_skipped(self):
        breaker = _breaker(cooldown_seconds=3600)  # cooldown loin dans le futur
        candidate = _candidate()
        for _ in range(3):
            breaker.report_failure(candidate, is_timeout=True)

        decision = breaker.decide(candidate)
        assert decision.decision == Decision.SKIP
        assert decision.reason == "OPEN_COOLDOWN"

    def test_open_circuit_stays_skipped_on_repeated_decisions_no_extra_network_call(self):
        """Preuve directe anti-thundering-herd (§10) : décider 5 fois de suite
        sur un circuit OPEN ne doit JAMAIS retourner ATTEMPT tant que le
        cooldown n'est pas écoulé — aucune tentative réseau cachée."""
        breaker = _breaker(cooldown_seconds=3600)
        candidate = _candidate()
        for _ in range(3):
            breaker.report_failure(candidate, is_timeout=True)

        for _ in range(5):
            assert breaker.decide(candidate).decision == Decision.SKIP


class TestHalfOpenProbe:
    def test_cooldown_elapsed_grants_exactly_one_probe(self):
        breaker = _breaker(cooldown_seconds=0.01)
        candidate = _candidate()
        for _ in range(3):
            breaker.report_failure(candidate, is_timeout=True)
        time.sleep(0.02)

        decision = breaker.decide(candidate)
        assert decision.decision == Decision.PROBE

    def test_a_second_caller_during_the_same_probe_window_is_skipped(self):
        """§14 : le verrou de probe empêche un 2e appelant de sonder EN MÊME
        TEMPS — c'est la garantie concrète contre le fallback storm au
        moment précis où le cooldown vient d'expirer."""
        store = {}
        breaker_a = _breaker(store, cooldown_seconds=0.01)
        breaker_b = _breaker(store, cooldown_seconds=0.01)
        candidate = _candidate()
        for _ in range(3):
            breaker_a.report_failure(candidate, is_timeout=True)
        time.sleep(0.02)

        decision_a = breaker_a.decide(candidate)
        decision_b = breaker_b.decide(candidate)

        assert decision_a.decision == Decision.PROBE
        assert decision_b.decision == Decision.SKIP
        assert decision_b.reason == "PROBE_IN_PROGRESS"

    def test_a_successful_probe_closes_the_circuit(self):
        breaker = _breaker(cooldown_seconds=0.01, half_open_probes=1)
        candidate = _candidate()
        for _ in range(3):
            breaker.report_failure(candidate, is_timeout=True)
        time.sleep(0.02)
        decision = breaker.decide(candidate)
        assert decision.decision == Decision.PROBE

        breaker.report_success(candidate, latency_ms=150.0)

        # Le circuit doit être CLOSED — un nouvel appel décide ATTEMPT direct.
        assert breaker.decide(candidate).decision == Decision.ATTEMPT

    def test_a_failed_probe_reopens_the_circuit_with_a_fresh_cooldown(self):
        breaker = _breaker(cooldown_seconds=0.01)
        candidate = _candidate()
        for _ in range(3):
            breaker.report_failure(candidate, is_timeout=True)
        time.sleep(0.02)
        assert breaker.decide(candidate).decision == Decision.PROBE

        breaker.report_failure(candidate, is_timeout=True)

        # Cooldown frais, encore loin dans le futur -> SKIP immédiat, pas de
        # 2e probe tant que CE nouveau cooldown n'est pas écoulé.
        decision = breaker.decide(candidate)
        assert decision.decision == Decision.SKIP


class TestConfigErrorAlwaysSkipped:
    def test_a_config_error_candidate_is_never_attempted_again(self):
        breaker = _breaker()
        candidate = _candidate()
        breaker._health.mark_config_error(candidate, "model not found")

        for _ in range(3):
            assert breaker.decide(candidate).decision == Decision.SKIP

    def test_a_legacy_config_error_record_without_cooldown_grants_a_probe(self):
        """Incident réel (2026-09-09) : un candidat Bedrock (clé API
        expirée, classé CONFIG_ERROR) est resté SKIP pour toujours parce que
        l'enregistrement Redis avait `cooldown_until=None` — legacy, créé
        avant que `mark_config_error` ne pose systématiquement ce champ.
        Aucun probe n'était plus jamais tenté, même des jours après que la
        clé a été renouvelée. Un `cooldown_until` absent doit être traité
        comme déjà écoulé (probe immédiat), jamais comme un blocage
        permanent — voir `HealthRecord.cooldown_elapsed`."""
        store: dict = {}
        breaker = _breaker(store)
        candidate = _candidate()
        legacy_record = HealthRecord(
            state=CircuitState.OPEN,
            config_error=True,
            config_error_message="Error code: 401 - invalid_api_key: expired token",
            cooldown_until=None,
        )
        redis = make_fake_redis(store)
        redis.set(_key(candidate.key), json.dumps(legacy_record.to_dict()))

        decision = breaker.decide(candidate)
        assert decision.decision == Decision.PROBE
