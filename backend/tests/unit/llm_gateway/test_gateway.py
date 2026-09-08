"""`llm_gateway/gateway.py::LLMGateway` — bout-en-bout SANS réseau réel (client
factory injecté, `_FakeRedis` en mémoire) : §9/§10/§16/§17/§18/§19 du brief.

Chaque test construit sa propre `LLMGateway` avec un `registry`/`client_factory`
dédiés — house style "fakes locaux", voir `tests/unit/test_get_llm_circuit_
breaker.py` pour le précédent direct dans ce repo."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from tests.conftest import run
from tests.unit.llm_gateway.conftest import make_fake_redis

from agriconnect.graphs.agents.market_coach.llm_gateway.circuit_breaker import (
    CircuitBreaker,
)
from agriconnect.graphs.agents.market_coach.llm_gateway.gateway import (
    LLMGateway,
    LLMGatewayExhausted,
)
from agriconnect.graphs.agents.market_coach.llm_gateway.health_registry import (
    HealthRegistry,
)
from agriconnect.graphs.agents.market_coach.llm_gateway.registry import ModelRegistry
from agriconnect.graphs.agents.market_coach.llm_gateway.types import LLMProfile


# ── Fakes locaux ─────────────────────────────────────────────────────
class _FakeMsg:
    def __init__(self, content: str):
        self.content = content


class _FakeChoice:
    def __init__(self, content: str):
        self.message = _FakeMsg(content)


class _FakeCompletion:
    def __init__(self, content: str = "{}", model: str = "unknown"):
        self.choices = [_FakeChoice(content)]
        self.model = model
        self.usage = None


class _ScriptedClient:
    """Un client par (provider), forme `client.chat.completions.create(...)`.
    `behavior` reçoit **kwargs (dont `model=`) et doit soit retourner un
    `_FakeCompletion`, soit lever."""

    def __init__(self, behavior):
        self.calls: List[dict] = []
        self._behavior = behavior
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._behavior(**kwargs)


def _always_succeeds(**kwargs) -> _FakeCompletion:
    return _FakeCompletion(content="{}", model=kwargs.get("model"))


def _always_raises(exc_factory):
    def _behavior(**kwargs):
        raise exc_factory()

    return _behavior


def _sleeps_then_succeeds(sleep_s: float):
    def _behavior(**kwargs):
        time.sleep(sleep_s)
        return _FakeCompletion(content="{}", model=kwargs.get("model"))

    return _behavior


def _fake_settings(**overrides) -> SimpleNamespace:
    base = dict(
        LLM_FAST_PRIMARY="provider_a:model-a",
        LLM_FAST_FALLBACK_1="provider_b:model-b",
        LLM_FAST_FALLBACK_2="",
        LLM_REASONING_PRIMARY="provider_a:model-a",
        LLM_REASONING_FALLBACK_1="provider_b:model-b",
        LLM_REASONING_FALLBACK_2="",
        LLM_CIRCUIT_FAILURE_THRESHOLD=3,
        LLM_CIRCUIT_COOLDOWN_SECONDS=30.0,
        LLM_HALF_OPEN_PROBES=1,
        LLM_PROBE_LOCK_SECONDS=10.0,
        LLM_FAST_BUDGET_SECONDS=5.0,
        LLM_REASONING_BUDGET_SECONDS=5.0,
        ADMIN_ALERT_WEBHOOK_URL="",
        SENTRY_ENVIRONMENT="test",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_gateway(*, provider_clients: Dict[str, Any], settings=None, store=None):
    settings = settings or _fake_settings()
    if store is None:
        store = {}
    # `registry.py` valide contre un catalogue de providers connus — pour
    # tester le comportement du Gateway sans dépendre de groq/bedrock réels,
    # on monkeypatch temporairement la liste connue le temps de charger le
    # registry. Plus simple : construire les ModelCandidate directement.
    from agriconnect.graphs.agents.market_coach.llm_gateway.types import ModelCandidate

    def _parse(raw, profile):
        provider, _, model = raw.partition(":")
        return ModelCandidate(
            provider=provider,
            model=model,
            profile=profile,
            priority=0,
            timeout_seconds=2.0,
            capabilities={"structured_output": True},
        )

    class _StubRegistry:
        def __init__(self):
            self._by_profile = {
                LLMProfile.FAST: [
                    _parse(settings.LLM_FAST_PRIMARY, LLMProfile.FAST),
                    _parse(settings.LLM_FAST_FALLBACK_1, LLMProfile.FAST),
                ],
                LLMProfile.REASONING: [
                    _parse(settings.LLM_REASONING_PRIMARY, LLMProfile.REASONING),
                    _parse(settings.LLM_REASONING_FALLBACK_1, LLMProfile.REASONING),
                ],
            }

        def candidates_for(self, profile):
            return list(self._by_profile.get(profile, []))

        def primary_model_name(self, profile):
            c = self._by_profile.get(profile) or []
            return c[0].model if c else None

    registry = _StubRegistry()
    health = HealthRegistry(redis_client=make_fake_redis(store))
    circuit = CircuitBreaker(
        health,
        failure_threshold=settings.LLM_CIRCUIT_FAILURE_THRESHOLD,
        cooldown_seconds=settings.LLM_CIRCUIT_COOLDOWN_SECONDS,
        half_open_probes=settings.LLM_HALF_OPEN_PROBES,
        probe_lock_seconds=settings.LLM_PROBE_LOCK_SECONDS,
    )

    def client_factory(provider: str):
        return provider_clients[provider]

    from agriconnect.graphs.agents.market_coach.llm_gateway.alerting import (
        IncidentDeduplicator,
    )

    return LLMGateway(
        registry=registry,
        health_registry=health,
        circuit_breaker=circuit,
        settings=settings,
        incident_dedup=IncidentDeduplicator(redis_client=make_fake_redis(store)),
        client_factory=client_factory,
    )


class TestPrimaryHealthy:
    def test_primary_success_is_used_directly_no_fallback_call(self):
        client_a = _ScriptedClient(_always_succeeds)
        client_b = _ScriptedClient(_always_succeeds)
        gw = _make_gateway(provider_clients={"provider_a": client_a, "provider_b": client_b})

        completion = run(
            gw.complete(profile=LLMProfile.FAST, messages=[{"role": "user", "content": "hi"}])
        )

        assert completion.choices[0].message.content == "{}"
        assert len(client_a.calls) == 1
        assert len(client_b.calls) == 0


class TestFallbackOnFailure:
    def test_primary_timeout_falls_back_to_the_next_candidate(self):
        client_a = _ScriptedClient(_sleeps_then_succeeds(2.0))  # dépasse son timeout (2.0s défini par candidat)
        client_b = _ScriptedClient(_always_succeeds)
        gw = _make_gateway(provider_clients={"provider_a": client_a, "provider_b": client_b})

        completion = run(
            gw.complete(profile=LLMProfile.FAST, messages=[{"role": "user", "content": "hi"}])
        )

        assert completion.choices[0].message.content == "{}"
        assert len(client_b.calls) == 1

    def test_primary_exception_falls_back(self):
        client_a = _ScriptedClient(_always_raises(lambda: ConnectionError("down")))
        client_b = _ScriptedClient(_always_succeeds)
        gw = _make_gateway(provider_clients={"provider_a": client_a, "provider_b": client_b})

        completion = run(
            gw.complete(profile=LLMProfile.FAST, messages=[{"role": "user", "content": "hi"}])
        )
        assert completion.model == "model-b"


class TestCircuitOpenSkipsDirectlyNoNetworkCall:
    def test_an_open_primary_is_never_called_again_fallback_used_immediately(self):
        store: Dict[str, Any] = {}
        client_a = _ScriptedClient(_always_raises(lambda: ConnectionError("down")))
        client_b = _ScriptedClient(_always_succeeds)
        settings = _fake_settings(LLM_CIRCUIT_FAILURE_THRESHOLD=3, LLM_CIRCUIT_COOLDOWN_SECONDS=3600)
        gw = _make_gateway(
            provider_clients={"provider_a": client_a, "provider_b": client_b},
            settings=settings,
            store=store,
        )

        # 3 tours suffisent à ouvrir le circuit de provider_a.
        for _ in range(3):
            run(gw.complete(profile=LLMProfile.FAST, messages=[{"role": "user", "content": "hi"}]))

        calls_after_open = len(client_a.calls)
        run(gw.complete(profile=LLMProfile.FAST, messages=[{"role": "user", "content": "hi"}]))

        # Aucun NOUVEL appel réseau vers provider_a une fois le circuit OPEN —
        # c'est la garantie anti-fallback-storm (§10).
        assert len(client_a.calls) == calls_after_open
        assert len(client_b.calls) == 4


class TestStructuredOutputCapability:
    def test_a_candidate_without_structured_output_is_skipped_when_required(self):
        from agriconnect.graphs.agents.market_coach.llm_gateway.types import ModelCandidate

        client_a = _ScriptedClient(_always_succeeds)
        client_b = _ScriptedClient(_always_succeeds)
        gw = _make_gateway(provider_clients={"provider_a": client_a, "provider_b": client_b})
        # provider_a ne supporte PAS le structured output pour ce test.
        gw._registry._by_profile[LLMProfile.FAST][0] = ModelCandidate(
            provider="provider_a",
            model="model-a",
            profile=LLMProfile.FAST,
            priority=0,
            timeout_seconds=2.0,
            capabilities={"structured_output": False},
        )

        run(
            gw.complete(
                profile=LLMProfile.FAST,
                messages=[{"role": "user", "content": "hi"}],
                response_format={"type": "json_object"},
            )
        )

        assert len(client_a.calls) == 0  # jamais tenté — incompatible
        assert len(client_b.calls) == 1


class TestBudgetExhaustion:
    def test_budget_exhausted_stops_the_chain_before_trying_every_candidate(self):
        client_a = _ScriptedClient(_sleeps_then_succeeds(1.8))
        client_b = _ScriptedClient(_always_succeeds)
        settings = _fake_settings(LLM_FAST_BUDGET_SECONDS=1.9)  # à peine assez pour 1 tentative
        gw = _make_gateway(
            provider_clients={"provider_a": client_a, "provider_b": client_b},
            settings=settings,
        )

        with pytest.raises(LLMGatewayExhausted):
            run(
                gw.complete(
                    profile=LLMProfile.FAST, messages=[{"role": "user", "content": "hi"}]
                )
            )
        # provider_b n'a jamais pu être tenté — budget global épuisé après
        # l'échec (lent) de provider_a.
        assert len(client_b.calls) == 0


class TestAllCandidatesFail:
    def test_raises_llm_gateway_exhausted_when_every_candidate_fails(self):
        client_a = _ScriptedClient(_always_raises(lambda: ConnectionError("down")))
        client_b = _ScriptedClient(_always_raises(lambda: ConnectionError("also down")))
        gw = _make_gateway(provider_clients={"provider_a": client_a, "provider_b": client_b})

        with pytest.raises(LLMGatewayExhausted):
            run(
                gw.complete(
                    profile=LLMProfile.FAST, messages=[{"role": "user", "content": "hi"}]
                )
            )


class TestProviderAvailabilityPrecheck:
    """§6/§7 du brief incident 2026-09-05 : un candidat structurellement
    inutilisable (identifiant manquant pour SON provider) ne doit JAMAIS
    déclencher d'appel réseau — le pré-check le saute AVANT le disjoncteur."""

    def test_a_candidate_with_a_missing_credential_is_skipped_with_zero_network_calls(
        self,
    ):
        client_a = _ScriptedClient(_always_succeeds)
        client_b = _ScriptedClient(_always_succeeds)
        settings = _fake_settings(
            LLM_FAST_PRIMARY="groq:model-a",
            LLM_FAST_FALLBACK_1="provider_b:model-b",
            llm_api_key="",  # provider_a=groq, credential absent
        )
        gw = _make_gateway(
            provider_clients={"groq": client_a, "provider_b": client_b},
            settings=settings,
        )

        completion = run(
            gw.complete(profile=LLMProfile.FAST, messages=[{"role": "user", "content": "hi"}])
        )

        assert completion.model == "model-b"
        assert len(client_a.calls) == 0  # jamais tenté — CREDENTIAL_MISSING
        assert len(client_b.calls) == 1

    def test_precheck_never_touches_health_or_circuit_state(self):
        """Un candidat structurellement inutilisable n'a jamais eu de
        "santé" à dégrader — ce n'est pas une panne, c'est une
        impossibilité connue à l'avance (§7)."""
        from agriconnect.graphs.agents.market_coach.llm_gateway.types import (
            CircuitState,
        )

        client_a = _ScriptedClient(_always_succeeds)
        client_b = _ScriptedClient(_always_succeeds)
        settings = _fake_settings(
            LLM_FAST_PRIMARY="groq:model-a",
            LLM_FAST_FALLBACK_1="provider_b:model-b",
            llm_api_key="",
        )
        gw = _make_gateway(
            provider_clients={"groq": client_a, "provider_b": client_b},
            settings=settings,
        )

        run(gw.complete(profile=LLMProfile.FAST, messages=[{"role": "user", "content": "hi"}]))

        record = gw.health_registry().get("groq:model-a")
        assert record.state == CircuitState.CLOSED
        assert record.config_error is False
        assert record.total_requests == 0


