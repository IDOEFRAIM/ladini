"""Déduplication RÉELLE côté serveur MCP (2026-09-03, phase 2 — clôture de
l'audit qui montrait `_idempotency_key` supprimée avant dispatch sans
protection derrière).

## Ce que ce module ferme, et EXACTEMENT jusqu'où

Avant ce module, `AgriDBMCPServer.call_tool` (`infrastructure/mcp/runtime.py`)
retirait `_idempotency_key` sans jamais s'en servir — la clé traversait tout
le transport pour ne rien protéger côté serveur. Ce module lui donne un
VRAI mécanisme de déduplication au niveau requête : même
`(idempotency_key, tool_name)` + même payload (hash) → le résultat déjà
obtenu est REJOUÉ, l'outil n'est PAS ré-exécuté ; même clé + payload
DIFFÉRENT → `IDEMPOTENCY_CONFLICT` explicite, jamais un résultat silencieux.

**Ce que ce module NE ferme PAS** (limite réelle, pas maquillée) : le trou
temporel entre "l'outil a réellement produit son effet externe" et "cette
table a été mise à jour en `COMPLETED`" — si le process meurt DANS cette
fenêtre, la ligne reste `PENDING`. Sans API de lookup par référence externe
côté `create_auction` (aucune colonne `idempotency_key`/`client_request_id`
sur `Auction`, vérifié — voir `domain/orders/models.py`), il est
IMPOSSIBLE de savoir depuis cette table seule si l'effet a eu lieu. C'est
exactement le rôle de `EXECUTION_UNKNOWN` + réconciliation au niveau
`ProcurementDraft` (`domain/procurement_draft.py`) : cette table réduit la
fenêtre de risque (dédup avant exécution, replay après succès) sans la
fermer entièrement pour les outils qui n'ont pas de lookup dédié. Elle ne
prétend PAS résoudre l'exactly-once — voir `IdempotencyOutcome.IN_PROGRESS`
plus bas pour la sémantique honnête de ce cas.

## Schéma — 1 ligne par (idempotency_key, tool_name)

```sql
CREATE TABLE IF NOT EXISTS marketplace.mcp_idempotency_records (
    idempotency_key TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL,              -- PENDING | COMPLETED | FAILED
    external_result JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (idempotency_key, tool_name)
)
```

`UNIQUE(idempotency_key, tool_name)` via la clé primaire composite — la
même clé logique peut légitimement identifier des tentatives sur des outils
DIFFÉRENTS (peu probable en pratique, mais jamais une collision imposée).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from sqlalchemy import text

from ladini.core.database import get_sessionmaker

logger = logging.getLogger("ladini.services.database.mcp_idempotency_store")

_SELECT_SQL = text(
    "SELECT idempotency_key, tool_name, request_hash, status, external_result "
    "FROM marketplace.mcp_idempotency_records "
    "WHERE idempotency_key = :idempotency_key AND tool_name = :tool_name"
)
_INSERT_PENDING_SQL = text(
    "INSERT INTO marketplace.mcp_idempotency_records "
    "(idempotency_key, tool_name, request_hash, status, created_at, updated_at) "
    "VALUES (:idempotency_key, :tool_name, :request_hash, 'PENDING', now(), now()) "
    "ON CONFLICT (idempotency_key, tool_name) DO NOTHING"
)
_UPDATE_STATUS_SQL = text(
    "UPDATE marketplace.mcp_idempotency_records "
    "SET status = :status, external_result = :external_result, updated_at = now() "
    "WHERE idempotency_key = :idempotency_key AND tool_name = :tool_name"
)
# Ré-ouvre une tentative CONFIRMÉE ÉCHOUÉE (pas d'effet externe, mandat §2.5 :
# un FAILED n'a jamais produit l'effet, retenter EST légitime — contrairement
# à PENDING, qui est ambigu par définition) — remet PENDING pour permettre un
# nouvel essai sous la MÊME clé, jamais une nouvelle ligne.
_REOPEN_FAILED_SQL = text(
    "UPDATE marketplace.mcp_idempotency_records "
    "SET status = 'PENDING', external_result = NULL, updated_at = now() "
    "WHERE idempotency_key = :idempotency_key AND tool_name = :tool_name "
    "AND status = 'FAILED' AND request_hash = :request_hash"
)


def _decode_external_result(raw: Any) -> Any:
    """JSONB peut revenir déjà désérialisé (dict) OU en texte brut selon le
    driver/dialecte — même défense que `procurement_draft_store.py::
    _decode_payload`, jamais une hypothèse sur laquelle des deux formes le
    pilote réel choisit."""
    if isinstance(raw, (dict, list)) or raw is None:
        return raw
    if isinstance(raw, (str, bytes)):
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return raw
    return raw


def compute_request_hash(args: dict) -> str:
    """Hash stable du payload (clés triées, `default=str` pour les types non
    JSON natifs) — sert à distinguer une VRAIE relecture (même requête,
    rejouée) d'une réutilisation abusive de la même clé pour une requête
    DIFFÉRENTE (mandat §2.4)."""
    import hashlib

    canonical = json.dumps(args, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class IdempotencyOutcome(str, Enum):
    CLAIMED = "CLAIMED"  # nouvelle tentative — l'appelant DOIT exécuter puis appeler complete()/fail()
    REPLAY = "REPLAY"  # même clé + même payload, déjà COMPLETED — résultat REJOUÉ, ne pas exécuter
    CONFLICT = "CONFLICT"  # même clé, payload DIFFÉRENT — jamais résolu silencieusement
    IN_PROGRESS = "IN_PROGRESS"  # même clé + même payload, encore PENDING — voir limite dans la docstring du module
    UNAVAILABLE = "UNAVAILABLE"  # DB injoignable — l'appelant ne PEUT PAS dédupliquer pour cette tentative


@dataclass(frozen=True)
class IdempotencyClaim:
    outcome: IdempotencyOutcome
    stored_result: Optional[dict] = None


async def claim(idempotency_key: str, tool_name: str, request_hash: str) -> IdempotencyClaim:
    """Tente de réserver `(idempotency_key, tool_name)` pour CETTE tentative.

    `CLAIMED` : l'appelant a gagné — DOIT appeler `complete()` (succès) ou
    `fail()` (échec confirmé, sans effet externe) une fois l'exécution
    terminée. `REPLAY`/`CONFLICT`/`IN_PROGRESS`/`UNAVAILABLE` : l'appelant ne
    doit JAMAIS exécuter l'outil pour cette tentative."""
    try:
        sessionmaker = get_sessionmaker()
        if sessionmaker is None:
            logger.warning("MCP_IDEMPOTENCY_STORE_NO_SESSIONMAKER")
            return IdempotencyClaim(outcome=IdempotencyOutcome.UNAVAILABLE)
        async with sessionmaker() as session:
            inserted = await session.execute(
                _INSERT_PENDING_SQL,
                {
                    "idempotency_key": idempotency_key,
                    "tool_name": tool_name,
                    "request_hash": request_hash,
                },
            )
            if inserted.rowcount == 1:
                await session.commit()
                return IdempotencyClaim(outcome=IdempotencyOutcome.CLAIMED)

            # Ligne déjà existante — inspecte-la pour décider.
            existing = await session.execute(
                _SELECT_SQL, {"idempotency_key": idempotency_key, "tool_name": tool_name}
            )
            row = existing.mappings().first()
            if row is None:
                # Course improbable (supprimée entre l'INSERT et le SELECT) —
                # traité comme injoignable plutôt que de deviner.
                await session.rollback()
                return IdempotencyClaim(outcome=IdempotencyOutcome.UNAVAILABLE)

            if row.request_hash != request_hash:
                await session.rollback()
                return IdempotencyClaim(outcome=IdempotencyOutcome.CONFLICT)

            if row.status == "COMPLETED":
                await session.rollback()
                return IdempotencyClaim(
                    outcome=IdempotencyOutcome.REPLAY,
                    stored_result=_decode_external_result(row.external_result),
                )

            if row.status == "FAILED":
                # Échec confirmé SANS effet externe — retry légitime, réouvre
                # SOUS LA MÊME clé (jamais une nouvelle ligne).
                reopened = await session.execute(
                    _REOPEN_FAILED_SQL,
                    {
                        "idempotency_key": idempotency_key,
                        "tool_name": tool_name,
                        "request_hash": request_hash,
                    },
                )
                await session.commit()
                if reopened.rowcount == 1:
                    return IdempotencyClaim(outcome=IdempotencyOutcome.CLAIMED)
                # Un autre écrivain a gagné la réouverture entre notre lecture
                # et notre écriture — traité comme en cours, jamais une
                # double exécution.
                return IdempotencyClaim(outcome=IdempotencyOutcome.IN_PROGRESS)

            # status == PENDING : une autre tentative (ou une tentative
            # passée jamais finalisée — crash) tient la clé. Voir la
            # docstring du module : ce module NE PEUT PAS, seul, distinguer
            # "en cours ailleurs" de "morte après un crash" — c'est
            # exactement le rôle de la réconciliation au niveau du domaine
            # (ProcurementDraft.EXECUTING/EXECUTION_UNKNOWN), pas de cette
            # table générique.
            await session.rollback()
            return IdempotencyClaim(outcome=IdempotencyOutcome.IN_PROGRESS)
    except Exception:
        logger.exception(
            "MCP_IDEMPOTENCY_CLAIM_ERROR | idempotency_key=%s | tool_name=%s",
            idempotency_key,
            tool_name,
        )
        return IdempotencyClaim(outcome=IdempotencyOutcome.UNAVAILABLE)


