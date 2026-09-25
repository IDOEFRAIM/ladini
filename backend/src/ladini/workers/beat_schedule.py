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
    # Confirmations de besoin récurrent restées en doute (timeout/crash après envoi) :
    # rejeu SÛR de la même confirmation (garde PostgreSQL), voir
    # `services/reconciliation/recurring_need_reconciliation_service.py`.
    "recurring-need-reconciliation": {
        "task": "workers.recurring_need_reconciliation",
        "schedule": settings.RECURRING_NEED_RECONCILIATION_INTERVAL_SECONDS,
        "options": {
            "expires": max(1.0, settings.RECURRING_NEED_RECONCILIATION_INTERVAL_SECONDS - 30)
        },
    },
    # Réapprovisionnement des occurrences (Phase 3, mandat MONTHLY §17/§18) : la fenêtre
    # `[aujourd'hui, aujourd'hui+OCCURRENCE_WINDOW_DAYS]` n'était matérialisée QU'UNE FOIS, à la
    # création (`services/database/recurring_supply.py::_insert_one_recurring_need`) — le module
    # documentait déjà ce cron comme prévu ("réconciliation Celery Beat qui étendra la fenêtre
    # chaque jour") mais il n'avait jamais été câblé. Générique à TOUS les types de récurrence,
    # aucune branche MONTHLY.
    "recurring-need-occurrence-replenishment": {
        "task": "workers.recurring_need_occurrence_replenishment",
        "schedule": settings.RECURRING_NEED_OCCURRENCE_REPLENISHMENT_INTERVAL_SECONDS,
        "options": {
            "expires": max(1.0, settings.RECURRING_NEED_OCCURRENCE_REPLENISHMENT_INTERVAL_SECONDS - 3600)
        },
    },
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
    # Matching de l'approvisionnement récurrent (Phase 3) : filet de sécurité fréquent — même principe
    # que "proximity-matching" (fenêtre glissante sur les produits récemment publiés/réapprovisionnés),
    # jamais un scan complet des besoins.
    "recurring-match-recent-products": {
        "task": "workers.recurring_match_recent_products",
        "schedule": settings.RECURRING_MATCH_RECENT_PRODUCTS_INTERVAL_SECONDS,
        "options": {
            "expires": max(1.0, settings.RECURRING_MATCH_RECENT_PRODUCTS_INTERVAL_SECONDS - 30)
        },
    },
    # Rematch temporel léger (mandat §13) : occurrences dues bientôt, au cas où un changement de stock
    # serait passé entre deux fenêtres du cron ci-dessus (Beat n'est jamais garanti sans retard).
    "recurring-match-upcoming-occurrences": {
        "task": "workers.recurring_match_upcoming_occurrences",
        "schedule": settings.RECURRING_MATCH_UPCOMING_INTERVAL_SECONDS,
        "options": {
            "expires": max(1.0, settings.RECURRING_MATCH_UPCOMING_INTERVAL_SECONDS - 60)
        },
    },
    # Digest d'approvisionnement (Phase 4) : 1 notification par (acheteur, date), jamais une par
    # besoin — voir `workers/automation/recurring_supply_digest_service.py`. Idempotent par
    # construction (dedupe_key signée) : un tick de plus sur la même date n'envoie rien de plus.
    "recurring-supply-digest": {
        "task": "workers.recurring_supply_digest",
        "schedule": settings.RECURRING_SUPPLY_DIGEST_INTERVAL_SECONDS,
        "options": {
            "expires": max(1.0, settings.RECURRING_SUPPLY_DIGEST_INTERVAL_SECONDS - 300)
        },
    },
}
