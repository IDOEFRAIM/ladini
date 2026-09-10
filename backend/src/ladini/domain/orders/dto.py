"""DTOs du contexte Orders — frontière API + Tools injectés dans l'Agent IA."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import Field, field_validator

from ladini.domain.base_model import BaseMarketplaceModel


class OrderItemDTO(BaseMarketplaceModel):
    """↔ marketplace.order_items"""

    id: Optional[str] = Field(
        default=None, description="Identifiant de la ligne de commande."
    )
    order_id: Optional[str] = Field(default=None, description="Commande parente.")
    product_id: str = Field(description="Produit commandé.")
    quantity: Decimal = Field(description="Quantité commandée.")
    price_at_sale: Decimal = Field(
        description="Prix unitaire au moment de la vente (figé, indépendant du prix catalogue actuel)."
    )

    @field_validator("quantity", "price_at_sale")
    @classmethod
    def _positive(cls, v):
        return cls._non_negative(v)


class OrderDTO(BaseMarketplaceModel):
    """↔ marketplace.orders — commande catalogue OU précommande.

    Machine à états métier : Commande → Paiement → Livraison → Confirmation.
    """

    id: Optional[str] = Field(default=None, description="Identifiant de la commande.")
    buyer_id: Optional[str] = Field(
        default=None, description="Acheteur passant la commande."
    )
    client_id: Optional[str] = Field(
        default=None, description="Client final, si distinct de l'acheteur plateforme."
    )
    market_offer_id: Optional[str] = Field(
        default=None,
        description="Offre de précommande visée (null si commande catalogue standard).",
    )

    customer_name: Optional[str] = Field(
        default=None, description="Nom du client final pour la livraison."
    )
    customer_phone: Optional[str] = Field(
        default=None, description="Téléphone du client final pour la livraison."
    )

    status: str = Field(
        default="PENDING",
        description="PENDING -> CONFIRMED -> FULFILLED -> CLOSED (ou CANCELLED).",
    )
    payment_status: str = Field(
        default="PENDING", description="PENDING -> PAID -> REFUNDED."
    )
    delivery_status: str = Field(
        default="PENDING", description="PENDING -> ASSIGNED -> DELIVERED."
    )
    payment_method: str = Field(
        default="CASH", description="CASH, MOBILE_MONEY, ou autre méthode acceptée."
    )
    order_type: str = Field(
        default="STANDARD",
        description="STANDARD (catalogue) ou PREORDER (production future).",
    )
    currency: str = Field(default="XOF", description="Devise de la transaction.")

    subtotal: Decimal = Field(
        default=Decimal("0"), description="Somme des lignes de commande hors frais."
    )
    delivery_fee: Decimal = Field(
        default=Decimal("0"), description="Frais de livraison."
    )
    tax_amount: Decimal = Field(
        default=Decimal("0"), description="Montant de taxes applicables."
    )
    total_amount: Decimal = Field(description="Montant total dû par l'acheteur.")

    delivery_date: Optional[datetime] = Field(
        default=None, description="Date de livraison souhaitée/prévue."
    )
    expected_fulfillment_date: Optional[datetime] = Field(
        default=None, description="Date prévue de disponibilité pour une précommande."
    )
    confirmed_at: Optional[datetime] = Field(
        default=None, description="Date de confirmation de la commande."
    )
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    items: list[OrderItemDTO] = Field(
        default_factory=list, description="Lignes de la commande."
    )

    @field_validator("subtotal", "delivery_fee", "tax_amount", "total_amount")
    @classmethod
    def _positive(cls, v):
        return cls._non_negative(v)


class PaymentDTO(BaseMarketplaceModel):
    """↔ marketplace.payments"""

    id: Optional[str] = Field(default=None, description="Identifiant du paiement.")
    order_id: str = Field(description="Commande associée à ce paiement.")
    amount: Decimal = Field(description="Montant du paiement.")
    currency: str = Field(default="XOF", description="Devise du paiement.")
    method: str = Field(default="CASH", description="Méthode de paiement utilisée.")
    status: str = Field(
        default="PENDING",
        description="PENDING -> AUTHORIZED -> CAPTURED -> FAILED -> REFUNDED.",
    )
    provider: Optional[str] = Field(
        default=None, description="Fournisseur de paiement (mobile money, etc.)."
    )
    provider_ref: Optional[str] = Field(
        default=None, description="Référence transactionnelle chez le fournisseur."
    )
    failure_reason: Optional[str] = Field(
        default=None, description="Motif d'échec du paiement, si applicable."
    )
    authorized_at: Optional[datetime] = None
    captured_at: Optional[datetime] = None
    refunded_at: Optional[datetime] = None
    created_at: Optional[datetime] = None

    @field_validator("amount")
    @classmethod
    def _positive(cls, v):
        return cls._non_negative(v)


class AuctionDTO(BaseMarketplaceModel):
    """↔ marketplace.auctions — appel d'offres publié par un acheteur."""

    id: Optional[str] = Field(default=None, description="Identifiant de l'enchère.")
    buyer_id: str = Field(description="Acheteur ayant publié cet appel d'offres.")
    sub_category_id: str = Field(description="Sous-catégorie de produit recherché.")
    winner_bid_id: Optional[str] = Field(
        default=None, description="Offre gagnante désignée, si l'enchère est clôturée."
    )
    quantity: Decimal = Field(description="Quantité totale recherchée.")
    unit: str = Field(default="TONNE", description="Unité de mesure de la quantité.")
    max_price_per_unit: Decimal = Field(description="Prix plafond accepté par unité.")
    delivery_location: str = Field(description="Lieu de livraison souhaité.")
    delivery_deadline: datetime = Field(
        description="Date limite de livraison souhaitée."
    )
    deadline: datetime = Field(
        description="Date limite pour recevoir des offres des producteurs."
    )
    status: str = Field(
        default="OPEN", description="OPEN | AWARDED | CANCELLED | EXPIRED."
    )

    @field_validator("quantity", "max_price_per_unit")
    @classmethod
    def _positive(cls, v):
        return cls._non_negative(v)