class TestExhaustedReasonDistinguishesWhy:
    """§11/§12 : `LLMGatewayExhausted.reason` doit permettre à l'appelant de
    savoir si un second appel a ne serait-ce qu'un sens."""

    def test_all_candidates_structurally_unusable_yields_all_candidates_unavailable(self):
        client_a = _ScriptedClient(_always_succeeds)
        settings = _fake_settings(
            LLM_FAST_PRIMARY="groq:model-a",
            LLM_FAST_FALLBACK_1="groq:model-b",
            llm_api_key="",  # les deux candidats sont groq — aucun n'est usable
        )
        gw = _make_gateway(
            provider_clients={"groq": client_a}, settings=settings
        )

        with pytest.raises(LLMGatewayExhausted) as excinfo:
            run(
                gw.complete(
                    profile=LLMProfile.FAST, messages=[{"role": "user", "content": "hi"}]
                )
            )
        assert excinfo.value.reason == "ALL_CANDIDATES_UNAVAILABLE"
        assert len(client_a.calls) == 0

    def test_at_least_one_real_attempt_yields_all_attempts_failed(self):
        client_a = _ScriptedClient(_always_raises(lambda: ConnectionError("down")))
        client_b = _ScriptedClient(_always_raises(lambda: ConnectionError("also down")))
        gw = _make_gateway(provider_clients={"provider_a": client_a, "provider_b": client_b})

        with pytest.raises(LLMGatewayExhausted) as excinfo:
            run(
                gw.complete(
                    profile=LLMProfile.FAST, messages=[{"role": "user", "content": "hi"}]
                )
            )
        assert excinfo.value.reason == "ALL_ATTEMPTS_FAILED"

    def test_budget_exhausted_before_any_attempt_is_reported_as_such(self):
        client_a = _ScriptedClient(_sleeps_then_succeeds(1.8))
        client_b = _ScriptedClient(_always_succeeds)
        settings = _fake_settings(LLM_FAST_BUDGET_SECONDS=1.0)  # < _MIN_VIABLE_SECONDS
        gw = _make_gateway(
            provider_clients={"provider_a": client_a, "provider_b": client_b},
            settings=settings,
        )

        with pytest.raises(LLMGatewayExhausted) as excinfo:
            run(
                gw.complete(
                    profile=LLMProfile.FAST, messages=[{"role": "user", "content": "hi"}]
                )
            )
        assert excinfo.value.reason == "BUDGET_EXHAUSTED"
        assert len(client_a.calls) == 0


