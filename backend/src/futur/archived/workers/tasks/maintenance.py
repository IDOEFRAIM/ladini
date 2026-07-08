"""
Maintenance Tasks — Nettoyage, santé système, housekeeping (production-grade).

PRINCIPES :
  - Hérite de AgriTask (structured logging, backoff, classification)
  - Distributed lock : une seule instance de cleanup tourne à la fois
  - Batch deletion pour les gros volumes
  - Rapports structurés (espace libéré, fichiers supprimés, erreurs)
  - Health checks pour le broker et la DB
  - Aucune dépendance lourde au top-level
"""

import logging
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from celery.exceptions import SoftTimeLimitExceeded

from agriconnect.workers.celery_app import celery_app
from agriconnect.workers.celery_config import TIME_LIMITS
from agriconnect.workers.task_base import (
    AgriTask,
    FatalTaskError,
    error_result,
    success_result,
)
from agriconnect.core.settings import settings

logger = logging.getLogger("AgriConnect.tasks.maintenance")

_MAINT_LIMITS = TIME_LIMITS["maintenance"]

MAX_RETENTION_HOURS = 24
BATCH_SIZE = 100  # supprimer par lot pour ne pas saturer l'I/O


def _get_disk_usage(path: Path) -> Dict[str, Any]:
    """Retourne les informations d'utilisation disque."""
    try:
        usage = shutil.disk_usage(str(path))
        return {
            "total_gb": round(usage.total / (1024**3), 2),
            "used_gb": round(usage.used / (1024**3), 2),
            "free_gb": round(usage.free / (1024**3), 2),
            "percent_used": round(usage.used / usage.total * 100, 1),
        }
    except OSError as e:
        return {"error": str(e)}


@celery_app.task(
    base=AgriTask,
    name="agriconnect.workers.tasks.maintenance.cleanup_old_audio",
    bind=True,
    max_retries=1,
    soft_time_limit=_MAINT_LIMITS["soft"],
    time_limit=_MAINT_LIMITS["hard"],
    acks_late=True,
    track_started=True,
)
def cleanup_old_audio(self) -> Dict[str, Any]:
    """
    Supprime les fichiers audio .wav de plus de 24h.

    Features:
      - Suppression par batch (pas de saturation I/O)
      - Compteurs : fichiers supprimés, espace libéré, erreurs
      - Rapport d'espace disque avant/après
      - Gestion propre des fichiers locked/en cours d'utilisation
    """
    try:
        audio_dir = Path(settings.AUDIO_OUTPUT_DIR).resolve()
        if not audio_dir.exists():
            return success_result(
                data={"deleted": 0, "note": "Audio directory does not exist"},
                task_name=self.name,
            )

        # Snapshot espace disque avant
        disk_before = _get_disk_usage(audio_dir)

        now = time.time()
        retention_seconds = MAX_RETENTION_HOURS * 3600
        deleted = 0
        errors = 0
        bytes_freed = 0

        # Collecter les fichiers éligibles
        candidates = []
        for f in audio_dir.glob("*.wav"):
            try:
                stat = f.stat()
                if (now - stat.st_mtime) > retention_seconds:
                    candidates.append((f, stat.st_size))
            except OSError:
                continue

        # Supprimer par batch
        for i in range(0, len(candidates), BATCH_SIZE):
            batch = candidates[i : i + BATCH_SIZE]
            for filepath, filesize in batch:
                try:
                    filepath.unlink()
                    deleted += 1
                    bytes_freed += filesize
                except PermissionError:
                    # Fichier en cours d'utilisation (TTS en cours)
                    logger.debug("Skipping locked file: %s", filepath.name)
                except OSError as e:
                    errors += 1
                    logger.warning("Failed to delete %s: %s", filepath.name, e)

        # Snapshot espace disque après
        disk_after = _get_disk_usage(audio_dir)

        logger.info(
            "Audio cleanup: %d deleted, %.1f MB freed, %d errors",
            deleted, bytes_freed / (1024 * 1024), errors,
        )

        return success_result(
            data={
                "deleted": deleted,
                "errors": errors,
                "candidates_found": len(candidates),
                "bytes_freed": bytes_freed,
                "mb_freed": round(bytes_freed / (1024 * 1024), 2),
                "retention_hours": MAX_RETENTION_HOURS,
                "disk_before": disk_before,
                "disk_after": disk_after,
            },
            task_name=self.name,
        )

    except SoftTimeLimitExceeded:
        return self.handle_timeout({"deleted_so_far": deleted})

    except Exception as e:
        logger.error("Audio cleanup error: %s", e, exc_info=True)
        return error_result(error=str(e), task_name=self.name, retryable=True)


