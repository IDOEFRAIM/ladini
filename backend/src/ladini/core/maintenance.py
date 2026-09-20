"""Pause CONTRÔLÉE des producteurs de tâches Celery (2026-09-20, bascule
Upstash → Valkey) — mécanisme minimal ajouté pour ce chantier : aucun
mécanisme de maintenance n'existait avant (vérifié, aucune référence
`maintenance`/`drain` dans `api/`).

## Le problème que ce module ferme

Pendant une bascule de broker (Celery `REDIS_URL`), il existe sinon une
fenêtre où l'API publie déjà sur le NOUVEAU broker (Valkey) alors que le
worker écoute encore l'ANCIEN (Upstash) — ou l'inverse : le worker a déjà
basculé mais l'API publie encore sur l'ancien broker, et plus aucun worker
ne le consomme. Dans les deux cas, une tâche publiée est perdue ou jamais
traitée, silencieusement.

## Le mécanisme

Un simple fichier drapeau, vérifié par chaque point d'entrée qui PRODUIT
une tâche Celery (`process_agent_task.delay`/`process_paydunya_ipn.delay`,
voir `api/routes/market.py`, `twilio_webhook.py`, `whatsapp_webhook.py`,
`paydunya_webhook.py`) JUSTE AVANT l'enqueue — jamais au début de la
requête (le reste du traitement, ex. persistance GPS synchrone, reste
idempotent et peut s'exécuter sans risque ; seul le PRODUCTEUR est gardé).

`os.path.exists()` : aucune dépendance Redis pour ce check (on ne peut pas
dépendre du système qu'on est justement en train de basculer), coût
négligeable par requête, et togglable INSTANTANÉMENT sans redémarrer aucun
conteneur — voir `scripts/celery_maintenance_mode.sh` (touche/retire ce
fichier DANS le conteneur `api` en cours d'exécution via `docker compose
exec`).

## Ce qui se passe côté appelant (Twilio/WhatsApp/Paydunya/webchat)

Chaque webhook renvoie un statut non-2xx pendant la pause plutôt que de
publier vers un broker dont on n'est pas certain qu'un worker l'écoute
encore. Twilio et Paydunya redélivrent leur webhook en cas d'échec — le
message N'EST PAS perdu, seulement RETARDÉ de quelques minutes. WhatsApp
Cloud API a une politique de re-livraison plus stricte (désabonnement du
webhook possible après un taux d'échec soutenu) — acceptable ici
UNIQUEMENT parce que la fenêtre visée est courte (1-3 minutes, voir
`docs/REDIS_VALKEY_PRODUCTION_CUTOVER_2026-09-20.md` §G) et ponctuelle, pas
un pattern d'échecs soutenu qui déclencherait ce seuil."""

from __future__ import annotations

import os

MAINTENANCE_FLAG_PATH = os.environ.get(
    "MAINTENANCE_FLAG_PATH", "/tmp/ladini_celery_maintenance"
)


def is_celery_producer_paused() -> bool:
    """True si la production de nouvelles tâches Celery doit être refusée
    (bascule de broker en cours). Best-effort : une erreur de lecture du
    système de fichiers (permissions, etc.) est traitée comme "pas en
    pause" — ce mécanisme ne doit jamais, par sa propre panne, bloquer le
    trafic normal."""
    try:
        return os.path.exists(MAINTENANCE_FLAG_PATH)
    except OSError:
        return False


MAINTENANCE_MESSAGE = (
    "Ladini effectue une maintenance technique de quelques minutes. "
    "Veuillez réessayer dans un instant."
)

__all__ = ["is_celery_producer_paused", "MAINTENANCE_FLAG_PATH", "MAINTENANCE_MESSAGE"]
