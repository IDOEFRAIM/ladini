"""DTO transactionnels : OrderItem, Order, Payment, StatusHistory, Reminder, Delivery.

Machine à états métier : Commande → Paiement → Livraison → Confirmation.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import field_validator

from agriconnect.domain.base_model import BaseMarketplaceModel


class OrderItemModel(BaseMarketplaceModel):
    """↔ marketplace.order_items"""

    id: Optional[str] = None
    order_id: Optional[str] = None
    product_id: str
    quantity: Decimal
    price_at_sale: Decimal

    @field_validator("quantity", "price_at_sale")
    @classmethod
    def _positive(cls, v):
        return cls._non_negative(v)


class OrderModel(BaseMarketplaceModel):
    """↔ marketplace.orders"""

    id: Optional[str] = None
    buyer_id: Optional[str] = None
    client_id: Optional[str] = None
    market_offer_id: Optional[str] = None  # prévente

    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None

    status: str = "PENDING"           # PENDING→CONFIRMED→FULFILLED→CLOSED
    payment_status: str = "PENDING"   # PENDING→PAID→REFUNDED
    delivery_status: str = "PENDING"  # PENDING→ASSIGNED→DELIVERED
    payment_method: str = "CASH"
    order_type: str = "STANDARD"
    currency: str = "XOF"

    subtotal: Decimal = Decimal("0")
    delivery_fee: Decimal = Decimal("0")
    tax_amount: Decimal = Decimal("0")
    total_amount: Decimal

    delivery_date: Optional[datetime] = None
    expected_fulfillment_date: Optional[datetime] = None
    confirmed_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    items: list[OrderItemModel] = []

    @field_validator("subtotal", "delivery_fee", "tax_amount", "total_amount")
    @classmethod
    def _positive(cls, v):
        return cls._non_negative(v)


class PaymentModel(BaseMarketplaceModel):
    """↔ marketplace.payments"""

    id: Optional[str] = None
    order_id: str
    amount: Decimal
    currency: str = "XOF"
    method: str = "CASH"
    status: str = "PENDING"   # PENDING→AUTHORIZED→CAPTURED→FAILED→REFUNDED
    provider: Optional[str] = None
    provider_ref: Optional[str] = None
    failure_reason: Optional[str] = None
    authorized_at: Optional[datetime] = None
    captured_at: Optional[datetime] = None
    refunded_at: Optional[datetime] = None
    created_at: Optional[datetime] = None

    @field_validator("amount")
    @classmethod
    def _positive(cls, v):
        return cls._non_negative(v)


class OrderStatusHistoryModel(BaseMarketplaceModel):
    """↔ marketplace.order_status_history"""

    id: Optional[str] = None
    order_id: str
    status_type: str          # ORDER | PAYMENT | DELIVERY
    from_status: Optional[str] = None
    to_status: str
    actor_id: Optional[str] = None
    note: Optional[str] = None
    created_at: Optional[datetime] = None


class OrderReminderModel(BaseMarketplaceModel):
    """↔ marketplace.order_reminders"""

    id: Optional[str] = None
    order_id: str
    type: str                 # PAYMENT_DUE | CONFIRM_RECEIPT | LEAVE_REVIEW | PREORDER_READY
    channel: str = "WHATSAPP"
    status: str = "SCHEDULED"
    scheduled_at: datetime
    sent_at: Optional[datetime] = None
    attempts: int = 0


class DeliveryModel(BaseMarketplaceModel):
    """↔ marketplace.deliveries"""

    id: Optional[str] = None
    order_id: str
    delivery_agent_id: Optional[str] = None
    status: str = "PENDING"
    delivery_code: Optional[str] = None
    destination_desc: Optional[str] = None
    estimated_distance_km: Optional[float] = None
    actual_distance_km: Optional[float] = None
    shipping_condition: Optional[str] = None
    proof_of_delivery_url: Optional[str] = None
    assigned_at: Optional[datetime] = None
    picked_up_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None
