from typing import Any, Dict, Optional
from sqlalchemy import select, delete, or_
from sqlalchemy.ext.asyncio import AsyncSession

from .common import _uuid, TransactionStaging, Product, Order, OrderItem


class TransactionsMixin:
    async def prepare_transaction_staging(self, session: AsyncSession, payload: dict, expires_in_seconds: int = 3600) -> Dict[str, Any]:
        import datetime
        ts_id = _uuid()
        tx_id = payload.get("transaction_id") or _uuid()
        expires_at = datetime.datetime.utcnow() + datetime.timedelta(seconds=expires_in_seconds)
        staging = TransactionStaging(id=ts_id, transaction_id=tx_id, payload=payload, status="PENDING", expires_at=expires_at)
        session.add(staging)
        await session.flush()
        return staging.to_dict()

    async def get_staged_transaction(self, session: AsyncSession, transaction_id: str) -> Optional[Dict[str, Any]]:
        stmt = select(TransactionStaging).where(TransactionStaging.transaction_id == transaction_id)
        result = await session.execute(stmt)
        st = result.scalar_one_or_none()
        return st.to_dict() if st else None

    async def commit_staged_transaction(self, session: AsyncSession, transaction_id: str, approved: bool = True) -> Dict[str, Any]:
        stmt = select(TransactionStaging).where(TransactionStaging.transaction_id == transaction_id).with_for_update()
        result = await session.execute(stmt)
        st = result.scalar_one_or_none()
        if not st:
            return {"status": "NOT_FOUND", "message": "Transaction introuvable."}

        if st.status != "PENDING":
            return {"status": "ALREADY_HANDLED", "message": f"Transaction déjà {st.status}."}

        if not approved:
            st.status = "REJECTED"
            await session.flush()
            return {"status": "REJECTED", "message": "Transaction annulée."}

        payload = st.payload or {}
        product_id = payload.get("product_id")
        quantity = float(payload.get("quantity_kg", 0))
        price = float(payload.get("price_fcfa_per_unit", 0))
        buyer_phone = payload.get("buyer_phone")

        prod_stmt = select(Product).where(Product.id == product_id).with_for_update()
        prod_res = await session.execute(prod_stmt)
        product = prod_res.scalar_one_or_none()
        if not product:
            st.status = "FAILED"
            await session.flush()
            return {"status": "FAILED", "message": "Produit introuvable."}

        if product.quantity_for_sale < quantity:
            st.status = "FAILED"
            await session.flush()
            return {"status": "FAILED", "message": "Stock insuffisant."}

        order_id = _uuid()
        total = round(quantity * price, 2)
        order = Order(id=order_id, buyer_id=None, customer_name=None, customer_phone=buyer_phone, zone_id=payload.get("zone_id"), payment_method=payload.get("payment_method", "CASH"), status="PENDING", source=payload.get("source", "MCP"), total_amount=total, is_agent_order=True, organization_id=None)
        session.add(order)
        item = OrderItem(id=_uuid(), order_id=order_id, product_id=product_id, quantity=quantity, price_at_sale=price)
        session.add(item)

        product.quantity_for_sale -= quantity

        try:
            await self.log_audit(session, actor_id="system", action="COMMIT_TRANSACTION", entity_type="order", entity_id=order_id, old_value=None, new_value={"order_id": order_id, "total": total})
        except Exception:
            pass

        st.status = "COMMITTED"
        await session.flush()

        return {"status": "COMMITTED", "message": "Transaction enregistrée.", "order_id": order_id}

    async def delete_expired_stagings(self, session: AsyncSession, older_than_seconds: int = 0) -> int:
        from datetime import datetime, timedelta
        now = datetime.utcnow()
        cutoff = now - timedelta(seconds=older_than_seconds) if older_than_seconds > 0 else now
        stmt = delete(TransactionStaging).where(or_(TransactionStaging.expires_at.is_not(None), TransactionStaging.created_at.is_not(None))).where(or_(TransactionStaging.expires_at < now, TransactionStaging.created_at < cutoff))
        result = await session.execute(stmt)
        await session.flush()
        return result.rowcount
