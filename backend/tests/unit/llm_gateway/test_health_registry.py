"""`llm_gateway/health_registry.py::HealthRegistry` — §11/§12 du brief.

Verrouille : compteurs succès/échec, transition CLOSED→OPEN au seuil,
verrou de probe anti-thundering-herd (§14), et le partage RÉEL de l'état
entre deux "processus" (deux `_FakeRedis` pointant sur le même store) —
c'est la garantie centrale de tout le LLM Gateway (§11 : jamais un
`self.health = {}` local)."""

from __future__ import annotations

from tests.conftest import run
from tests.unit.llm_gateway.conftest import make_fake_redis

from agriconnect.graphs.agents.market_coach.llm_gateway.health_registry import (
    HealthRegistry,
)
from agriconnect.graphs.agents.market_coach.llm_gateway.types import (
    CircuitState,
    LLMProfile,
    ModelCandidate,
)


def _candidate(provider="bedrock_gateway", model="deepseek.v3.2") -> ModelCandidate:
    return ModelCandidate(
        provider=provider,
        model=model,
        profile=LLMProfile.REASONING,
        priority=0,
        timeout_seconds=12.0,
    )


class TestRecordSuccessAndFailure:
    def test_a_fresh_candidate_starts_closed_and_available(self):
        registry = HealthRegistry(redis_client=make_fake_redis())
        record = registry.get(_candidate().key)
        assert record.state == CircuitState.CLOSED
        assert registry.is_available(_candidate())

    def test_record_failure_increments_consecutive_failures(self):
        registry = HealthRegistry(redis_client=make_fake_redis())
        candidate = _candidate()
        registry.record_failure(
            candidate, failure_threshold=3, cooldown_seconds=30, is_timeout=True
        )
        record = registry.get(candidate.key)
        assert record.consecutive_failures == 1
        assert record.timeout_count == 1
        assert record.state == CircuitState.CLOSED  # sous le seuil

    def test_reaching_the_failure_threshold_opens_the_circuit(self):
        registry = HealthRegistry(redis_client=make_fake_redis())
        candidate = _candidate()
        for _ in range(3):
            registry.record_failure(
                candidate, failure_threshold=3, cooldown_seconds=30, is_timeout=False
            )
        record = registry.get(candidate.key)
        assert record.state == CircuitState.OPEN
        assert record.cooldown_until is not None
        assert registry.is_available(candidate) is False

    def test_a_success_resets_consecutive_failures(self):
        registry = HealthRegistry(redis_client=make_fake_redis())
        candidate = _candidate()
        registry.record_failure(
            candidate, failure_threshold=3, cooldown_seconds=30, is_timeout=False
        )
        registry.record_failure(
            candidate, failure_threshold=3, cooldown_seconds=30, is_timeout=False
        )
        registry.record_success(candidate, latency_ms=120.0, half_open_probes=1)
        record = registry.get(candidate.key)
        assert record.consecutive_failures == 0
        assert record.state == CircuitState.CLOSED

    def test_config_error_disables_the_candidate_without_counting_as_transient(self):
        registry = HealthRegistry(redis_client=make_fake_redis())
        candidate = _candidate(provider="bedrock_gateway", model="anthropic.claude-haiku-4-5")
        registry.mark_config_error(
            candidate, "does not support the '/v1/chat/completions' API"
        )
        record = registry.get(candidate.key)
        assert record.config_error is True
        assert registry.is_available(candidate) is False
        # Jamais compté dans consecutive_failures — pas une panne transitoire.
        assert record.consecutive_failures == 0


class TestPercentiles:
    def test_p50_and_p95_computed_from_recent_latencies(self):
        registry = HealthRegistry(redis_client=make_fake_redis())
        candidate = _candidate()
        for latency in [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000]:
            registry.record_success(candidate, latency_ms=latency, half_open_probes=1)
        percentiles = registry.percentiles(candidate.key)
        assert percentiles["p50"] in (500, 600)  # dépend de l'arrondi de l'index
        assert percentiles["p95"] >= 900

    def test_no_samples_yields_none_percentiles(self):
        registry = HealthRegistry(redis_client=make_fake_redis())
        assert registry.percentiles("nobody:home") == {"p50": None, "p95": None}


class TestProbeLockAntiThunderingHerd:
    def test_a_second_concurrent_lock_attempt_fails(self):
        store = {}
        registry_a = HealthRegistry(redis_client=make_fake_redis(store))
        registry_b = HealthRegistry(redis_client=make_fake_redis(store))
        candidate = _candidate()

        acquired_a = registry_a.try_acquire_probe_lock(candidate.key, ttl_seconds=10)
        acquired_b = registry_b.try_acquire_probe_lock(candidate.key, ttl_seconds=10)

        assert acquired_a is True
        assert acquired_b is False  # §14 : un seul probe à la fois


class TestSharedHealthAcrossWorkers:
    """§11 — la garantie centrale : deux 'processus' (API + worker Celery)
    pointant sur le MÊME Redis voient le MÊME état, sans rien se dire
    directement entre eux."""

    def test_a_failure_recorded_by_one_worker_is_visible_to_another(self):
        store = {}
        worker_api = HealthRegistry(redis_client=make_fake_redis(store))
        worker_celery = HealthRegistry(redis_client=make_fake_redis(store))
        candidate = _candidate()

        for _ in range(3):
            worker_api.record_failure(
                candidate, failure_threshold=3, cooldown_seconds=30, is_timeout=True
            )

        # Le worker Celery, qui n'a RIEN enregistré lui-même, voit le circuit
        # déjà OPEN — c'est exactement ce qui évite le fallback storm (§10).
        assert worker_celery.is_available(candidate) is False
        assert worker_celery.get(candidate.key).state == CircuitState.OPEN
