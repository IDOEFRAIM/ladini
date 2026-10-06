"""Scénario de panne complet — §44 du brief LLM Gateway.

`deepseek.v3.2` (candidat primaire REASONING) se met à timeout. Vérifie,
dans l'ordre exact demandé :

    request 1 -> timeout -> fallback
    request 2 -> timeout -> fallback
    request 3 -> threshold -> circuit OPEN
    request 4 -> deepseek ignoré -> fallback direct (AUCUN appel réseau)
    ... cooldown ...
    -> HALF_OPEN -> une seule probe
    -> succès -> HEALTHY -> recovery event -> trafic restauré

Miroir direct de l'incident réel qui a motivé cette Gateway (deepseek.v3.1
mesuré à 12.6s contre un budget de 10s, cf. rapport de session 2026-09-02).
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, Dict, List

from ladini.graphs.agents.market_coach.llm_gateway.alerting import (
    IncidentDeduplicator,
)
from ladini.graphs.agents.market_coach.llm_gateway.circuit_breaker import (
    CircuitBreaker,
)
from ladini.graphs.agents.market_coach.llm_gateway.gateway import LLMGateway
from ladini.graphs.agents.market_coach.llm_gateway.health_registry import (
    HealthRegistry,
)
from ladini.graphs.agents.market_coach.llm_gateway.types import (
    CircuitState,
    LLMProfile,
    ModelCandidate,
)
from tests.conftest import run
from tests.unit.llm_gateway.conftest import make_fake_redis


class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)


class _Completion:
    def __init__(self, content="{}", model=None):
        self.choices = [_Choice(content)]
        self.model = model


class _FlakyThenHealthyClient:
    """Simule `deepseek.v3.2` : lève un timeout jusqu'à un appel explicite à
    `heal()`, puis répond normalement (le "probe" de récupération réussit).

    (2026-09-13, Incrément G) : basé sur un flag explicite plutôt qu'un
    compteur d'appels fixe — depuis que le Gateway retente une fois le
    MÊME candidat sur un timeout (spec §5/§6), le nombre d'appels
    PHYSIQUES par tour n'est plus 1:1 avec le nombre de tours (`_turn()`)
    de ce scénario ; ce qui compte pour l'histoire racontée par ce test
    ("le primaire est en panne jusqu'à ce que le probe HALF_OPEN
    réussisse") est le moment sémantique de la guérison, pas un compte
    d'appels devenu fragile."""

    def __init__(self):
        self._healthy = False
        self.calls: List[dict] = []
        self.chat = self
        self.completions = self

    def heal(self) -> None:
        self._healthy = True

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._healthy:
            raise TimeoutError("deepseek.v3.2 a dépassé le budget (incident réel)")
        return _Completion(model=kwargs.get("model"))


class _AlwaysHealthyFallback:
    def __init__(self):
        self.calls: List[dict] = []
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _Completion(model=kwargs.get("model"))


class _StubRegistry:
    def __init__(self, primary: ModelCandidate, fallback: ModelCandidate):
        self._candidates = [primary, fallback]

    def candidates_for(self, profile):
        return list(self._candidates)

    def primary_model_name(self, profile):
        return self._candidates[0].model


class TestFullOutageAndRecoveryScenario:
    def test_the_exact_sequence_from_the_brief(self):
        store: Dict[str, Any] = {}
        primary_client = _FlakyThenHealthyClient()
        fallback_client = _AlwaysHealthyFallback()

        primary = ModelCandidate(
            provider="provider_primary",
            model="deepseek.v3.2",
            profile=LLMProfile.REASONING,
            priority=0,
            timeout_seconds=1.0,
        )
        fallback = ModelCandidate(
            provider="provider_fallback",
            model="openai.gpt-oss-120b",
            profile=LLMProfile.REASONING,
            priority=1,
            timeout_seconds=1.0,
        )
        registry = _StubRegistry(primary, fallback)
        health = HealthRegistry(redis_client=make_fake_redis(store))
        # Cooldown très court pour que le test reste rapide.
        circuit = CircuitBreaker(
            health,
            failure_threshold=3,
            cooldown_seconds=0.05,
            half_open_probes=1,
            probe_lock_seconds=10.0,
        )
        settings = SimpleNamespace(
            LLM_FAST_BUDGET_SECONDS=10.0,
            LLM_REASONING_BUDGET_SECONDS=10.0,
            ADMIN_ALERT_WEBHOOK_URL="",
            SENTRY_ENVIRONMENT="test",
        )
        gateway = LLMGateway(
            registry=registry,
            health_registry=health,
            circuit_breaker=circuit,
            settings=settings,
            incident_dedup=IncidentDeduplicator(redis_client=make_fake_redis(store)),
            client_factory=lambda provider: {
                "provider_primary": primary_client,
                "provider_fallback": fallback_client,
            }[provider],
        )

        def _turn():
            return run(
                gateway.complete(
                    profile=LLMProfile.REASONING,
                    messages=[{"role": "user", "content": "je veux vendre du lait"}],
                )
            )

        # request 1 -> timeout -> fallback
        c1 = _turn()
        assert c1.model == "openai.gpt-oss-120b"
        assert health.get(primary.key).state == CircuitState.CLOSED
        assert health.get(primary.key).consecutive_failures == 1

        # request 2 -> timeout -> fallback
        c2 = _turn()
        assert c2.model == "openai.gpt-oss-120b"
        assert health.get(primary.key).consecutive_failures == 2

        # request 3 -> threshold atteint -> circuit OPEN
        c3 = _turn()
        assert c3.model == "openai.gpt-oss-120b"
        assert health.get(primary.key).state == CircuitState.OPEN
        # (2026-09-13, Incrément G) : plus un compte fixe (le retry
        # same-candidate sur TIMEOUT fait 2 appels physiques par tour en
        # échec, contre 1 avant) — ce qui compte ici est qu'AUCUN nouvel
        # appel ne survienne une fois le circuit OPEN, vérifié juste après.
        calls_to_primary_after_open = len(primary_client.calls)
        assert calls_to_primary_after_open > 0

        # request 4 -> primaire ignoré (cooldown pas écoulé) -> fallback DIRECT
        c4 = _turn()
        assert c4.model == "openai.gpt-oss-120b"
        assert len(primary_client.calls) == calls_to_primary_after_open  # AUCUN nouvel appel

        # ── cooldown ──
        time.sleep(0.06)
        primary_client.heal()

        # -> HALF_OPEN -> une seule probe -> succès -> HEALTHY
        c5 = _turn()
        assert c5.model == "deepseek.v3.2"  # le probe a réussi (heal() appelé)
        record_after_recovery = health.get(primary.key)
        assert record_after_recovery.state == CircuitState.CLOSED

        # -> trafic restauré : le tour suivant retourne directement sur le
        # primaire, sans repasser par le fallback.
        c6 = _turn()
        assert c6.model == "deepseek.v3.2"

    def test_recovery_notification_fires_exactly_once(self):
        """Complète le scénario ci-dessus côté alerting (§28/§32) : une seule
        notification d'ouverture, une seule de récupération, quel que soit
        le nombre de tours en échec/en succès traversés."""
        store: Dict[str, Any] = {}
        primary_client = _FlakyThenHealthyClient()
        fallback_client = _AlwaysHealthyFallback()
        primary = ModelCandidate(
            provider="provider_primary",
            model="deepseek.v3.2",
            profile=LLMProfile.REASONING,
            priority=0,
            timeout_seconds=1.0,
        )
        fallback = ModelCandidate(
            provider="provider_fallback",
            model="openai.gpt-oss-120b",
            profile=LLMProfile.REASONING,
            priority=1,
            timeout_seconds=1.0,
        )
        registry = _StubRegistry(primary, fallback)
        health = HealthRegistry(redis_client=make_fake_redis(store))
        circuit = CircuitBreaker(
            health,
            failure_threshold=3,
            cooldown_seconds=0.05,
            half_open_probes=1,
            probe_lock_seconds=10.0,
        )
        settings = SimpleNamespace(
            LLM_FAST_BUDGET_SECONDS=10.0,
            LLM_REASONING_BUDGET_SECONDS=10.0,
            ADMIN_ALERT_WEBHOOK_URL="",
            SENTRY_ENVIRONMENT="test",
        )

        sent_alerts: List[str] = []

        class _RecordingNotifier:
            def send_incident(self, alert):
                sent_alerts.append(f"{alert.severity}:{alert.provider}:{alert.model}")

        gateway = LLMGateway(
            registry=registry,
            health_registry=health,
            circuit_breaker=circuit,
            settings=settings,
            notifier=_RecordingNotifier(),
            incident_dedup=IncidentDeduplicator(redis_client=make_fake_redis(store)),
            client_factory=lambda provider: {
                "provider_primary": primary_client,
                "provider_fallback": fallback_client,
            }[provider],
        )

        def _turn():
            return run(
                gateway.complete(
                    profile=LLMProfile.REASONING,
                    messages=[{"role": "user", "content": "hi"}],
                )
            )

        for _ in range(4):
            _turn()
        time.sleep(0.06)
        primary_client.heal()
        _turn()  # probe réussi -> recovery

        critical_alerts = [a for a in sent_alerts if a.startswith("CRITICAL")]
        recovery_alerts = [a for a in sent_alerts if a.startswith("RECOVERY")]
        assert len(critical_alerts) == 1
        assert len(recovery_alerts) == 1
