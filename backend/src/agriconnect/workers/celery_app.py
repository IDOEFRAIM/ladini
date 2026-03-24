"""
Celery App — Point d'entrée UNIQUE de l'application Celery.

C'est le SEUL endroit où Celery() est instancié.
Ne JAMAIS redéfinir celery_app ailleurs.

Production Checklist:
  ✅ Configuration externalisée (celery_config.py)
  ✅ Dead Letter Queues pour les tâches échouées
  ✅ Rate limiting par tâche
  ✅ Exponential backoff avec jitter
  ✅ Broker transport robuste (retry, timeout, visibility)
  ✅ Worker signals (startup validation, graceful shutdown)
  ✅ Compression gzip (broker + résultats)
  ✅ Task tracking (Flower-compatible)
  ✅ Beat schedule pour les tâches périodiques
"""

import logging
import os

from celery import Celery
from celery.schedules import crontab
from celery.signals import (
    celeryd_init,
    task_failure,
    task_revoked,
    worker_ready,
    worker_shutting_down,
)

from agriconnect.core.settings import settings
from agriconnect.workers.celery_config import get_celery_config, ENVIRONMENT

logger = logging.getLogger("AgriConnect.celery")


# ===================================================================
# 1. CELERY APP INSTANCE
# ===================================================================

celery_app = Celery(
    "agriconnect",
    broker=settings.celery_broker,
    backend=settings.celery_backend,
)


# ===================================================================
# 2. APPLY CONFIGURATION
# ===================================================================

celery_app.conf.update(get_celery_config())


# ===================================================================
# 3. PERIODIC TASKS (Celery Beat)
# ===================================================================

celery_app.conf.beat_schedule = {
    # ── Traitement continu des actions de matching marketplace (chaque minute) ──
    "marketplace-matching-every-minute": {
        "task": "agriconnect.workers.tasks.marketplace.process_pending_actions",
        "schedule": crontab(minute="*"),
        "options": {
            "queue": "marketplace",
            "expires": 50,
            "priority": 4,
        },
    },

    # ── Monitoring météo toutes les 6 heures ──
    "weather-monitoring-every-6h": {
        "task": "agriconnect.workers.tasks.monitoring.check_weather_alerts",
        "schedule": crontab(hour="*/6", minute=0),
        "options": {
            "queue": "monitoring",
            "expires": 3600,
            "priority": 3,
        },
    },

    # ── Nettoyage audio quotidien à 3h du matin ──
    "cleanup-audio-daily-3am": {
        "task": "agriconnect.workers.tasks.maintenance.cleanup_old_audio",
        "schedule": crontab(hour=3, minute=0),
        "options": {
            "queue": "maintenance",
            "expires": 7200,
            "priority": 9,
        },
    },

    # ── Nettoyage des résultats expirés (quotidien, 4h) ──
    "cleanup-expired-results-daily": {
        "task": "agriconnect.workers.tasks.maintenance.cleanup_expired_results",
        "schedule": crontab(hour=4, minute=0),
        "options": {
            "queue": "maintenance",
            "expires": 7200,
            "priority": 9,
        },
    },

    # ── Health check du broker (5 min en prod, 15 min sinon) ──
    "broker-health-check": {
        "task": "agriconnect.workers.tasks.maintenance.broker_health_check",
        "schedule": crontab(minute="*/5") if ENVIRONMENT == "production" else crontab(minute="*/15"),
        "options": {
            "queue": "maintenance",
            "expires": 120,
            "priority": 1,
        },
    },
}


# ===================================================================
# 4. AUTO-DISCOVER TASKS
# ===================================================================

celery_app.autodiscover_tasks([
    "agriconnect.workers.tasks",
])


# ===================================================================
# 5. WORKER SIGNALS (lifecycle hooks)
# ===================================================================

@celeryd_init.connect
def _on_celeryd_init(sender=None, conf=None, **kwargs):
    """Appelé au tout début du worker, avant le fork."""
    logger.info(
        "🔧 Celery worker initializing — env=%s broker=%s",
        ENVIRONMENT,
        _mask_url(str(conf.broker_url)) if conf else "?",
    )


@worker_ready.connect
def _on_worker_ready(sender=None, **kwargs):
    """Appelé quand le worker est prêt à consommer des tâches."""
    logger.info("✅ Celery worker READY — PID=%s env=%s", os.getpid(), ENVIRONMENT)
    try:
        conn = celery_app.connection()
        conn.ensure_connection(max_retries=3, interval_start=0.5)
        conn.close()
        logger.info("✅ Broker connection verified.")
    except Exception as e:
        logger.critical("❌ Broker connection FAILED at startup: %s", e)


@worker_shutting_down.connect
def _on_worker_shutdown(sig=None, how=None, exitcode=None, **kwargs):
    """Appelé quand le worker reçoit un signal d'arrêt."""
    logger.info(
        "🛑 Celery worker shutting down — signal=%s method=%s exit=%s",
        sig, how, exitcode,
    )


@task_failure.connect
def _on_task_failure(sender=None, task_id=None, exception=None, **kwargs):
    """Appelé quand une tâche échoue après tous les retries."""
    logger.error(
        "💀 Task FAILED permanently — task=%s id=%s error=%s",
        sender.name if sender else "?", task_id, exception,
    )


@task_revoked.connect
def _on_task_revoked(sender=None, request=None, terminated=None, **kwargs):
    """Appelé quand une tâche est révoquée (annulée)."""
    task_id = request.id if request else "?"
    logger.warning("🚫 Task REVOKED — id=%s terminated=%s", task_id, terminated)


# ===================================================================
# 6. HELPERS
# ===================================================================

def _mask_url(url: str) -> str:
    """Masque le mot de passe dans une URL pour les logs."""
    if "@" in url:
        pre, post = url.rsplit("@", 1)
        if ":" in pre:
            scheme_user = pre.rsplit(":", 1)[0]
            return f"{scheme_user}:***@{post}"
    return url


# ===================================================================
# 7. CLI ENTRY POINT
# ===================================================================

if __name__ == "__main__":
    celery_app.start()
