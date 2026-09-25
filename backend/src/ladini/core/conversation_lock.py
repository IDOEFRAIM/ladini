"""Sérialisation par conversation (Phase 2 hardening, commit 9, mandat décision E).

Deux messages quasi simultanés sur LA MÊME conversation (deux requêtes webchat concurrentes,
deux workers Celery qui traitent le même numéro WhatsApp presque en même temps) ne doivent
JAMAIS faire tourner deux tours du graphe en parallèle sur le même état — chacun lirait/
écrirait le même Workspace sans jamais voir le patch de l'autre (lost update classique). Décision
produit déjà actée (mandat) : sérialiser avec une ATTENTE BORNÉE, jamais un rejet immédiat du
second message.

Construit sur `core/idempotency.py::claim_once`/`release` (le SET-NX-EX Redis déjà partagé par
tout le reste du moteur pour "at most once") plutôt qu'un nouveau mécanisme : un verrou n'est
qu'un claim qu'on retente jusqu'à une échéance au lieu d'abandonner au premier échec. Redis est
mono-thread côté serveur, donc cette garantie tient sous une VRAIE concurrence (plusieurs
processus/workers), pas seulement en apparence comme le ferait un simple `asyncio.Lock` en
mémoire (qui ne protégerait que les requêtes du MÊME processus)."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from ladini.core.idempotency import claim_once, release

logger = logging.getLogger("ladini.core.conversation_lock")

#: Cadence de réessai — assez fin pour ne pas ajouter de latence perceptible une fois le
#: premier tour terminé, assez grossier pour ne pas marteler Redis.
_POLL_INTERVAL_SECONDS = 0.05


def _lock_key(conversation_id: str) -> str:
    return f"conversation_turn_lock:{conversation_id}"


@asynccontextmanager
async def conversation_turn_lock(
    conversation_id: str, *, timeout_seconds: float
) -> AsyncIterator[bool]:
    """Verrou d'exclusion par conversation, attente BORNÉE à `timeout_seconds` (mandat
    décision E — réutilise le même budget que le timeout d'un tour agent, jamais une
    constante inventée séparément : voir `orchestrator.py::_AGENT_TIMEOUT_SECONDS`, passé par
    l'appelant).

    Cède `True` si le verrou a été acquis, `False` si l'attente a expiré sans jamais l'obtenir
    — dans CE cas, le tour continue quand même (mode dégradé, jamais un blocage indéfini ni un
    message utilisateur silencieusement perdu : même philosophie que `services/database/
    recurring_supply.py::_persist`, "un échec n'est bloquant que pour l'exécution, pas pour la
    conversation"), mais journalise l'événement — un dépassement répété signale un problème
    réel (tour bloqué, Redis indisponible) à surveiller, pas une situation normale.

    Fail-open : si Redis est indisponible, `claim_once` renvoie `True` immédiatement (voir sa
    propre docstring) — ce verrou n'ajoute donc jamais de panne supplémentaire là où l'idempotence
    du reste du moteur est déjà dégradée pour la même raison."""
    key = _lock_key(conversation_id)
    ttl_seconds = max(1, int(timeout_seconds) + 5)
    deadline = time.monotonic() + timeout_seconds
    acquired = claim_once(key, ttl_seconds=ttl_seconds)
    waited = False
    while not acquired and time.monotonic() < deadline:
        waited = True
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        acquired = claim_once(key, ttl_seconds=ttl_seconds)
    if waited and acquired:
        logger.info("CONVERSATION_LOCK_SERIALIZED | conversation=%s", conversation_id)
    elif not acquired:
        logger.warning(
            "CONVERSATION_LOCK_TIMEOUT | conversation=%s | after=%.1fs — "
            "le tour continue en mode dégradé (jamais bloqué indéfiniment)",
            conversation_id,
            timeout_seconds,
        )
    try:
        yield acquired
    finally:
        if acquired:
            release(key)


__all__ = ["conversation_turn_lock"]
