"""Incrément G (2026-09-13, "Production LLM Hardening") — rate limiter
partagé (spec §14-18/§43).

Couvre : désactivé par défaut (spec §15, jamais de bridage du trafic
normal), RPM/TPM/in-flight dépassés, libération in-flight, et le fail-open
explicite quand Redis est indisponible (spec §43)."""
from __future__ import annotations

from typing import Any, Dict

from ladini.graphs.agents.market_coach.llm_gateway.rate_limiter import (
    RateLimiter,
    build_rate_limiter,
)
from tests.unit.llm_gateway.conftest import make_fake_redis


def _limiter(store: Dict[str, Any] = None, **limits) -> RateLimiter:
    return RateLimiter(redis_client=make_fake_redis(store if store is not None else {}), **limits)


class TestDisabledByDefault:
    def test_no_limits_configured_always_allows_without_touching_redis(self):
        class _BoomingRedis:
            def incr(self, *a, **k):
                raise AssertionError("Redis ne doit jamais être touché si désactivé")

        limiter = RateLimiter(redis_client=_BoomingRedis())
        decision = limiter.check_and_reserve("groq", 1000)
        assert decision.allowed is True
        assert decision.reason == "DISABLED"

    def test_build_rate_limiter_defaults_to_disabled(self):
        from types import SimpleNamespace

        settings = SimpleNamespace(GROQ_RPM_LIMIT=0, GROQ_TPM_LIMIT=0, GROQ_MAX_INFLIGHT=0)
        limiter = build_rate_limiter(settings)
        assert limiter.enabled is False


class TestRPMLimit:
    def test_within_limit_is_allowed(self):
        limiter = _limiter(rpm_limit=5)
        for _ in range(5):
            assert limiter.check_and_reserve("groq", 0).allowed is True

    def test_exceeding_rpm_is_refused(self):
        limiter = _limiter(rpm_limit=2)
        assert limiter.check_and_reserve("groq", 0).allowed is True
        assert limiter.check_and_reserve("groq", 0).allowed is True
        decision = limiter.check_and_reserve("groq", 0)
        assert decision.allowed is False
        assert decision.reason == "RPM_EXCEEDED"

    def test_different_providers_have_independent_counters(self):
        limiter = _limiter(rpm_limit=1)
        assert limiter.check_and_reserve("groq", 0).allowed is True
        assert limiter.check_and_reserve("bedrock_gateway", 0).allowed is True


class TestTPMLimit:
    def test_exceeding_tpm_is_refused(self):
        limiter = _limiter(tpm_limit=1000)
        assert limiter.check_and_reserve("groq", 600).allowed is True
        decision = limiter.check_and_reserve("groq", 600)
        assert decision.allowed is False
        assert decision.reason == "TPM_EXCEEDED"


class TestInflightLimit:
    def test_exceeding_inflight_is_refused_and_release_frees_a_slot(self):
        limiter = _limiter(max_inflight=1)
        assert limiter.check_and_reserve("groq", 0).allowed is True
        refused = limiter.check_and_reserve("groq", 0)
        assert refused.allowed is False
        assert refused.reason == "INFLIGHT_LIMIT_EXCEEDED"

        limiter.release_inflight("groq")
        assert limiter.check_and_reserve("groq", 0).allowed is True

    def test_refused_reservation_does_not_leak_the_inflight_slot(self):
        # Un refus ne doit pas laisser le compteur "consommé" pour rien —
        # sinon un seul burst refusé userait le budget in-flight à vide.
        limiter = _limiter(max_inflight=1)
        limiter.check_and_reserve("groq", 0)  # occupe le seul slot
        limiter.check_and_reserve("groq", 0)  # refusé — ne doit pas fuiter
        limiter.release_inflight("groq")  # libère le slot occupé
        assert limiter.check_and_reserve("groq", 0).allowed is True


class TestFailOpenOnRedisError:
    def test_redis_exception_during_check_fails_open(self):
        class _BoomingRedis:
            def incr(self, *a, **k):
                raise ConnectionError("redis down")

        limiter = RateLimiter(redis_client=_BoomingRedis(), rpm_limit=1)
        decision = limiter.check_and_reserve("groq", 0)
        assert decision.allowed is True
        assert decision.reason == "FAIL_OPEN_REDIS_ERROR"

    def test_redis_exception_during_release_never_raises(self):
        class _BoomingRedis:
            def decr(self, *a, **k):
                raise ConnectionError("redis down")

        limiter = RateLimiter(redis_client=_BoomingRedis(), max_inflight=1)
        limiter.release_inflight("groq")  # ne doit jamais lever
