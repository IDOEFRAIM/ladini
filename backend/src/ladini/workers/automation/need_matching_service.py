"""NeedMatchingService — Phase 3 : « quelles offres de stock peuvent couvrir une occurrence de besoin
récurrent maintenant ? ». Responsabilité UNIQUE (mandat §1) : ne fait ni notification, ni commande,
ni décrément de stock — voir `domain/recurring_supply/matching.py::allocate` pour l'algorithme pur.

Même contrat que `ProximityMatchingService` (mandat §1) : logique pure côté persistance, idempotente,
n'écrit que `need_allocations` + `recurring_need_occurrences` (jamais `products`/`market_offers`).
Diffère volontairement sur la mécanique de concurrence : `ProximityMatchingService` n'a pas besoin de
verrou (upsert idempotent sur `solicitations`, jamais de recomposition d'un total) ; ici, plusieurs
événements peuvent recalculer la MÊME occurrence en même temps (mandat §11) — un
``SELECT ... FOR UPDATE`` sur la ligne d'occurrence est donc le point d'atomicité.

## Buyer zone (mandat §4)

`buyer_profiles` n'a pas de `zone_id` propre — mais `auth.users.zone_id` existe déjà et sert DÉJÀ à
cet usage exact ailleurs (`services/database/auction.py::create_auction`,
`target_zone_id = user_obj.zone_id`). Aucune migration n'est nécessaire : la zone de l'acheteur est
`users.zone_id` via `buyer_profiles.user_id`, résolue en une jointure supplémentaire bornée.

## Stock jamais consommé (mandat §7)

Ce module ne fait AUCUN `UPDATE` sur `products.quantity_for_sale`. Une allocation est une
PROPOSITION, jamais une réservation.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ladini.domain.analytics.business_events import BusinessEventName
from ladini.domain.analytics.emitter import BusinessEventEmitter
from ladini.domain.analytics.metric_dictionary import Journey
from ladini.domain.models import NeedAllocation
from ladini.domain.recurring_supply.matching import (
    Allocation,
    MatchCandidate,
    allocate_single_source,
)

logger = logging.getLogger("ladini.workers.automation.need_matching")

# Occurrences encore éligibles à un (re)matching — jamais une occurrence qui a quitté ce couple
# (mandat §18 « occurrence non éligible ») : SKIPPED/ACCEPTED/PARTIALLY_ACCEPTED/REJECTED/EXPIRED/
# FULFILLED/PARTIALLY_FULFILLED/UNFULFILLED/CANCELLED ne sont JAMAIS modifiées par ce service.
_MATCHABLE_OCCURRENCE_STATUSES = ("OPEN", "MATCHED")


@dataclass
class NeedMatchReport:
    """Une entrée par occurrence traitée — journalisée telle quelle (mandat §16)."""

    occurrence_id: str
    trigger: str
    sub_category_id: Optional[str] = None
    candidate_count: int = 0
    allocation_count: int = 0
    requested_quantity: float = 0.0
    matched_quantity: float = 0.0
    changed: bool = False
    skipped_reason: Optional[str] = None
    error: Optional[str] = None
    duration_ms: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class NeedMatchBatchReport:
    trigger: str
    occurrences_examined: int = 0
    reports: List[NeedMatchReport] = field(default_factory=list)


_OCCURRENCE_FOR_UPDATE_SQL = text(
    "SELECT o.id, o.status, o.requested_quantity, o.unit, o.version, o.occurrence_date, "
    "n.id AS need_id, n.status AS need_status, n.sub_category_id, n.max_price_per_unit, n.buyer_id "
    "FROM marketplace.recurring_need_occurrences o "
    "JOIN marketplace.recurring_needs n ON n.id = o.recurring_need_id "
    "WHERE o.id = :occurrence_id "
    "FOR UPDATE OF o"
)

# ── Fiabilité producteur (mandat matching pilote, 2026-09-23) ──────────────
# Aucune nouvelle table/colonne : réutilise `Order.status`/`Order.cancellation_role` (déjà écrits
# par `ProducerMgmtMixin.confirm_order_by_producer`/`cancel_confirmed_order`, voir `services/
# database/producer.py`) et `Order.order_type` (posé par `accept_match_proposal`, VS4). Un producteur
# n'a AUCUN historique `RECURRING_SUPPLY` -> `resolved=0` -> traité comme "signal inconnu" par
# `_load_reliability`, jamais comme "0% fiable" (mandat : pas de fausse précision).
_RELIABILITY_SQL = text(
    "SELECT p.producer_id AS producer_id, "
    "count(*) FILTER (WHERE o.status = 'CANCELLED' AND o.cancellation_role = 'PRODUCER') AS producer_cancelled, "
    "count(*) FILTER (WHERE o.status IN ('CONFIRMED', 'CANCELLED')) AS resolved "
    "FROM marketplace.orders o "
    "JOIN marketplace.order_items oi ON oi.order_id = o.id "
    "JOIN marketplace.products p ON p.id = oi.product_id "
    "WHERE o.order_type = 'RECURRING_SUPPLY' AND p.producer_id = ANY(:producer_ids) "
    "GROUP BY p.producer_id"
)
# En dessous de ce nombre de commandes RÉSOLUES (honorée ou annulée), un ratio serait bruité par un
# seul incident/coup de chance — mandat : "ne crée pas une fausse précision" plutôt qu'un seuil
# scientifique. Valeur délibérément basse (pilote, peu de volume) ; ajustable sans migration.
_MIN_RELIABILITY_SAMPLE = 3

_BUYER_ZONE_SQL = text(
    "SELECT u.zone_id FROM marketplace.buyer_profiles bp "
    "JOIN auth.users u ON u.id = bp.user_id WHERE bp.id = :buyer_id"
)

# Une seule requête pour TOUS les candidats d'une sous-catégorie (mandat §14 : pas de N+1 — le
# nombre de producteurs n'ajoute jamais de round-trip).
_CANDIDATES_SQL = text(
    "SELECT p.id AS product_id, p.producer_id, p.unit, p.price, p.quantity_for_sale, pr.zone_id "
    "FROM marketplace.products p "
    "JOIN marketplace.producers pr ON pr.id = p.producer_id "
    "WHERE p.sub_category_id = :sub_category_id AND p.is_available = true AND p.quantity_for_sale > 0"
)

_ACTIVE_ALLOCATIONS_SQL = text(
    "SELECT producer_id, product_id, quantity, unit_price FROM marketplace.need_allocations "
    "WHERE occurrence_id = :occurrence_id AND status = 'PROPOSED' ORDER BY producer_id, product_id"
)

# Deux tableaux parallèles (producteurs / produits) plutôt qu'un tableau de lignes composites : plus
# portable et sans ambiguïté de binding entre pilotes SQL — `unnest` sur deux `uuid[]` est un idiome
# PostgreSQL standard pour comparer une liste de paires.
_EXPIRE_STALE_SQL = text(
    "UPDATE marketplace.need_allocations na SET status = 'EXPIRED', updated_at = now() "
    "WHERE na.occurrence_id = :occurrence_id AND na.status = 'PROPOSED' "
    "AND NOT EXISTS ("
    "  SELECT 1 FROM unnest(CAST(:kept_producer_ids AS uuid[]), CAST(:kept_product_ids AS uuid[])) AS k(producer_id, product_id) "
    "  WHERE k.producer_id = na.producer_id AND k.product_id = na.product_id"
    ")"
)

_UPDATE_OCCURRENCE_SQL = text(
    "UPDATE marketplace.recurring_need_occurrences "
    "SET status = :status, quantity_matched = :quantity_matched, version = version + 1, updated_at = now() "
    "WHERE id = :occurrence_id"
)

# Occurrences pertinentes pour une sous-catégorie donnée — index (sub_category_id,status) côté
# recurring_needs, (status,occurrence_date) côté occurrences (Phase 1) : pas de scan complet.
_OCCURRENCES_FOR_SUBCATEGORY_SQL = text(
    "SELECT o.id FROM marketplace.recurring_need_occurrences o "
    "JOIN marketplace.recurring_needs n ON n.id = o.recurring_need_id "
    "WHERE n.sub_category_id = :sub_category_id AND n.status = 'ACTIVE' "
    "AND o.status = ANY(:statuses) AND o.occurrence_date >= :from_date "
    "ORDER BY o.id"
)

_RECENT_PRODUCTS_SQL = text(
    "SELECT DISTINCT sub_category_id FROM marketplace.products "
    "WHERE is_available = true AND quantity_for_sale > 0 "
    "AND updated_at >= :since AND sub_category_id IS NOT NULL"
)

_UPCOMING_OCCURRENCES_SQL = text(
    "SELECT o.id FROM marketplace.recurring_need_occurrences o "
    "JOIN marketplace.recurring_needs n ON n.id = o.recurring_need_id "
    "WHERE n.status = 'ACTIVE' AND o.status = ANY(:statuses) "
    "AND o.occurrence_date >= :from_date AND o.occurrence_date <= :to_date "
    "ORDER BY o.id"
)


class NeedMatchingService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ─── UNITÉ DE TRAVAIL : une occurrence, verrouillée ────────────────

    async def rematch_occurrence(self, occurrence_id: Any, *, trigger: str = "manual") -> NeedMatchReport:
        started = time.monotonic()
        report = NeedMatchReport(occurrence_id=str(occurrence_id), trigger=trigger)
        logger.info("recurring_match.started | occurrence_id=%s | trigger=%s", occurrence_id, trigger)
        try:
            row = (await self.session.execute(_OCCURRENCE_FOR_UPDATE_SQL, {"occurrence_id": occurrence_id})).mappings().first()
            if row is None:
                report.skipped_reason = "occurrence_not_found"
                return report
            report.sub_category_id = str(row["sub_category_id"])
            report.requested_quantity = float(row["requested_quantity"])

            if row["need_status"] != "ACTIVE" or row["status"] not in _MATCHABLE_OCCURRENCE_STATUSES:
                report.skipped_reason = f"not_matchable(need={row['need_status']},occ={row['status']})"
                return report

            buyer_zone = await self.session.scalar(_BUYER_ZONE_SQL, {"buyer_id": row["buyer_id"]})

            candidate_rows = (
                await self.session.execute(_CANDIDATES_SQL, {"sub_category_id": row["sub_category_id"]})
            ).mappings().all()
            report.candidate_count = len(candidate_rows)
            reliability = await self._load_reliability({str(c["producer_id"]) for c in candidate_rows})
            candidates = [
                MatchCandidate(
                    producer_id=str(c["producer_id"]),
                    product_id=str(c["product_id"]),
                    unit=c["unit"],
                    unit_price=float(c["price"] or 0),
                    available_quantity=float(c["quantity_for_sale"]),
                    same_zone=bool(buyer_zone) and c["zone_id"] == buyer_zone,
                    reliability=reliability.get(str(c["producer_id"])),
                )
                for c in candidate_rows
            ]

            # Chemin nominal pilote (mandat matching, 2026-09-23) : source UNIQUE, jamais une
            # combinaison automatique — voir `domain/recurring_supply/matching.py::
            # allocate_single_source` pour l'ordre exact (fournisseur unique > fiabilité > prix >
            # zone > partiel individuel). L'ancien `allocate()` glouton multi-source N'EST PAS
            # supprimé (toujours exporté par ce module) — simplement plus appelé ICI.
            new_allocations = allocate_single_source(
                requested_quantity=float(row["requested_quantity"]),
                unit=row["unit"],
                candidates=candidates,
                max_price_per_unit=(float(row["max_price_per_unit"]) if row["max_price_per_unit"] is not None else None),
            )
            report.allocation_count = len(new_allocations)

            changed = await self._persist_allocations(occurrence_id, row, new_allocations, buyer_zone=buyer_zone)
            report.changed = changed
            report.matched_quantity = sum(a.quantity for a in new_allocations)

            if not new_allocations and report.candidate_count == 0:
                logger.info("recurring_match.no_candidate | occurrence_id=%s | sub_category_id=%s", occurrence_id, row["sub_category_id"])

            await self.session.commit()
            logger.info(
                "recurring_match.completed | %s",
                report.as_dict(),
            )
            if changed:
                logger.info("recurring_match.changed | occurrence_id=%s | matched_quantity=%s", occurrence_id, report.matched_quantity)
            return report
        except Exception as exc:  # noqa: BLE001 — best-effort, jamais de tour/tâche cassée par le matching
            await self.session.rollback()
            report.error = str(exc)
            logger.exception("recurring_match.error | occurrence_id=%s | trigger=%s", occurrence_id, trigger)
            return report
        finally:
            report.duration_ms = int((time.monotonic() - started) * 1000)

    async def _load_reliability(self, producer_ids: set) -> Dict[str, Optional[float]]:
        """Une requête bornée (mandat §14 : jamais un round-trip par candidat) — `{producer_id:
        ratio}` pour les seuls producteurs candidats de CETTE occurrence. Absent du résultat ou
        échantillon `< _MIN_RELIABILITY_SAMPLE` -> `None` (signal inconnu, jamais 0.0 : un producteur
        neuf n'est pas "peu fiable", il n'a simplement pas encore d'historique)."""
        if not producer_ids:
            return {}
        rows = (
            await self.session.execute(_RELIABILITY_SQL, {"producer_ids": list(producer_ids)})
        ).mappings().all()
        result: Dict[str, Optional[float]] = {}
        for r in rows:
            resolved = int(r["resolved"] or 0)
            if resolved < _MIN_RELIABILITY_SAMPLE:
                continue
            cancelled = int(r["producer_cancelled"] or 0)
            result[str(r["producer_id"])] = (resolved - cancelled) / resolved
        return result

    async def _persist_allocations(
        self, occurrence_id: Any, occ_row: Any, new_allocations: Sequence[Allocation], *, buyer_zone: Any = None
    ) -> bool:
        """Upsert idempotent + expiration des allocations devenues invalides (mandat §8) ; ne bump
        `version` QUE si l'ensemble des allocations ACTIVES a réellement changé (mandat §10)."""
        before = (await self.session.execute(_ACTIVE_ALLOCATIONS_SQL, {"occurrence_id": occurrence_id})).all()
        before_signature = {(str(p), str(pr), float(q), float(up)) for p, pr, q, up in before}

        now = datetime.utcnow()
        if new_allocations:
            rows = [
                {
                    "id": uuid.uuid4(),
                    "occurrence_id": occurrence_id,
                    "producer_id": a.producer_id,
                    "product_id": a.product_id,
                    "quantity": a.quantity,
                    "unit_price": a.unit_price,
                    "unit": a.unit,
                    "status": "PROPOSED",
                    "created_at": now,
                    "updated_at": now,
                }
                for a in new_allocations
            ]
            stmt = pg_insert(NeedAllocation).values(rows)
            stmt = stmt.on_conflict_do_update(
                index_elements=["occurrence_id", "producer_id", "product_id"],
                set_={
                    "quantity": stmt.excluded.quantity,
                    "unit_price": stmt.excluded.unit_price,
                    "unit": stmt.excluded.unit,
                    "status": "PROPOSED",
                    "updated_at": now,
                },
            )
            returning_stmt = stmt.returning(
                NeedAllocation.id,
                NeedAllocation.producer_id,
                NeedAllocation.product_id,
                NeedAllocation.quantity,
                NeedAllocation.unit_price,
                NeedAllocation.unit,
            )
            persisted = (await self.session.execute(returning_stmt)).all()

            # Analytics Phase C : 1 event = 1 allocation matérialisée. La clé suit l'`id` de la ligne
            # `need_allocations` — stable à travers les rematchs, car l'upsert
            # (occurrence, producteur, produit) CONSERVE l'id existant (le `uuid4()` proposé dans
            # `rows` n'est retenu qu'à l'insertion réelle). Un retry/rematch identique => même clé
            # => ON CONFLICT DO NOTHING ; une allocation qui change de quantité/prix garde son
            # event initial (le fait "match trouvé" ne se répète pas). Émis seulement si l'ensemble
            # actif a réellement changé, pour ne pas ajouter d'écritures à un rematch inerte.
            if before_signature != {
                (a.producer_id, a.product_id, float(a.quantity), float(a.unit_price)) for a in new_allocations
            }:
                emitter = BusinessEventEmitter(self.session)
                for alloc_id, producer_id, product_id, quantity, unit_price, unit in persisted:
                    await emitter.emit(
                        event_name=BusinessEventName.RECURRING_MATCH_FOUND,
                        journey=Journey.RECURRING,
                        actor_type="SYSTEM",
                        buyer_id=occ_row["buyer_id"],
                        producer_id=producer_id,
                        zone_id=buyer_zone,
                        entity_type="NEED_ALLOCATION",
                        entity_id=alloc_id,
                        idempotency_key=f"RECURRING_MATCH_FOUND:{alloc_id}",
                        sub_category_id=occ_row["sub_category_id"],
                        quantity=float(quantity),
                        unit=unit,
                        amount=float(quantity) * float(unit_price),
                        metadata={
                            "occurrence_id": str(occurrence_id),
                            "recurring_need_id": str(occ_row["need_id"]),
                            "product_id": str(product_id),
                            "unit_price": float(unit_price),
                        },
                    )
            kept_producer_ids = [a.producer_id for a in new_allocations]
            kept_product_ids = [a.product_id for a in new_allocations]
        else:
            kept_producer_ids, kept_product_ids = [], []

        await self.session.execute(
            _EXPIRE_STALE_SQL,
            {
                "occurrence_id": occurrence_id,
                "kept_producer_ids": kept_producer_ids,
                "kept_product_ids": kept_product_ids,
            },
        )

        after_quantity = sum(a.quantity for a in new_allocations)
        after_signature = {(a.producer_id, a.product_id, float(a.quantity), float(a.unit_price)) for a in new_allocations}
        changed = before_signature != after_signature

        if changed:
            requested = float(occ_row["requested_quantity"])
            new_status = "MATCHED" if after_quantity >= requested and requested > 0 else "OPEN"
            await self.session.execute(
                _UPDATE_OCCURRENCE_SQL,
                {"occurrence_id": occurrence_id, "status": new_status, "quantity_matched": after_quantity},
            )
        return changed

    # ─── DÉCLENCHEMENT ÉVÉNEMENTIEL : un produit devient disponible ────

    async def match_product(self, product_id: Any, *, trigger: str = "publication") -> NeedMatchBatchReport:
        sub_category_id = await self.session.scalar(
            text("SELECT sub_category_id FROM marketplace.products WHERE id = :id"), {"id": product_id}
        )
        if sub_category_id is None:
            return NeedMatchBatchReport(trigger=trigger)
        return await self._match_sub_category(sub_category_id, trigger=trigger)

    async def _match_sub_category(self, sub_category_id: Any, *, trigger: str, from_date: Optional[date] = None) -> NeedMatchBatchReport:
        occ_ids = (
            await self.session.execute(
                _OCCURRENCES_FOR_SUBCATEGORY_SQL,
                {
                    "sub_category_id": sub_category_id,
                    "statuses": list(_MATCHABLE_OCCURRENCE_STATUSES),
                    "from_date": datetime.combine(from_date or _today(), datetime.min.time()),
                },
            )
        ).scalars().all()
        batch = NeedMatchBatchReport(trigger=trigger, occurrences_examined=len(occ_ids))
        for occ_id in occ_ids:
            batch.reports.append(await self.rematch_occurrence(occ_id, trigger=trigger))
        return batch

    # ─── CRONS (Beat) ───────────────────────────────────────────────────

    async def match_recent_products(self, *, since: datetime, trigger: str = "scheduled") -> NeedMatchBatchReport:
        """Filet de sécurité fréquent (mandat §12/§13) : produits publiés/réapprovisionnés depuis
        `since` (index `products_producer_available_idx`/`updated_at`), regroupés par sous-catégorie
        pour ne rematcher chaque occurrence concernée qu'UNE fois même si plusieurs produits de la
        même sous-catégorie ont changé dans la fenêtre."""
        sub_category_ids = (await self.session.execute(_RECENT_PRODUCTS_SQL, {"since": since})).scalars().all()
        merged = NeedMatchBatchReport(trigger=trigger)
        for sub_category_id in sub_category_ids:
            batch = await self._match_sub_category(sub_category_id, trigger=trigger)
            merged.occurrences_examined += batch.occurrences_examined
            merged.reports.extend(batch.reports)
        return merged

    async def match_upcoming_occurrences(self, *, within_hours: int, trigger: str = "scheduled") -> NeedMatchBatchReport:
        """Rematch temporel léger (mandat §13) : occurrences OPEN/MATCHED dues dans les prochaines
        `within_hours` heures — jamais un scan de l'historique complet."""
        from_date = _today()
        to_date = datetime.combine(from_date, datetime.min.time()) + timedelta(hours=within_hours)
        occ_ids = (
            await self.session.execute(
                _UPCOMING_OCCURRENCES_SQL,
                {
                    "statuses": list(_MATCHABLE_OCCURRENCE_STATUSES),
                    "from_date": datetime.combine(from_date, datetime.min.time()),
                    "to_date": to_date,
                },
            )
        ).scalars().all()
        batch = NeedMatchBatchReport(trigger=trigger, occurrences_examined=len(occ_ids))
        for occ_id in occ_ids:
            batch.reports.append(await self.rematch_occurrence(occ_id, trigger=trigger))
        return batch


def _today() -> date:
    return datetime.utcnow().date()


__all__ = ["NeedMatchingService", "NeedMatchReport", "NeedMatchBatchReport"]
