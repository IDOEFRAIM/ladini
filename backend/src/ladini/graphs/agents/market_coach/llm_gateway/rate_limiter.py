"""Rate limiter partagé (Redis) — Incrément G (2026-09-13, "Production LLM
Hardening"), spec §14-18.

## Philosophie (spec §15)

Groq payant en production : le quota réel dépassera très largement le trafic
de Ladini. Ce limiteur n'existe PAS pour brider l'usage normal — il existe
pour absorber un BURST/retry-storm/bug de boucle (spec §15/§18) : `RPM`/
`TPM`/`MAX_INFLIGHT` valent `0` (désactivé) par défaut, JAMAIS une valeur
"Groq production" inventée (spec §52 : les vrais quotas contractuels ne sont
pas codés en dur ici — configurables via env, activés seulement quand
l'opérateur connaît les vraies limites à respecter).

## Fail-open explicite (spec §43)

Une panne Redis pendant une décision de rate limit retombe TOUJOURS sur
"autorisé" (fail-open) — même philosophie que `core/idempotency.py`/
`health_registry.py` déjà dans ce repo : un Redis down ne doit JAMAIS
transformer une panne d'observabilité/protection en panne de service. Le
compromis assumé : en cas de panne Redis, un burst n'est plus amorti — accepté
comme risque mineur face au risque majeur (bloquer TOUT le trafic LLM parce
qu'un mécanisme de PROTECTION est indisponible serait pire que le problème
qu'il protège).

## Dimensionnement (spec §16/§17)

- RPM/TPM : fenêtre glissante simple (compteur `INCR`+`EXPIRE` par minute
  civile, pas un sliding-window précis à la seconde — suffisant pour de la
  protection anti-burst, pas une facturation exacte, spec §16 "ne nécessite
  pas une précision parfaite").
- TPM : réserve `input_tokens + max_tokens` AVANT l'appel (majorant sûr),
  jamais corrigée après coup avec l'usage réel — corriger après reviendrait
  à sur-complexifier une protection anti-burst pour un gain de précision
  qui n'est pas le but ici (spec §16 le permet explicitement : "ne nécessite
  pas une précision parfaite").
- In-flight : compteur `INCR`/`DECR` avec TTL de sécurité (`release()` mal
  appelé — crash process — ne doit jamais laisser le compteur bloqué
  indéfiniment)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger("ladini.llm_gateway.rate_limiter")

_INFLIGHT_TTL_SECONDS = 120  # filet de sécurité si release() n'est jamais appelé


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    reason: str = "OK"


class RateLimiter:
    """Un limiteur par PROVIDER (spec §14 : "plusieurs workers Celery" —
    partagé, jamais un état process-local). `rpm_limit`/`tpm_limit`/
    `max_inflight` à `0` ou `None` désactivent la dimension correspondante
    (spec §15 : jamais de valeur par défaut qui bride le trafic normal)."""

    def __init__(
        self,
        redis_client: Any = None,
        *,
        rpm_limit: int = 0,
        tpm_limit: int = 0,
        max_inflight: int = 0,
    ):
        self._redis = redis_client
        self._rpm_limit = max(0, int(rpm_limit or 0))
        self._tpm_limit = max(0, int(tpm_limit or 0))
        self._max_inflight = max(0, int(max_inflight or 0))

    def _client(self) -> Any:
        if self._redis is not None:
            return self._redis
        from ladini.graphs.agents.market_coach.llm_gateway.redis_client import (
            get_redis,
        )

        return get_redis()

    @property
    def enabled(self) -> bool:
        return bool(self._rpm_limit or self._tpm_limit or self._max_inflight)

    def check_and_reserve(self, provider: str, estimated_tokens: int) -> RateLimitDecision:
        """Décision UNIQUE pour les 3 dimensions — appelée AVANT l'appel
        réseau (spec §16 "réserver le budget avant appel"). Ne lève jamais :
        toute panne Redis retombe en `RateLimitDecision(allowed=True,
        reason="FAIL_OPEN_REDIS_ERROR")` (spec §43)."""
        if not self.enabled:
            return RateLimitDecision(True, "DISABLED")
        try:
            client = self._client()
            now_minute = int(time.time() // 60)

            if self._rpm_limit:
                rpm_key = f"llm:ratelimit:rpm:{provider}:{now_minute}"
                current_rpm = client.incr(rpm_key)
                if current_rpm == 1:
                    client.expire(rpm_key, 120)
                if current_rpm > self._rpm_limit:
                    return RateLimitDecision(False, "RPM_EXCEEDED")

            if self._tpm_limit:
                tpm_key = f"llm:ratelimit:tpm:{provider}:{now_minute}"
                current_tpm = client.incrby(tpm_key, max(0, int(estimated_tokens)))
                if current_tpm == estimated_tokens:
                    client.expire(tpm_key, 120)
                if current_tpm > self._tpm_limit:
                    return RateLimitDecision(False, "TPM_EXCEEDED")

            if self._max_inflight:
                inflight_key = f"llm:ratelimit:inflight:{provider}"
                current_inflight = client.incr(inflight_key)
                client.expire(inflight_key, _INFLIGHT_TTL_SECONDS)
                if current_inflight > self._max_inflight:
                    # Le slot in-flight vient d'être réservé mais refusé —
                    # le décrémenter immédiatement (jamais de fuite pour une
                    # requête qui ne sera finalement pas exécutée).
                    try:
                        client.decr(inflight_key)
                    except Exception:
                        pass
                    return RateLimitDecision(False, "INFLIGHT_LIMIT_EXCEEDED")

            return RateLimitDecision(True, "OK")
        except Exception as exc:
            logger.debug(
                "[rate_limiter] Redis indisponible (%s) — fail-open (spec §43).",
                exc,
            )
            return RateLimitDecision(True, "FAIL_OPEN_REDIS_ERROR")

    def release_inflight(self, provider: str) -> None:
        """À appeler dans un `finally` après l'appel réseau — jamais
        bloquant, jamais levé (même philosophie fail-open que ci-dessus)."""
        if not self._max_inflight:
            return
        try:
            client = self._client()
            inflight_key = f"llm:ratelimit:inflight:{provider}"
            new_value = client.decr(inflight_key)
            if new_value is not None and new_value < 0:
                # Défense contre un double-release/désynchronisation —
                # jamais un compteur négatif qui biaiserait la décision
                # suivante.
                client.set(inflight_key, 0, ex=_INFLIGHT_TTL_SECONDS)
        except Exception as exc:
            logger.debug(
                "[rate_limiter] release_inflight échoué pour %s (%s) — "
                "ignoré, le TTL de sécurité purgera le compteur.",
                provider,
                exc,
            )


def build_rate_limiter(settings: Optional[Any] = None) -> RateLimiter:
    if settings is None:
        from ladini.core.settings import settings as _settings

        settings = _settings
    return RateLimiter(
        rpm_limit=int(getattr(settings, "GROQ_RPM_LIMIT", 0) or 0),
        tpm_limit=int(getattr(settings, "GROQ_TPM_LIMIT", 0) or 0),
        max_inflight=int(getattr(settings, "GROQ_MAX_INFLIGHT", 0) or 0),
    )


__all__ = ["RateLimiter", "RateLimitDecision", "build_rate_limiter"]
