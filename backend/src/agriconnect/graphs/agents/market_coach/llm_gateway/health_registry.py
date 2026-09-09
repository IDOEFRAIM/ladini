"""Health Registry — état de santé PARTAGÉ (Redis) par (provider, modèle).

§11 du brief : plusieurs processus (API, workers Celery, MCP) doivent voir le
MÊME état — jamais un `self.health = {}` local. Remplace le `_CircuitBreaker`
process-local existant (`core/get_llm.py`) pour la décision "essayer ce
candidat ou pas", sans toucher à ce dernier (qui continue de protéger, en
plus et en interne, le repli same-provider sur 429/throttling — une
préoccupation différente, plus fine-grained, laissée intacte).

Atomicité : lecture-modification-écriture via `redis.Redis.transaction()`
(WATCH/MULTI/EXEC, retry automatique en cas de concurrence) plutôt qu'un
script Lua — même garantie (aucune mise à jour perdue entre deux workers
concurrents), plus simple à relire/maintenir pour ce volume d'opérations
(quelques mises à jour par appel LLM, pas un chemin chaud à micro-optimiser —
§51 "la sélection doit être rapide", pas "chaque bit compte").
"""

from __future__ import annotations

import json
import logging
import time
from typing import Optional

from agriconnect.graphs.agents.market_coach.llm_gateway.redis_client import get_redis
from agriconnect.graphs.agents.market_coach.llm_gateway.types import (
    CircuitState,
    HealthRecord,
    ModelCandidate,
)

logger = logging.getLogger("agriconnect.llm_gateway.health")

_KEY_PREFIX = "llm:health:"
_PROBE_LOCK_PREFIX = "llm:probe_lock:"
# Housekeeping : une entrée jamais réécrite (candidat abandonné) s'auto-purge
# plutôt que de traîner indéfiniment dans Redis.
_HEALTH_KEY_TTL_SECONDS = 7 * 24 * 3600
_MAX_LATENCY_SAMPLES = 100


def _key(candidate_key: str) -> str:
    return f"{_KEY_PREFIX}{candidate_key}"


def _probe_lock_key(candidate_key: str) -> str:
    return f"{_PROBE_LOCK_PREFIX}{candidate_key}"


