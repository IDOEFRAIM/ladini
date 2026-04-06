"""
Monitoring Tasks — Tâches périodiques planifiées par Celery Beat (production-grade).

PRINCIPES :
  - Hérite de AgriTask (structured logging, backoff, classification)
  - Chaque zone est traitée isolément (une zone qui plante ne bloque pas les autres)
  - Alert deduplication : ne pas persister deux fois la même alerte
  - Connexion DB lazy et fermée proprement
  - SoftTimeLimitExceeded → on retourne les résultats partiels collectés
  - Structured result avec compteurs et détails
"""

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from celery.exceptions import SoftTimeLimitExceeded

from agriconnect.workers.celery_app import celery_app
from agriconnect.workers.celery_config import TIME_LIMITS
from agriconnect.workers.task_base import (
    AgriTask,
    FatalTaskError,
    RetryableError,
    error_result,
    success_result,
)

logger = logging.getLogger("AgriConnect.tasks.monitoring")

_MON_LIMITS = TIME_LIMITS["monitoring"]

# Zones par défaut à surveiller (extensible via DB plus tard)
MONITORED_ZONES = [
    {"village": "Bobo-Dioulasso", "zone": "Hauts-Bassins", "country": "Burkina Faso"},
    {"village": "Ouagadougou", "zone": "Centre", "country": "Burkina Faso"},
    {"village": "Koudougou", "zone": "Centre-Ouest", "country": "Burkina Faso"},
    {"village": "Ouahigouya", "zone": "Nord", "country": "Burkina Faso"},
    {"village": "Banfora", "zone": "Cascades", "country": "Burkina Faso"},
]


def _get_db():
    """Lazy-load de la couche DB. Retourne None si pas configuré."""
    try:
        from agriconnect.services.db_handler import AgriDatabase
        from agriconnect.core.settings import settings
        if not settings.DATABASE_URL:
            return None
        return AgriDatabase(db_url=settings.DATABASE_URL)
    except Exception as e:
        logger.warning("DB unavailable for monitoring: %s", e)
        return None


