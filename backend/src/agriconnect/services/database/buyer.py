import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List

from sqlalchemy import select, desc, update, case,literal
from sqlalchemy.orm import selectinload, joinedload


from .common import normalize_phone
from .base import BaseMixin
# Import des modèles alignés sur le schéma
from agriconnect.domain.models import (
    BuyerProfile, Order, OrderItem, Delivery, 
    Product, User, BuyerType, Zone, Producer, TrustScore
)

logger = logging.getLogger("agriconnect.services.database.buyer")

class BuyerMixin(BaseMixin):
    """
    BuyerMixin - Couche Métier Acheteur.
    Strictement DRY : s'appuie exclusivement sur les résolutions de profils de BaseMixin.
    """

    # ─── SECTION 1 : CONTEXTES ET COMPORTEMENTS LLM ───────────────────────
         
    async def get_buyer_context(self, phone: str) -> Dict[str, Any]:
        """Génère le contexte d'activité complet à destination des agents LangGraph."""
        try:
            # Consommation directe de la méthode héritée de BaseMixin
            user_obj, profile_obj = await self.get_buyer_profile(phone=phone)
        except Exception:
            return {
                "role": "new_user",
                "message": "Profil acheteur introuvable.",
                "user_info": None
            }

        # Résolution du nom de la zone si elle est présente sur l'user
        zone_name = "Inconnue"
        if user_obj.zone_id:
            zone_obj = await self.session.scalar(select(Zone.name).where(Zone.id == user_obj.zone_id))
            if zone_obj:
                zone_name = zone_obj

        active_orders = await self.get_active_orders_context_by_phone(phone=phone)

        return {
            "role": "existing_buyer",
            "user_info": {
                "id": str(profile_obj.id),
                "name": getattr(profile_obj, 'establishment_name', None) or user_obj.name or "Acheteur",
                "zone": zone_name,
                "zone_id": str(user_obj.zone_id) if user_obj.zone_id else None
            },
            "recent_history": active_orders[:3],
            "can_order": user_obj.zone_id is not None
        }

    # ─── SECTION 2 : GESTION DES FLUX (COMMANDES & LIVRAISONS) ──────────────

    async def get_active_orders_context_by_phone(self, phone: str) -> List[Dict[str, Any]]:
        """Récupère le statut des commandes en cours d'un acheteur."""
        current_session = self.session
        if not current_session:
            return []
        try:
            clean_phone = normalize_phone(phone)
            ACTIVE_STATUSES = ["PENDING", "CONFIRMED", "PAID", "SHIPPED", "PICKED_UP"]

            stmt = (
                select(Order)
                .join(BuyerProfile, Order.buyer_id == BuyerProfile.id)
                .join(User, BuyerProfile.user_id == User.id)
                .options(
                    selectinload(Order.items).joinedload(OrderItem.product),
                    selectinload(Order.delivery).joinedload(Delivery.agent)
                )
                .where(User.phone == clean_phone, Order.status.in_(ACTIVE_STATUSES))
                .order_by(desc(Order.created_at))
            )

            result = await current_session.execute(stmt)
            orders = result.unique().scalars().all()
            
            return [
                {
                    "id": str(o.id),
                    "status": o.status,
                    "total": o.total_amount,
                    "items": [{"name": i.product.name, "qty": i.quantity} for i in o.items] if o.items else [],
                    "date": o.created_at.isoformat() if o.created_at else None
                } for o in orders
            ]
        except Exception as e:
            logger.error(f"Erreur context commandes pour {phone}: {e}")
            return []

    # ─── SECTION 3 : RECHERCHE GÉOLOCALISÉE ───────────────────────────


    async def search_products(self, product: str, phone: str, limit: int = 15) -> Dict[str, Any]:
        """Recherche Marketplace robuste avec jointures défensives (outerjoin).
        
        Garantit que les produits s'affichent même si les relations producteurs, 
        utilisateurs ou zones sont partiellement incomplètes en base de données.
        """
        current_session = self.session
        if not current_session:
            return {"status": "error", "message": "Session de base de données indisponible."}
            
        try:
            clean_product = str(product).strip()
            if not clean_product:
                return {"status": "error", "message": "Le nom du produit à rechercher est vide."}

            # Résolution du profil de l'acheteur
            user_obj, _ = await self.get_buyer_profile(phone=phone)
            target_uuid = user_obj.zone_id if user_obj else None
            parent_id = None

            if target_uuid:
                parent_id = await current_session.scalar(
                    select(Zone.parent_id).where(Zone.id == target_uuid)
                )

            # Construction dynamique des scores de proximité
            conditions = []
            if target_uuid:
                conditions.append((Producer.zone_id == target_uuid, 1))
            if parent_id:
                conditions.append((Zone.parent_id == parent_id, 2))
                
            priority_score = case(*conditions, else_=3).label("priority") if conditions else literal(3).label("priority")

            # 🛡️ UTILISATION DE OUTERJOIN (LEFT JOIN) POUR ÉVITER L'ÉVAPORATION DES PRODUITS
            stmt = (
                select(
                    Product.id, 
                    Product.name, 
                    Product.price, 
                    Product.quantity_for_sale, 
                    Product.unit,
                    User.name.label("producer_name"), 
                    priority_score
                )
                .outerjoin(Producer, Producer.id == Product.producer_id)
                .outerjoin(User, User.id == Producer.user_id)
                .outerjoin(Zone, Zone.id == Producer.zone_id)
                .where(
                    Product.name.ilike(f"%{clean_product}%"), 
                    Product.quantity_for_sale > 0
                )
                .order_by("priority", Product.price.asc())
                .limit(limit)
            )

            result = await current_session.execute(stmt)
            rows = result.mappings().all()
            
            if not rows:
                return {
                    "status": "empty", 
                    "message": f"Désolé, aucun produit correspondant à '{clean_product}' n'est disponible."
                }

            formatted_results = []
            for idx, row in enumerate(rows, 1):
                is_local = target_uuid is not None and row.get("priority") and int(row["priority"]) <= 2
                tag = "📍 Local" if is_local else "🌐 National"
                
                formatted_results.append({
                    "display_index": idx,
                    "id": str(row["id"]),
                    "name": f"{row['name']} ({tag})",
                    "price": float(row["price"]),
                    "unit": str(row["unit"] or "KG").upper(),
                    "vendor": row["producer_name"] or "Producteur Anonyme",
                    "is_local": is_local
                })

            return {
                "status": "success",
                "results": formatted_results,
                "search_info": "Trié par circuits courts ou par défaut (Prix)."
            }
            
        except Exception as e:
            logger.error(f"[Marketplace Search] Erreur critique: {str(e)}", exc_info=True)
            return {"status": "error", "message": "Erreur technique lors de la recherche."}

        
    # ─── SECTION 4 : CRÉATION TRANSACTIONNELLE DE COMMANDES ──────────────────

    async def finalize_multi_order(self, items: List[Dict[str, Any]], phone: str) -> Dict[str, Any]:
        """Crée une commande ferme multi-produits avec Row-level locking strict."""
        current_session = self.session
        if not current_session:
            return {"status": "error", "message": "Session indisponible."}
        try:
            # Appel direct à BaseMixin, plus de duplication locale
            user_obj, profile_obj = await self.get_buyer_profile(phone=phone)
            
            if not user_obj.zone_id:
                return {"status": "error", "message": "Veuillez configurer votre zone de livraison."}

            new_order = Order(
                id=uuid.uuid4(),
                buyer_id=profile_obj.id,
                zone_id=user_obj.zone_id,
                total_amount=0.0,
                status="PENDING",
                payment_status="PENDING",
                delivery_status="PENDING",
                source="WHATSAPP",
                created_at=datetime.now(timezone.utc)
            )
            current_session.add(new_order)
            await current_session.flush()

            running_total = 0.0
            summary_items = []

            for item in items:
                p_id = item.get("product_id")
                qty = float(item.get("quantity", 0))
                if qty <= 0:
                    continue

                p_uuid = uuid.UUID(p_id) if isinstance(p_id, str) else p_id
                
                # Verrou exclusif d'écriture (Race-Condition Proof)
                product = await current_session.scalar(
                    select(Product).where(Product.id == p_uuid).with_for_update()
                )
                
                if not product:
                    raise ValueError("Un produit sélectionné n'est plus disponible.")
                if product.quantity_for_sale < qty:
                    raise ValueError(f"Stock insuffisant pour {product.name} (Dispo: {product.quantity_for_sale}).")

                line_total = float(product.price) * qty
                running_total += line_total

                current_session.add(OrderItem(
                    id=uuid.uuid4(), order_id=new_order.id, product_id=product.id,
                    quantity=qty, price_at_sale=float(product.price)
                ))

                product.quantity_for_sale -= qty
                summary_items.append(f"{product.name} (x{qty} {product.unit or 'u'})")

            if not summary_items:
                raise ValueError("Le panier ne contient aucun article valide.")

            new_order.total_amount = running_total
            await current_session.flush()

            return {
                "status": "success",
                "order_id": str(new_order.id),
                "order_number": str(new_order.id)[:8].upper(),
                "total_amount": running_total,
                "summary": ", ".join(summary_items),
                "message": f"✅ Commande enregistrée ! Total: {running_total} FCFA."
            }
        except ValueError as ve:
            logger.warning(f"Validation refusée pour {phone}: {ve}")
            return {"status": "error", "message": str(ve)}
        except Exception as e:
            logger.error(f"Échec critique finalize_multi_order pour {phone}: {str(e)}")
            return {"status": "error", "message": "Erreur technique lors de la validation du panier."}

    # ─── SECTION 5 : FEEDBACK & RÉPUTATION ───────────────────────────────────

    async def rate_delivery(self, order_id: str, rating: int, comment: str) -> Dict[str, Any]:
        """Évalue une livraison et ajuste le TrustScore de l'agent."""
        current_session = self.session
        if not current_session:
            return {"status": "error", "message": "Session indisponible."}
        try:
            o_uuid = uuid.UUID(order_id) if isinstance(order_id, str) else order_id
            
            delivery = await current_session.scalar(
                select(Delivery).where(Delivery.order_id == o_uuid).options(joinedload(Delivery.agent))
            )
            
            if not delivery or not delivery.agent_id or not delivery.agent:
                return {"status": "error", "message": "Aucun agent de livraison associé à cette commande."}

            score_impact = 0.5 if rating >= 4 else (-0.5 if rating <= 2 else 0.0)
            
            if score_impact != 0.0:
                trust_score = await current_session.scalar(
                    select(TrustScore).where(TrustScore.user_id == delivery.agent.user_id)
                )
                
                if trust_score:
                    trust_score.reliability_index = float(trust_score.reliability_index or 0.0) + score_impact
                    trust_score.updated_at = datetime.now(timezone.utc)
                else:
                    current_session.add(TrustScore(
                        id=uuid.uuid4(),
                        user_id=delivery.agent.user_id,
                        reliability_index=5.0 + score_impact,
                        updated_at=datetime.now(timezone.utc)
                    ))
                
                await current_session.flush()

            return {"status": "success", "message": "Merci pour votre retour ! Pris en compte."}
        except Exception as e:
            logger.error(f"Erreur lors de la notation de la livraison {order_id}: {e}")
            return {"status": "error", "message": "Erreur technique lors de la sauvegarde de la note."}

    async def list_buyer_types(self) -> List[Dict[str, Any]]:
        """Liste les typologies/segments d'acheteurs de la plateforme."""
        current_session = self.session
        if not current_session:
            return []
        result = await current_session.execute(select(BuyerType).order_by(BuyerType.name))
        return [{"id": str(bt.id), "name": bt.name} for bt in result.scalars().all()]

    async def set_buyer_type(self, buyer_id: str, type_id: str) -> None:
        """Assigne une catégorie de ciblage commercial au profil de l'acheteur."""
        current_session = self.session
        if not current_session:
            return
        b_uuid = uuid.UUID(buyer_id) if isinstance(buyer_id, str) else buyer_id
        t_uuid = uuid.UUID(type_id) if isinstance(type_id, str) else type_id
        
        await current_session.execute(
            update(BuyerProfile).where(BuyerProfile.id == b_uuid).values(buyer_type_id=t_uuid)
        )
        await current_session.flush()

    async def get_buyer_orders_dashboard(self, phone: str) -> Dict[str, Any]:
        """Génère un tableau de bord WhatsApp des commandes en cours pour cet acheteur."""
        current_session = self.session
        if not current_session:
            return {"status": "error", "message": "Session indisponible."}
        try:
            # Phone-First : Résolution du profil via BaseMixin
            _, profile_obj = await self.get_buyer_profile(phone=phone)
            
            # Statuts traduits avec des emojis explicites pour l'utilisateur WhatsApp
            status_map = {
                "PENDING": "⏳ En attente de validation",
                "CONFIRMED": "✅ Confirmée (Préparation stock)",
                "PAID": "💰 Payée (En attente d'expédition)",
                "SHIPPED": "🚛 En cours de route",
                "PICKED_UP": "📍 Arrivée au point de collecte",
                "DELIVERED": "📦 Livrée avec succès",
                "CANCELLED": "❌ Annulée"
            }

            stmt = (
                select(Order)
                .options(selectinload(Order.items).joinedload(OrderItem.product))
                .where(Order.buyer_id == profile_obj.id)
                .order_by(desc(Order.created_at))
                .limit(5)
            )
            result = await current_session.execute(stmt)
            orders = result.unique().scalars().all()

            if not orders:
                return {
                    "status": "success",
                    "formatted_menu": "📦 *Vos Commandes :*\n\nVous n'avez pas encore passé de commande sur AgriConnect. Tapez *Marketplace* pour voir les produits disponibles !"
                }

            menu_lines = ["📦 *SUIVI DE VOS COMMANDES (Top 5) :*"]
            mapping_cache = {}

            for idx, order in enumerate(orders, start=1):
                items_summary = ", ".join([f"{item.product.name} (x{item.quantity})" for item in order.items])
                display_status = status_map.get(order.status.upper(), f"🔄 Status: {order.status}")
                date_str = order.created_at.strftime("%d/%m/%Y")
                
                line = (
                    f"\n*{idx}. Commande #{str(order.id)[:8].upper()}* ({date_str})\n"
                    f"🛒 Articles : {items_summary}\n"
                    f"💵 Total : *{order.total_amount} CFA*\n"
                    f"📊 État : {display_status}\n"
                )
                menu_lines.append(line)
                mapping_cache[str(idx)] = str(order.id)

            menu_lines.append("\n_Pour annuler une commande en attente, répondez avec le numéro correspondant._")

            return {
                "status": "success",
                "formatted_menu": "\n".join(menu_lines),
                "mapping": mapping_cache
            }
        except Exception as e:
            logger.error(f"Erreur get_buyer_orders_dashboard pour {phone}: {e}")
            return {"status": "error", "message": "Impossible de charger votre suivi de commande."}

    async def cancel_pending_order(self, order_id: str, phone: str) -> Dict[str, Any]:
        """Annule une commande PENDING et recrédite les stocks des produits associés."""
        current_session = self.session
        if not current_session:
            return {"status": "error", "message": "Session indisponible."}
        try:
            user_obj, profile_obj = await self.get_buyer_profile(phone=phone)
            o_uuid = uuid.UUID(order_id) if isinstance(order_id, str) else order_id

            # Récupération sécurisée avec verrouillage exclusif
            stmt = (
                select(Order)
                .options(selectinload(Order.items).joinedload(OrderItem.product))
                .where(Order.id == o_uuid, Order.buyer_id == profile_obj.id)
                .with_for_update()
            )
            order = await current_session.scalar(stmt)

            if not order:
                return {"status": "error", "message": "Commande introuvable ou non autorisée."}
            if order.status.upper() != "PENDING":
                return {"status": "error", "message": f"Impossible d'annuler une commande déjà en statut : {order.status}."}

            # Restitution physique des stocks aux producteurs
            for item in order.items:
                if item.product:
                    prod_stmt = select(Product).where(Product.id == item.product_id).with_for_update()
                    product = await current_session.scalar(prod_stmt)
                    if product:
                        product.quantity_for_sale += item.quantity

            order.status = "CANCELLED"
            await current_session.flush()

            return {
                "status": "success",
                "message": f"❌ La commande #{str(order.id)[:8].upper()} a été annulée. Les stocks ont été restitués aux agriculteurs."
            }
        except Exception as e:
            logger.error(f"Erreur annulation commande {order_id} par {phone}: {e}")
            # AJOUT INDISPENSABLE : Toujours retourner un dictionnaire en cas de crash
            return {
                "status": "error", 
                "message": "Erreur technique ou utilisateur introuvable lors de l'annulation."
            }


    async def estimate_delivery_cost(self, phone: str, product_ids: List[str]) -> Dict[str, Any]:
        """Calcule une estimation transparente des frais de transport du panier vers la zone de l'acheteur."""
        current_session = self.session
        if not current_session:
            return {"status": "error", "message": "Session indisponible."}
        try:
            user_obj, _ = await self.get_buyer_profile(phone=phone)
            if not user_obj.zone_id:
                return {"status": "error", "message": "Veuillez configurer votre zone pour estimer la livraison."}

            buyer_zone_id = user_obj.zone_id
            total_transport_estimate = 0.0
            number_of_pickup_points = 0

            for p_id in product_ids:
                p_uuid = uuid.UUID(p_id) if isinstance(p_id, str) else p_id
                
                # Récupération de la zone de production
                stmt = select(Producer.zone_id).join(Product, Product.producer_id == Producer.id).where(Product.id == p_uuid)
                producer_zone_id = await current_session.scalar(stmt)

                if not producer_zone_id:
                    continue

                number_of_pickup_points += 1
                
                # Heuristique intelligente de routage logistique ouest-africain
                if producer_zone_id == buyer_zone_id:
                    total_transport_estimate += 1500.0  # Même ville / village (Circuit ultra-court)
                else:
                    # Vérification si même région (même parent logistique)
                    p_parent = await current_session.scalar(select(Zone.parent_id).where(Zone.id == producer_zone_id))
                    b_parent = await current_session.scalar(select(Zone.parent_id).where(Zone.id == buyer_zone_id))
                    
                    if p_parent and p_parent == b_parent:
                        total_transport_estimate += 4000.0  # Transit régional (Inter-communal)
                    else:
                        total_transport_estimate += 12000.0 # Transit national / Longue distance

            formatted_text = (
                f"🚛 *ESTIMATION LOGISTIQUE AGRICONNECT :*\n\n"
                f"📍 Destination : _{user_obj.name}_\n"
                f"🏢 Nombre de fermes de ramassage : *{number_of_pickup_points}*\n"
                f"💰 Coût estimé du transport : *{total_transport_estimate} CFA*\n\n"
                f"_💡 Note : Ce tarif est une estimation automatique basée sur les circuits courts. Le prix définitif vous sera confirmé lors de l'attribution du livreur._"
            )

            return {
                "status": "success",
                "estimated_cost": total_transport_estimate,
                "formatted_text": formatted_text
            }
        except Exception as e:
            logger.error(f"Erreur estimation transport pour {phone}: {e}")
            return {"status": "error", "message": "Calcul logistique indisponible temporairement."}

