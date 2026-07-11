"""OrderService — machine à états Commande→Paiement→Livraison→Confirmation."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from agriconnect.domain.models import (
    Order,
    OrderItem,
    OrderStatusHistory,
    OrderReminder,
    Payment,
)
from agriconnect.domain.dto.orders import (
    OrderModel,
    PaymentModel,
    OrderReminderModel,
)
from agriconnect.services.database.base_service import BaseService, transactional


def _now() -> datetime:
    return datetime.now(timezone.utc)


class OrderService(BaseService):

    # ── Lecture ─────────────────────────────────────────────────────────────
    @transactional(write=False)
    async def get_order(self, session: AsyncSession, order_id: str) -> Optional[OrderModel]:
        stmt = select(Order).where(Order.id == order_id).options(selectinload(Order.items))
        obj = (await session.execute(stmt)).scalar_one_or_none()
        return OrderModel.model_validate(obj) if obj else None

    @transactional(write=False)
    async def list_buyer_orders(
        self, session: AsyncSession, buyer_id: str, *, status: Optional[str] = None
    ) -> list[OrderModel]:
        stmt = select(Order).where(Order.buyer_id == buyer_id)
        if status:
            stmt = stmt.where(Order.status == status)
        stmt = stmt.order_by(Order.created_at.desc()).options(selectinload(Order.items))
        rows = (await session.execute(stmt)).scalars().all()
        return [OrderModel.model_validate(o) for o in rows]

    # ── Création atomique (commande + lignes + trace initiale) ──────────────
    @transactional(write=True)
    async def create_order(self, session: AsyncSession, order: OrderModel) -> str:
        data = order.to_db()
        items = data.pop("items", [])
        obj = Order(**data)
        session.add(obj)
        await session.flush()

        for it in items:
            it.pop("order_id", None)
            session.add(OrderItem(order_id=obj.id, **it))

        session.add(
            OrderStatusHistory(order_id=obj.id, status_type="ORDER", to_status=obj.status)
        )
        await session.flush()
        return str(obj.id)

    # ── Transitions d'état (avec journalisation systématique) ───────────────
    async def _transition(
        self,
        session: AsyncSession,
        order: Order,
        *,
        status_type: str,
        field: str,
        to_status: str,
        actor_id: Optional[str] = None,
        note: Optional[str] = None,
    ) -> None:
        from_status = getattr(order, field)
        setattr(order, field, to_status)
        session.add(
            OrderStatusHistory(
                order_id=order.id,
                status_type=status_type,
                from_status=from_status,
                to_status=to_status,
                actor_id=actor_id,
                note=note,
            )
        )

    @transactional(write=True)
    async def advance_order_status(
        self, session: AsyncSession, order_id: str, to_status: str,
        *, actor_id: Optional[str] = None, note: Optional[str] = None,
    ) -> bool:
        order = await session.get(Order, order_id)
        if order is None:
            return False
        await self._transition(
            session, order, status_type="ORDER", field="status",
            to_status=to_status, actor_id=actor_id, note=note,
        )
        if to_status == "CONFIRMED":
            order.confirmed_at = _now()
        return True

    @transactional(write=True)
    async def advance_delivery_status(
        self, session: AsyncSession, order_id: str, to_status: str,
        *, actor_id: Optional[str] = None,
    ) -> bool:
        order = await session.get(Order, order_id)
        if order is None:
            return False
        await self._transition(
            session, order, status_type="DELIVERY", field="delivery_status",
            to_status=to_status, actor_id=actor_id,
        )
        return True

    # ── Paiement (journal + reflet du statut sur la commande) ───────────────
    @transactional(write=True)
    async def record_payment(self, session: AsyncSession, payment: PaymentModel) -> str:
        obj = Payment(**payment.to_db())
        session.add(obj)
        await session.flush()

        order = await session.get(Order, obj.order_id)
        if order is not None and obj.status == "CAPTURED":
            await self._transition(
                session, order, status_type="PAYMENT", field="payment_status",
                to_status="PAID", note=f"payment:{obj.id}",
            )
        return str(obj.id)

    # ── Relances automatiques ───────────────────────────────────────────────
    @transactional(write=True)
    async def schedule_reminder(self, session: AsyncSession, reminder: OrderReminderModel) -> str:
        obj = OrderReminder(**reminder.to_db())
        session.add(obj)
        await session.flush()
        return str(obj.id)

    @transactional(write=False)
    async def due_reminders(self, session: AsyncSession, *, limit: int = 100) -> list[dict]:
        stmt = (
            select(OrderReminder)
            .where(OrderReminder.status == "SCHEDULED")
            .where(OrderReminder.scheduled_at <= _now())
            .order_by(OrderReminder.scheduled_at.asc())
            .limit(limit)
        )
        rows = (await session.execute(stmt)).scalars().all()
        return [r.to_dict() for r in rows]
