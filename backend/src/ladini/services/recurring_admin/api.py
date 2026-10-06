"""Recurring OPERATIONS (admin) — inspecter UN objet métier réel + configurer le délai de départ.

OPERATIONS ≠ ANALYTICS : ce module répond « que se passe-t-il pour CE besoin ? » (liste détaillée, fiche
d'un besoin, occurrences, allocations, commandes) ; l'analytics (`services/analytics/*`,
`/internal/analytics/buyers/recurring`) répond « comment le système performe-t-il en agrégat ? ». Aucun de ces
deux ne réutilise l'autre.

Posture : LECTURE seule pour la liste/la fiche (jamais d'écriture directe sur un besoin : toute mutation d'un besoin
passe par `update_recurring_need`, le service métier). La seule écriture est le réglage global
`minimum_recurring_start_lead_days` (`services/platform_settings.py` : admin vérifié en base, validé, audité).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Optional

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ladini.domain.governance.models import SubCategory, Zone
from ladini.domain.identity.models import BuyerProfile, Producer, User
from ladini.domain.orders.models import Order
from ladini.domain.recurring_supply.models import (
    NeedAllocation,
    RecurringNeed,
    RecurringNeedOccurrence,
)
from ladini.domain.recurring_supply.recurrence import RecurrenceRule, next_due_date
from ladini.domain.recurring_supply.start_policy import first_due_date
from ladini.services.platform_settings import (
    RecurringSettings,
    SettingsError,
    get_recurring_settings,
    update_recurring_settings,
)

NEED_STATUSES = ("ACTIVE", "PAUSED", "CANCELLED")
RECURRENCE_TYPES = ("DAILY", "WEEKLY_DAYS", "WEEKLY", "MONTHLY", "ONE_OFF")
_NON_DELIVERABLE_OCCURRENCE = ("SKIPPED", "CANCELLED")
_FAILURE_OCCURRENCE = ("REJECTED", "EXPIRED", "UNFULFILLED")
_FAILURE_ALLOCATION = ("REJECTED", "EXPIRED")


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _today() -> date:
    # Même horloge métier que `services/database/recurring_supply.py::_today` (dette timezone documentée).
    return datetime.now().date()


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _num(value: Any) -> Optional[float]:
    return float(value) if value is not None else None


@dataclass(frozen=True)
class ListParams:
    status: Optional[str] = None
    product: Optional[str] = None
    region: Optional[str] = None
    frequency: Optional[str] = None
    buyer: Optional[str] = None
    q: Optional[str] = None
    starts_from: Optional[date] = None
    starts_to: Optional[date] = None
    next_from: Optional[date] = None
    next_to: Optional[date] = None
    limit: int = 50
    offset: int = 0


def _parse_date(raw: Any, label: str) -> Optional[date]:
    if raw in (None, ""):
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        raise ApiError(400, f"{label} invalide (AAAA-MM-JJ attendu).") from None


def parse_list_params(query: Any) -> ListParams:
    status = (str(query.get("status") or "").strip().upper()) or None
    if status and status not in NEED_STATUSES:
        raise ApiError(400, f"Statut invalide : {status}")
    frequency = (str(query.get("frequency") or "").strip().upper()) or None
    if frequency and frequency not in RECURRENCE_TYPES:
        raise ApiError(400, f"Fréquence invalide : {frequency}")
    try:
        limit = int(query.get("limit") or 50)
        offset = int(query.get("offset") or 0)
    except (TypeError, ValueError):
        raise ApiError(400, "limit/offset invalides.") from None
    if not (1 <= limit <= 200) or offset < 0:
        raise ApiError(400, "limit doit être entre 1 et 200, offset >= 0.")
    return ListParams(
        status=status,
        product=(str(query.get("product") or "").strip()) or None,
        region=(str(query.get("region") or "").strip()) or None,
        frequency=frequency,
        buyer=(str(query.get("buyer") or "").strip()) or None,
        q=(str(query.get("q") or "").strip()) or None,
        starts_from=_parse_date(query.get("starts_from"), "starts_from"),
        starts_to=_parse_date(query.get("starts_to"), "starts_to"),
        next_from=_parse_date(query.get("next_from"), "next_from"),
        next_to=_parse_date(query.get("next_to"), "next_to"),
        limit=limit,
        offset=offset,
    )


# ─── SETTINGS ─────────────────────────────────────────────────────────


async def read_settings(session: AsyncSession) -> dict[str, Any]:
    current = await get_recurring_settings(session)
    return {"recurring": _settings_payload(current)}


def _settings_payload(current: RecurringSettings) -> dict[str, Any]:
    return {
        **current.to_dict(),
        "label": "Délai minimum avant première livraison",
        "description": (
            "Nombre minimal de jours entre la création d'un besoin récurrent et sa première livraison. "
            "Ne modifie jamais les besoins déjà créés."
        ),
    }


async def write_settings(
    session: AsyncSession,
    *,
    actor_id: Any,
    minimum_start_lead_days: Any,
    expected_version: Any = None,
    ip_address: str | None = None,
) -> dict[str, Any]:
    try:
        version = int(expected_version) if expected_version not in (None, "") else None
    except (TypeError, ValueError):
        raise ApiError(400, "expected_version invalide.") from None
    try:
        updated = await update_recurring_settings(
            session,
            actor_id=actor_id,
            minimum_start_lead_days=minimum_start_lead_days,
            expected_version=version,
            ip_address=ip_address,
        )
    except SettingsError as exc:
        raise ApiError(exc.status, exc.message) from None
    return {"recurring": _settings_payload(updated)}


# ─── LISTE ────────────────────────────────────────────────────────────


def _need_rule(need: RecurringNeed) -> RecurrenceRule:
    return RecurrenceRule(
        recurrence_type=str(need.recurrence_type),
        weekly_days=tuple(need.weekly_days or ()),
        excluded_weekdays=tuple(need.excluded_weekdays or ()),
        starts_at=need.starts_at.date(),
        ends_at=need.ends_at.date() if need.ends_at else None,
    )


async def list_needs(session: AsyncSession, params: ListParams) -> dict[str, Any]:
    today_dt = datetime.combine(_today(), datetime.min.time())
    next_occ = (
        select(
            RecurringNeedOccurrence.recurring_need_id.label("need_id"),
            func.min(RecurringNeedOccurrence.occurrence_date).label("next_date"),
        )
        .where(
            RecurringNeedOccurrence.occurrence_date >= today_dt,
            RecurringNeedOccurrence.status.notin_(_NON_DELIVERABLE_OCCURRENCE),
        )
        .group_by(RecurringNeedOccurrence.recurring_need_id)
        .subquery()
    )
    occ_stats = (
        select(
            RecurringNeedOccurrence.recurring_need_id.label("need_id"),
            func.count(RecurringNeedOccurrence.id).label("occurrence_count"),
            func.max(RecurringNeedOccurrence.updated_at).label("last_occurrence_activity"),
        )
        .group_by(RecurringNeedOccurrence.recurring_need_id)
        .subquery()
    )
    next_status = (
        select(RecurringNeedOccurrence.recurring_need_id, RecurringNeedOccurrence.status, RecurringNeedOccurrence.occurrence_date)
        .join(
            next_occ,
            (next_occ.c.need_id == RecurringNeedOccurrence.recurring_need_id)
            & (next_occ.c.next_date == RecurringNeedOccurrence.occurrence_date),
        )
        .subquery()
    )

    stmt = (
        select(
            RecurringNeed,
            SubCategory.name.label("product"),
            User.name.label("buyer_name"),
            User.phone.label("buyer_phone"),
            Zone.name.label("region"),
            next_occ.c.next_date,
            next_status.c.status.label("sourcing_state"),
            occ_stats.c.occurrence_count,
            occ_stats.c.last_occurrence_activity,
        )
        .join(SubCategory, SubCategory.id == RecurringNeed.sub_category_id)
        .join(BuyerProfile, BuyerProfile.id == RecurringNeed.buyer_id)
        .join(User, User.id == BuyerProfile.user_id)
        .outerjoin(Zone, Zone.id == User.zone_id)
        .outerjoin(next_occ, next_occ.c.need_id == RecurringNeed.id)
        .outerjoin(next_status, next_status.c.recurring_need_id == RecurringNeed.id)
        .outerjoin(occ_stats, occ_stats.c.need_id == RecurringNeed.id)
    )
    if params.status:
        stmt = stmt.where(RecurringNeed.status == params.status)
    if params.frequency:
        stmt = stmt.where(RecurringNeed.recurrence_type == params.frequency)
    if params.product:
        stmt = stmt.where(SubCategory.name.ilike(f"%{params.product}%"))
    if params.region:
        stmt = stmt.where(Zone.name.ilike(f"%{params.region}%"))
    if params.buyer:
        stmt = stmt.where(or_(User.name.ilike(f"%{params.buyer}%"), User.phone.ilike(f"%{params.buyer}%")))
    if params.q:
        like = f"%{params.q}%"
        stmt = stmt.where(
            or_(
                cast(RecurringNeed.id, String).ilike(f"{params.q}%"),
                User.name.ilike(like),
                User.phone.ilike(like),
            )
        )
    if params.starts_from:
        stmt = stmt.where(RecurringNeed.starts_at >= datetime.combine(params.starts_from, datetime.min.time()))
    if params.starts_to:
        stmt = stmt.where(RecurringNeed.starts_at < datetime.combine(params.starts_to + timedelta(days=1), datetime.min.time()))
    if params.next_from:
        stmt = stmt.where(next_occ.c.next_date >= datetime.combine(params.next_from, datetime.min.time()))
    if params.next_to:
        stmt = stmt.where(next_occ.c.next_date < datetime.combine(params.next_to + timedelta(days=1), datetime.min.time()))

    total = (await session.execute(select(func.count()).select_from(stmt.order_by(None).subquery()))).scalar_one()
    rows = (
        await session.execute(
            stmt.order_by(RecurringNeed.created_at.desc(), RecurringNeed.id).limit(params.limit).offset(params.offset)
        )
    ).all()

    items = []
    for row in rows:
        need: RecurringNeed = row[0]
        last_activity = max(
            (d for d in (need.updated_at, row.last_occurrence_activity) if d is not None), default=None
        )
        items.append(
            {
                "id": str(need.id),
                "short_id": str(need.id)[:8],
                "buyer": {"name": row.buyer_name, "phone": row.buyer_phone},
                "product": row.product,
                "quantity": _num(need.quantity),
                "unit": need.unit,
                "frequency": need.recurrence_type,
                "weekly_days": list(need.weekly_days or []),
                "status": need.status,
                "starts_at": _iso(need.starts_at),
                "created_at": _iso(need.created_at),
                "next_occurrence": _iso(row.next_date),
                "region": row.region,
                "occurrence_count": int(row.occurrence_count or 0),
                "sourcing_state": row.sourcing_state,
                "last_activity": _iso(last_activity),
            }
        )
    return {"items": items, "total": int(total), "limit": params.limit, "offset": params.offset}


# ─── FICHE D'UN BESOIN ────────────────────────────────────────────────


def _uuid_or_404(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(raw))
    except (ValueError, AttributeError):
        raise ApiError(404, "Besoin introuvable.") from None


async def get_need_detail(session: AsyncSession, need_id_raw: str) -> dict[str, Any]:
    need_id = _uuid_or_404(need_id_raw)
    head = (
        await session.execute(
            select(RecurringNeed, SubCategory.name, User.name, User.phone, Zone.name)
            .join(SubCategory, SubCategory.id == RecurringNeed.sub_category_id)
            .join(BuyerProfile, BuyerProfile.id == RecurringNeed.buyer_id)
            .join(User, User.id == BuyerProfile.user_id)
            .outerjoin(Zone, Zone.id == User.zone_id)
            .where(RecurringNeed.id == need_id)
        )
    ).first()
    if head is None:
        raise ApiError(404, "Besoin introuvable.")
    need, product, buyer_name, buyer_phone, region = head

    occurrences = (
        (
            await session.execute(
                select(RecurringNeedOccurrence)
                .where(RecurringNeedOccurrence.recurring_need_id == need_id)
                .order_by(RecurringNeedOccurrence.occurrence_date)
            )
        )
        .scalars()
        .all()
    )
    occ_ids = [o.id for o in occurrences]

    allocations: list[Any] = []
    if occ_ids:
        alloc_result = await session.execute(
            select(NeedAllocation, Producer.business_name)
            .join(Producer, Producer.id == NeedAllocation.producer_id)
            .where(NeedAllocation.occurrence_id.in_(occ_ids))
            .order_by(NeedAllocation.created_at)
        )
        allocations = list(alloc_result.all())
    allocs_by_occ: dict[Any, list[dict[str, Any]]] = {}
    for alloc, producer_name in allocations:
        allocs_by_occ.setdefault(alloc.occurrence_id, []).append(
            {
                "id": str(alloc.id),
                "producer_id": str(alloc.producer_id),
                "producer": producer_name,
                "product_id": str(alloc.product_id),
                "quantity": _num(alloc.quantity),
                "unit": alloc.unit,
                "unit_price": _num(alloc.unit_price),
                "status": alloc.status,
                "created_at": _iso(alloc.created_at),
                "updated_at": _iso(alloc.updated_at),
                "converted": alloc.order_item_id is not None or alloc.status == "CONVERTED",
                "order_item_id": str(alloc.order_item_id) if alloc.order_item_id else None,
            }
        )

    groups = {o.order_group_id: o for o in occurrences if o.order_group_id is not None}
    orders_payload: list[dict[str, Any]] = []
    if groups:
        order_rows = (
            (
                await session.execute(
                    select(Order)
                    .where(Order.checkout_group_id.in_(list(groups)), Order.order_type == "RECURRING_SUPPLY")
                    .order_by(Order.created_at)
                )
            )
            .scalars()
            .all()
        )
        for order in order_rows:
            occ = groups.get(order.checkout_group_id)
            orders_payload.append(
                {
                    "id": str(order.id),
                    "occurrence_id": str(occ.id) if occ else None,
                    "occurrence_date": _iso(occ.occurrence_date) if occ else None,
                    "total_amount": _num(order.total_amount),
                    "status": order.status,
                    "payment_status": order.payment_status,
                    "delivery_status": order.delivery_status,
                    "expected_fulfillment_date": _iso(order.expected_fulfillment_date),
                    "cancellation_role": order.cancellation_role,
                    "created_at": _iso(order.created_at),
                }
            )

    occ_payload = [
        {
            "id": str(o.id),
            "date": _iso(o.occurrence_date),
            "status": o.status,
            "requested_quantity": _num(o.requested_quantity),
            "unit": o.unit,
            "quantity_matched": _num(o.quantity_matched),
            "quantity_confirmed": _num(o.quantity_confirmed),
            "quantity_delivered": _num(o.quantity_delivered),
            "version": o.version,
            "skipped": o.status == "SKIPPED",
            # Exception ponctuelle de quantité : écart avec la quantité permanente du besoin.
            "quantity_overridden": _num(o.requested_quantity) != _num(need.quantity) and o.status != "SKIPPED",
            "order_group_id": str(o.order_group_id) if o.order_group_id else None,
            "notified_at": _iso(o.notified_at),
            "accepted_at": _iso(o.accepted_at),
            "expires_at": _iso(o.expires_at),
            "allocations": allocs_by_occ.get(o.id, []),
        }
        for o in occurrences
    ]

    rule = _need_rule(need)
    today = _today()
    materialized_next = next(
        (
            o.occurrence_date
            for o in occurrences
            if o.occurrence_date.date() >= today and o.status not in _NON_DELIVERABLE_OCCURRENCE
        ),
        None,
    )
    calculated_next = next_due_date(rule, from_date=today)
    first_date = first_due_date(rule)
    current = await get_recurring_settings(session)

    diagnostics: list[dict[str, Any]] = []
    for o in occurrences:
        if o.status in _FAILURE_OCCURRENCE:
            diagnostics.append({"kind": f"OCCURRENCE_{o.status}", "occurrence_id": str(o.id), "date": _iso(o.occurrence_date)})
        for alloc in allocs_by_occ.get(o.id, []):
            if alloc["status"] in _FAILURE_ALLOCATION:
                diagnostics.append(
                    {"kind": f"ALLOCATION_{alloc['status']}", "occurrence_id": str(o.id), "allocation_id": alloc["id"],
                     "producer": alloc["producer"], "at": alloc["updated_at"]}
                )
    for order in orders_payload:
        if order["status"] == "CANCELLED":
            diagnostics.append(
                {"kind": "ORDER_CANCELLED", "order_id": order["id"], "by": order["cancellation_role"],
                 "occurrence_id": order["occurrence_id"]}
            )

    return {
        "overview": {
            "id": str(need.id),
            "buyer": {"name": buyer_name, "phone": buyer_phone},
            "product": product,
            "quantity": _num(need.quantity),
            "unit": need.unit,
            "frequency": need.recurrence_type,
            "status": need.status,
            "starts_at": _iso(need.starts_at),
            "ends_at": _iso(need.ends_at),
            "paused_until": _iso(need.paused_until),
            "max_price_per_unit": _num(need.max_price_per_unit),
            "created_at": _iso(need.created_at),
            "updated_at": _iso(need.updated_at),
            "region": region,
        },
        "schedule": {
            "recurrence_type": need.recurrence_type,
            "weekly_days": list(need.weekly_days or []),
            "excluded_weekdays": list(need.excluded_weekdays or []),
            "effective_start_date": _iso(need.starts_at),
            "first_delivery_date": _iso(first_date),
            "next_calculated_occurrence": _iso(calculated_next),
            "next_materialized_occurrence": _iso(materialized_next),
            # Le délai appliqué à la création n'est PAS historisé : on n'affiche que le réglage COURANT.
            "lead_time_policy": {
                "current_minimum_start_lead_days": current.minimum_start_lead_days,
                "applied_at_creation": None,
                "note": "Le délai appliqué à la création n'est pas conservé ; starts_at fait foi.",
            },
        },
        "occurrences": occ_payload,
        "orders": orders_payload,
        # Aucun journal durable des mutations (pause, reprise, quantité, fréquence…) n'est lisible aujourd'hui :
        # on ne reconstitue JAMAIS un faux historique depuis l'état courant.
        "mutations": {
            "available": False,
            "reason": "Aucun journal durable des mutations n'est exposé ; seules les versions courantes sont lisibles.",
            "need_version": _need_version(need),
        },
        "operational_state": {
            "sourcing_state": next((o["status"] for o in occ_payload if o["date"] and o["date"][:10] >= today.isoformat()
                                    and o["status"] not in _NON_DELIVERABLE_OCCURRENCE), None),
            "diagnostics": diagnostics,
        },
    }


def _need_version(need: RecurringNeed) -> Optional[int]:
    from ladini.services.database.recurring_supply import recurring_need_version

    try:
        return int(recurring_need_version(need))
    except Exception:  # noqa: BLE001 - champ informatif, jamais bloquant
        return None


__all__ = [
    "ApiError",
    "ListParams",
    "get_need_detail",
    "list_needs",
    "parse_list_params",
    "read_settings",
    "write_settings",
]
