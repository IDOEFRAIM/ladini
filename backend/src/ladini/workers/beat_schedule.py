"""Planification Celery Beat des crons d'orchestration proactive.

Données pures (aucun import Celery) → importable par ``celery_app`` sans cycle.
Cadences en secondes ; ``expires`` évite l'empilement si un tick est en retard.
"""

from __future__ import annotations

from typing import Any, Dict

from ladini.core.settings import settings

BEAT_SCHEDULE: Dict[str, Dict[str, Any]] = {
    # Engagement enchères : réactif (les producteurs doivent répondre vite).
    "auction-solicitation": {
        "task": "workers.auction_solicitation",
        "schedule": 120.0,  # toutes les 2 min
        "options": {"expires": 110},
    },
    # Livraison de l'outbox : fréquent pour une notif quasi temps réel.
    "outbox-dispatch": {
        "task": "workers.outbox_dispatch",
        "schedule": 30.0,  # toutes les 30 s
        "options": {"expires": 25},
    },
    # Matching de proximité : moins urgent, lot plus large.
    "proximity-matching": {
        "task": "workers.proximity_matching",
        "schedule": 900.0,  # toutes les 15 min
        "options": {"expires": 600},
    },
    # Expiration des paiements escrow (Paydunya) non réglés dans les temps —
    # fréquence modérée, ce n'est pas critique à la minute près (la fenêtre
    # de réservation se compte en heures, PAYDUNYA_PAYMENT_TTL_HOURS).
    "order-payment-expiry": {
        "task": "workers.order_expiry",
        "schedule": 900.0,  # toutes les 15 min
        "options": {"expires": 600},
    },
    # Réconciliation des ProcurementDraft bloqués en EXECUTING (2026-09-03,
    # mandat recovery) — cadence configurable, jamais codée en dur ici (voir
    # `settings.PROCUREMENT_RECONCILIATION_INTERVAL_SECONDS`).
    "procurement-reconciliation": {
        "task": "workers.procurement_reconciliation",
        "schedule": settings.PROCUREMENT_RECONCILIATION_INTERVAL_SECONDS,
        "options": {
            "expires": max(1.0, settings.PROCUREMENT_RECONCILIATION_INTERVAL_SECONDS - 30)
        },
    },
    # Réconciliation des PreorderDraft bloqués en EXECUTING/AWAITING_PAYMENT
    # (2026-09-03, clôture escrow/IPN) — même principe que PROCUREMENT.
    "preorder-reconciliation": {
        "task": "workers.preorder_reconciliation",
        "schedule": settings.PREORDER_RECONCILIATION_INTERVAL_SECONDS,
        "options": {
            "expires": max(1.0, settings.PREORDER_RECONCILIATION_INTERVAL_SECONDS - 30)
        },
    },
    # Réconciliation des SalesPublishDraft bloqués en EXECUTING
    # (2026-09-04, migration SALES) — même principe que PROCUREMENT/PREORDER.
    # Rétention de la télémétrie de l'agent (cockpit /admin/monitoring) : purge batchée, non bloquante,
    # des tours plus vieux que `AGENT_MONITORING_RETENTION_DAYS`. Toutes les 6 h suffit (volume modeste).
    "agent-telemetry-retention": {
        "task": "workers.agent_telemetry_retention",
        "schedule": 21600.0,
        "options": {"expires": 3600},
    },
    "sales-publish-reconciliation": {
        "task": "workers.sales_publish_reconciliation",
        "schedule": settings.SALES_RECONCILIATION_INTERVAL_SECONDS,
        "options": {
            "expires": max(1.0, settings.SALES_RECONCILIATION_INTERVAL_SECONDS - 30)
        },
    },
}
