import logging
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional, Union

from sqlalchemy import and_, desc, func, select

from ladini.domain.analytics.emitter import BusinessEventEmitter
from ladini.domain.models import Order, OrderItem, Product
from ladini.domain.pricing_tiers import (
    PricingTierError,
    tiers_to_dicts,
    validate_pricing_tiers,
)

from .base import BaseMixin
from .common import clean_text, positive_float

logger = logging.getLogger("ladini.services.catalog")

_MAX_PHOTOS_PER_PRODUCT = 8


class ProductMixin(BaseMixin):
    """
    Mixin pour la gestion du catalogue par le producteur via son numéro de téléphone.
    L'instance gère elle-même sa session de base de données via self.session.
    """

    # ─── SECTION 1 : LECTURE ET INVENTAIRE ──────────────────────────────────

    async def get_my_products(
        self, phone: str
    ) -> Union[List[Dict[str, Any]], Dict[str, str]]:
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
        pricing_tiers: Optional[List[Dict[str, Any]]] = None,
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
                .where(
                    and_(
                        Product.id == uuid.UUID(product_id),
                        Product.producer_id == producer.id,
                    )
                )
                .with_for_update()
            )
            res = await self.session.execute(stmt)
            product = res.scalar_one_or_none()

            if not product:
                return {
                    "status": "error",
                    "message": "Produit introuvable ou non autorisé.",
                }

            was_sellable = bool(product.is_available) and float(product.quantity_for_sale or 0.0) > 0
            previous_quantity = float(product.quantity_for_sale or 0.0)

            changed: List[str] = []
            if price is not None:
                product.price = positive_float(price, "price", allow_zero=True)
                changed.append("prix")
            if quantity is not None:
                product.quantity_for_sale = positive_float(
                    quantity, "quantity", allow_zero=True
                )
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
            if pricing_tiers is not None:
                # Valide contre l'unité de base FINALE (nouvelle si fournie
                # dans cette même mise à jour, sinon celle déjà en base) —
                # `product.unit` a déjà été réassigné ci-dessus si `unit`
                # était fourni.
                try:
                    validated_tiers = validate_pricing_tiers(
                        pricing_tiers, product.unit
                    )
                except PricingTierError as exc:
                    return {"status": "error", "message": str(exc)}
                product.pricing_tiers = tiers_to_dicts(validated_tiers) or None
                changed.append("tarifs multiples")

            if not changed:
                return {
                    "status": "error",
                    "message": "Aucun champ à modifier n'a été fourni.",
                }

            if quantity is not None:
                await BusinessEventEmitter(self.session).emit_product_quantity_changed(
                    product, previous_quantity=previous_quantity, source="producer_adjustment"
                )
            is_sellable = bool(product.is_available) and float(product.quantity_for_sale or 0.0) > 0
            if is_sellable and not was_sellable:
                await BusinessEventEmitter(self.session).emit_product_published_for_sale(product)

            await self.session.flush()
            await self.session.refresh(product)

            logger.info(
                "PRODUCT_UPDATED: ID %s par %s (champs: %s)",
                product_id,
                phone,
                ", ".join(changed),
            )

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

    async def add_product_photo(
        self,
        phone: str,
        product_id: str,
        image_url: str,
        replace: bool = False,
    ) -> Dict[str, Any]:
        """
        Lie une photo (déjà uploadée sur Supabase Storage) à un produit du
        catalogue — ``replace=True`` remplace toutes les photos existantes
        (cas "mettre à jour"), sinon l'URL est ajoutée à la liste
        (idempotent : une même URL n'est jamais dupliquée).
        Sécurisé par un verrou d'écriture (Row-Level Locking), même pattern
        que `update_product_price_and_qty`.
        """
        try:
            phone = clean_text(phone, "phone", required=True)
            product_id = clean_text(product_id, "product_id", required=True)
            image_url = clean_text(
                image_url, "image_url", required=True, max_length=2048
            )
            _, producer = await self.get_producer_profile(phone)

            stmt = (
                select(Product)
                .where(
                    and_(
                        Product.id == uuid.UUID(product_id),
                        Product.producer_id == producer.id,
                    )
                )
                .with_for_update()
            )
            res = await self.session.execute(stmt)
            product = res.scalar_one_or_none()

            if not product:
                return {
                    "status": "error",
                    "message": "Produit introuvable ou non autorisé.",
                }

            if replace:
                current_images = [image_url]
            else:
                current_images = list(product.images or [])
                if image_url in current_images:
                    product_dict = product.to_dict()
                    if isinstance(product_dict.get("price"), Decimal):
                        product_dict["price"] = float(product_dict["price"])
                    return {
                        "status": "success",
                        "message": "Photo déjà associée à ce produit.",
                        "data": product_dict,
                    }
                current_images.append(image_url)
                if len(current_images) > _MAX_PHOTOS_PER_PRODUCT:
                    current_images = current_images[-_MAX_PHOTOS_PER_PRODUCT:]

            product.images = current_images

            await self.session.flush()
            await self.session.refresh(product)

            logger.info(
                "PRODUCT_PHOTO_ADDED: ID %s par %s (total photos: %d, replace=%s)",
                product_id,
                phone,
                len(current_images),
                replace,
            )

            product_dict = product.to_dict()
            if isinstance(product_dict.get("price"), Decimal):
                product_dict["price"] = float(product_dict["price"])

            return {
                "status": "success",
                "message": "Photo ajoutée avec succès.",
                "data": product_dict,
            }

        except ValueError as e:
            return {"status": "error", "message": str(e)}

    async def toggle_product_availability(
        self, phone: str, product_id: str
    ) -> Dict[str, Any]:
        """
        Active ou désactive un produit du catalogue (bascule `is_available`).

        Correctif Producer Analytics Phase B (2026-09-27) — bug de données réel,
        pas un choix de conception : cette méthode simulait auparavant OFF/ON en
        écrivant `quantity_for_sale = 0.0`/`1.0`, DÉTRUISANT DÉFINITIVEMENT la
        quantité réelle à chaque reprise (500 KG mis en pause -> repris ->
        1.0 KG, pour toujours) — voir docs/analytics/PRODUCER_ANALYTICS_
        ARCHITECTURE.md §3.2/§30. `is_available` existe déjà exactement pour
        représenter la disponibilité commerciale, séparément de la quantité
        physique (déjà le champ que `BuyerMixin.search_products` filtre —
        `Product.is_available.is_(True)`), donc AUCUNE migration n'est
        nécessaire : la sémantique correcte existait déjà, elle n'était
        simplement pas utilisée ici. Invariant restauré : la quantité ne
        change JAMAIS dans cette méthode, seule la visibilité bascule.
        """
        try:
            phone = clean_text(phone, "phone", required=True)
            product_id = clean_text(product_id, "product_id", required=True)
            _, producer = await self.get_producer_profile(phone)

            stmt = (
                select(Product)
                .where(
                    and_(
                        Product.id == uuid.UUID(product_id),
                        Product.producer_id == producer.id,
                    )
                )
                .with_for_update()
            )
            res = await self.session.execute(stmt)
            product = res.scalar_one_or_none()

            if not product:
                return {
                    "status": "error",
                    "message": "Produit introuvable ou non autorisé.",
                }

            was_available = bool(product.is_available)
            product.is_available = not was_available

            if product.is_available and not was_available:
                await BusinessEventEmitter(self.session).emit_product_published_for_sale(product)

            await self.session.flush()
            await self.session.refresh(product)

            status_msg = (
                "Produit remis en vente"
                if product.is_available
                else "Produit masqué du catalogue"
            )
            product_dict = product.to_dict()
            if isinstance(product_dict.get("price"), Decimal):
                product_dict["price"] = float(product_dict["price"])

            return {
                "status": "success",
                "data": product_dict,
                "active": product.is_available,
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
                .where(
                    and_(Product.id == product_uuid, Product.producer_id == producer.id)
                )
                .with_for_update()
            )
            res = await self.session.execute(stmt)
            product = res.scalar_one_or_none()

            if not product:
                return {
                    "status": "error",
                    "message": "Produit introuvable ou non autorisé.",
                }

            stats_stmt = (
                select(
                    func.count(OrderItem.id).label("total_orders"),
                    func.count(
                        func.nullif(
                            Order.status.in_(["PENDING", "CONFIRMED", "SHIPPED"]), False
                        )
                    ).label("active_orders"),
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
                previous_quantity = float(product.quantity_for_sale or 0.0)
                product.quantity_for_sale = 0.0
                product.is_available = False

                await BusinessEventEmitter(self.session).emit_product_quantity_changed(
                    product, previous_quantity=previous_quantity, source="soft_delete"
                )
                await self.session.flush()
                logger.info(
                    "PRODUCT_SOFT_DELETED: ID %s archivé pour préserver l'historique.",
                    product_id,
                )
                return {
                    "status": "success",
                    "message": "Le produit a été retiré et masqué définitivement du catalogue (conservé pour vos statistiques de vente).",
                }

            await self.session.delete(product)
            await self.session.flush()

            logger.info(
                "PRODUCT_HARD_DELETED: ID %s purgé physiquement de la DB par %s",
                product_id,
                phone,
            )
            return {"status": "success", "message": "Produit supprimé avec succès."}

        except ValueError as e:
            return {"status": "error", "message": str(e)}
