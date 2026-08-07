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
            _, producer = await self.get_producer_profile(phone)

            stmt = (
                select(Product)
                .where(Product.producer_id == producer.id)
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

            return {"status": "success", "data": result_list}

        except ValueError as e:
            return {"status": "error", "message": str(e)}

    # ─── SECTION 2 : ÉDITION ET VISIBILITÉ ──────────────────────────────────

    async def update_product_price_and_qty(
        self,
        phone: str,
        product_id: str,
        price: Optional[float] = None,
        quantity: Optional[float] = None,
        name: Optional[str] = None,
        unit: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Met à jour partiellement un produit du catalogue : prix, quantité, nom
        et/ou unité (seuls les champs fournis sont modifiés).
        Sécurisé par un verrou d'écriture (Row-Level Locking) via self.session.
        """
        try:
            phone = clean_text(phone, "phone", required=True)
            product_id = clean_text(product_id, "product_id", required=True)
            _, producer = await self.get_producer_profile(phone)

            stmt = (
                select(Product)
                .where(and_(Product.id == uuid.UUID(product_id), Product.producer_id == producer.id))
                .with_for_update()
            )
            res = await self.session.execute(stmt)
            product = res.scalar_one_or_none()

            if not product:
                return {"status": "error", "message": "Produit introuvable ou non autorisé."}

            changed: List[str] = []
            if price is not None:
                product.price = positive_float(price, "price", allow_zero=True)
                changed.append("prix")
            if quantity is not None:
                product.quantity_for_sale = positive_float(quantity, "quantity", allow_zero=True)
                changed.append("quantité")
            if name is not None:
                clean_name = clean_text(name, "name", required=True, max_length=120)
                product.name = clean_name
                changed.append("nom")
            if unit is not None:
                clean_unit = clean_text(unit, "unit", max_length=16)
                if clean_unit:
                    product.unit = clean_unit.upper()
                    changed.append("unité")

            if not changed:
                return {"status": "error", "message": "Aucun champ à modifier n'a été fourni."}

            await self.session.flush()
            await self.session.refresh(product)

            logger.info("PRODUCT_UPDATED: ID %s par %s (champs: %s)", product_id, phone, ", ".join(changed))

            product_dict = product.to_dict()
            if isinstance(product_dict.get("price"), Decimal):
                product_dict["price"] = float(product_dict["price"])

            return {
                "status": "success",
                "message": f"Produit mis à jour ({', '.join(changed)}).",
                "data": product_dict,
            }

        except ValueError as e:
            return {"status": "error", "message": str(e)}

    async def toggle_product_availability(self, phone: str, product_id: str) -> Dict[str, Any]:
        """
        Active ou désactive un produit du catalogue (Bascule rapide de la quantité entre 0 et 1).
        """
        try:
            phone = clean_text(phone, "phone", required=True)
            product_id = clean_text(product_id, "product_id", required=True)
            _, producer = await self.get_producer_profile(phone)

            stmt = (
                select(Product)
                .where(and_(Product.id == uuid.UUID(product_id), Product.producer_id == producer.id))
                .with_for_update()
            )
            res = await self.session.execute(stmt)
            product = res.scalar_one_or_none()

            if not product:
                return {"status": "error", "message": "Produit introuvable ou non autorisé."}

            new_quantity = 0.0 if product.quantity_for_sale > 0 else 1.0
            product.quantity_for_sale = new_quantity

            await self.session.flush()
            await self.session.refresh(product)

            status_msg = "Produit remis en vente" if new_quantity > 0 else "Produit masqué du catalogue"
            product_dict = product.to_dict()
            if isinstance(product_dict.get("price"), Decimal):
                product_dict["price"] = float(product_dict["price"])

            return {
                "status": "success",
                "data": product_dict,
                "active": new_quantity > 0,
                "message": status_msg,
            }
        except ValueError as e:
            return {"status": "error", "message": str(e)}

    # ─── SECTION 3 : CYCLE DE VIE ET SUPPRESSION SÉCURISÉE ──────────────────

    async def delete_product(self, phone: str, product_id: str) -> Dict[str, Any]:
        """
        Supprime un produit ou l'archive après validation d'intégrité référentielle.
        """
        try:
            phone = clean_text(phone, "phone", required=True)
            product_id = clean_text(product_id, "product_id", required=True)
            _, producer = await self.get_producer_profile(phone)

            product_uuid = uuid.UUID(product_id)

            stmt = (
                select(Product)
                .where(and_(Product.id == product_uuid, Product.producer_id == producer.id))
                .with_for_update()
            )
            res = await self.session.execute(stmt)
            product = res.scalar_one_or_none()

            if not product:
                return {"status": "error", "message": "Produit introuvable ou non autorisé."}

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
                    "status": "error",
                    "message": f"Suppression impossible : {active_count} commande(s) active(s) en cours de livraison.",
                }

            if total_count > 0:
                product.quantity_for_sale = 0.0
                product.is_available = False

                await self.session.flush()
                logger.info("PRODUCT_SOFT_DELETED: ID %s archivé pour préserver l'historique.", product_id)
                return {
                    "status": "success",
                    "message": "Le produit a été retiré et masqué définitivement du catalogue (conservé pour vos statistiques de vente).",
                }

            await self.session.delete(product)
            await self.session.flush()

            logger.info("PRODUCT_HARD_DELETED: ID %s purgé physiquement de la DB par %s", product_id, phone)
            return {"status": "success", "message": "Produit supprimé avec succès."}

        except ValueError as e:
            return {"status": "error", "message": str(e)}