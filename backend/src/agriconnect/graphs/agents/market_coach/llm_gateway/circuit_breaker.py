"""Circuit Breaker — décide, pour UN candidat, s'il faut l'essayer normalement,
le sonder (probe HALF_OPEN) ou le sauter — §13/§14 du brief.

Logique pure de décision au-dessus de `HealthRegistry` (qui porte l'état
Redis) — ce module ne fait aucun appel LLM lui-même, seulement la décision +
l'acquisition du verrou de probe. Le CALLER (gateway.py) est responsable
d'exécuter réellement l'appel et de rapporter le résultat via
`HealthRegistry.record_success/record_failure`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum

from agriconnect.graphs.agents.market_coach.llm_gateway.health_registry import (
    HealthRegistry,
)
from agriconnect.graphs.agents.market_coach.llm_gateway.types import (
    CircuitState,
    ModelCandidate,
)

logger = logging.getLogger("agriconnect.llm_gateway.circuit")


class Decision(str, Enum):
    ATTEMPT = "ATTEMPT"  # circuit CLOSED — appel normal
    PROBE = "PROBE"  # cooldown expiré, verrou de probe obtenu — SEUL appel autorisé
    SKIP = "SKIP"  # OPEN (cooldown en cours OU probe déjà en cours ailleurs) ou CONFIG_ERROR


@dataclass(frozen=True)
class CircuitDecision:
    decision: Decision
    reason: str


class CircuitBreaker:
    def __init__(
        self,
        health_registry: HealthRegistry,
        *,
        failure_threshold: int,
        cooldown_seconds: float,
        half_open_probes: int,
        probe_lock_seconds: float,
    ):
        self._health = health_registry
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._half_open_probes = half_open_probes
        self._probe_lock_seconds = probe_lock_seconds

    def decide(self, candidate: ModelCandidate) -> CircuitDecision:
        record = self._health.get(candidate.key)

        if record.config_error:
            return CircuitDecision(Decision.SKIP, "CONFIG_ERROR")

        if record.state == CircuitState.CLOSED:
            return CircuitDecision(Decision.ATTEMPT, "CLOSED")

        # OPEN ou HALF_OPEN : la porte de sortie est TOUJOURS le cooldown +
        # le verrou de probe — jamais le libellé `state` seul, qui peut
        # rester HALF_OPEN entre l'acquisition du verrou et l'écriture du
        # résultat par le probe en cours ailleurs.
        now = time.time()
        cooldown_elapsed = bool(record.cooldown_until and now >= record.cooldown_until)
        if not cooldown_elapsed:
            return CircuitDecision(Decision.SKIP, "OPEN_COOLDOWN")

        acquired = self._health.try_acquire_probe_lock(
            candidate.key, self._probe_lock_seconds
        )
        if not acquired:
            # §14 anti-thundering-herd : un autre appelant/process sonde déjà
            # ce candidat en ce moment — on reste sur le fallback, PAS de
            # deuxième appel réseau concurrent vers un candidat encore
            # incertain.
            return CircuitDecision(Decision.SKIP, "PROBE_IN_PROGRESS")

        self._health.transition_to_half_open(candidate)
        return CircuitDecision(Decision.PROBE, "HALF_OPEN_PROBE")

    def report_success(self, candidate: ModelCandidate, latency_ms: float):
        """Retourne le `HealthRecord` résultant — permet à l'appelant (gateway.py)
        de détecter une transition HALF_OPEN→CLOSED pour émettre l'alerte de
        récupération une seule fois."""
        return self._health.record_success(
            candidate, latency_ms=latency_ms, half_open_probes=self._half_open_probes
        )

    def report_failure(self, candidate: ModelCandidate, *, is_timeout: bool):
        """Retourne le `HealthRecord` résultant — permet à l'appelant de
        détecter une transition CLOSED/HALF_OPEN→OPEN (juste ouvert MAINTENANT,
        pas déjà ouvert) pour émettre l'alerte d'ouverture une seule fois."""
        return self._health.record_failure(
            candidate,
            failure_threshold=self._failure_threshold,
            cooldown_seconds=self._cooldown_seconds,
            is_timeout=is_timeout,
        )
