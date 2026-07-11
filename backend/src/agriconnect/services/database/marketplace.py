from typing import Any, Dict, List, Optional, Union
import logging
from sqlalchemy import select, func, desc
from datetime import datetime
import uuid
from .common import clean_text, positive_float
from .base import BaseMixin
from agriconnect.domain.models import (
    Client,
    Expense,
    Farm,
    Order,
    OrderItem,
    Producer,
    Product,
    Stock,
    StockMovement,
    User,
    Zone,
    _uuid4,
)


logger = logging.getLogger("agriconnect.services.database")


def _uuid() -> str:
    return _uuid4()


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError):
        return False


class MarketplaceMixin(BaseMixin):

    # =======================================================================
    # RESOLUTION HELPERS
    # =======================================================================

    async def _resolve_producer_id(self, identifier: str) -> Union[str, Dict[str, Any]]:
        """Resolve a producer_id from either a UUID or a phone number."""
        current_session = self.session
        if _is_uuid(identifier):
            stmt = select(Producer).where(Producer.id == identifier)
            result = await current_session.execute(stmt)
            prod = result.scalar_one_or_none()
            if prod:
                return str(prod.id)
            stmt2 = select(Producer).where(Producer.user_id == identifier)
            result2 = await current_session.execute(stmt2)
            prod2 = result2.scalar_one_or_none()
            if prod2:
                return str(prod2.id)
            return {"status": "error", "message": f"Profil producteur introuvable pour l'identifiant : {identifier}"}
        else:
            stmt = select(User).where(User.phone == str(identifier).strip())
            result = await current_session.execute(stmt)
            user = result.scalar_one_or_none()
            if not user:
                return {"status": "error", "message": f"Aucun utilisateur trouvé pour : {identifier}"}
            stmt2 = select(Producer).where(Producer.user_id == user.id)
            result2 = await current_session.execute(stmt2)
            prod = result2.scalar_one_or_none()
            if not prod:
                return {"status": "error", "message": f"Profil producteur introuvable pour l'utilisateur : {identifier}"}
            return str(prod.id)

    # =======================================================================
    # CATEGORY RESOLUTION
    # =======================================================================

    async def guess_category(self, product_name: str) -> str:
        """Guess the product category from its name using fallback mapping."""
        if not product_name:
            return "AUTRES"

        name_clean = product_name.strip().upper()

        MAPPING = {
            "CÉRÉALES": ["MAÏS", "RIZ", "MIL", "SORGHO", "FONIO", "BLÉ"],
            "LÉGUMES": ["TOMATE", "OIGNON", "PIMENT", "CHOU", "GOMBO", "CAROTTE", "HARICOT"],
            "FRUITS": ["MANGUE", "ORANGE", "CITRON", "BANANE", "PAPAYE", "ANANAS"],
            "ANIMAUX": ["POULET", "BOEUF", "MOUTON", "CHÈVRE", "OEUF", "PORC"],
            "TUBERCULES": ["MANIOC", "IGNAME", "PATATE", "POMME DE TERRE"],
        }

        for category, keywords in MAPPING.items():
            if any(kw in name_clean for kw in keywords):
                return category

        return "AUTRES"

    # =======================================================================
    # GESTION DE L'EXPLOITATION (FARM)
    # =======================================================================

    async def get_or_create_farm(self, producer_id: str, farm_name: str = "Ma ferme", zone_id: str = None) -> Dict[str, Any]:
        """Récupère la ferme existante d'un producteur ou en crée une nouvelle."""
        current_session = self.session
        resolved_producer_id = await self._resolve_producer_id(producer_id)
        if isinstance(resolved_producer_id, dict):
            return resolved_producer_id

        stmt = select(Farm).where(Farm.producer_id == resolved_producer_id)
        result = await current_session.execute(stmt)
        farm = result.scalar_one_or_none()

        if farm:
            return farm.to_dict()

        farm_id = _uuid()
        farm = Farm(id=farm_id, name=farm_name, producer_id=resolved_producer_id, zone_id=zone_id)
        current_session.add(farm)
        await current_session.flush()

        logger.info("Ferme créée: %s pour producer_id=%s", farm_name, resolved_producer_id)
        return farm.to_dict()

    async def update_farm(self, farm_id: str, **kwargs) -> Optional[Dict[str, Any]]:
        """Met à jour les attributs d'une ferme via son identifiant."""
        current_session = self.session
        try:
            stmt = select(Farm).where(Farm.id == farm_id)
            result = await current_session.execute(stmt)
            farm = result.scalar_one_or_none()

            if not farm:
                logger.warning("Ferme introuvable: %s", farm_id)
                return None

            for key, value in kwargs.items():
                if key not in ("id", "producer_id") and hasattr(farm, key):
                    setattr(farm, key, value)

            await current_session.flush()
            await current_session.refresh(farm)
            return farm.to_dict()

        except Exception as e:
            logger.error("Erreur critique lors de la mise à jour de la ferme (%s) : %s", farm_id, e)
            raise

    # =======================================================================
    # GESTION DES STOCKS
    # =======================================================================

    async def add_stock(self, farm_id: str, item_name: str, quantity: float, unit: str = "KG", stock_type: str = "HARVEST", reason: str = "Ajout via agent", warehouse_id: str = None, organization_id: str = None) -> Dict[str, Any]:
        """Incrémente ou crée une ligne de stock pour un produit donné."""
        current_session = self.session
        farm_id = clean_text(farm_id, "farm_id", required=True)
        item_name = clean_text(item_name, "item_name", required=True)
        quantity = positive_float(quantity, "quantity")
        unit = clean_text(unit, "unit", required=False, max_length=20) or "KG"

        try:
            farm_stmt = select(Farm).where(Farm.id == farm_id)
            farm_result = await current_session.execute(farm_stmt)
            farm = farm_result.scalar_one_or_none()
            if not farm:
                return {"status": "error", "message": f"Ferme introuvable: {farm_id}"}

            stmt = select(Stock).where(Stock.farm_id == farm_id, func.lower(Stock.item_name) == item_name.lower()).with_for_update()
            result = await current_session.execute(stmt)
            stock = result.scalar_one_or_none()

            if stock:
                stock.quantity += quantity
                stock_id = str(stock.id)
                new_total = stock.quantity
            else:
                stock_id = _uuid()
                stock = Stock(id=stock_id, farm_id=farm_id, item_name=item_name, quantity=quantity, unit=unit, type=stock_type, warehouse_id=warehouse_id, organization_id=organization_id)
                current_session.add(stock)
                new_total = quantity

            mvt = StockMovement(id=_uuid(), stock_id=stock_id, type="IN", quantity=quantity, reason=reason)
            current_session.add(mvt)
            await current_session.flush()

            return {"status": "success", "data": {"stock_id": stock_id, "item_name": item_name, "added": quantity, "new_total": new_total, "unit": unit}}

        except ValueError as e:
            return {"status": "error", "message": str(e)}

    async def remove_stock(self, farm_id: str, item_name: str, quantity: float, reason: str = "Retrait", movement_type: str = "OUT") -> Dict[str, Any]:
        """Décrémente le stock d'un produit après vérification des disponibilités."""
        current_session = self.session
        farm_id = clean_text(farm_id, "farm_id", required=True)
        item_name = clean_text(item_name, "item_name", required=True)
        quantity = positive_float(quantity, "quantity")

        try:
            stmt = select(Stock).where(Stock.farm_id == farm_id, func.lower(Stock.item_name) == item_name.lower()).with_for_update()
            result = await current_session.execute(stmt)
            stock = result.scalar_one_or_none()

            if not stock:
                return {"status": "error", "message": f"Aucun stock de '{item_name}' trouvé pour cette exploitation."}
            if stock.quantity < quantity:
                return {"status": "error", "message": f"Stock insuffisant : {stock.quantity} {stock.unit} disponibles, retrait de {quantity} {stock.unit} demandé."}

            stock.quantity -= quantity

            mvt = StockMovement(id=_uuid(), stock_id=str(stock.id), type=movement_type, quantity=quantity, reason=reason)
            current_session.add(mvt)
            await current_session.flush()

            return {"status": "success", "data": {"stock_id": str(stock.id), "item_name": item_name, "removed": quantity, "remaining": stock.quantity, "unit": stock.unit}}

        except ValueError as e:
            return {"status": "error", "message": str(e)}

    async def adjust_stock(self, farm_id: str, item_name: str, quantity_change: float, reason: str = "Adjustment via MCP", unit: str = "KG", stock_type: str = "HARVEST", warehouse_id: str = None, organization_id: str = None) -> Dict[str, Any]:
        if quantity_change >= 0:
            return await self.add_stock(farm_id=farm_id, item_name=item_name, quantity=quantity_change, unit=unit, stock_type=stock_type, reason=reason, warehouse_id=warehouse_id, organization_id=organization_id)
        else:
            return await self.remove_stock(farm_id=farm_id, item_name=item_name, quantity=abs(quantity_change), reason=reason)

    async def get_stocks(self, farm_id: str) -> Union[List[Dict[str, Any]], Dict[str, str]]:
        current_session = self.session
        stmt = select(Stock).where(Stock.farm_id == farm_id).order_by(Stock.item_name)
        result = await current_session.execute(stmt)
        return [s.to_dict() for s in result.scalars()]

    async def get_stock_movements(self, stock_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        current_session = self.session
        stmt = select(StockMovement).where(StockMovement.stock_id == stock_id).order_by(desc(StockMovement.created_at)).limit(limit)
        result = await current_session.execute(stmt)
        return [{"type": m.type, "quantity": m.quantity, "reason": m.reason, "date": m.created_at.isoformat() if m.created_at else None} for m in result.scalars()]

    # =======================================================================
    # PRODUCTS
    # =======================================================================

    async def create_product(self, producer_id: str, name: str, price: float, quantity_for_sale: float, unit: str = "KG", category_label: str = None, sub_category_id: str = None, description: str = None, local_names: dict = None) -> Dict[str, Any]:
        """Crée un produit pour un producteur."""
        current_session = self.session
        producer_id = clean_text(producer_id, "producer_id", required=True)
        name = clean_text(name, "name", required=True)
        price = positive_float(price, "price", allow_zero=True)
        quantity_for_sale = positive_float(quantity_for_sale, "quantity_for_sale", allow_zero=True)
        unit = clean_text(unit, "unit", required=False, max_length=20) or "KG"

        resolved_producer_id = await self._resolve_producer_id(producer_id)
        if isinstance(resolved_producer_id, dict):
            return resolved_producer_id

        product_id = _uuid()
        short_code = product_id[:8].upper()

        category_label = clean_text(category_label, "category_label", required=False) or await self.guess_category(name)

        product = Product(
            id=product_id,
            short_code=short_code,
            name=name,
            price=price,
            unit=unit,
            quantity_for_sale=quantity_for_sale,
            producer_id=resolved_producer_id,
            category_label=category_label,
            sub_category_id=sub_category_id,
            description=description,
            local_names=local_names,
        )
        current_session.add(product)
        await current_session.flush()

        return {
            "status": "success",
            "data": {
                "product_id": product_id,
                "short_code": short_code,
                "name": name,
                "price_fcfa": price,
                "quantity": quantity_for_sale,
                "unit": unit,
                "category_label": category_label,
            },
        }

    # =======================================================================
    # ORDERS
    # =======================================================================

    async def update_order_status(self, order_id: str, new_status: str, payment_status: str = None) -> Optional[Dict[str, Any]]:
        current_session = self.session
        stmt = select(Order).where(Order.id == order_id)
        result = await current_session.execute(stmt)
        order = result.scalar_one_or_none()
        if not order:
            return None
        order.status = new_status
        if payment_status:
            order.payment_status = payment_status
        await current_session.flush()
        return order.to_dict()

    # =======================================================================
    # EXPENSES
    # =======================================================================

    async def add_expense(self, farm_id: str, label: str, amount: float, category: str = "OTHER", date: datetime = None) -> Dict[str, Any]:
        current_session = self.session
        farm_id = clean_text(farm_id, "farm_id", required=True)
        label = clean_text(label, "label", required=True)
        amount = positive_float(amount, "amount")
        expense = Expense(id=_uuid(), farm_id=farm_id, label=label, amount=amount, category=category, date=date or datetime.utcnow())
        current_session.add(expense)
        await current_session.flush()
        return {"status": "success", "data": expense.to_dict()}

    async def get_expenses(self, farm_id: str, category: str = None, limit: int = 50) -> List[Dict[str, Any]]:
        current_session = self.session
        stmt = select(Expense).where(Expense.farm_id == farm_id)
        if category:
            stmt = stmt.where(Expense.category == category)
        stmt = stmt.order_by(desc(Expense.date)).limit(limit)
        result = await current_session.execute(stmt)
        return [e.to_dict() for e in result.scalars()]

    async def get_expense_summary(self, farm_id: str) -> Dict[str, Any]:
        current_session = self.session
        stmt = select(Expense.category, func.sum(Expense.amount).label("total"), func.count(Expense.id).label("count")).where(Expense.farm_id == farm_id).group_by(Expense.category)
        result = await current_session.execute(stmt)
        categories = {}
        grand_total = 0.0
        for row in result:
            categories[row.category] = {"total": float(row.total), "count": int(row.count)}
            grand_total += float(row.total)
        return {"status": "success", "data": {"categories": categories, "grand_total": grand_total}}
