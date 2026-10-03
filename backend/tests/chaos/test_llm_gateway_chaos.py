"""Incrément G (2026-09-13, "Production LLM Hardening") — chaos tests du
LLM Gateway (spec §37/§41/§42/§43/§50).

Complète (ne duplique pas) `tests/unit/llm_gateway/test_retry_policy.py`
(429/500/401/413 déjà couverts là) : ici, connexion réseau réinitialisée,
Redis indisponible pendant la décision du disjoncteur/rate limiter (fail-
open, spec §43), N échecs consécutifs → circuit OPEN → HALF_OPEN → CLOSED
(spec §41, bout en bout), et un petit test de charge contrôlé (spec §50)."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any, Dict, List

from ladini.graphs.agents.market_coach.llm_gateway.alerting import IncidentDeduplicator
from ladini.graphs.agents.market_coach.llm_gateway.circuit_breaker import CircuitBreaker
from ladini.graphs.agents.market_coach.llm_gateway.gateway import LLMGateway
from ladini.graphs.agents.market_coach.llm_gateway.health_registry import HealthRegistry
from ladini.graphs.agents.market_coach.llm_gateway.rate_limiter import RateLimiter
from ladini.graphs.agents.market_coach.llm_gateway.types import (
    CircuitState,
    LLMProfile,
    ModelCandidate,
)
from tests.conftest import run
from tests.unit.llm_gateway.conftest import make_fake_redis


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


class _AlwaysSucceedsClient:
    def __init__(self, model: str, sleep_s: float = 0.0):
        self.calls: List[dict] = []
        self._model = model
        self._sleep_s = sleep_s
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._sleep_s:
            time.sleep(self._sleep_s)
        return _FakeCompletion(model=self._model)


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        LLM_FAST_BUDGET_SECONDS=8.0,
        LLM_REASONING_BUDGET_SECONDS=8.0,
        LLM_CIRCUIT_FAILURE_THRESHOLD=3,
        LLM_CIRCUIT_COOLDOWN_SECONDS=0.05,
        LLM_HALF_OPEN_PROBES=1,
        LLM_PROBE_LOCK_SECONDS=10.0,
        ADMIN_ALERT_WEBHOOK_URL="",
        SENTRY_ENVIRONMENT="test",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_gateway(*, provider_clients: Dict[str, Any], settings=None, store=None, rate_limiter=None):
    settings = settings or _settings()
    store = store if store is not None else {}

    class _StubRegistry:
        def __init__(self):
            self._candidates = [
                ModelCandidate(provider="provider_a", model="model-a", profile=LLMProfile.FAST, priority=0, timeout_seconds=1.0),
                ModelCandidate(provider="provider_b", model="model-b", profile=LLMProfile.FAST, priority=1, timeout_seconds=1.0),
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
        rate_limiter=rate_limiter,
        settings=settings,
        incident_dedup=IncidentDeduplicator(redis_client=make_fake_redis(store)),
        client_factory=lambda provider: provider_clients[provider],
    )


# =====================================================================
# A. CONNEXION RÉSEAU RÉINITIALISÉE (spec §37)
# =====================================================================


class TestConnectionResetChaos:
    def test_a_connection_reset_is_retried_once_then_falls_back_if_persistent(self):
        exc = ConnectionError("connection reset by peer")
        client_a = _ScriptedClient([exc, exc])
        client_b = _ScriptedClient([_FakeCompletion(model="model-b")])
        gw = _make_gateway(provider_clients={"provider_a": client_a, "provider_b": client_b})

        completion = run(gw.complete(profile=LLMProfile.FAST, messages=[{"role": "user", "content": "hi"}]))

        assert completion.model == "model-b"
        assert len(client_a.calls) == 2


# =====================================================================
# B. CIRCUIT BREAKER — SÉQUENCE COMPLÈTE (spec §41)
# =====================================================================


class TestCircuitBreakerFullSequence:
    def test_closed_to_open_to_half_open_to_closed(self):
        exc = Exception("internal error")
        exc.status_code = 500
        client_a = _ScriptedClient([exc] * 20)
        client_b = _ScriptedClient([_FakeCompletion(model="model-b")] * 20)
        store: Dict[str, Any] = {}
        settings = _settings(LLM_CIRCUIT_COOLDOWN_SECONDS=1.0)
        gw = _make_gateway(
            provider_clients={"provider_a": client_a, "provider_b": client_b},
            settings=settings,
            store=store,
        )
        health = gw.health_registry()

        for _ in range(3):
            run(gw.complete(profile=LLMProfile.FAST, messages=[{"role": "user", "content": "hi"}]))
        assert health.get("provider_a:model-a").state == CircuitState.OPEN

        calls_before = len(client_a.calls)
        run(gw.complete(profile=LLMProfile.FAST, messages=[{"role": "user", "content": "hi"}]))
        assert len(client_a.calls) == calls_before  # aucun nouvel appel, cooldown actif

        time.sleep(1.1)  # cooldown 1.0 s (marge CI : 0.05 s expirait avant le 4e appel sur runner lent)
        # Guérit le candidat primaire juste avant le probe HALF_OPEN.
        client_a._behaviors = [_FakeCompletion(model="model-a")] * 5
        run(gw.complete(profile=LLMProfile.FAST, messages=[{"role": "user", "content": "hi"}]))
        assert health.get("provider_a:model-a").state == CircuitState.CLOSED


# =====================================================================
# C. UNKNOWN / REPAIR NE TOUCHENT JAMAIS LE DISJONCTEUR (spec §12/§31/§32)
# =====================================================================


class TestUnknownAndRepairNeverAffectCircuitBreaker:
    def test_a_successful_http_call_with_unparseable_json_content_never_opens_the_circuit(self):
        # Le Gateway ne voit QUE la réponse HTTP réussie — un JSON invalide
        # dans le CONTENU est géré par le micro-prompt appelant (repair),
        # jamais remonté au Gateway comme une exception réseau.
        client_a = _ScriptedClient([_FakeCompletion(content="ceci n'est pas du JSON", model="model-a")])
        client_b = _ScriptedClient([_FakeCompletion(model="model-b")])
        gw = _make_gateway(provider_clients={"provider_a": client_a, "provider_b": client_b})

        completion = run(
            gw.complete(
                profile=LLMProfile.FAST,
                messages=[{"role": "user", "content": "hi"}],
                response_format={"type": "json_object"},
            )
        )
        assert completion.model == "model-a"  # succès HTTP, jamais un fallback
        record = gw.health_registry().get("provider_a:model-a")
        assert record.state == CircuitState.CLOSED
        assert record.consecutive_failures == 0


# =====================================================================
# D. REDIS INDISPONIBLE (spec §43) — fail-open documenté
# =====================================================================


class TestRedisUnavailableFailsOpen:
    def test_health_registry_read_failure_fails_open_not_blocked(self):
        # (2026-09-13, Incrément G, spec §43) : trouvé par CE test — sans
        # le fix apporté à `HealthRegistry.get()`, une panne Redis ici
        # remontait non catchée et aurait fait échouer TOUT appel LLM.
        # Décision explicite : fail-open (candidat traité comme sain),
        # cohérente avec la philosophie déjà affichée par ce module pour
        # `try_acquire_probe_lock`.
        class _BoomingRedis:
            def get(self, *a, **k):
                raise ConnectionError("redis down")

        health = HealthRegistry(redis_client=_BoomingRedis())
        candidate = ModelCandidate(
            provider="provider_a", model="model-a", profile=LLMProfile.FAST, priority=0, timeout_seconds=1.0
        )
        assert health.is_available(candidate) is True
        assert health.get(candidate.key).state == CircuitState.CLOSED

    def test_rate_limiter_fails_open_when_redis_is_down(self):
        class _BoomingRedis:
            def incr(self, *a, **k):
                raise ConnectionError("redis down")

        limiter = RateLimiter(redis_client=_BoomingRedis(), rpm_limit=1)
        decision = limiter.check_and_reserve("groq", 0)
        assert decision.allowed is True  # spec §43 : fail-open explicite


# =====================================================================
# E. PETIT TEST DE CHARGE CONTRÔLÉ (spec §50)
# =====================================================================


class TestLightLoadTest:
    def test_20_concurrent_calls_all_succeed_without_thundering_herd(self):
        client_a = _AlwaysSucceedsClient("model-a")
        client_b = _AlwaysSucceedsClient("model-b")
        gw = _make_gateway(provider_clients={"provider_a": client_a, "provider_b": client_b})

        async def _run_load():
            tasks = [
                gw.complete(profile=LLMProfile.FAST, messages=[{"role": "user", "content": f"msg {i}"}])
                for i in range(20)
            ]
            return await asyncio.gather(*tasks)

        t0 = time.perf_counter()
        results = run(_run_load())
        elapsed_s = time.perf_counter() - t0

        assert len(results) == 20
        assert all(r.model == "model-a" for r in results)
        assert len(client_a.calls) == 20
        assert len(client_b.calls) == 0  # jamais de fallback, primaire toujours sain
        # Débit raisonnable — 20 appels factices (aucune vraie latence
        # réseau) ne devraient jamais prendre plusieurs secondes.
        assert elapsed_s < 5.0
