"""Cron : réapprovisionnement des occurrences de besoin récurrent (Phase 3, mandat MONTHLY §17/§18).

Aucune logique métier ici — voir `services/database/recurring_supply.py::replenish_occurrence_windows`
(rejeu idempotent de `_materialize_occurrences`, `ON CONFLICT DO NOTHING` : deux passages qui se
chevauchent ne créent jamais de doublon). Générique à TOUS les types de récurrence (DAILY/WEEKLY_DAYS/
WEEKLY/MONTHLY/ONE_OFF) — ce cron ne connaît aucune valeur de `recurrence_type`, il ne fait que rejouer
la fenêtre `[aujourd'hui, aujourd'hui+OCCURRENCE_WINDOW_DAYS]` pour chaque besoin `ACTIVE`.
"""

from __future__ import annotations

import logging

from ladini.api.celery_app import celery_app
from ladini.workers.runtime import run_async

logger = logging.getLogger("Ladini.Workers.Cron.RecurringNeedOccurrenceReplenishment")


async def _run() -> dict:
    from ladini.services.database.d import AgriDatabaseService

    svc = AgriDatabaseService()
    summary = dict(await svc.replenish_occurrence_windows())
    # No-response (B12) : les occurrences passées restées ouvertes passent EXPIRED (même cron, aucun
    # nouvel ordonnanceur) — avant ce correctif elles masquaient la vraie prochaine occurrence.
    summary.update(await svc.expire_past_occurrences())
    return summary


@celery_app.task(name="workers.recurring_need_occurrence_replenishment", bind=True, max_retries=1)
def run_recurring_need_occurrence_replenishment_cron(self) -> dict:
    try:
        summary: dict = run_async(_run())
        return summary
    except Exception as exc:
        logger.exception("Cron recurring_need_occurrence_replenishment en échec")
        raise self.retry(exc=exc, countdown=30) from exc
