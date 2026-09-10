"""`ProcurementDraft` — persistance transactionnelle réelle (2026-09-03,
clôture du gap de persistance identifié précédemment).

## Root cause du gap fermé ici

`ProcurementDraft` vivait UNIQUEMENT dans l'état LangGraph, persisté via
`WorkspaceCheckpointer` en un blob JSONB "dernier écrivain gagne" — aucune
protection par compare-and-swap au niveau ligne, donc aucune garantie
transactionnelle réelle entre deux workers/tours concurrents sur LE MÊME
draft. Ce module ajoute la table `marketplace.procurement_drafts` et
l'unique opération atomique dont dépend tout le reste :
`compare_and_swap` — un vrai `UPDATE ... WHERE version = :expected`,
`rowcount` vérifié, jamais un verrou Python.

## Décision : snapshot versionné, pas event log (mandat §2)

Une SEULE ligne par `draft_id` (l'état COURANT), `version` comme compteur
de verrouillage optimiste — PAS un historique append-only par version.

Pourquoi :
  - `ProcurementDraft`/`apply_domain_action` (domain/procurement_draft.py)
    sont déjà conçus comme une machine à état à instantané UNIQUE — chaque
    version REMPLACE conceptuellement la précédente. Un event log
    dupliquerait ce modèle au lieu de le persister simplement.
  - Le draft est une donnée de TRAVAIL précédant la création réelle de
    l'auction (elle-même déjà durablement persistée par `create_auction`)
    — pas un registre métier à conserver indéfiniment.

Trade-offs acceptés :
  - Pas d'historique des corrections intermédiaires (v1→v2→v3) interrogeable
    en DB — seule la DERNIÈRE version existe en ligne. Le log applicatif
    (`PROCUREMENT_TRACE`) reste la seule trace des versions passées,
    suffisant pour le diagnostic, pas pour un audit formel.
  - Impact recovery : après un crash, on ne peut relire QUE l'état final
    connu (ex: `EXECUTING` figé) — jamais "à quoi ressemblait v2". Jugé
    acceptable : la reconciliation (mandat §9) porte sur l'ISSUE de
    l'exécution, pas sur l'historique des corrections utilisateur.

## Ce que ce module garantit — et ce qu'il NE garantit PAS

`compare_and_swap` protège contre deux ÉCRITURES concurrentes sur le MÊME
`draft_id` au niveau de CETTE table. Il NE PROTÈGE PAS, à lui seul, contre
une incohérence entre cette table et l'état LangGraph
(`state["procurement_draft"]`) : celui-ci est désormais une PROJECTION DE
TRAVAIL, jamais relue comme autoritative — voir
`flows/buyer/procurement_confirmation.py`, qui recharge TOUJOURS depuis
CETTE table avant de décider quoi que ce soit.

## Ce module N'A PAS de test contre une vraie instance Postgres

Ce dépôt n'a aucune infrastructure de test DB réelle aujourd'hui (vérifié :
pas de docker-compose, pas de fixture Postgres, tous les tests DB
existants mockent la session). Les tests de ce module (voir
`tests/architecture/test_procurement_draft_persistence.py`) utilisent un
faux moteur qui reproduit fidèlement la sémantique `UPDATE...WHERE...`
→ `rowcount` de Postgres — ils prouvent que la LOGIQUE Python autour du
CAS est correcte, PAS que le SQL lui-même est valide contre un vrai
moteur. Une validation CI contre une vraie base reste un chantier séparé.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from sqlalchemy import text

from ladini.core.database import get_sessionmaker
from ladini.graphs.agents.market_coach.domain.procurement_draft import (
    ProcurementDraft,
)
from ladini.services.database.draft_store_support import decode_json_payload

logger = logging.getLogger("ladini.services.database.procurement_draft_store")

# Suit la convention établie par `services/database/common.py::SCHEMA_COLUMN_DDL`
# — DDL idempotent, appliqué best-effort au démarrage worker (voir
# `AgriDatabaseService.ensure_performance_indexes`, où ce tuple est inclus).
PROCUREMENT_DRAFT_SCHEMA_DDL = (
    "CREATE TABLE IF NOT EXISTS marketplace.procurement_drafts ("
    "draft_id TEXT PRIMARY KEY, "
    "conversation_id TEXT NOT NULL, "
    "version INTEGER NOT NULL, "
    "status TEXT NOT NULL, "
    "payload JSONB NOT NULL, "
    "created_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
    "updated_at TIMESTAMPTZ NOT NULL DEFAULT now()"
    ")",
    "CREATE INDEX IF NOT EXISTS ix_procurement_drafts_conversation "
    "ON marketplace.procurement_drafts (conversation_id)",
)

_SELECT_SQL = text(
    "SELECT draft_id, version, status, payload "
    "FROM marketplace.procurement_drafts WHERE draft_id = :draft_id"
)
_INSERT_SQL = text(
    "INSERT INTO marketplace.procurement_drafts "
    "(draft_id, conversation_id, version, status, payload, created_at, updated_at) "
    "VALUES (:draft_id, :conversation_id, :version, :status, :payload, now(), now()) "
    "ON CONFLICT (draft_id) DO NOTHING"
)
_CAS_UPDATE_SQL = text(
    "UPDATE marketplace.procurement_drafts "
    "SET version = :new_version, status = :status, payload = :payload, updated_at = now() "
    "WHERE draft_id = :draft_id AND version = :expected_version"
)
# (2026-09-03, mandat §11) : détection des `EXECUTING` bloqués — AUCUN
# nouveau champ `started_at` dupliqué dans le modèle domaine : `updated_at`
# EST déjà "quand cette ligne a changé pour la dernière fois", ce qui, pour
# une ligne ACTUELLEMENT `EXECUTING`, est EXACTEMENT "quand elle est entrée
# dans cet état" (aucune transition n'a eu lieu depuis, par définition —
# `EXECUTING` n'a que des transitions SORTANTES vers un statut terminal,
# jamais vers lui-même). Réutiliser cette colonne évite une 3e vérité
# (domaine + DB + un `started_at` séparé à synchroniser) pour représenter
# la MÊME notion.
_SELECT_STALE_EXECUTING_SQL = text(
    "SELECT draft_id, version, status, payload FROM marketplace.procurement_drafts "
    "WHERE status = 'EXECUTING' AND updated_at < now() - (:older_than_seconds || ' seconds')::interval"
)


def _row_to_draft(row: Any) -> Optional[ProcurementDraft]:
    payload = decode_json_payload(row.payload)
    # Les colonnes dédiées (draft_id/version/status) restent la vérité —
    # le payload JSONB est le reste des champs métier ; fusion défensive
    # au cas où l'un des deux dérive (ne devrait jamais arriver, écrits
    # ensemble dans la même transaction).
    payload["draft_id"] = row.draft_id
    payload["version"] = row.version
    payload["status"] = row.status
    return ProcurementDraft.from_dict(payload)


async def load(draft_id: str) -> Optional[ProcurementDraft]:
    """Lecture AUTORITATIVE — jamais depuis le cache LangGraph. Best-effort :
    ne lève jamais, retourne `None` si la ligne n'existe pas OU si la DB est
    indisponible (l'appelant retombe alors sur `NO_DRAFT`, jamais un crash
    de tour — même discipline que `core/location.py::persist_shared_location`)."""
    try:
        sessionmaker = get_sessionmaker()
        if sessionmaker is None:
            logger.warning("PROCUREMENT_DRAFT_STORE_NO_SESSIONMAKER")
            return None
        async with sessionmaker() as session:
            result = await session.execute(_SELECT_SQL, {"draft_id": draft_id})
            row = result.mappings().first()
            if row is None:
                return None
            return _row_to_draft(row)
    except Exception:
        logger.exception("PROCUREMENT_DRAFT_LOAD_ERROR | draft_id=%s", draft_id)
        return None


async def insert(draft: ProcurementDraft, *, conversation_id: str) -> bool:
    """INSERT initial (v1, ou toute recréation) — best-effort. `False` si la
    ligne existe déjà (le `draft_id` est une UUID fraîche à chaque
    bootstrap, ne devrait jamais arriver) ou si la DB est indisponible."""
    try:
        sessionmaker = get_sessionmaker()
        if sessionmaker is None:
            logger.warning("PROCUREMENT_DRAFT_STORE_NO_SESSIONMAKER")
            return False
        async with sessionmaker() as session:
            result = await session.execute(
                _INSERT_SQL,
                {
                    "draft_id": draft.draft_id,
                    "conversation_id": conversation_id,
                    "version": draft.version,
                    "status": draft.status.value,
                    "payload": json.dumps(draft.to_dict()),
                },
            )
            await session.commit()
            return result.rowcount == 1
    except Exception:
        logger.exception("PROCUREMENT_DRAFT_INSERT_ERROR | draft_id=%s", draft.draft_id)
        return False


async def compare_and_swap(
    draft_id: str, *, expected_version: int, new_draft: ProcurementDraft
) -> bool:
    """LE point d'atomicité transactionnelle (mandat §1) : un vrai
    `UPDATE ... WHERE version = :expected_version`, `rowcount` vérifié —
    jamais un verrou Python. `True` (rowcount==1) : cette écriture a gagné
    la course, `new_draft` est maintenant la version persistée. `False`
    (rowcount==0, DB indisponible, ou toute exception) : soit un autre
    écrivain a déjà avancé la version depuis notre lecture (VERSION_CONFLICT
    réel), soit la DB est injoignable — dans les deux cas, l'appelant NE
    DOIT PAS supposer que l'écriture a eu lieu."""
    try:
        sessionmaker = get_sessionmaker()
        if sessionmaker is None:
            logger.warning("PROCUREMENT_DRAFT_STORE_NO_SESSIONMAKER")
            return False
        async with sessionmaker() as session:
            result = await session.execute(
                _CAS_UPDATE_SQL,
                {
                    "draft_id": draft_id,
                    "expected_version": expected_version,
                    "new_version": new_draft.version,
                    "status": new_draft.status.value,
                    "payload": json.dumps(new_draft.to_dict()),
                },
            )
            await session.commit()
            return result.rowcount == 1
    except Exception:
        logger.exception(
            "PROCUREMENT_DRAFT_CAS_ERROR | draft_id=%s | expected_version=%s",
            draft_id,
            expected_version,
        )
        return False