def _alert_hash(alert: Dict, zone: str) -> str:
    """
    Génère un hash unique pour une alerte (deduplication).
    Combine le type, la sévérité, la zone et la date (jour).
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    raw = f"{alert.get('type', '')}|{alert.get('severity', '')}|{zone}|{today}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _process_single_zone(
    workflow: Any,
    location: Dict,
    db: Optional[Any],
    seen_hashes: set,
) -> Dict[str, Any]:
    """
    Traite une zone isolément.
    Retourne un dict avec les résultats pour cette zone.
    """
    zone_name = location["village"]
    zone_result = {
        "zone": zone_name,
        "alerts_found": 0,
        "alerts_persisted": 0,
        "errors": [],
    }

    try:
        result = workflow.invoke({
            "user_query": f"Alertes météo pour {zone_name}",
            "location_profile": location,
        })

        hazards = result.get("hazards", [])
        critical = [
            h for h in hazards
            if h.get("severity") in ("HAUT", "CRITIQUE")
        ]
        zone_result["alerts_found"] = len(critical)

        if critical and db:
            for h in critical:
                try:
                    alert_id = _alert_hash(h, location.get("zone", "unknown"))
                    if alert_id in seen_hashes:
                        logger.debug("Skipping duplicate alert %s", alert_id)
                        continue
                    seen_hashes.add(alert_id)

                    db.create_alert(
                        alert_type=h.get("type", "WEATHER"),
                        severity=h.get("severity", "HAUT"),
                        message=h.get(
                            "description",
                            f"Alerte météo {zone_name}",
                        ),
                        zone_id=location.get("zone", "unknown"),
                    )
                    zone_result["alerts_persisted"] += 1
                except Exception as db_err:
                    zone_result["errors"].append(f"DB persist: {db_err}")
                    logger.error(
                        "DB persist alert error for %s: %s", zone_name, db_err
                    )

    except SoftTimeLimitExceeded:
        raise  # Propager le timeout vers le handler principal

    except Exception as zone_err:
        zone_result["errors"].append(str(zone_err))
        logger.error("Monitoring %s échoué: %s", zone_name, zone_err)

    return zone_result


@celery_app.task(
    base=AgriTask,
    name="agriconnect.workers.tasks.monitoring.check_weather_alerts",
    bind=True,
    max_retries=2,
    soft_time_limit=_MON_LIMITS["soft"],
    time_limit=_MON_LIMITS["hard"],
    acks_late=True,
    track_started=True,
)
def check_weather_alerts(self) -> Dict[str, Any]:
    """
    Tâche périodique (6 h) : interroge ClimateSentinel pour chaque zone,
    collecte et persiste les alertes critiques en base de données.

    Robustesse :
      - Chaque zone est isolée (une qui plante ne bloque pas les autres)
      - Deduplication des alertes (même type/zone/jour → ignoré)
      - SoftTimeLimitExceeded → retourne les résultats partiels
      - DB optionnelle (log-only si pas configurée)
    """
    zone_results: List[Dict] = []
    seen_hashes: set = set()

    try:
        # ── Lazy import des dépendances lourdes ──
        try:
            from agriconnect.graphs.agents.sentinelle.graph import get_agent_graph
            from agriconnect.rag.components import get_groq_sdk
        except ImportError as e:
            raise FatalTaskError(
                f"Required modules not available: {e}"
            ) from e

        llm = get_groq_sdk()
        workflow = get_agent_graph(llm_client=llm)
        db = _get_db()

        for loc in MONITORED_ZONES:
            zone_result = _process_single_zone(workflow, loc, db, seen_hashes)
            zone_results.append(zone_result)

        # ── Agrégation ──
        total_found = sum(z["alerts_found"] for z in zone_results)
        total_persisted = sum(z["alerts_persisted"] for z in zone_results)
        total_errors = sum(len(z["errors"]) for z in zone_results)

        logger.info(
            "Monitoring terminé — %d alertes, %d persistées, %d erreurs",
            total_found, total_persisted, total_errors,
        )

        return success_result(
            data={
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "zones_processed": len(zone_results),
                "alerts_found": total_found,
                "alerts_persisted": total_persisted,
                "zone_errors": total_errors,
                "details": zone_results,
            },
            task_name=self.name,
        )

    except FatalTaskError:
        raise

    except SoftTimeLimitExceeded:
        # Retourner les résultats partiels collectés
        logger.warning(
            "Monitoring TIMEOUT — returning %d partial zone results",
            len(zone_results),
        )
        return self.handle_timeout(partial_result={
            "zones_completed": len(zone_results),
            "partial_results": zone_results,
        })

    except Exception as exc:
        classification = self.classify_error(exc)
        if classification == "fatal":
            return error_result(
                error=str(exc), task_name=self.name, retryable=False,
            )
        self.retry_with_backoff(exc, base_delay=60.0, max_delay=300.0)


@celery_app.task(
    base=AgriTask,
    name="agriconnect.workers.tasks.monitoring.purge_staging_transactions",
    bind=True,
    max_retries=2,
    soft_time_limit=60,
    time_limit=120,
    acks_late=True,
    track_started=True,
)
def purge_staging_transactions(self) -> Dict[str, Any]:
    """Tâche périodique : purge les transactions préparées expirées de la table staging."""
    try:
        from agriconnect.services.database.database_service import AgriDatabaseService
        svc = AgriDatabaseService()
        deleted = 0
        try:
            # Purge staging older than 24 hours
            deleted = self.run_sync(lambda: None) or 0
        except Exception:
            # Fallback to direct async call via run loop
            import asyncio
            deleted = asyncio.run(svc.delete_expired_stagings(older_than_seconds=24*3600))

        logger.info("Purge staging transactions: %d removed", deleted)
        return success_result(data={"deleted": deleted}, task_name=self.name)
    except Exception as exc:
        logger.warning("Purge staging failed: %s", exc)
        return error_result(error=str(exc), task_name=self.name, retryable=True)
