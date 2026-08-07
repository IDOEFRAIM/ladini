"""Planification Celery Beat des crons d'orchestration proactive.

Données pures (aucun import Celery) → importable par ``celery_app`` sans cycle.
Cadences en secondes ; ``expires`` évite l'empilement si un tick est en retard.
"""
from __future__ import annotations

from typing import Any, Dict

BEAT_SCHEDULE: Dict[str, Dict[str, Any]] = {
    # Engagement enchères : réactif (les producteurs doivent répondre vite).
    "auction-solicitation": {
        "task": "workers.auction_solicitation",
        "schedule": 120.0,           # toutes les 2 min
        "options": {"expires": 110},
    },
    # Livraison de l'outbox : fréquent pour une notif quasi temps réel.
    "outbox-dispatch": {
        "task": "workers.outbox_dispatch",
        "schedule": 30.0,            # toutes les 30 s
        "options": {"expires": 25},
    },
    # Matching de proximité : moins urgent, lot plus large.
    "proximity-matching": {
        "task": "workers.proximity_matching",
        "schedule": 900.0,           # toutes les 15 min
        "options": {"expires": 600},
    },
    # Expiration des paiements escrow (Paydunya) non réglés dans les temps —
    # fréquence modérée, ce n'est pas critique à la minute près (la fenêtre
    # de réservation se compte en heures, PAYDUNYA_PAYMENT_TTL_HOURS).
    "order-payment-expiry": {
        "task": "workers.order_expiry",
        "schedule": 900.0,           # toutes les 15 min
        "options": {"expires": 600},
    },
}
