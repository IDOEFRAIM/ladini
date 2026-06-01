import logging
import uuid
from typing import Any, Dict, List, Optional, Union
from decimal import Decimal

from sqlalchemy import select, update, delete, and_, func, desc
from sqlalchemy.orm import joinedload

from agriconnect.domain.models import Product, Producer, Order, OrderItem
from .base import BaseMixin
from .common import clean_text, positive_float

logger = logging.getLogger("agriconnect.services.catalog")


class ProductMixin(BaseMixin):
    """
    Mixin pour la gestion du catalogue par le producteur via son numéro de téléphone.
    L'instance gère elle-même sa session de base de données via self.session.
    """

    # ─── SECTION 1 : LECTURE ET INVENTAIRE ──────────────────────────────────

    async def get_my_products(self, phone: str) -> Union[List[Dict[str, Any]], Dict[str, str]]:
        """
        Récupère tous les produits du catalogue d'un producteur via son téléphone.
        """
        try:
            phone = clean_text(phone, "phone", required=True)
            user, _ = await self.get_producer_profile(phone)

            stmt = (
                select(Product)
                .where(Product.producer_id == user.id)
                .order_by(desc(Product.updated_at))
            )
            res = await self.session.execute(stmt)
            products = res.scalars().all()
            
            result_list = []
            for p in products:
                p_dict = p.to_dict()
                if "price" in p_dict and isinstance(p_dict["price"], Decimal):
                    p_dict["price"] = float(p_dict["price"])
                result_list.append(p_dict)
                
            return result_list
            
        except ValueError as e:
            return {"error": str(e)}

    # ─── SECTION 2 : ÉDITION ET VISIBILITÉ ──────────────────────────────────

    async def update_product_price_and_qty(self, phone: str, product_id: str, price: Optional[float] = None, quantity: Optional[float] = None) -> Dict[str, Any]:
        """
        Met à jour le prix et/ou la quantité disponible d'un produit.
        Sécurisé par un verrou d'écriture (Row-Level Locking) via self.session.
        """
        try:
            phone = clean_text(phone, "phone", required=True)
            product_id = clean_text(product_id, "product_id", required=True)
            user, _ = await self.get_producer_profile(phone)

            stmt = (
                select(Product)
                .where(and_(Product.id == uuid.UUID(product_id), Product.producer_id == user.id))
                .with_for_update()
            )
            res = await self.session.execute(stmt)
            product = res.scalar_one_or_none()

            if not product:
                return {"success": False, "error": "Produit introuvable ou non autorisé."}

            if price is not None:
                product.price = positive_float(price, "price", allow_zero=True)
            if quantity is not None:
                product.quantity_for_sale = positive_float(quantity, "quantity", allow_zero=True)

            await self.session.flush()
            # FIX ANTI-GREENLET : On recharge l'état complet de l'objet de manière asynchrone avant le to_dict()
            await self.session.refresh(product)
            
            logger.info("PRODUCT_UPDATED: ID %s par %s (Prix: %s, Qty: %s)", product_id, phone, price, quantity)
            
            product_dict = product.to_dict()
            if isinstance(product_dict.get("price"), Decimal):
                product_dict["price"] = float(product_dict["price"])
                
            return {"success": True, "product": product_dict}

        except ValueError as e:
            return {"success": False, "error": str(e)}

    async def toggle_product_availability(self, phone: str, product_id: str) -> Dict[str, Any]:
        """
        Active ou désactive un produit du catalogue (Bascule rapide de la quantité entre 0 et 1).
        """
        try:
            phone = clean_text(phone, "phone", required=True)
            product_id = clean_text(product_id, "product_id", required=True)
            user, _ = await self.get_producer_profile(phone)

            stmt = (
                select(Product)
                .where(and_(Product.id == uuid.UUID(product_id), Product.producer_id == user.id))
                .with_for_update()
            )
            res = await self.session.execute(stmt)
            product = res.scalar_one_or_none()

            if not product:
                return {"success": False, "error": "Produit introuvable ou non autorisé."}

            new_quantity = 0.0 if product.quantity_for_sale > 0 else 1.0
            product.quantity_for_sale = new_quantity
            
            await self.session.flush()
            # FIX ANTI-GREENLET : Même combat ici pour éviter l'expiration de l'état
            await self.session.refresh(product)

            status_msg = "Produit remis en vente" if new_quantity > 0 else "Produit masqué du catalogue"
            product_dict = product.to_dict()
            if isinstance(product_dict.get("price"), Decimal):
                product_dict["price"] = float(product_dict["price"])

            return {
                "success": True, 
                "active": new_quantity > 0,
                "message": status_msg,
                "product": product_dict
            }
        except ValueError as e:
            return {"success": False, "error": str(e)}

    # ─── SECTION 3 : CYCLE DE VIE ET SUPPRESSION SÉCURISÉE ──────────────────

    async def delete_product(self, phone: str, product_id: str) -> Dict[str, Any]:
        """
        Supprime un produit ou l'archive après validation d'intégrité référentielle.
        """
        try:
            phone = clean_text(phone, "phone", required=True)
            product_id = clean_text(product_id, "product_id", required=True)
            user, _ = await self.get_producer_profile(phone)
            
            product_uuid = uuid.UUID(product_id)

            stmt = (
                select(Product)
                .where(and_(Product.id == product_uuid, Product.producer_id == user.id))
                .with_for_update()
            )
            res = await self.session.execute(stmt)
            product = res.scalar_one_or_none()

            if not product:
                return {"success": False, "error": "Produit introuvable ou non autorisé."}

            stats_stmt = (
                select(
                    func.count(OrderItem.id).label("total_orders"),
                    func.count(func.nullif(Order.status.in_(['PENDING', 'CONFIRMED', 'SHIPPED']), False)).label("active_orders")
                )
                .join(Order, OrderItem.order_id == Order.id)
                .where(OrderItem.product_id == product_uuid)
            )
            stats_res = (await self.session.execute(stats_stmt)).fetchone()
            
            total_count = stats_res.total_orders if stats_res else 0
            active_count = stats_res.active_orders if stats_res else 0

            if active_count > 0:
                return {
                    "success": False, 
                    "error": f"Suppression impossible : {active_count} commande(s) active(s) en cours de livraison."
                }

            if total_count > 0:
                product.quantity_for_sale = 0.0
                if hasattr(product, 'is_archived'):
                    product.is_archived = True
                
                await self.session.flush()
                logger.info("PRODUCT_SOFT_DELETED: ID %s archivé pour préserver l'historique.", product_id)
                return {
                    "success": True, 
                    "message": "Le produit a été retiré et masqué définitivement du catalogue (conservé pour vos statistiques de vente)."
                }

            await self.session.delete(product)
            await self.session.flush()
            
            logger.info("PRODUCT_HARD_DELETED: ID %s purgé physiquement de la DB par %s", product_id, phone)
            return {"success": True, "message": "Produit supprimé avec succès."}

        except ValueError as e:
            return {"success": False, "error": str(e)}