class TestConfigErrorNoRetryStorm:
    def test_a_config_error_candidate_is_tried_once_then_never_again(self):
        class _ConfigError(Exception):
            status_code = 401

        client_a = _ScriptedClient(_always_raises(lambda: _ConfigError("unauthorized")))
        client_b = _ScriptedClient(_always_succeeds)
        gw = _make_gateway(provider_clients={"provider_a": client_a, "provider_b": client_b})

        for _ in range(3):
            run(
                gw.complete(
                    profile=LLMProfile.FAST, messages=[{"role": "user", "content": "hi"}]
                )
            )

        # 1 seul appel réseau vers provider_a sur 3 tours — désactivé dès le
        # premier 401, jamais retenté (§18 "pas de retry pour 401/403").
        assert len(client_a.calls) == 1
        assert len(client_b.calls) == 3


class TestConfigErrorEventuallyRecovers:
    """Incident réel (2026-09-05) : un candidat `config_error=True` restait
    bloqué INDÉFINIMENT — aucun chemin de retour hors TTL Redis 7 jours ou
    intervention manuelle sur Redis. Une fois le token rotaté / le modèle
    republié côté provider, le système ne s'en apercevait jamais tout seul.
    §21/§25 du brief : recovery doit réutiliser le MÊME mécanisme cooldown
    + probe HALF_OPEN qu'un OPEN transitoire ordinaire."""

    def test_a_config_error_candidate_is_probed_again_after_its_cooldown_and_recovers(
        self,
    ):
        class _ConfigError(Exception):
            status_code = 404

        # provider_a échoue une seule fois (401/404), puis serait de nouveau
        # sain si on le retentait — simule "le token a été rotaté / le modèle
        # republié" pendant le cooldown.
        calls_a: List[dict] = []

        def _fails_once_then_succeeds(**kwargs):
            calls_a.append(kwargs)
            if len(calls_a) == 1:
                raise _ConfigError("model not found")
            return _FakeCompletion(content="{}", model=kwargs.get("model"))

        client_a = _ScriptedClient(_fails_once_then_succeeds)
        client_b = _ScriptedClient(_always_succeeds)
        settings = _fake_settings(LLM_CIRCUIT_COOLDOWN_SECONDS=0.05)
        gw = _make_gateway(
            provider_clients={"provider_a": client_a, "provider_b": client_b},
            settings=settings,
        )

        # Tour 1 : provider_a en CONFIG_ERROR, repli sur provider_b.
        c1 = run(
            gw.complete(profile=LLMProfile.FAST, messages=[{"role": "user", "content": "hi"}])
        )
        assert c1.model == "model-b"
        record = gw.health_registry().get("provider_a:model-a")
        assert record.config_error is True
        assert record.cooldown_until is not None  # §recovery : plus jamais None

        # Tour 2, AVANT le cooldown : toujours sauté, zéro nouvel appel réseau.
        run(gw.complete(profile=LLMProfile.FAST, messages=[{"role": "user", "content": "hi"}]))
        assert len(client_a.calls) == 1

        # ── cooldown écoulé ──
        time.sleep(0.06)

        # Tour 3 : probe HALF_OPEN sur provider_a — réussit cette fois.
        c3 = run(
            gw.complete(profile=LLMProfile.FAST, messages=[{"role": "user", "content": "hi"}])
        )
        assert c3.model == "model-a"
        record_after_recovery = gw.health_registry().get("provider_a:model-a")
        assert record_after_recovery.config_error is False
        assert record_after_recovery.config_error_message is None

        # Tour 4 : trafic restauré directement sur provider_a, plus de repli.
        c4 = run(
            gw.complete(profile=LLMProfile.FAST, messages=[{"role": "user", "content": "hi"}])
        )
        assert c4.model == "model-a"

    def test_a_config_error_candidate_is_still_skipped_before_its_cooldown_elapses(self):
        """Non-régression du comportement anti-tempête existant : le
        cooldown doit réellement s'écouler, pas de probe prématuré."""

        class _ConfigError(Exception):
            status_code = 401

        client_a = _ScriptedClient(_always_raises(lambda: _ConfigError("unauthorized")))
        client_b = _ScriptedClient(_always_succeeds)
        settings = _fake_settings(LLM_CIRCUIT_COOLDOWN_SECONDS=3600.0)
        gw = _make_gateway(
            provider_clients={"provider_a": client_a, "provider_b": client_b},
            settings=settings,
        )

        for _ in range(5):
            run(
                gw.complete(
                    profile=LLMProfile.FAST, messages=[{"role": "user", "content": "hi"}]
                )
            )

        assert len(client_a.calls) == 1  # jamais reprobé — cooldown de 1h non écoulé
