"""RecurringSupplyMixin — approvisionnement récurrent (Phase 2).

Trois opérations, mandat §12 : `create_recurring_need`, `update_recurring_need`,
`list_my_recurring_needs`. Même conventions que `AuctionMixin` (résolution acheteur/produit,
`self.session` fourni par le service composé, exceptions `BusinessRuleException`) — pas de 4ᵉ manière
de faire, pas de couche `repository.py` séparée (aucun mixin de ce paquet n'en a une).

## Ownership (mandat §12)

Toute mutation résout `buyer_id` depuis le TÉLÉPHONE authentifié (`self.get_buyer_profile`), jamais
depuis un identifiant fourni par l'appelant. `update_recurring_need`/`list_my_recurring_needs`
filtrent en plus explicitement `WHERE buyer_id = :buyer_id` — un acheteur ne peut ni lire ni modifier
le besoin d'un autre (`RECURRING_NEED_NOT_FOUND`, jamais une fuite d'existence).

## Transaction (mandat §14)

`create_recurring_need` insère `recurring_needs` PUIS matérialise la fenêtre J→J+7 d'occurrences dans
LA MÊME transaction (un seul `flush`, un seul commit porté par `@transactional(write=True)` — voir
`d.py`) : jamais de besoin créé sans ses occurrences.

## Idempotence (mandat §13)

Ce module ne réimplémente PAS de 4ᵉ mécanisme : la déduplication d'un rejeu de webhook/tâche pour
`create_recurring_need` est assurée EN AMONT par `mcp_idempotency_store` (dédup serveur générique de
`AgriDBMCPServer.call_tool`, voir `infrastructure/mcp/runtime.py`) — ce mixin n'a besoin de garantir
que sa propre idempotence STRUCTURELLE : générer deux fois la fenêtre d'occurrences d'un MÊME besoin
ne doit jamais produire de doublon (`UNIQUE(recurring_need_id, occurrence_date)`, `ON CONFLICT DO
NOTHING`) — c'est ce qui protège aussi la réconciliation Celery Beat (Phase 3) qui étendra la fenêtre
chaque jour.

## Sémantique des modifications (mandat §9/§10)

`update_recurring_need` reçoit une `action` structurée (voir `RECURRING_NEED_ACTIONS`) :

- `PERMANENT_QUANTITY` / `PERMANENT_FREQUENCY` : modifie `recurring_needs`, puis met à jour UNIQUEMENT
  les occurrences FUTURES encore `OPEN` (jamais `SKIPPED`/`MATCHED`/`PROPOSED`/`ACCEPTED`/
  `PARTIALLY_ACCEPTED`/`FULFILLED`/`PARTIALLY_FULFILLED`/`EXPIRED`/`CANCELLED` — une occurrence qui a
  quitté `OPEN` a déjà une vie propre, voir `_MUTABLE_OCCURRENCE_STATUSES`). Une exception ponctuelle
  déjà posée sur une occurrence encore `OPEN` (`requested_quantity` ≠ la quantité permanente d'origine)
  n'est PAS écrasée : voir `_apply_permanent_update` — c'est le point du mandat §10 vérifié par
  `test_a_permanent_update_never_silently_overwrites_an_explicit_exception`.
- `PAUSE` : `recurring_needs.status = PAUSED`, `paused_until` posé. Les occurrences déjà matérialisées
  et encore `OPEN` dans la fenêtre de pause passent à `SKIPPED` (mandat §9 : « suspend cette semaine »
  ne doit pas laisser une occurrence `OPEN` que Phase 3 matcherait quand même).
- `RESUME` : `recurring_needs.status = ACTIVE`, `paused_until = NULL`. Les occurrences qu'on venait de
  passer `SKIPPED` par CETTE pause ne repassent PAS `OPEN` automatiquement (un skip reste un fait
  historique) — seules les occurrences futures nouvellement générées seront `OPEN`.
- `CANCEL` : `recurring_needs.status = CANCELLED`. Les occurrences `OPEN` futures passent `CANCELLED`.
- `OCCURRENCE_OVERRIDE` : modifie `requested_quantity` d'UNE occurrence précise, `version += 1`. Ne
  touche jamais `recurring_needs`.
- `OCCURRENCE_SKIP` : `status = SKIPPED` sur UNE occurrence précise, `version += 1`.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import select, update

from ladini.domain.models import (
    RecurringNeed,
    RecurringNeedOccurrence,
    SubCategory,
)
from ladini.domain.recurring_supply.recurrence import (
    OCCURRENCE_WINDOW_DAYS,
    RecurrenceRule,
    generate_occurrence_dates,
)

from .base import BaseMixin
from .errors import BusinessRuleException

logger = logging.getLogger("ladini.services.database.recurring_supply")

# Une occurrence encore à cet état n'a reçu aucune réponse fournisseur — seule une mise à jour
# permanente ou une exception ponctuelle peut encore la faire changer sans piétiner du travail déjà
# engagé (matching, acceptation, livraison — tous hors scope Phase 2, mais le schéma les anticipe).
_MUTABLE_OCCURRENCE_STATUSES = ("OPEN",)

RECURRING_NEED_ACTIONS = (
    "PERMANENT_QUANTITY",
    "PERMANENT_FREQUENCY",
    "PAUSE",
    "RESUME",
    "CANCEL",
    "OCCURRENCE_OVERRIDE",
    "OCCURRENCE_SKIP",
)


def _today() -> date:
    return datetime.now().date()


class RecurringSupplyMixin(BaseMixin):
    # ─── CREATE ───────────────────────────────────────────────────────

    async def create_recurring_need(
        self,
        phone: str,
        product_query: str,
        quantity: float,
        unit: str,
        recurrence_type: str,
        weekly_days: Optional[List[int]] = None,
        excluded_weekdays: Optional[List[int]] = None,
        starts_at: Optional[Any] = None,
        ends_at: Optional[Any] = None,
        max_price_per_unit: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Crée un `RecurringNeed` PUIS matérialise la fenêtre J→J+7 d'occurrences, dans une seule
        transaction (mandat §14). `starts_at` non fourni = demain (mandat §4 : "À partir de demain.")."""
        current_session = self.session
        if not current_session:
            raise BusinessRuleException("Session indisponible.")

        user_obj, buyer_profile = await self.get_buyer_profile(phone=str(phone))
        buyer_id = buyer_profile.id

        sub_cat = await self._resolve_sub_category(product_query)

        start_date = _parse_date(starts_at) or (_today() + timedelta(days=1))
        end_date = _parse_date(ends_at)

        rule = RecurrenceRule(
            recurrence_type=recurrence_type,
            weekly_days=weekly_days or (),
            excluded_weekdays=excluded_weekdays or (),
            starts_at=start_date,
            ends_at=end_date,
        )

        need = RecurringNeed(
            id=uuid.uuid4(),
            buyer_id=buyer_id,
            sub_category_id=sub_cat.id,
            quantity=float(quantity),
            unit=unit.upper().strip(),
            recurrence_type=recurrence_type,
            weekly_days=list(weekly_days) if weekly_days else None,
            excluded_weekdays=list(excluded_weekdays) if excluded_weekdays else None,
            starts_at=datetime.combine(start_date, datetime.min.time()),
            ends_at=datetime.combine(end_date, datetime.min.time()) if end_date else None,
            status="ACTIVE",
            max_price_per_unit=float(max_price_per_unit) if max_price_per_unit is not None else None,
        )
        current_session.add(need)
        await current_session.flush()

        window_end = _today() + timedelta(days=OCCURRENCE_WINDOW_DAYS)
        created = await self._materialize_occurrences(need, rule, from_date=_today(), to_date=window_end)

        logger.info(
            "recurring_need.created | recurring_need_id=%s | buyer_id=%s | occurrences=%s",
            need.id, buyer_id, created,
        )
        return {
            "status": "success",
            "recurring_need_id": str(need.id),
            "sub_category": sub_cat.name,
            "occurrences_created": created,
        }

    async def _materialize_occurrences(
        self, need: RecurringNeed, rule: RecurrenceRule, *, from_date: date, to_date: date
    ) -> int:
        """Idempotent (mandat §8) : `ON CONFLICT (recurring_need_id, occurrence_date) DO NOTHING` —
        un deuxième appel sur la même fenêtre produit 0 doublon. Chaque occurrence SNAPSHOTTE
        `requested_quantity`/`unit` au moment de la génération (mandat §16, propriété testée)."""
        dates = generate_occurrence_dates(rule, from_date=from_date, to_date=to_date)
        if not dates:
            return 0
        current_session = self.session
        rows = [
            {
                "recurring_need_id": need.id,
                "occurrence_date": datetime.combine(d, datetime.min.time()),
                "requested_quantity": need.quantity,
                "unit": need.unit,
            }
            for d in dates
        ]
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = pg_insert(RecurringNeedOccurrence).values(rows)
        stmt = stmt.on_conflict_do_nothing(index_elements=["recurring_need_id", "occurrence_date"])
        result = await current_session.execute(stmt)
        return result.rowcount or 0

    async def _resolve_sub_category(self, product_query: str) -> SubCategory:
        """Même résolution catalogue que `AuctionMixin.create_auction` (fuzzy trigram + auto-
        provisioning) — un besoin récurrent exprime un besoin acheteur, pas un article de catalogue
        déjà stabilisé, même raisonnement que pour un appel d'offres."""
        current_session = self.session
        from .category import _is_confident_category_match
        from .search import fuzzy_match, similarity_rank

        product_query_clean = str(product_query or "").strip()
        if not product_query_clean:
            raise BusinessRuleException("Produit manquant.")

        sub_cat_stmt = (
            select(SubCategory)
            .where(fuzzy_match(SubCategory.name, product_query_clean))
            .order_by(similarity_rank(SubCategory.name, product_query_clean))
            .limit(1)
        )
        sub_cat = await current_session.scalar(sub_cat_stmt)
        if sub_cat and not _is_confident_category_match(product_query_clean, sub_cat.name):
            sub_cat = None
        if not sub_cat:
            candidate = await self._fuzzy_match_sub_category(product_query_clean)
            if candidate and _is_confident_category_match(product_query_clean, candidate.name):
                sub_cat = candidate
        if not sub_cat:
            sub_cat = await self._get_or_create_sub_category_for_rfq(product_query_clean)
        return sub_cat

    # ─── READ ─────────────────────────────────────────────────────────

    async def list_my_recurring_needs(self, phone: str) -> Dict[str, Any]:
        """Les besoins de l'acheteur + leur prochaine occurrence ouverte — UNE requête jointe (mandat
        §19, pas de N+1 : jamais 1 requête d'occurrence par besoin listé)."""
        current_session = self.session
        if not current_session:
            raise BusinessRuleException("Session indisponible.")
        _user_obj, buyer_profile = await self.get_buyer_profile(phone=str(phone))
        buyer_id = buyer_profile.id

        needs_stmt = (
            select(RecurringNeed, SubCategory.name)
            .join(SubCategory, RecurringNeed.sub_category_id == SubCategory.id)
            .where(RecurringNeed.buyer_id == buyer_id, RecurringNeed.status != "CANCELLED")
            .order_by(RecurringNeed.created_at.asc())
        )
        rows = (await current_session.execute(needs_stmt)).all()
        needs = [n for n, _ in rows]
        need_ids = [n.id for n in needs]

        next_occurrence_by_need: Dict[Any, RecurringNeedOccurrence] = {}
        if need_ids:
            occ_stmt = (
                select(RecurringNeedOccurrence)
                .where(
                    RecurringNeedOccurrence.recurring_need_id.in_(need_ids),
                    RecurringNeedOccurrence.status == "OPEN",
                )
                .order_by(RecurringNeedOccurrence.recurring_need_id, RecurringNeedOccurrence.occurrence_date.asc())
            )
            for occ in (await current_session.execute(occ_stmt)).scalars().all():
                next_occurrence_by_need.setdefault(occ.recurring_need_id, occ)

        items = []
        for need, sub_category_name in rows:
            next_occ = next_occurrence_by_need.get(need.id)
            items.append(
                {
                    "recurring_need_id": str(need.id),
                    "product": sub_category_name,
                    "quantity": float(need.quantity),
                    "unit": need.unit,
                    "recurrence_type": need.recurrence_type,
                    "weekly_days": need.weekly_days,
                    "status": need.status,
                    "next_occurrence_date": (
                        next_occ.occurrence_date.date().isoformat() if next_occ else None
                    ),
                }
            )
        return {"status": "success", "items": items}

    # ─── UPDATE ───────────────────────────────────────────────────────

    async def update_recurring_need(
        self,
        phone: str,
        recurring_need_id: str,
        action: str,
        *,
        quantity: Optional[float] = None,
        recurrence_type: Optional[str] = None,
        weekly_days: Optional[List[int]] = None,
        excluded_weekdays: Optional[List[int]] = None,
        paused_until: Optional[Any] = None,
        occurrence_date: Optional[Any] = None,
    ) -> Dict[str, Any]:
        if action not in RECURRING_NEED_ACTIONS:
            raise BusinessRuleException(f"Action inconnue : {action!r}")

        current_session = self.session
        if not current_session:
            raise BusinessRuleException("Session indisponible.")
        _user_obj, buyer_profile = await self.get_buyer_profile(phone=str(phone))
        buyer_id = buyer_profile.id

        need = await current_session.scalar(
            select(RecurringNeed).where(RecurringNeed.id == recurring_need_id, RecurringNeed.buyer_id == buyer_id)
        )
        if need is None:
            # Jamais de fuite d'existence (mandat §12) : même message qu'un id inexistant.
            raise BusinessRuleException("Besoin introuvable.")

        if action == "PERMANENT_QUANTITY":
            return await self._apply_permanent_update(need, quantity=quantity)
        if action == "PERMANENT_FREQUENCY":
            return await self._apply_permanent_update(
                need, recurrence_type=recurrence_type, weekly_days=weekly_days, excluded_weekdays=excluded_weekdays
            )
        if action == "PAUSE":
            return await self._apply_pause(need, paused_until=_parse_date(paused_until))
        if action == "RESUME":
            return await self._apply_resume(need)
        if action == "CANCEL":
            return await self._apply_cancel(need)
        if action == "OCCURRENCE_OVERRIDE":
            return await self._apply_occurrence_override(need, occurrence_date=_parse_date(occurrence_date), quantity=quantity)
        if action == "OCCURRENCE_SKIP":
            return await self._apply_occurrence_skip(need, occurrence_date=_parse_date(occurrence_date))
        raise AssertionError(action)  # pragma: no cover — filtré par RECURRING_NEED_ACTIONS ci-dessus

    async def _apply_permanent_update(
        self,
        need: RecurringNeed,
        *,
        quantity: Optional[float] = None,
        recurrence_type: Optional[str] = None,
        weekly_days: Optional[List[int]] = None,
        excluded_weekdays: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        """Modifie `recurring_needs` PUIS les occurrences FUTURES encore `OPEN` — jamais une
        occurrence qui a quitté cet état (mandat §9), et jamais une exception ponctuelle déjà posée
        sur une occurrence encore `OPEN` (mandat §10) : une occurrence dont `requested_quantity` a
        déjà été explicitement modifiée (≠ l'ancienne quantité permanente) reste intacte — une mise à
        jour générale ne doit jamais écraser silencieusement une exception explicite."""
        current_session = self.session
        old_quantity = need.quantity
        old_unit = need.unit

        if quantity is not None:
            need.quantity = float(quantity)
        if recurrence_type is not None:
            need.recurrence_type = recurrence_type
        if weekly_days is not None:
            need.weekly_days = list(weekly_days)
        if excluded_weekdays is not None:
            need.excluded_weekdays = list(excluded_weekdays)
        await current_session.flush()

        updated_count = 0
        if quantity is not None:
            # Ne touche QUE les occurrences encore OPEN dont la quantité est EXACTEMENT l'ancienne
            # quantité permanente — une occurrence déjà en exception (quantité différente posée
            # explicitement par `OCCURRENCE_OVERRIDE`) n'est jamais écrasée silencieusement.
            stmt = (
                update(RecurringNeedOccurrence)
                .where(
                    RecurringNeedOccurrence.recurring_need_id == need.id,
                    RecurringNeedOccurrence.status.in_(_MUTABLE_OCCURRENCE_STATUSES),
                    RecurringNeedOccurrence.occurrence_date >= datetime.combine(_today(), datetime.min.time()),
                    RecurringNeedOccurrence.requested_quantity == old_quantity,
                    RecurringNeedOccurrence.unit == old_unit,
                )
                .values(requested_quantity=need.quantity, unit=need.unit, version=RecurringNeedOccurrence.version + 1)
            )
            result = await current_session.execute(stmt)
            updated_count = result.rowcount or 0

        logger.info("recurring_need.updated | recurring_need_id=%s | occurrences_updated=%s", need.id, updated_count)
        return {"status": "success", "recurring_need_id": str(need.id), "occurrences_updated": updated_count}

    async def _apply_pause(self, need: RecurringNeed, *, paused_until: Optional[date]) -> Dict[str, Any]:
        current_session = self.session
        need.status = "PAUSED"
        need.paused_until = datetime.combine(paused_until, datetime.min.time()) if paused_until else None
        await current_session.flush()

        stmt = (
            update(RecurringNeedOccurrence)
            .where(
                RecurringNeedOccurrence.recurring_need_id == need.id,
                RecurringNeedOccurrence.status.in_(_MUTABLE_OCCURRENCE_STATUSES),
                RecurringNeedOccurrence.occurrence_date >= datetime.combine(_today(), datetime.min.time()),
            )
        )
        if paused_until:
            stmt = stmt.where(RecurringNeedOccurrence.occurrence_date <= datetime.combine(paused_until, datetime.min.time()))
        stmt = stmt.values(status="SKIPPED", version=RecurringNeedOccurrence.version + 1)
        result = await current_session.execute(stmt)

        logger.info("recurring_need.paused | recurring_need_id=%s | occurrences_skipped=%s", need.id, result.rowcount)
        return {"status": "success", "recurring_need_id": str(need.id), "occurrences_skipped": result.rowcount or 0}

    async def _apply_resume(self, need: RecurringNeed) -> Dict[str, Any]:
        current_session = self.session
        need.status = "ACTIVE"
        need.paused_until = None
        await current_session.flush()
        logger.info("recurring_need.resumed | recurring_need_id=%s", need.id)
        return {"status": "success", "recurring_need_id": str(need.id)}

    async def _apply_cancel(self, need: RecurringNeed) -> Dict[str, Any]:
        current_session = self.session
        need.status = "CANCELLED"
        await current_session.flush()

        stmt = (
            update(RecurringNeedOccurrence)
            .where(
                RecurringNeedOccurrence.recurring_need_id == need.id,
                RecurringNeedOccurrence.status.in_(_MUTABLE_OCCURRENCE_STATUSES),
                RecurringNeedOccurrence.occurrence_date >= datetime.combine(_today(), datetime.min.time()),
            )
            .values(status="CANCELLED", version=RecurringNeedOccurrence.version + 1)
        )
        result = await current_session.execute(stmt)
        logger.info("recurring_need.cancelled | recurring_need_id=%s | occurrences_cancelled=%s", need.id, result.rowcount)
        return {"status": "success", "recurring_need_id": str(need.id), "occurrences_cancelled": result.rowcount or 0}

    async def _apply_occurrence_override(
        self, need: RecurringNeed, *, occurrence_date: Optional[date], quantity: Optional[float]
    ) -> Dict[str, Any]:
        if occurrence_date is None or quantity is None:
            raise BusinessRuleException("Date et quantité requises.")
        occ = await self._get_mutable_occurrence(need, occurrence_date)
        current_session = self.session
        occ.requested_quantity = float(quantity)
        occ.version = occ.version + 1
        await current_session.flush()
        logger.info("occurrence.updated | occurrence_id=%s | requested_quantity=%s", occ.id, quantity)
        return {"status": "success", "occurrence_id": str(occ.id), "requested_quantity": float(quantity)}

    async def _apply_occurrence_skip(self, need: RecurringNeed, *, occurrence_date: Optional[date]) -> Dict[str, Any]:
        if occurrence_date is None:
            raise BusinessRuleException("Date requise.")
        occ = await self._get_mutable_occurrence(need, occurrence_date)
        current_session = self.session
        occ.status = "SKIPPED"
        occ.version = occ.version + 1
        await current_session.flush()
        logger.info("occurrence.skipped | occurrence_id=%s", occ.id)
        return {"status": "success", "occurrence_id": str(occ.id)}

    async def _get_mutable_occurrence(self, need: RecurringNeed, occurrence_date: date) -> RecurringNeedOccurrence:
        current_session = self.session
        occ = await current_session.scalar(
            select(RecurringNeedOccurrence).where(
                RecurringNeedOccurrence.recurring_need_id == need.id,
                RecurringNeedOccurrence.occurrence_date == datetime.combine(occurrence_date, datetime.min.time()),
            )
        )
        if occ is None:
            raise BusinessRuleException("Aucune occurrence à cette date.")
        if occ.status not in _MUTABLE_OCCURRENCE_STATUSES:
            raise BusinessRuleException(
                f"Cette date n'est plus modifiable (statut actuel : {occ.status})."
            )
        return occ


def _parse_date(value: Optional[Any]) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    raise BusinessRuleException(f"Date invalide : {value!r}")


__all__ = ["RecurringSupplyMixin", "RECURRING_NEED_ACTIONS"]