async def find_stale_executing(*, older_than_seconds: float) -> list[ProcurementDraft]:
    """Requête de RÉCONCILIATION (mandat §11) — les drafts `EXECUTING`
    depuis plus de `older_than_seconds` sans transition (voir la note sur
    `_SELECT_STALE_EXECUTING_SQL` : `updated_at` sert de `started_at`
    implicite pour ce statut). Best-effort : liste vide si la DB est
    indisponible, jamais une exception.

    N'APPLIQUE aucune décision elle-même — un appelant (ex: un futur job
    Celery Beat périodique, PAS câblé cette session, voir le rapport final)
    doit, pour chaque draft retourné, appeler `adapt_mcp_result(None, None)`
    (résultat volontairement ambigu — on ne sait pas ce qui s'est réellement
    passé côté MCP) puis `finalize_after_execution` vers `EXECUTION_UNKNOWN`,
    JAMAIS un retry aveugle de `create_auction` (mandat §9/§21)."""
    try:
        sessionmaker = get_sessionmaker()
        if sessionmaker is None:
            logger.warning("PROCUREMENT_DRAFT_STORE_NO_SESSIONMAKER")
            return []
        async with sessionmaker() as session:
            result = await session.execute(
                _SELECT_STALE_EXECUTING_SQL, {"older_than_seconds": older_than_seconds}
            )
            rows = result.mappings().all()
            return [d for d in (_row_to_draft(row) for row in rows) if d is not None]
    except Exception:
        logger.exception(
            "PROCUREMENT_DRAFT_FIND_STALE_EXECUTING_ERROR | older_than_seconds=%s",
            older_than_seconds,
        )
        return []


__all__ = [
    "PROCUREMENT_DRAFT_SCHEMA_DDL",
    "load",
    "insert",
    "compare_and_swap",
    "find_stale_executing",
]