@dataclass(frozen=True)
class IdempotencyRecord:
    status: str  # PENDING | COMPLETED | FAILED
    external_result: Optional[Any]


async def peek(idempotency_key: str, tool_name: str) -> Optional[IdempotencyRecord]:
    """Lecture SEULE, sans INSERT — pour la réconciliation (mandat phase 1) :
    inspecte ce que la couche MCP sait déjà d'une tentative, sans jamais
    prétendre en réserver une nouvelle. `None` = aucune ligne connue (soit
    jamais tentée, soit antérieure au câblage de cette table) — voir
    `services/reconciliation/procurement_reconciliation_service.py` pour la
    sémantique honnête de ce cas (jamais confondu avec un échec confirmé)."""
    try:
        sessionmaker = get_sessionmaker()
        if sessionmaker is None:
            logger.warning("MCP_IDEMPOTENCY_STORE_NO_SESSIONMAKER")
            return None
        async with sessionmaker() as session:
            result = await session.execute(
                _SELECT_SQL, {"idempotency_key": idempotency_key, "tool_name": tool_name}
            )
            row = result.mappings().first()
            if row is None:
                return None
            return IdempotencyRecord(
                status=row.status,
                external_result=_decode_external_result(row.external_result),
            )
    except Exception:
        logger.exception(
            "MCP_IDEMPOTENCY_PEEK_ERROR | idempotency_key=%s | tool_name=%s",
            idempotency_key,
            tool_name,
        )
        return None


