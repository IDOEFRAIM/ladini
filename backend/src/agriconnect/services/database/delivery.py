import math
import random
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Literal

from sqlalchemy import select, update, and_, or_, func, text
from sqlalchemy.orm import selectinload, joinedload

from agriconnect.domain.models import (
    Order, OrderItem, Delivery, DeliveryAgent, 
    Product, Producer, User, _uuid4
)
from .base import BaseMixin

logger = logging.getLogger("agriconnect.services.delivery")

DeliveryStatusType = Literal['PENDING', 'ASSIGNED', 'IN_TRANSIT', 'DELIVERED', 'CANCELLED']


class DeliveryMixin(BaseMixin):
    """
    Mixin centralisé pour la logistique, l'affectation des transporteurs,
    le transit en temps réel et la validation par code sécurisé (OTP).
    """

    # ─── UTILS ──────────────────────────────────────────────────────────

    def _generate_delivery_otp(self) -> str:
        """Génère un jeton de sécurité OTP à 6 chiffres pour l'acheteur."""
        return str(random.randint(100000, 999999))

    def calculate_distance_km(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calcule la distance de Haversine entre deux points géographiques GPS."""
        if not all([lat1, lon1, lat2, lon2]):
            return 0.0
        R = 6371.0
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = (math.sin(dlat / 2)**2 + 
             math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2)
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return round(R * c, 2)

    # ─── PROFIL TRANSPORT COMPTE livreur ────────────────────────────────

    async def resolve_delivery_agent(self, user_id: str) -> DeliveryAgent:
        """Récupère le profil transporteur d'un utilisateur ou le crée à la volée."""
        stmt = select(DeliveryAgent).where(DeliveryAgent.user_id == user_id)
        res = await self.session.execute(stmt)
        agent = res.scalar_one_or_none()

        if not agent:
            agent = DeliveryAgent(
                id=str(_uuid4()),
                user_id=user_id,
                status='AVAILABLE'
            )
            self.session.add(agent)
            await self.session.flush()
        return agent

    # ─── FLUX LOGISTIQUE DE BOUT EN BOUT ─────────────────────────────────

    async def create_delivery(self, order_id: str) -> Dict[str, Any]:
        """
        Initialise une entité de livraison dès validation/paiement d'une commande.
        Calcule automatiquement la distance kilométrique de l'itinéraire.
        """
        # 1. Chargement de la commande avec jointures sécurisées vers le producteur d'origine
        stmt = (
            select(Order)
            .options(
                selectinload(Order.items)
                .joinedload(OrderItem.product)
                .joinedload(Product.producer)
                .joinedload(Producer.user)
            )
            .where(Order.id == order_id)
        )
        res = await self.session.execute(stmt)
        order = res.unique().scalar_one_or_none()

        if not order:
            raise ValueError("Commande introuvable.")

        # 2. Idempotence : Évite les doublons de livraisons
        existing_stmt = select(Delivery).where(Delivery.order_id == order_id)
        existing = (await self.session.execute(existing_stmt)).scalar_one_or_none()
        if existing:
            return {"status": "already_exists", "delivery": existing.to_dict()}

        # 3. Extraction de la localisation du producteur (Point d'enlèvement)
        origin_lat, origin_lng = None, None
        if order.items and order.items[0].product.producer.user:
            u = order.items[0].product.producer.user
            origin_lat, origin_lng = u.latitude, u.longitude

        # 4. Calcul de l'itinéraire de livraison
        dist = None
        if origin_lat and order.gps_lat:
            dist = self.calculate_distance_km(origin_lat, origin_lng, order.gps_lat, order.gps_lng)

        # 5. Création de l'ordre de route
        new_delivery = Delivery(
            id=str(_uuid4()),
            order_id=order.id,
            status='PENDING',
            delivery_code=self._generate_delivery_otp(),
            origin_gps_lat=origin_lat,
            origin_gps_lng=origin_lng,
            destination_gps_lat=order.gps_lat,
            destination_gps_lng=order.gps_lng,
            estimated_distance_km=dist
        )
        
        order.delivery_status = 'PENDING'
        self.session.add(new_delivery)
        await self.session.flush()
        return new_delivery.to_dict()

    async def claim_delivery(self, delivery_id: str, user_id: str) -> Dict[str, Any]:
        """Action du livreur : Accepte et réserve une course de livraison disponible."""
        agent = await self.resolve_delivery_agent(user_id)
        
        if agent.status == 'OFFLINE':
            raise PermissionError("Opération impossible : vous êtes configuré hors-ligne.")

        # Verrouillage et mise à jour atomique pour parer à la concurrence entre livreurs
        stmt = (
            update(Delivery)
            .where(and_(
                Delivery.id == delivery_id,
                Delivery.delivery_agent_id == None,
                Delivery.status == 'PENDING'
            ))
            .values(
                delivery_agent_id=agent.id,
                status='ASSIGNED',
                assigned_at=datetime.now()
            )
            .returning(Delivery.order_id)
        )
        
        res = await self.session.execute(stmt)
        order_id = res.scalar_one_or_none()

        if not order_id:
            raise ValueError("Désolé, cette course a déjà été acceptée par un autre transporteur.")

        # Synchronisation des statuts
        agent.status = 'BUSY'
        await self.session.execute(
            update(Order).where(Order.id == order_id).values(delivery_status='ASSIGNED')
        )
        
        logger.info(f"Livraison {delivery_id} verrouillée avec succès par l'agent {agent.id}")
        return {"success": True, "status": "ASSIGNED"}

    async def start_delivery_transit(self, delivery_id: str, user_id: str) -> Dict[str, Any]:
        """Action du livreur : Indique qu'il a récupéré les produits et entame le transit."""
        agent = await self.resolve_delivery_agent(user_id)
        
        stmt = select(Delivery).where(and_(Delivery.id == delivery_id, Delivery.delivery_agent_id == agent.id))
        res = await self.session.execute(stmt)
        delivery = res.scalar_one_or_none()
        
        if not delivery or delivery.status != 'ASSIGNED':
            raise ValueError("Mise en transit impossible. Statut non éligible.")
            
        delivery.status = 'IN_TRANSIT'
        
        # Propagation de l'état vers la table des commandes principales
        await self.session.execute(
            update(Order).where(Order.id == delivery.order_id).values(delivery_status='IN_TRANSIT')
        )
        await self.session.flush()
        return {"success": True, "status": "IN_TRANSIT"}

    async def confirm_delivery_with_otp(self, delivery_id: str, otp_code: str, user_id: str) -> Dict[str, Any]:
        """Validation finale : Le livreur soumet le code secret fourni par l'acheteur."""
        stmt = (
            select(Delivery)
            .options(joinedload(Delivery.order))
            .where(Delivery.id == delivery_id)
        )
        res = await self.session.execute(stmt)
        delivery = res.scalar_one_or_none()

        if not delivery or delivery.delivery_code != otp_code.strip():
            raise ValueError("Validation refusée : Code OTP de confirmation invalide.")

        # Clôture définitive du cycle de livraison
        delivery.status = 'DELIVERED'
        delivery.delivered_at = datetime.now()
        delivery.order.status = 'DELIVERED'
        delivery.order.delivery_status = 'DELIVERED'
        
        # Libération opérationnelle du livreur
        agent = await self.resolve_delivery_agent(user_id)
        agent.status = 'AVAILABLE'
        
        await self.session.flush()
        return {"success": True, "status": "DELIVERED"}

    # ─── CONSOLE DE SUIVI MULTI-ACTEURS (IA & USERS) ───────────────────

    async def get_delivery_status_tracking(self, order_id: str) -> Dict[str, Any]:
        """
        Moteur d'analyse de statut pour l'IA et les utilisateurs.
        Retourne un état d'avancement textuel et structurel précis pour le tracking.
        """
        stmt = (
            select(Delivery)
            .options(
                joinedload(Delivery.order),
                joinedload(Delivery.delivery_agent).joinedload(DeliveryAgent.user)
            )
            .where(Delivery.order_id == order_id)
        )
        res = await self.session.execute(stmt)
        d = res.unique().scalar_one_or_none()
        
        if not d:
            return {
                "order_id": order_id,
                "status": "PREPARATION",
                "progress_percentage": 10,
                "display_message": "Le producteur prépare votre colis. En attente de prise en charge logistique."
            }
            
        MESSAGES_MAPPING = {
            'PENDING': "Recherche active d'un transporteur partenaire disponible...",
            'ASSIGNED': f"Course acceptée par le livreur. Récupération des marchandises en cours chez le producteur.",
            'IN_TRANSIT': "Le livreur a récupéré votre commande ! Elle est en route vers votre position.",
            'DELIVERED': "Colis remis en main propre ! Transaction validée et clôturée.",
            'CANCELLED': "La procédure de livraison de cette commande a été annulée."
        }
        
        PROGRESS_MAPPING = {'PENDING': 25, 'ASSIGNED': 50, 'IN_TRANSIT': 75, 'DELIVERED': 100, 'CANCELLED': 0}
        
        agent_info = None
        if d.delivery_agent and d.delivery_agent.user:
            agent_info = {
                "name": d.delivery_agent.user.name,
                "phone": d.delivery_agent.user.phone,
                "current_status": d.delivery_agent.status
            }
            
        return {
            "delivery_id": d.id,
            "order_id": str(order_id),
            "status": d.status,
            "progress_percentage": PROGRESS_MAPPING.get(d.status, 0),
            "display_message": MESSAGES_MAPPING.get(d.status, "Statut inconnu."),
            "estimated_distance_km": d.estimated_distance_km,
            "delivery_agent": agent_info,
            "requires_otp_validation": d.status == 'IN_TRANSIT',
            "timestamps": {
                "assigned_at": d.assigned_at.isoformat() if d.assigned_at else None,
                "delivered_at": d.delivered_at.isoformat() if d.delivered_at else None
            }
        }