class BidDTO(BaseMarketplaceModel):
    """↔ marketplace.bids — offre d'un producteur sur une enchère."""

    id: Optional[str] = Field(default=None, description="Identifiant de l'offre.")
    auction_id: str = Field(description="Enchère visée par cette offre.")
    producer_id: str = Field(description="Producteur émettant cette offre.")
    offered_price: Decimal = Field(
        description="Prix proposé par le producteur, par unité."
    )
    linked_stock_id: Optional[str] = Field(
        default=None, description="Lot de stock lié à cette offre, si applicable."
    )
    is_winner: bool = Field(
        default=False, description="True si cette offre a été désignée gagnante."
    )
    status: str = Field(
        default="PENDING", description="PENDING | WON | LOST | WITHDRAWN."
    )
    message: Optional[str] = Field(
        default=None, description="Message libre du producteur accompagnant l'offre."
    )

    @field_validator("offered_price")
    @classmethod
    def _positive(cls, v):
        return cls._non_negative(v)


class DeliveryDTO(BaseMarketplaceModel):
    """↔ marketplace.deliveries"""

    id: Optional[str] = Field(default=None, description="Identifiant de la livraison.")
    order_id: str = Field(description="Commande associée à cette livraison.")
    delivery_agent_id: Optional[str] = Field(
        default=None, description="Livreur assigné."
    )
    status: str = Field(
        default="PENDING", description="PENDING -> ASSIGNED -> PICKED_UP -> DELIVERED."
    )
    delivery_code: Optional[str] = Field(
        default=None, description="Code de confirmation remis au destinataire."
    )
    destination_desc: Optional[str] = Field(
        default=None, description="Description textuelle de la destination."
    )
    estimated_distance_km: Optional[float] = Field(
        default=None, description="Distance estimée en kilomètres."
    )
    actual_distance_km: Optional[float] = Field(
        default=None, description="Distance réellement parcourue."
    )


class OrderStatusHistoryDTO(BaseMarketplaceModel):
    """↔ marketplace.order_status_history — traçabilité des transitions."""

    id: Optional[str] = Field(
        default=None, description="Identifiant de l'entrée d'historique."
    )
    order_id: str = Field(description="Commande concernée par ce changement de statut.")
    status_type: str = Field(
        description="Type de statut concerné : ORDER, PAYMENT ou DELIVERY."
    )
    from_status: Optional[str] = Field(
        default=None, description="Statut précédent, si applicable."
    )
    to_status: str = Field(description="Nouveau statut appliqué.")
    actor_id: Optional[str] = Field(
        default=None,
        description="Identifiant de l'acteur ayant déclenché le changement.",
    )
    note: Optional[str] = Field(
        default=None, description="Note libre expliquant le changement."
    )
    created_at: Optional[datetime] = None


class OrderReminderDTO(BaseMarketplaceModel):
    """↔ marketplace.order_reminders — relances programmées."""

    id: Optional[str] = Field(default=None, description="Identifiant du rappel.")
    order_id: str = Field(description="Commande concernée par ce rappel.")
    type: str = Field(
        description="Type de rappel : PAYMENT_DUE, CONFIRM_RECEIPT, LEAVE_REVIEW, PREORDER_READY."
    )
    channel: str = Field(default="WHATSAPP", description="Canal d'envoi du rappel.")
    status: str = Field(
        default="SCHEDULED", description="SCHEDULED | SENT | CANCELLED."
    )
    scheduled_at: datetime = Field(description="Date/heure prévue d'envoi du rappel.")
    sent_at: Optional[datetime] = Field(
        default=None, description="Date/heure d'envoi effectif."
    )
    attempts: int = Field(
        default=0, description="Nombre de tentatives d'envoi déjà effectuées."
    )


__all__ = [
    "OrderItemDTO",
    "OrderDTO",
    "PaymentDTO",
    "OrderStatusHistoryDTO",
    "OrderReminderDTO",
    "AuctionDTO",
    "BidDTO",
    "DeliveryDTO",
]
