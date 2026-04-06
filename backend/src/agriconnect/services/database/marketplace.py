from typing import Any, Dict, List, Optional
from sqlalchemy import select, update, delete, func, and_, or_, desc
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime

from .common import _uuid, logger, Farm, Stock, StockMovement, Product, Order, OrderItem, Client, Expense, CropCycle, Producer, User, Zone
from agriconnect.services.models_legacy import SurplusOffer


class MarketplaceMixin:
    async def get_or_create_farm(self, session: AsyncSession, producer_id: str, farm_name: str = "Ma ferme", zone_id: str = None) -> Dict[str, Any]:
        stmt = select(Farm).where(Farm.producer_id == producer_id)
        result = await session.execute(stmt)
        farm = result.scalar_one_or_none()
        if farm:
            return farm.to_dict()

        farm_id = _uuid()
        farm = Farm(id=farm_id, name=farm_name, producer_id=producer_id, zone_id=zone_id)
        session.add(farm)
        await session.flush()
        logger.info("Ferme créée: %s pour producer %s", farm_name, producer_id)
        return farm.to_dict()

    async def get_farms(self, session: AsyncSession, producer_id: str) -> List[Dict[str, Any]]:
        stmt = select(Farm).where(Farm.producer_id == producer_id)
        result = await session.execute(stmt)
        return [f.to_dict() for f in result.scalars()]

    async def update_farm(self, session: AsyncSession, farm_id: str, **kwargs) -> Optional[Dict[str, Any]]:
        stmt = select(Farm).where(Farm.id == farm_id)
        result = await session.execute(stmt)
        farm = result.scalar_one_or_none()
        if not farm:
            return None
        for k, v in kwargs.items():
            if hasattr(farm, k) and v is not None:
                setattr(farm, k, v)
        await session.flush()
        return farm.to_dict()

    # Stocks
    async def add_stock(self, session: AsyncSession, farm_id: str, item_name: str, quantity: float, unit: str = "KG", stock_type: str = "HARVEST", reason: str = "Ajout via agent", warehouse_id: str = None, organization_id: str = None) -> Dict[str, Any]:
        stmt = select(Stock).where(Stock.farm_id == farm_id, func.lower(Stock.item_name) == item_name.lower()).with_for_update()
        result = await session.execute(stmt)
        stock = result.scalar_one_or_none()

        if stock:
            stock.quantity += quantity
            stock_id = stock.id
            new_total = stock.quantity
        else:
            stock_id = _uuid()
            stock = Stock(id=stock_id, farm_id=farm_id, item_name=item_name, quantity=quantity, unit=unit, type=stock_type, warehouse_id=warehouse_id, organization_id=organization_id)
            session.add(stock)
            new_total = quantity

        mvt = StockMovement(id=_uuid(), stock_id=stock_id, type="IN", quantity=quantity, reason=reason)
        session.add(mvt)
        await session.flush()

        return {"stock_id": stock_id, "item_name": item_name, "added": quantity, "new_total": new_total, "unit": unit}

    async def remove_stock(self, session: AsyncSession, farm_id: str, item_name: str, quantity: float, reason: str = "Retrait", movement_type: str = "OUT") -> Dict[str, Any]:
        stmt = select(Stock).where(Stock.farm_id == farm_id, func.lower(Stock.item_name) == item_name.lower()).with_for_update()
        result = await session.execute(stmt)
        stock = result.scalar_one_or_none()

        if not stock:
            return {"error": f"Aucun stock de {item_name} trouvé."}
        if stock.quantity < quantity:
            return {"error": f"Stock insuffisant: {stock.quantity} {stock.unit} disponibles."}

        stock.quantity -= quantity
        mvt = StockMovement(id=_uuid(), stock_id=stock.id, type=movement_type, quantity=quantity, reason=reason)
        session.add(mvt)
        await session.flush()

        return {"stock_id": stock.id, "item_name": item_name, "removed": quantity, "remaining": stock.quantity, "unit": stock.unit}

    async def adjust_stock(self, session: AsyncSession, farm_id: str, item_name: str, quantity_change: float, reason: str = "Adjustment via MCP", unit: str = "KG", stock_type: str = "HARVEST", warehouse_id: str = None, organization_id: str = None) -> Dict[str, Any]:
        if quantity_change >= 0:
            return await MarketplaceMixin.add_stock(self, session, farm_id=farm_id, item_name=item_name, quantity=quantity_change, unit=unit, stock_type=stock_type, reason=reason, warehouse_id=warehouse_id, organization_id=organization_id)
        else:
            return await MarketplaceMixin.remove_stock(self, session, farm_id=farm_id, item_name=item_name, quantity=abs(quantity_change), reason=reason)

    async def get_stocks(self, session: AsyncSession, farm_id: str) -> List[Dict[str, Any]]:
        stmt = select(Stock).where(Stock.farm_id == farm_id).order_by(Stock.item_name)
        result = await session.execute(stmt)
        return [s.to_dict() for s in result.scalars()]

    async def get_stock_movements(self, session: AsyncSession, stock_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        stmt = select(StockMovement).where(StockMovement.stock_id == stock_id).order_by(desc(StockMovement.created_at)).limit(limit)
        result = await session.execute(stmt)
        return [{"type": m.type, "quantity": m.quantity, "reason": m.reason, "date": m.created_at.isoformat() if m.created_at else None} for m in result.scalars()]

    # Products
    async def create_product(self, session: AsyncSession, producer_id: str, name: str, price: float, quantity_for_sale: float, unit: str = "KG", category_label: str = "Céréales", sub_category_id: str = None, description: str = None, local_names: dict = None) -> Dict[str, Any]:
        product_id = _uuid()
        short_code = product_id[:8].upper()
        product = Product(id=product_id, short_code=short_code, name=name, category_label=category_label, sub_category_id=sub_category_id, description=description, price=price, unit=unit, quantity_for_sale=quantity_for_sale, producer_id=producer_id, local_names=local_names)
        session.add(product)
        await session.flush()

        return {"product_id": product_id, "short_code": short_code, "name": name, "price_fcfa": price, "quantity": quantity_for_sale, "unit": unit}

    async def list_products(self, session: AsyncSession, producer_id: str) -> List[Dict[str, Any]]:
        stmt = select(Product).where(Product.producer_id == producer_id).order_by(desc(Product.created_at))
        result = await session.execute(stmt)
        return [p.to_dict() for p in result.scalars()]

    async def search_products(self, session: AsyncSession, product_name: str, zone_id: str = None, limit: int = 10) -> List[Dict[str, Any]]:
        stmt = select(Product.id, Product.name, Product.price, Product.quantity_for_sale, Product.unit, Product.short_code, Producer.zone_id, User.phone.label("producer_phone"), User.name.label("producer_name")).join(Producer, Producer.id == Product.producer_id).join(User, User.id == Producer.user_id).where(Product.name.ilike(f"%{product_name}%"), Product.quantity_for_sale > 0)
        if zone_id:
            stmt = stmt.where(or_(Producer.zone_id == zone_id, Producer.zone_id.in_(select(Zone.id).where(Zone.parent_id == select(Zone.parent_id).where(Zone.id == zone_id).scalar_subquery()))))
        stmt = stmt.order_by(Product.price.asc()).limit(limit)
        result = await session.execute(stmt)
        return [dict(r._mapping) for r in result]

    # Orders
    async def create_order(self, session: AsyncSession, product_id: str, quantity: float, buyer_phone: str, buyer_name: str = None, zone_id: str = None, source: str = "WHATSAPP", buyer_id: str = None, organization_id: str = None, payment_method: str = "CASH") -> Dict[str, Any]:
        stmt = select(Product).where(Product.id == product_id).with_for_update()
        result = await session.execute(stmt)
        product = result.scalar_one_or_none()
        if not product:
            return {"error": "Produit introuvable."}
        if product.quantity_for_sale < quantity:
            return {"error": f"Stock insuffisant: {product.quantity_for_sale} {product.unit} disponibles."}

        total = product.price * quantity
        order_id = _uuid()

        order = Order(id=order_id, buyer_id=buyer_id, customer_name=buyer_name, customer_phone=buyer_phone, zone_id=zone_id, payment_method=payment_method, status="PENDING", source=source, total_amount=total, is_agent_order=True, organization_id=organization_id)
        session.add(order)

        item = OrderItem(id=_uuid(), order_id=order_id, product_id=product_id, quantity=quantity, price_at_sale=product.price)
        session.add(item)

        product.quantity_for_sale -= quantity
        await session.flush()

        return {"order_id": order_id, "product_name": product.name, "quantity": quantity, "total_fcfa": total, "status": "PENDING", "payment_method": payment_method}

    async def get_orders(self, session: AsyncSession, buyer_id: str = None, buyer_phone: str = None, status: str = None, limit: int = 20) -> List[Dict[str, Any]]:
        stmt = select(Order)
        conditions = []
        if buyer_id:
            conditions.append(Order.buyer_id == buyer_id)
        if buyer_phone:
            conditions.append(Order.customer_phone == buyer_phone)
        if status:
            conditions.append(Order.status == status)
        if conditions:
            stmt = stmt.where(and_(*conditions))
        stmt = stmt.order_by(desc(Order.created_at)).limit(limit)
        result = await session.execute(stmt)
        return [o.to_dict() for o in result.scalars()]

    async def update_order_status(self, session: AsyncSession, order_id: str, new_status: str, payment_status: str = None) -> Optional[Dict[str, Any]]:
        stmt = select(Order).where(Order.id == order_id)
        result = await session.execute(stmt)
        order = result.scalar_one_or_none()
        if not order:
            return None
        order.status = new_status
        if payment_status:
            order.payment_status = payment_status
        await session.flush()
        return order.to_dict()

    # Clients
    async def get_or_create_client(self, session: AsyncSession, producer_id: str, name: str, phone: str, email: str = None, location: str = None) -> Dict[str, Any]:
        stmt = select(Client).where(Client.producer_id == producer_id, Client.phone == phone)
        result = await session.execute(stmt)
        client = result.scalar_one_or_none()
        if client:
            return client.to_dict()

        client = Client(id=_uuid(), name=name, phone=phone, email=email, location=location, producer_id=producer_id)
        session.add(client)
        await session.flush()
        return client.to_dict()

    async def get_clients(self, session: AsyncSession, producer_id: str) -> List[Dict[str, Any]]:
        stmt = select(Client).where(Client.producer_id == producer_id).order_by(desc(Client.total_spent))
        result = await session.execute(stmt)
        return [c.to_dict() for c in result.scalars()]

    # Expenses
    async def add_expense(self, session: AsyncSession, farm_id: str, label: str, amount: float, category: str = "OTHER", date: datetime = None) -> Dict[str, Any]:
        expense = Expense(id=_uuid(), farm_id=farm_id, label=label, amount=amount, category=category, date=date or datetime.utcnow())
        session.add(expense)
        await session.flush()
        return expense.to_dict()

    async def get_expenses(self, session: AsyncSession, farm_id: str, category: str = None, limit: int = 50) -> List[Dict[str, Any]]:
        stmt = select(Expense).where(Expense.farm_id == farm_id)
        if category:
            stmt = stmt.where(Expense.category == category)
        stmt = stmt.order_by(desc(Expense.date)).limit(limit)
        result = await session.execute(stmt)
        return [e.to_dict() for e in result.scalars()]

    async def get_expense_summary(self, session: AsyncSession, farm_id: str) -> Dict[str, Any]:
        stmt = select(Expense.category, func.sum(Expense.amount).label("total"), func.count(Expense.id).label("count")).where(Expense.farm_id == farm_id).group_by(Expense.category)
        result = await session.execute(stmt)
        categories = {}
        grand_total = 0
        for row in result:
            categories[row.category] = {"total": row.total, "count": row.count}
            grand_total += row.total
        return {"categories": categories, "grand_total": grand_total}

    # Crops
    async def create_crop_cycle(self, session: AsyncSession, farm_id: str, crop_type: str, area_size: float, planted_at: datetime, expected_harvest_date: datetime, expected_yield: float, status: str = "PLANTED") -> Dict[str, Any]:
        cycle = CropCycle(id=_uuid(), farm_id=farm_id, crop_type=crop_type, area_size=area_size, planted_at=planted_at, expected_harvest_date=expected_harvest_date, expected_yield=expected_yield, status=status)
        session.add(cycle)
        await session.flush()
        return cycle.to_dict()

    async def get_crop_cycles(self, session: AsyncSession, farm_id: str) -> List[Dict[str, Any]]:
        stmt = select(CropCycle).where(CropCycle.farm_id == farm_id)
        result = await session.execute(stmt)
        return [c.to_dict() for c in result.scalars()]

    # Surplus offers
    async def create_surplus_offer(self, session: AsyncSession, user_id: str | None, product_name: str, quantity_kg: float, price_kg: float | None = None, zone_id: str | None = None, location: str | None = None, channel: str = "api") -> Dict[str, Any]:
        """Persist a surplus offer reported by MarketCoach.

        This is the async counterpart to the legacy `save_surplus_offer` helper
        kept for compatibility in the synchronous DB handler.
        """
        offer_id = _uuid()
        offer = SurplusOffer(
            id=offer_id,
            user_id=user_id or "anonymous",
            product_name=product_name,
            quantity_kg=quantity_kg,
            price_kg=price_kg,
            zone_id=zone_id,
            location=location,
            channel=channel,
        )
        session.add(offer)
        await session.flush()
        logger.info("💰 Surplus offer saved: %s kg of %s (id=%s)", quantity_kg, product_name, offer_id)
        return offer.to_dict()