async def complete(idempotency_key: str, tool_name: str, external_result: Any) -> bool:
    """Marque la tentative CLAIMED comme réussie — `external_result` devient
    ce qu'un `REPLAY` ultérieur renverra TEL QUEL, sans ré-exécuter l'outil."""
    return await _update_status(idempotency_key, tool_name, "COMPLETED", external_result)


async def fail(idempotency_key: str, tool_name: str, error: Optional[str] = None) -> bool:
    """Marque la tentative CLAIMED comme échouée SANS effet externe — un
    retry ultérieur sous la MÊME clé pourra réessayer (voir `claim`,
    branche FAILED)."""
    return await _update_status(
        idempotency_key, tool_name, "FAILED", {"error": error} if error else None
    )


async def _update_status(
    idempotency_key: str, tool_name: str, status: str, external_result: Any
) -> bool:
    try:
        sessionmaker = get_sessionmaker()
        if sessionmaker is None:
            logger.warning("MCP_IDEMPOTENCY_STORE_NO_SESSIONMAKER")
            return False
        async with sessionmaker() as session:
            result = await session.execute(
                _UPDATE_STATUS_SQL,
                {
                    "idempotency_key": idempotency_key,
                    "tool_name": tool_name,
                    "status": status,
                    "external_result": json.dumps(external_result, default=str)
                    if external_result is not None
                    else None,
                },
            )
            await session.commit()
            return result.rowcount == 1
    except Exception:
        logger.exception(
            "MCP_IDEMPOTENCY_UPDATE_ERROR | idempotency_key=%s | tool_name=%s | status=%s",
            idempotency_key,
            tool_name,
            status,
        )
        return False


__all__ = [
    "IdempotencyOutcome",
    "IdempotencyClaim",
    "IdempotencyRecord",
    "compute_request_hash",
    "claim",
    "peek",
    "complete",
    "fail",
]
