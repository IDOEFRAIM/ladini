"""Alerting admin — §27/§28/§30 du brief.

`NotificationService` (Protocol) + `WebhookNotifier`, sur le même modèle que
`workers/outbox/channels/base.py::NotificationChannel` déjà présent dans ce
repo (structure similaire, préoccupation différente : celui-ci alerte
l'ADMIN d'un incident LLM, pas l'utilisateur final WhatsApp).

Déterministe (§29) : jamais de LLM impliqué dans la production d'une alerte —
le service fonctionne même si TOUS les LLM sont down. Déduplication
d'incident via Redis (§28) : une seule notification d'OUVERTURE, une seule de
RÉCUPÉRATION par incident, jamais une par requête en échec.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from agriconnect.graphs.agents.market_coach.llm_gateway.redis_client import get_redis

logger = logging.getLogger("agriconnect.llm_gateway.alerting")

_INCIDENT_KEY_PREFIX = "llm:incident:"
_INCIDENT_KEY_TTL_SECONDS = 24 * 3600


@dataclass(frozen=True)
class LLMIncidentAlert:
    """Contenu d'une alerte — §30 : jamais de secret dedans (vérifié : aucun
    champ ici ne porte de clé/token/mot de passe)."""

    incident_type: str  # "PRIMARY_DEGRADED" | "PROVIDER_UNAVAILABLE" | "PROFILE_UNAVAILABLE" | "ALL_PROVIDERS_DOWN" | "MODEL_CONFIG_ERROR"
    severity: str  # "WARNING" | "CRITICAL" | "RECOVERY"
    environment: str
    profile: str
    provider: str
    model: str
    reason: str
    failure_rate: Optional[float] = None
    fallback_selected: Optional[str] = None
    last_successful_call: Optional[str] = None
    p95_latency_ms: Optional[float] = None
    timestamp: Optional[str] = None


class NotificationService(Protocol):
    def send_incident(self, alert: LLMIncidentAlert) -> None: ...


class LogOnlyNotifier:
    """Provider par défaut si `ADMIN_ALERT_WEBHOOK_URL` est vide — log
    structuré ERROR/CRITICAL, jamais un échec silencieux total."""

    def send_incident(self, alert: LLMIncidentAlert) -> None:
        level = logging.CRITICAL if alert.severity == "CRITICAL" else logging.WARNING
        logger.log(
            level,
            "LLM_INCIDENT_ALERT | type=%s severity=%s profile=%s provider=%s "
            "model=%s reason=%s fallback=%s",
            alert.incident_type,
            alert.severity,
            alert.profile,
            alert.provider,
            alert.model,
            alert.reason,
            alert.fallback_selected,
        )


class WebhookNotifier:
    """POST JSON vers un webhook générique — payload compatible Slack
    Incoming Webhook (`{"text": "..."}`) pour fonctionner tel quel sans
    configuration supplémentaire, avec les détails structurés en pièce
    jointe (`attachments`) pour les canaux qui les affichent (Slack)."""

    def __init__(self, webhook_url: str, *, timeout_seconds: float = 5.0):
        self._webhook_url = webhook_url
        self._timeout_seconds = timeout_seconds

    def send_incident(self, alert: LLMIncidentAlert) -> None:
        payload = self._build_payload(alert)
        try:
            import httpx

            httpx.post(self._webhook_url, json=payload, timeout=self._timeout_seconds)
        except Exception as exc:
            # Une alerte qui échoue à partir ne doit JAMAIS remonter — elle
            # dégraderait le tour utilisateur pour un problème d'observabilité.
            logger.warning("[llm_gateway] envoi webhook d'alerte échoué: %s", exc)
        # Toujours logger aussi (le webhook peut être mal configuré sans
        # que personne ne le remarque autrement).
        LogOnlyNotifier().send_incident(alert)

    @staticmethod
    def _build_payload(alert: LLMIncidentAlert) -> dict:
        emoji = "🔴" if alert.severity == "CRITICAL" else (
            "🟢" if alert.severity == "RECOVERY" else "🟠"
        )
        title = f"{emoji} LLM Gateway — {alert.incident_type} ({alert.severity})"
        lines = [
            f"*Profile*: {alert.profile}",
            f"*Provider/Model*: {alert.provider}/{alert.model}",
            f"*Reason*: {alert.reason}",
            f"*Environment*: {alert.environment}",
        ]
        if alert.failure_rate is not None:
            lines.append(f"*Failure rate*: {alert.failure_rate:.0%}")
        if alert.fallback_selected:
            lines.append(f"*Fallback*: {alert.fallback_selected}")
        if alert.p95_latency_ms is not None:
            lines.append(f"*p95*: {alert.p95_latency_ms:.0f}ms")
        if alert.last_successful_call:
            lines.append(f"*Last success*: {alert.last_successful_call}")
        text = title + "\n" + "\n".join(lines)
        return {
            "text": text,
            "attachments": [{"text": json.dumps(_alert_to_dict(alert))}],
        }


def _alert_to_dict(alert: LLMIncidentAlert) -> dict:
    return {k: v for k, v in alert.__dict__.items()}


def build_notifier(settings=None) -> NotificationService:
    if settings is None:
        from agriconnect.core.settings import settings as _settings

        settings = _settings
    webhook_url = (getattr(settings, "ADMIN_ALERT_WEBHOOK_URL", "") or "").strip()
    if webhook_url:
        return WebhookNotifier(webhook_url)
    return LogOnlyNotifier()


class IncidentDeduplicator:
    """État d'incident partagé (Redis) — §28 : une seule notification
    OUVERTURE, une seule RÉCUPÉRATION par incident, quel que soit le nombre
    de workers/requêtes qui détectent le même problème en parallèle."""

    def __init__(self, redis_client=None):
        self._redis = redis_client or get_redis()

    def _key(self, candidate_key: str) -> str:
        return f"{_INCIDENT_KEY_PREFIX}{candidate_key}"

    def should_notify_open(self, candidate_key: str) -> bool:
        """`True` seulement pour le PREMIER appelant qui ouvre cet incident —
        `SET NX` atomique, même pattern que le verrou de probe."""
        acquired = self._redis.set(
            self._key(candidate_key), "open", nx=True, ex=_INCIDENT_KEY_TTL_SECONDS
        )
        return bool(acquired)

    def should_notify_recovery(self, candidate_key: str) -> bool:
        """`True` seulement si un incident était effectivement ouvert (évite
        une notification de "récupération" pour un candidat qui n'a jamais
        été signalé en panne).

        GET puis DELETE (pas `GETDEL`, commande Redis 6.2+ absente de
        certaines instances managées plus anciennes — confirmé en pratique
        sur cette session) : fenêtre de course négligeable pour un simple
        signal de déduplication d'alerte, jamais pour une décision
        métier/financière."""
        key = self._key(candidate_key)
        existed = self._redis.get(key) is not None
        if existed:
            self._redis.delete(key)
        return existed