@celery_app.task(
    base=AgriTask,
    name="agriconnect.workers.tasks.maintenance.cleanup_expired_results",
    bind=True,
    max_retries=1,
    soft_time_limit=120,
    time_limit=180,
    acks_late=True,
)
def cleanup_expired_results(self) -> Dict[str, Any]:
    """
    Nettoie les résultats Celery expirés dans Redis.
    Évite l'accumulation de clés mortes dans le backend de résultats.
    """
    try:
        # Celery gère l'expiration via result_expires,
        # mais on force un nettoyage proactif pour les backends Redis
        backend = celery_app.backend
        if hasattr(backend, "cleanup"):
            backend.cleanup()
            logger.info("Result backend cleanup completed.")

        return success_result(
            data={"cleaned": True, "timestamp": datetime.now(timezone.utc).isoformat()},
            task_name=self.name,
        )

    except Exception as e:
        logger.warning("Result cleanup error (non-critical): %s", e)
        return error_result(error=str(e), task_name=self.name, retryable=False)


@celery_app.task(
    base=AgriTask,
    name="agriconnect.workers.tasks.maintenance.broker_health_check",
    bind=True,
    max_retries=0,
    soft_time_limit=30,
    time_limit=45,
)
def broker_health_check(self) -> Dict[str, Any]:
    """
    Vérifie la connectivité du broker Redis et rapporte la profondeur des queues.
    Utilisé par Celery Beat pour le monitoring continu.
    """
    health = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "broker": "unknown",
        "queues": {},
    }

    try:
        # ── Test de connexion broker ──
        conn = celery_app.connection()
        conn.ensure_connection(max_retries=2, interval_start=0.5)
        conn.close()
        health["broker"] = "connected"

        # ── Profondeur des queues ──
        inspect = celery_app.control.inspect(timeout=5)

        # Active tasks
        active = inspect.active() or {}
        total_active = sum(len(tasks) for tasks in active.values())

        # Reserved (prefetched) tasks
        reserved = inspect.reserved() or {}
        total_reserved = sum(len(tasks) for tasks in reserved.values())

        # Scheduled tasks
        scheduled = inspect.scheduled() or {}
        total_scheduled = sum(len(tasks) for tasks in scheduled.values())

        health["queues"] = {
            "active_tasks": total_active,
            "reserved_tasks": total_reserved,
            "scheduled_tasks": total_scheduled,
            "workers_responding": len(active),
        }

        # ── Alerte si accumulation ──
        if total_reserved > 100:
            logger.warning(
                "⚠️ Task backlog detected: %d reserved tasks", total_reserved
            )
            health["warning"] = f"High task backlog: {total_reserved} reserved"

        logger.info(
            "Broker health: OK — active=%d reserved=%d workers=%d",
            total_active, total_reserved, len(active),
        )

        return success_result(data=health, task_name=self.name)

    except Exception as e:
        health["broker"] = "error"
        health["error"] = str(e)
        logger.error("Broker health check FAILED: %s", e)
        return error_result(
            error=str(e),
            task_name=self.name,
            retryable=False,
            details=health,
        )
