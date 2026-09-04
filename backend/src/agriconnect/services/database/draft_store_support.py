"""Primitives MÉCANIQUES partagées par les stores CAS de drafts
transactionnels (`procurement_draft_store.py`, `preorder_draft_store.py`,
et — mandat hardening transverse, 2026-09-03 — bientôt
`sales_publish_draft_store.py`).

## Portée volontairement étroite

Ce module N'EST PAS un ORM générique de draft. Il extrait UNIQUEMENT les 2
fragments qui étaient déjà, mot pour mot, IDENTIQUES entre
`procurement_draft_store.py` et `preorder_draft_store.py` — et qui allaient
redevenir une 3e copie identique pour SALES :

1. `decode_json_payload` — décodage défensif d'une colonne JSONB (dict déjà
   décodé par le driver, str/bytes bruts, ou valeur invalide) — zéro
   dépendance métier.
2. `cas_finalize` — le motif "UPDATE...WHERE version=:expected, si
   rowcount==0 alors un AUTRE écrivain a gagné la course, recharger CE
   QU'IL a écrit plutôt que d'imposer notre propre verdict" — trouvé
   répété IDENTIQUEMENT à 6 endroits lors de l'audit transverse :
   `procurement_reconciliation_service.reconcile_draft`,
   `preorder_reconciliation_service.reconcile_executing_draft`,
   `flows/buyer/preorder_payment.py::_finalize_and_persist`,
   `flows/buyer/preorder_confirmation.py::_execute_and_finalize`,
   `flows/buyer/procurement_confirmation.py::resolve_procurement_confirmation`,
   et `flows/buyer/procurement_execution_finalizer.py::finalize_procurement_execution`
   (les 2 derniers distinguent en plus VERSION_CONFLICT réel vs DB
   injoignable — préservé par l'appelant via une comparaison d'identité
   sur le retour, `cas_finalize` restant neutre sur cette distinction).

Tout le reste (DDL, noms de colonnes dédiées, SQL `SELECT`/`INSERT`,
sémantique des colonnes déduites du domaine) reste ÉCRIT SÉPARÉMENT dans
chaque store — délibérément. Les colonnes dédiées diffèrent réellement
d'un domaine à l'autre (`preorder_drafts.order_id` n'a pas d'équivalent
PROCUREMENT), et un gabarit SQL générique pour les accommoder toutes
exigerait un système de colonnes templatées — exactement le genre de
framework générique que le mandat de hardening interdit de construire "pour
réduire quelques lignes". La duplication du SQL `SELECT`/`INSERT`/CAS-`UPDATE`
lui-même reste donc JUSTIFIÉE, documentée dans chaque store."""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable, Optional, Protocol, Tuple, TypeVar, runtime_checkable

logger = logging.getLogger("agriconnect.services.database.draft_store_support")


def decode_json_payload(raw: Any) -> dict:
    """Décodage défensif d'une colonne JSONB — le driver la renvoie parfois
    déjà comme un `dict` (asyncpg via SQLAlchemy), parfois comme du texte
    brut (selon le chemin de lecture) ; jamais d'exception remontée, un
    payload illisible redevient simplement `{}` (l'appelant retombe alors
    sur les colonnes dédiées `draft_id`/`version`/`status`, jamais un
    crash)."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (str, bytes)):
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return {}
    return {}


@runtime_checkable
class HasDraftVersion(Protocol):
    draft_id: str
    version: int


TDraft = TypeVar("TDraft", bound=HasDraftVersion)


async def cas_finalize(
    *,
    compare_and_swap: Callable[..., Awaitable[bool]],
    load: Callable[[str], Awaitable[Optional[TDraft]]],
    original: TDraft,
    finalized: TDraft,
    log_conflict: Optional[Callable[[], None]] = None,
) -> Tuple[bool, TDraft]:
    """Persiste `finalized` via CAS (`compare_and_swap(draft_id,
    expected_version=original.version, new_draft=finalized)`). Si un autre
    écrivain a déjà gagné la course entre notre lecture et notre écriture
    (`rowcount==0`), recharge CE QUI A RÉELLEMENT été écrit plutôt que
    d'imposer notre propre verdict — idempotence PAR CONSTRUCTION, même
    garantie partout où ce motif apparaissait déjà en triple (réconciliation
    PROCUREMENT, réconciliation PREORDER phase EXECUTING, synchronisation
    paiement PREORDER).

    Retourne `(persisted, final_draft)` — `persisted=True` : cette écriture
    a gagné, `final_draft is finalized`. `persisted=False` : soit un autre
    écrivain a gagné (`final_draft` = ce qu'il a écrit, si relisible), soit
    la DB était injoignable (`final_draft is finalized` par défaut, l'appelant
    reste responsable de journaliser cette dégradation — `log_conflict`,
    optionnel, permet à l'appelant de garder SON propre message de log,
    jamais un message générique qui masquerait le contexte d'origine)."""
    persisted = await compare_and_swap(
        original.draft_id, expected_version=original.version, new_draft=finalized
    )
    if persisted:
        return True, finalized
    if log_conflict is not None:
        log_conflict()
    reloaded = await load(original.draft_id)
    return False, (reloaded if reloaded is not None else finalized)


__all__ = ["decode_json_payload", "cas_finalize", "HasDraftVersion"]
