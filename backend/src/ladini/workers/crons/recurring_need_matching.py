"""Cron/tâches : matching déterministe des besoins récurrents (Phase 3).

`match_product_against_recurring_needs` est la tâche CIBLÉE du mandat §12 — appelable pour un seul
produit. Ce dépôt n'a aujourd'hui aucun mécanisme de dispatch synchrone depuis la couche
`services/database/*.py` (vérifié : aucun `.delay()`/`apply_async()` n'y existe — les mixins DB
n'importent jamais `celery_app`) ; y en ajouter un romprait la séparation écriture/orchestration déjà
en place partout ailleurs. Le déclenchement « quasi immédiat » est donc assuré, comme
`ProximityMatchingService`, par un cron **fréquent** (`run_recurring_match_recent_products_cron`,
2 min par défaut) qui scanne les produits publiés/réapprovisionnés récemment — même fenêtre glissante
que la proximité, en minutes. `match_product_against_recurring_needs` reste exposée séparément pour
un futur appel plus direct (webhook, tâche dédiée) sans changer sa signature.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from ladini.api.celery_app import celery_app
from ladini.core.settings import settings
from ladini.workers.automation.need_matching_service import NeedMatchingService
from ladini.workers.runtime import run_async, worker_session

logger = logging.getLogger("Ladini.Workers.Cron.RecurringNeedMatching")


async def _match_product(product_id: str) -> dict:
    async with worker_session() as session:
        batch = await NeedMatchingService(session).match_product(product_id, trigger="publication")
    return {"occurrences_examined": batch.occurrences_examined, "reports": [r.as_dict() for r in batch.reports]}


async def _match_recent_products(window_minutes: float) -> dict:
    since = datetime.utcnow() - timedelta(minutes=window_minutes)
    async with worker_session() as session:
        batch = await NeedMatchingService(session).match_recent_products(since=since, trigger="scheduled")
    return {"occurrences_examined": batch.occurrences_examined, "reports": [r.as_dict() for r in batch.reports]}


async def _match_upcoming_occurrences(within_hours: float) -> dict:
    async with worker_session() as session:
        batch = await NeedMatchingService(session).match_upcoming_occurrences(within_hours=int(within_hours), trigger="scheduled")
    return {"occurrences_examined": batch.occurrences_examined, "reports": [r.as_dict() for r in batch.reports]}


@celery_app.task(name="workers.match_product_against_recurring_needs", bind=True, max_retries=2)
def match_product_against_recurring_needs(self, product_id: str) -> dict:
    """Tâche ciblée (mandat §12) : ne rematche que les occurrences de la sous-catégorie de CE produit."""
    try:
        return run_async(_match_product(product_id))
    except Exception as exc:
        logger.exception("match_product_against_recurring_needs en échec | product_id=%s", product_id)
        raise self.retry(exc=exc, countdown=30) from exc


@celery_app.task(name="workers.recurring_match_recent_products", bind=True, max_retries=2)
def run_recurring_match_recent_products_cron(self, window_minutes: float | None = None) -> dict:
    try:
        return run_async(_match_recent_products(window_minutes or settings.RECURRING_MATCH_RECENT_WINDOW_MINUTES))
    except Exception as exc:
        logger.exception("Cron recurring_match_recent_products en échec")
        raise self.retry(exc=exc, countdown=60) from exc


@celery_app.task(name="workers.recurring_match_upcoming_occurrences", bind=True, max_retries=2)
def run_recurring_match_upcoming_occurrences_cron(self, within_hours: float | None = None) -> dict:
    try:
        return run_async(_match_upcoming_occurrences(within_hours or settings.RECURRING_MATCH_UPCOMING_WINDOW_HOURS))
    except Exception as exc:
        logger.exception("Cron recurring_match_upcoming_occurrences en échec")
        raise self.retry(exc=exc, countdown=120) from exc