class HealthRegistry:
    def __init__(self, redis_client=None):
        self._redis = redis_client or get_redis()

    # ── Lecture ──────────────────────────────────────────────────────
    def get(self, candidate_key: str) -> HealthRecord:
        raw = self._redis.get(_key(candidate_key))
        if not raw:
            return HealthRecord()
        try:
            return HealthRecord.from_dict(json.loads(raw))
        except Exception:
            logger.warning(
                "[llm_gateway] health record corrompu pour %s — reset à CLOSED",
                candidate_key,
            )
            return HealthRecord()

    def is_available(self, candidate: ModelCandidate) -> bool:
        """CLOSED ou cooldown expiré (implicitement HALF_OPEN-eligible) → True.
        OPEN (ou CONFIG_ERROR — même cooldown depuis 2026-09-05, voir
        `mark_config_error`) avec cooldown non expiré → False (skip, aucun
        appel réseau). Voir `HealthRecord.cooldown_elapsed` (incident
        2026-09-09) : un `cooldown_until` absent compte comme écoulé, jamais
        comme un blocage permanent."""
        record = self.get(candidate.key)
        now = time.time()
        if record.config_error:
            return record.cooldown_elapsed(now)
        if record.state != CircuitState.OPEN:
            return True
        return record.cooldown_elapsed(now)

    def percentiles(self, candidate_key: str) -> dict:
        record = self.get(candidate_key)
        samples = sorted(record.recent_latencies_ms)
        if not samples:
            return {"p50": None, "p95": None}
        return {
            "p50": _percentile(samples, 0.50),
            "p95": _percentile(samples, 0.95),
        }

    # ── Écriture (transactionnelle) ─────────────────────────────────
    def record_success(
        self,
        candidate: ModelCandidate,
        *,
        latency_ms: float,
        half_open_probes: int,
    ) -> HealthRecord:
        return self._update(
            candidate.key,
            lambda rec: self._apply_success(rec, latency_ms, half_open_probes),
        )

    def record_failure(
        self,
        candidate: ModelCandidate,
        *,
        failure_threshold: int,
        cooldown_seconds: float,
        is_timeout: bool,
    ) -> HealthRecord:
        return self._update(
            candidate.key,
            lambda rec: self._apply_failure(
                rec, failure_threshold, cooldown_seconds, is_timeout
            ),
        )

    def mark_config_error(
        self,
        candidate: ModelCandidate,
        message: str,
        *,
        cooldown_seconds: float = 30.0,
    ) -> HealthRecord:
        """CONFIG_ERROR (§15) : désactive le candidat sans le compter comme une
        panne transitoire — jamais retenté DANS L'IMMÉDIAT (pas de tempête de
        401/404 répétés).

        `cooldown_seconds` (2026-09-05, incident réel — un token expiré et un
        modèle décommissionné sont restés `config_error=True` en Redis
        pendant des heures, aucun chemin de retour tant que personne ne
        vidait la clé à la main ou n'attendait le TTL de 7 jours) : fixe
        `cooldown_until` comme pour un OPEN ordinaire, pour que
        `CircuitBreaker.decide()` puisse retenter un probe HALF_OPEN une
        fois le cooldown écoulé — RÉUTILISE le mécanisme de probe déjà en
        place (§9 "ne pas reconstruire le disjoncteur"), ne l'invente pas."""

        def _apply(rec: HealthRecord) -> HealthRecord:
            rec.config_error = True
            rec.config_error_message = message[:500]
            rec.state = CircuitState.OPEN
            rec.cooldown_until = time.time() + cooldown_seconds
            return rec

        record = self._update(candidate.key, _apply)
        logger.error(
            "LLM_MODEL_CONFIG_ERROR | candidate=%s | %s", candidate.key, message
        )
        return record

    def _apply_success(
        self, rec: HealthRecord, latency_ms: float, half_open_probes: int
    ) -> HealthRecord:
        rec.total_requests += 1
        rec.consecutive_failures = 0
        rec.consecutive_successes += 1
        rec.last_success_at = time.time()
        rec.recent_latencies_ms.append(latency_ms)
        if len(rec.recent_latencies_ms) > _MAX_LATENCY_SAMPLES:
            rec.recent_latencies_ms = rec.recent_latencies_ms[-_MAX_LATENCY_SAMPLES:]

        if rec.state == CircuitState.HALF_OPEN:
            if rec.consecutive_successes >= max(1, half_open_probes):
                rec.state = CircuitState.CLOSED
                rec.cooldown_until = None
                # Recovery (2026-09-05) : un probe HALF_OPEN réussi après une
                # CONFIG_ERROR (token rotaté, modèle republié...) doit
                # RÉELLEMENT rouvrir le candidat — sans ce reset, `decide()`
                # skiperait à nouveau indéfiniment sur `record.config_error`
                # malgré `state=CLOSED`.
                rec.config_error = False
                rec.config_error_message = None
        elif rec.state == CircuitState.OPEN:
            # Un appel a réussi alors qu'on pensait le circuit OPEN (ex: le
            # cooldown venait d'expirer et ce candidat a été essayé directement) :
            # traiter comme une reprise, fermer le circuit.
            rec.state = CircuitState.CLOSED
            rec.cooldown_until = None
            rec.config_error = False
            rec.config_error_message = None
        return rec

    def _apply_failure(
        self,
        rec: HealthRecord,
        failure_threshold: int,
        cooldown_seconds: float,
        is_timeout: bool,
    ) -> HealthRecord:
        now = time.time()
        rec.total_requests += 1
        rec.total_failures += 1
        rec.consecutive_successes = 0
        rec.consecutive_failures += 1
        rec.last_failure_at = now
        if is_timeout:
            rec.timeout_count += 1

        if rec.state == CircuitState.HALF_OPEN:
            # Le probe a échoué — retour direct en OPEN, nouveau cooldown.
            rec.state = CircuitState.OPEN
            rec.cooldown_until = now + cooldown_seconds
            logger.warning(
                "LLM_CIRCUIT_OPEN | probe HALF_OPEN échoué — cooldown %.0fs",
                cooldown_seconds,
            )
        elif rec.consecutive_failures >= failure_threshold:
            already_open = rec.state == CircuitState.OPEN
            rec.state = CircuitState.OPEN
            rec.cooldown_until = now + cooldown_seconds
            if not already_open:
                logger.warning(
                    "LLM_CIRCUIT_OPEN | %d échecs consécutifs — cooldown %.0fs",
                    rec.consecutive_failures,
                    cooldown_seconds,
                )
        return rec

    def _update(self, candidate_key: str, mutate) -> HealthRecord:
        key = _key(candidate_key)
        redis_client = self._redis

        result: dict = {}

        def _txn(pipe):
            raw = pipe.get(key)
            try:
                record = HealthRecord.from_dict(json.loads(raw)) if raw else HealthRecord()
            except Exception:
                record = HealthRecord()
            mutated = mutate(record)
            pipe.multi()
            pipe.set(key, json.dumps(mutated.to_dict()), ex=_HEALTH_KEY_TTL_SECONDS)
            result["record"] = mutated

        try:
            redis_client.transaction(_txn, key)
        except Exception as exc:
            logger.warning(
                "[llm_gateway] échec transaction Redis pour %s (%s) — état non "
                "persisté pour cet appel, la décision reste best-effort.",
                candidate_key,
                exc,
            )
            return mutate(self.get(candidate_key))
        return result.get("record") or self.get(candidate_key)

    # ── Verrou de probe (anti-thundering-herd, §14) ─────────────────
    def try_acquire_probe_lock(self, candidate_key: str, ttl_seconds: float) -> bool:
        """`SET key 1 NX EX ttl` — même pattern que le verrou d'idempotence
        déjà utilisé pour les webhooks WhatsApp/Twilio dans ce repo. Un seul
        appelant obtient `True` ; tous les autres (même instant, même
        candidat) reçoivent `False` et doivent utiliser le fallback plutôt
        que de probe eux aussi."""
        try:
            acquired = redis_client_set_nx(
                self._redis, _probe_lock_key(candidate_key), ttl_seconds
            )
            return bool(acquired)
        except Exception as exc:
            logger.debug(
                "[llm_gateway] verrou de probe indisponible pour %s (%s) — "
                "on suppose qu'il n'est PAS acquis (fail-safe : évite un "
                "thundering herd plutôt qu'un probe manqué).",
                candidate_key,
                exc,
            )
            return False

    def transition_to_half_open(self, candidate: ModelCandidate) -> None:
        """Appelé UNIQUEMENT par l'appelant qui a obtenu le verrou de probe —
        marque l'intention de probe avant l'appel réseau lui-même, pour que
        `is_available()` reste cohérent pendant toute la durée du probe."""

        def _apply(rec: HealthRecord) -> HealthRecord:
            rec.state = CircuitState.HALF_OPEN
            rec.consecutive_successes = 0
            return rec

        self._update(candidate.key, _apply)
        logger.info("LLM_CIRCUIT_HALF_OPEN | candidate=%s — probe unique en cours", candidate.key)


def redis_client_set_nx(redis_client, key: str, ttl_seconds: float) -> bool:
    return bool(redis_client.set(key, "1", nx=True, ex=max(1, int(ttl_seconds))))


def _percentile(sorted_samples: list, pct: float) -> float:
    if not sorted_samples:
        return 0.0
    idx = min(len(sorted_samples) - 1, int(round(pct * (len(sorted_samples) - 1))))
    return sorted_samples[idx]
