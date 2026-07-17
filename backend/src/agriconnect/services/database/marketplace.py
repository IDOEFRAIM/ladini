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

    # NB : `guess_category` vit désormais UNIQUEMENT dans `ProducerMgmtMixin`
    # (services/database/producer.py) — résolution DYNAMIQUE via la table
    # Category/SubCategory en base, avec repli sur mots-clés seulement en dernier
    # recours. Ce mixin en définissait un doublon STATIQUE (mapping figé, jamais
    # mis à jour) qui gagnait silencieusement via l'ordre du MRO. Voir
    # [[farm-autoprovision-critical-goals]] pour le mécanisme général du piège.

    # =======================================================================
    # GESTION DE L'EXPLOITATION (FARM)
    # =======================================================================

    # NB : `get_or_create_farm` et `update_farm` vivent désormais UNIQUEMENT dans
    # `ProducerMgmtMixin` (services/database/producer.py). Ce mixin (`MarketplaceMixin`)
    # en définissait autrefois des doublons — un piège de MRO silencieux : listé
    # AVANT `ProducerMgmtMixin` dans `AgriDatabaseService` (services/database/d.py),
    # ce doublon gagnait la résolution d'attribut et masquait la version phone-aware
    # qui auto-crée le profil Producer manquant. Symptôme observé : `get_or_create_farm`
    # rejetait "producer_id is a required property" alors que l'appelant envoyait
    # `phone` — CETTE version (supprimée) n'acceptait pas `phone` du tout. Voir
    # [[farm-autoprovision-critical-goals]]. Ne pas réintroduire un mixin dupliqué
    # sans vérifier `AgriDatabaseService.__mro__` pour la méthode concernée.

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

    # NB : `get_stocks` vit désormais UNIQUEMENT dans `ProducerMgmtMixin`
    # (services/database/producer.py) — vue catalogue complète (Product avec
    # prix/quantité_en_vente, multi-fermes, cycles à venir), résolue par
    # phone/producer_id. Ce mixin en définissait un doublon TRIVIAL (un simple
    # dump `Stock` pour UNE ferme, aucune donnée Product) qui gagnait
    # silencieusement via le MRO — cause du bug « le producteur ne voit pas
    # son catalogue » (SALES_GET_CATALOG). Voir [[farm-autoprovision-critical-goals]].

    async def get_stock_movements(self, stock_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        current_session = self.session
        stmt = select(StockMovement).where(StockMovement.stock_id == stock_id).order_by(desc(StockMovement.created_at)).limit(limit)
        result = await current_session.execute(stmt)
        return [{"type": m.type, "quantity": m.quantity, "reason": m.reason, "date": m.created_at.isoformat() if m.created_at else None} for m in result.scalars()]

    # =======================================================================
    # PRODUCTS
    # =======================================================================

    # NB : `create_product` vit désormais UNIQUEMENT dans `ProducerMgmtMixin`
    # (services/database/producer.py) — valide l'existence du profil producteur
    # (message clair sinon) ET auto-résout `sub_category_id` depuis le nom si
    # absent. Ce mixin en définissait un doublon qui laissait TOUJOURS
    # `sub_category_id=NULL` (jamais résolu) — cassait silencieusement le
    # filtrage MATCHABLE de `get_producer_auctions` (qui filtre sur
    # `Product.sub_category_id`), forçant un repli permanent sur ALL pour
    # chaque producteur. Voir [[farm-autoprovision-critical-goals]],
    # [[auction-bid-lifecycle]].

    async def record_sale(
        self,
        phone: str,
        product_name: str,
        quantity: float,
        total_price: float,
        unit: str = "KG",
        client_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Journalise une vente directe effectuée par un producteur."""

        current_session = self.session
        resolved_producer_id = await self._resolve_producer_id(phone)
        if isinstance(resolved_producer_id, dict):
            return resolved_producer_id

        product_label = clean_text(product_name, "product_name", required=True)
        unit_clean = clean_text(unit, "unit", required=False, max_length=20) or "KG"
        quantity_value = positive_float(quantity, "quantity")
        amount_value = positive_float(total_price, "total_price", allow_zero=True)

        stmt_product = (
            select(Product)
            .where(
                Product.producer_id == resolved_producer_id,
                func.lower(Product.name) == product_label.lower(),
            )
            .limit(1)
        )
        product_result = await current_session.execute(stmt_product)
        product = product_result.scalar_one_or_none()

        if not product:
            product = Product(
                id=_uuid(),
                short_code=_uuid()[:8].upper(),
                name=product_label,
                category_label="VENTE_DIRECTE",
                price=amount_value / quantity_value if quantity_value else amount_value,
                unit=unit_clean,
                quantity_for_sale=0,
                producer_id=resolved_producer_id,
                is_available=False,
            )
            current_session.add(product)
            await current_session.flush()

        order_id = _uuid()
        order = Order(
            id=order_id,
            customer_name=client_name or "Vente directe",
            customer_phone=str(phone).strip(),
            payment_method="CASH",
            payment_status="PAID",
            status="COMPLETED",
            delivery_status="FULFILLED",
            source="AGENT",
            order_type="DIRECT_SALE",
            total_amount=amount_value,
            subtotal=amount_value,
            tax_amount=0.0,
            currency="XOF",
            delivery_fee=0.0,
            is_agent_order=True,
        )
        current_session.add(order)

        item_price = amount_value / quantity_value if quantity_value else amount_value
        order_item = OrderItem(
            id=_uuid(),
            order_id=order_id,
            product_id=product.id,
            quantity=quantity_value,
            price_at_sale=item_price,
        )
        current_session.add(order_item)

        await current_session.flush()

        return {
            "status": "success",
            "data": {
                "sale_id": order_id,
                "product_id": str(product.id),
                "quantity": quantity_value,
                "unit": unit_clean,
                "total_amount": amount_value,
                "price_per_unit": item_price,
            },
        }

    # =======================================================================
    # ORDERS
    # =======================================================================

    # NB : `update_order_status` vit désormais UNIQUEMENT dans `ProducerMgmtMixin`
    # (services/database/producer.py) — verrouille la ligne (`with_for_update`)
    # avant mise à jour, évitant une course entre deux mises à jour concurrentes
    # du même statut de commande. Ce mixin en définissait un doublon sans
    # verrou. Voir [[farm-autoprovision-critical-goals]].

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
