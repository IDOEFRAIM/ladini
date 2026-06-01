from typing import Any, Dict, List, Optional,Union
import logging
from sqlalchemy import select, func, and_, or_, desc
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime
import uuid
from .common import clean_text, positive_float
from .base import BaseMixin
from agriconnect.domain.models import (
    Client,
    CropCycle,
    Expense,
    Farm,
    Order,
    OrderItem,
    Producer,
    Product,
    Stock,
    StockMovement,
    SurplusOffer,
    User,
    Zone,
    _uuid4,
)


logger = logging.getLogger("agriconnect.services.database")


def _uuid() -> str:
    return _uuid4()


def _is_uuid(value: str) -> bool:
    """Return True if *value* is a valid UUID string."""
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError):
        return False


class MarketplaceMixin(BaseMixin):

    # =======================================================================
    # RESOLUTION HELPERS
    # =======================================================================

    async def _resolve_producer_id(self, session: AsyncSession, identifier: str) -> Union[str, Dict[str, Any]]:
        """Resolve a producer_id from either a UUID or a phone number.

        Returns the Producer.id (str) on success, or an error dict on failure.
        """
        if _is_uuid(identifier):
            # Direct Producer lookup by UUID
            stmt = select(Producer).where(Producer.id == identifier)
            result = await session.execute(stmt)
            prod = result.scalar_one_or_none()
            if prod:
                return str(prod.id)
            # Maybe it's a User.id — fallback to user_id resolution
            stmt2 = select(Producer).where(Producer.user_id == identifier)
            result2 = await session.execute(stmt2)
            prod2 = result2.scalar_one_or_none()
            if prod2:
                return str(prod2.id)
            return {"error": f"Profil producteur introuvable pour l'identifiant : {identifier}"}
        else:
            # Phone-based resolution: User → Producer
            stmt = select(User).where(User.phone == str(identifier).strip())
            result = await session.execute(stmt)
            user = result.scalar_one_or_none()
            if not user:
                return {"error": f"Aucun utilisateur trouvé pour : {identifier}"}
            stmt2 = select(Producer).where(Producer.user_id == user.id)
            result2 = await session.execute(stmt2)
            prod = result2.scalar_one_or_none()
            if not prod:
                return {"error": f"Profil producteur introuvable pour l'utilisateur : {identifier}"}
            return str(prod.id)

    # =======================================================================
    # CATEGORY RESOLUTION
    # =======================================================================

    async def guess_category(self, session: AsyncSession, product_name: str) -> str:
        """Guess the product category from its name using DB lookup + fallback mapping."""
        if not product_name:
            return "AUTRES"

        name_clean = product_name.strip().upper()

        # Fallback heuristic mapping
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

    async def get_or_create_farm(self, session: AsyncSession, producer_id: str, farm_name: str = "Ma ferme", zone_id: str = None) -> Dict[str, Any]:
        """
        Récupère la ferme existante d'un producteur ou en crée une nouvelle si nécessaire.
        Accepte un producer_id (UUID du profil Producer) ou un numéro de téléphone.
        """
        # 1. Résolution du Producer (UUID direct ou via téléphone)
        resolved_producer_id = await self._resolve_producer_id(session, producer_id)
        if isinstance(resolved_producer_id, dict):
            return resolved_producer_id  # error dict

        # 2. Recherche d'une ferme existante associée à ce producteur
        stmt = select(Farm).where(Farm.producer_id == resolved_producer_id)
        result = await session.execute(stmt)
        farm = result.scalar_one_or_none()
        
        if farm:
            return farm.to_dict()

        # 3. Création de la ferme si aucune n'a été trouvée
        farm_id = _uuid()
        farm = Farm(id=farm_id, name=farm_name, producer_id=resolved_producer_id, zone_id=zone_id)
        session.add(farm)
        await session.flush()
        
        logger.info("Ferme créée: %s pour producer_id=%s", farm_name, resolved_producer_id)
        return farm.to_dict()

    async def update_farm(self, session: AsyncSession, farm_id: str, **kwargs) -> Optional[Dict[str, Any]]:
        """
        Met à jour les attributs d'une ferme via son identifiant.
        """
        try:
            stmt = select(Farm).where(Farm.id == farm_id)
            result = await session.execute(stmt)
            farm = result.scalar_one_or_none()

            if not farm:
                logger.warning("Ferme introuvable: %s", farm_id)
                return None

            # Mise à jour dynamique des attributs autorisés
            for key, value in kwargs.items():
                if key not in ("id", "producer_id") and hasattr(farm, key):
                    setattr(farm, key, value)
                else:
                    logger.debug(f"Attribut ignoré, protégé ou inexistant : {key}")

            await session.flush()
            await session.refresh(farm)

            return farm.to_dict()

        except Exception as e:
            logger.error(f"Erreur critique lors de la mise à jour de la ferme ({farm_id}) : {e}")
            raise

    # =======================================================================
    # GESTION DES STOCKS
    # =======================================================================

    async def add_stock(self, session: AsyncSession, farm_id: str, item_name: str, quantity: float, unit: str = "KG", stock_type: str = "HARVEST", reason: str = "Ajout via agent", warehouse_id: str = None, organization_id: str = None) -> Dict[str, Any]:
        """
        Incrémente ou crée une ligne de stock pour un produit donné.
        Accepte directement le farm_id (UUID de la ferme).
        """
        farm_id = clean_text(farm_id, "farm_id", required=True)
        item_name = clean_text(item_name, "item_name", required=True)
        quantity = positive_float(quantity, "quantity")
        unit = clean_text(unit, "unit", required=False, max_length=20) or "KG"

        try:
            # 1. Vérification de l'existence de la ferme
            farm_stmt = select(Farm).where(Farm.id == farm_id)
            farm_result = await session.execute(farm_stmt)
            farm = farm_result.scalar_one_or_none()
            if not farm:
                return {"error": f"Ferme introuvable: {farm_id}"}

            # 2. Recherche ou création de la ligne de stock (Verrou d'écriture sérialisé)
            stmt = select(Stock).where(Stock.farm_id == farm_id, func.lower(Stock.item_name) == item_name.lower()).with_for_update()
            result = await session.execute(stmt)
            stock = result.scalar_one_or_none()

            if stock:
                stock.quantity += quantity
                stock_id = str(stock.id)
                new_total = stock.quantity
            else:
                stock_id = _uuid()
                stock = Stock(id=stock_id, farm_id=farm_id, item_name=item_name, quantity=quantity, unit=unit, type=stock_type, warehouse_id=warehouse_id, organization_id=organization_id)
                session.add(stock)
                new_total = quantity

            # 3. Enregistrement du mouvement historique
            mvt = StockMovement(id=_uuid(), stock_id=stock_id, type="IN", quantity=quantity, reason=reason)
            session.add(mvt)
            await session.flush()

            return {"stock_id": stock_id, "item_name": item_name, "added": quantity, "new_total": new_total, "unit": unit}

        except ValueError as e:
            return {"error": str(e)}


    async def remove_stock(self, session: AsyncSession, farm_id: str, item_name: str, quantity: float, reason: str = "Retrait", movement_type: str = "OUT") -> Dict[str, Any]:
        """
        Décrémente le stock d'un produit après vérification des disponibilités.
        Accepte directement le farm_id (UUID de la ferme).
        """
        farm_id = clean_text(farm_id, "farm_id", required=True)
        item_name = clean_text(item_name, "item_name", required=True)
        quantity = positive_float(quantity, "quantity")

        try:
            # 1. Recherche et verrouillage de la ligne de stock
            stmt = select(Stock).where(Stock.farm_id == farm_id, func.lower(Stock.item_name) == item_name.lower()).with_for_update()
            result = await session.execute(stmt)
            stock = result.scalar_one_or_none()

            if not stock:
                return {"error": f"Aucun stock de '{item_name}' trouvé pour cette exploitation."}
            if stock.quantity < quantity:
                return {"error": f"Stock insuffisant : {stock.quantity} {stock.unit} disponibles, retrait de {quantity} {stock.unit} demandé."}

            stock.quantity -= quantity

            # 2. Enregistrement du mouvement de sortie historique
            mvt = StockMovement(id=_uuid(), stock_id=str(stock.id), type=movement_type, quantity=quantity, reason=reason)
            session.add(mvt)
            await session.flush()

            return {"stock_id": str(stock.id), "item_name": item_name, "removed": quantity, "remaining": stock.quantity, "unit": stock.unit}

        except ValueError as e:
            return {"error": str(e)}
            
    
    async def adjust_stock(self, session: AsyncSession, farm_id: str, item_name: str, quantity_change: float, reason: str = "Adjustment via MCP", unit: str = "KG", stock_type: str = "HARVEST", warehouse_id: str = None, organization_id: str = None) -> Dict[str, Any]:
        # Redirection vers add_stock ou remove_stock avec farm_id
        if quantity_change >= 0:
            return await MarketplaceMixin.add_stock(self, session, farm_id=farm_id, item_name=item_name, quantity=quantity_change, unit=unit, stock_type=stock_type, reason=reason, warehouse_id=warehouse_id, organization_id=organization_id)
        else:
            return await MarketplaceMixin.remove_stock(self, session, farm_id=farm_id, item_name=item_name, quantity=abs(quantity_change), reason=reason)


    async def get_stocks(self, session: AsyncSession, farm_id: str) -> Union[List[Dict[str, Any]], Dict[str, str]]:
        # Consultation des stocks par farm_id
        stmt = select(Stock).where(Stock.farm_id == farm_id).order_by(Stock.item_name)
        result = await session.execute(stmt)
        return [s.to_dict() for s in result.scalars()]


    async def get_stock_movements(self, session: AsyncSession, stock_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        # Cette fonction reste inchangée car elle s'appuie sur un identifiant de ligne de stock précis (historique d'une ligne)
        stmt = select(StockMovement).where(StockMovement.stock_id == stock_id).order_by(desc(StockMovement.created_at)).limit(limit)
        result = await session.execute(stmt)
        return [{"type": m.type, "quantity": m.quantity, "reason": m.reason, "date": m.created_at.isoformat() if m.created_at else None} for m in result.scalars()]
    # Products
    async def create_product(self, session: AsyncSession, producer_id: str, name: str, price: float, quantity_for_sale: float, unit: str = "KG", category_label: str = None, sub_category_id: str = None, description: str = None, local_names: dict = None) -> Dict[str, Any]:
        """
        Crée un produit pour un producteur.
        Accepte producer_id comme UUID du profil Producer ou comme numéro de téléphone.
        """
        producer_id = clean_text(producer_id, "producer_id", required=True)
        name = clean_text(name, "name", required=True)
        price = positive_float(price, "price", allow_zero=True)
        quantity_for_sale = positive_float(quantity_for_sale, "quantity_for_sale", allow_zero=True)
        unit = clean_text(unit, "unit", required=False, max_length=20) or "KG"

        # 1. Résolution du Producer (UUID direct ou via téléphone)
        resolved_producer_id = await self._resolve_producer_id(session, producer_id)
        if isinstance(resolved_producer_id, dict):
            return resolved_producer_id  # error dict

        # 2. Génération des identifiants et classification automatique si nécessaire
        product_id = _uuid()
        short_code = product_id[:8].upper()
        
        category_label = clean_text(category_label, "category_label", required=False) or await self.guess_category(session, name)
        
        # 3. Instanciation et persistance du produit
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
            local_names=local_names
        )
        session.add(product)
        await session.flush()

        return {
            "product_id": product_id, 
            "short_code": short_code, 
            "name": name, 
            "price_fcfa": price, 
            "quantity": quantity_for_sale, 
            "unit": unit,
            "category_label": category_label
        }
    
    
 



   

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
   

    # Expenses
    async def add_expense(self, session: AsyncSession, farm_id: str, label: str, amount: float, category: str = "OTHER", date: datetime = None) -> Dict[str, Any]:
        farm_id = clean_text(farm_id, "farm_id", required=True)
        label = clean_text(label, "label", required=True)
        amount = positive_float(amount, "amount")
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
