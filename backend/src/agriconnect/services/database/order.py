import logging
from typing import Any, Dict, List, Optional, Literal
from sqlalchemy import select, update, and_, text
from sqlalchemy.orm import selectinload, joinedload

from agriconnect.domain.models import (
    Order, OrderItem, Product, User, BuyerProfile
)
from .base import BaseMixin

logger = logging.getLogger("agriconnect.services.order")


class OrderMixin(BaseMixin):
    """
    Mixin gérant le cycle de vie des commandes.
    Inclut la validation des stocks, la création atomique et les hooks de statut.
    """

    # --- Utils ---
    
    def _map_payment_method(self, method: Optional[str]) -> str:
        mapping = {
            'cash': 'CASH',
            'mobile_money': 'MOBILE_MONEY',
            'bank_transfer': 'BANK_TRANSFER'
        }
        return mapping.get(method, 'CASH')

    # --- Coeur du Service ---

    # ✅ LA SIGNATURE CORRIGÉE : Plus de paramètre "session" inutile
    async def get_order_details(self, order_id: str) -> Optional[Dict[str, Any]]:
        """
        Récupère les détails complets (Items + Produits).
        Utilise selectinload pour éviter le N+1 sur les items via self.session.
        """
        stmt = (
            select(Order)
            .options(
                selectinload(Order.items).joinedload(OrderItem.product)
            )
            .where(Order.id == order_id)
        )
        # 🚀 Correction : Utilisation directe du contexte de l'instance
        result = await self.session.execute(stmt)
        order = result.unique().scalar_one_or_none()
        
        return order.to_dict() if order else None

    # --- Statut Hooks (Le moteur de workflow) ---

    # ✅ LA SIGNATURE CORRIGÉE : Alignée sur le reste du backend
    async def run_order_status_hooks(self, order_id: str, new_status: str):
        """
        Déclenche les actions automatiques suite à un changement de statut.
        (Auto-création de livraison, notifications, etc.)
        """
        upper_status = new_status.upper()
        
        # 1. Logique Logistique (Auto-livraison)
        # On délègue au DeliveryMixin si le statut est 'PAID' ou 'CONFIRMED'
        deliverable_statuses = ['CONFIRMED', 'PROCESSING', 'PAID', 'SHIPPED']
        if upper_status in deliverable_statuses:
            # Note: Grâce à l'héritage partagé, tu peux appeler directement self.create_delivery(...) ici
            pass

        # 2. Notifications
        # Intégrer ici l'appel à ton service de messagerie (WhatsApp/SMS)
        logger.info(f"STATUS_HOOK: Order {order_id} moved to {upper_status}")