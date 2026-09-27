"""Cron : recalcule les agrégats quotidiens analytics (Phase D) sur une fenêtre glissante.

Idempotent (DELETE+INSERT par jour, verrou consultatif) : un rejeu ou un chevauchement donne le
même résultat. La fenêtre glissante absorbe les événements tardifs (livraison enregistrée
aujourd'hui pour une commande créée il y a quelques jours) ; une correction plus ancienne se
fait à la demande via `DailyMetricsRefresher.recompute_day` / `recompute_range`.
"""

from __future__ import annotations

import logging
import os

from ladini.api.celery_app import celery_app
from ladini.services.analytics.daily_metrics_refresh import recompute_recent
from ladini.workers.runtime import run_async, worker_session

logger = logging.getLogger("Ladini.Workers.Cron.AnalyticsDailyMetrics")

DEFAULT_LOOKBACK_DAYS = 14


async def _run(lookback_days: int) -> dict:
    result = await recompute_recent(worker_session, lookback_days)
    return {"days_recomputed": len(result), "lookback_days": lookback_days}


@celery_app.task(name="workers.analytics_daily_metrics", bind=True, max_retries=1)
def run_analytics_daily_metrics_cron(self, lookback_days: int | None = None) -> dict:
    days = lookback_days or int(os.getenv("ANALYTICS_DAILY_LOOKBACK_DAYS", DEFAULT_LOOKBACK_DAYS))
    try:
        result: dict = run_async(_run(days))
        return result
    except Exception as exc:
        logger.exception("Cron analytics_daily_metrics en échec")
        raise self.retry(exc=exc, countdown=300) from exc
