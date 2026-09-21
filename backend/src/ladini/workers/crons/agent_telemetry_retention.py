"""Cron : rétention de la télémétrie de l'agent (`intelligence.agent_turns` et, en CASCADE, ses appels d'outils/LLM).

Purge BATCHÉE et non bloquante pour la production :
  * lots de `AGENT_MONITORING_BATCH_SIZE` lignes, un COMMIT par lot (verrous très courts) ;
  * `lock_timeout` / `statement_timeout` courts : sous contention, le lot est abandonné plutôt que d'attendre ;
  * pause entre lots et nombre de lots borné par exécution (le reliquat est repris à la prochaine exécution) ;
  * rétention minimale de 1 jour : une valeur ≤ 0 (faute de configuration) ne peut PAS vider la table.

Le schéma (FK ON DELETE CASCADE des tables filles) est défini par Drizzle ; ce module n'exécute que du DML.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from ladini.api.celery_app import celery_app
from ladini.core.settings import settings
from ladini.workers.runtime import run_async, worker_session

logger = logging.getLogger("Ladini.Workers.Cron.AgentTelemetryRetention")

BATCH_SIZE = 2000
MAX_BATCHES_PER_RUN = 100
PAUSE_SECONDS = 0.2

_DELETE_BATCH = text(
    "DELETE FROM intelligence.agent_turns WHERE id IN ("
    "SELECT id FROM intelligence.agent_turns WHERE created_at < :cutoff ORDER BY created_at LIMIT :n)"
)


async def purge_expired(
    *, retention_days: int | None = None, batch_size: int = BATCH_SIZE,
    max_batches: int = MAX_BATCHES_PER_RUN, pause_seconds: float = PAUSE_SECONDS, session_factory=None,
) -> dict:
    days = int(retention_days if retention_days is not None else getattr(settings, "AGENT_MONITORING_RETENTION_DAYS", 30))
    if days < 1:
        logger.warning("AGENT_MONITORING_RETENTION_DAYS=%s invalide (< 1) — purge ignorée par sécurité.", days)
        return {"status": "skipped", "reason": "invalid_retention", "deleted": 0}

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    deleted, batches = 0, 0
    for _ in range(max_batches):
        try:
            async with (session_factory() if session_factory else worker_session()) as session:
                await session.execute(text("SET LOCAL lock_timeout = '2s'"))
                await session.execute(text("SET LOCAL statement_timeout = '20s'"))
                result = await session.execute(_DELETE_BATCH, {"cutoff": cutoff, "n": batch_size})
                await session.commit()
        except Exception as exc:  # contention/timeout : on s'arrête proprement, reprise à la prochaine exécution
            logger.warning("Purge télémétrie interrompue (%s: %s) — reprise au prochain passage.", type(exc).__name__, exc)
            return {"status": "partial", "deleted": deleted, "batches": batches, "error": type(exc).__name__}
        n = int(result.rowcount or 0)
        deleted += n
        batches += 1
        if n < batch_size:
            break
        await asyncio.sleep(pause_seconds)
    status = "done" if batches < max_batches or n < batch_size else "more_pending"
    logger.info("Purge télémétrie : %s tour(s) supprimé(s) en %s lot(s) (rétention %s j).", deleted, batches, days)
    return {"status": status, "deleted": deleted, "batches": batches, "retention_days": days}


@celery_app.task(name="workers.agent_telemetry_retention", bind=True, max_retries=1)
def run_agent_telemetry_retention(self) -> dict:
    try:
        return run_async(purge_expired())
    except Exception as exc:
        logger.exception("Cron agent_telemetry_retention en échec")
        raise self.retry(exc=exc, countdown=300) from exc
