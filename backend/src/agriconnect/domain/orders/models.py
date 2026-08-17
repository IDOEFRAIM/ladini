"""ORM Orders — marketplace.orders/order_items/payments/auctions/bids/deliveries.

Extrait de l'ancien `domain/models.py` (voir `orm_base.py` pour la justification
du split — même `Base` partagé, zéro changement de schéma/colonnes/index).
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import relationship

from agriconnect.domain.orm_base import Base, _uuid4


class Delivery(Base):
    __tablename__ = "deliveries"
    __table_args__ = (
        Index("deliveries_order_unique", "order_id", unique=True),
        Index("deliveries_agent_idx", "delivery_agent_id"),
        Index("deliveries_status_idx", "status"),
        Index("deliveries_agent_status_idx", "delivery_agent_id", "status"),
        {"schema": "marketplace"},
    )

    id = Column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    order_id = Column(
        PG_UUID(as_uuid=True),
        ForeignKey("marketplace.orders.id"),
        nullable=False,
        unique=True,
    )
    delivery_agent_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("marketplace.delivery_agents.id")
    )
    status = Column(String, default="PENDING", nullable=False)
    delivery_code = Column(String)
    origin_gps_lat = Column(Float)
    origin_gps_lng = Column(Float)
    destination_gps_lat = Column(Float)
    destination_gps_lng = Column(Float)
    destination_desc = Column(Text)
    estimated_distance_km = Column(Float)
    actual_distance_km = Column(Float)
    shipping_condition = Column(String)
    proof_of_delivery_url = Column(String)
    assigned_at = Column(DateTime)
    picked_up_at = Column(DateTime)
    delivered_at = Column(DateTime)
    failed_at = Column(DateTime)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    agent = relationship("DeliveryAgent", foreign_keys=[delivery_agent_id])
    order = relationship("Order", back_populates="delivery", foreign_keys=[order_id])


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        Index("orders_buyer_idx", "buyer_id"),
        Index("orders_org_idx", "organization_id"),
        Index("orders_status_idx", "status"),
        Index("orders_delivery_status_idx", "delivery_status"),
        Index("orders_zone_idx", "zone_id"),
        Index("orders_created_idx", "created_at"),
        Index("orders_phone_idx", "customer_phone"),
        Index("orders_type_idx", "order_type"),
        Index("orders_market_offer_idx", "market_offer_id"),
        Index("orders_auction_unique", "auction_id", unique=True),
        Index("orders_winning_bid_idx", "winning_bid_id"),
        Index("orders_buyer_status_idx", "buyer_id", "status"),
        Index("orders_payment_status_idx", "payment_status"),
        {"schema": "marketplace"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    buyer_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("marketplace.buyer_profiles.id")
    )
    client_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.clients.id"))
    organization_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("governance.organizations.id")
    )
    zone_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id"))
    customer_name = Column(String)
    customer_phone = Column(String)
    payment_method = Column(String, default="CASH", nullable=False)
    payment_status = Column(String, default="PENDING", nullable=False)
    city = Column(String)
    gps_lat = Column(Float)
    gps_lng = Column(Float)
    delivery_desc = Column(Text)
    audio_url = Column(String)
    status = Column(String, default="PENDING", nullable=False)
    delivery_status = Column(String, default="PENDING", nullable=False)
    source = Column(String, default="APP", nullable=False)
    order_type = Column(String, default="STANDARD", nullable=False)
    whatsapp_id = Column(String)
    total_amount = Column(Numeric(14, 2), nullable=False)
    is_agent_order = Column(Boolean, default=False, nullable=False)
    delivery_date = Column(DateTime)
    subtotal = Column(Numeric(14, 2), default=0, nullable=False)
    tax_amount = Column(Numeric(14, 2), default=0, nullable=False)
    currency = Column(String, default="XOF", nullable=False)
    delivery_fee = Column(Numeric(14, 2), default=0, nullable=False)
    cancellation_role = Column(String)
    escrow_wallet_id = Column(PG_UUID(as_uuid=True), nullable=True)
    # --- Escrow Paydunya ---
    # `payment_status` (ci-dessus) porte désormais aussi : ESCROWED (payé,
    # fonds bloqués), PAID_OUT (livraison confirmée par OTP, fonds débloqués),
    # REFUNDED — en plus des valeurs existantes PENDING/PAID/CANCELLED.
    # Colonnes ajoutées après coup — voir services/database/common.py::
    # SCHEMA_COLUMN_DDL pour l'ALTER TABLE idempotent correspondant (pas
    # d'Alembic dans ce repo).
    paydunya_invoice_token = Column(String, nullable=True)
    delivery_otp = Column(String, nullable=True)
    payment_expires_at = Column(DateTime, nullable=True)
    locked_amount = Column(Numeric(14, 2), nullable=True)
    market_offer_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("marketplace.market_offers.id")
    )
    expected_fulfillment_date = Column(DateTime)
    preorder_converted_at = Column(DateTime)
    confirmed_at = Column(DateTime)
    auction_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("marketplace.auctions.id"), unique=True
    )
    winning_bid_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.bids.id"))
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    items = relationship("OrderItem", back_populates="order", lazy="selectin")
    delivery = relationship("Delivery", back_populates="order", uselist=False)
    offer = relationship("MarketOffer", back_populates="orders")
    payments = relationship(
        "Payment", back_populates="order", cascade="all, delete-orphan"
    )
    status_history = relationship(
        "OrderStatusHistory", back_populates="order", cascade="all, delete-orphan"
    )
    reminders = relationship(
        "OrderReminder", back_populates="order", cascade="all, delete-orphan"
    )


class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = (
        Index("order_items_order_idx", "order_id"),
        Index("order_items_product_idx", "product_id"),
        {"schema": "marketplace"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    order_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("marketplace.orders.id"), nullable=False
    )
    product_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("marketplace.products.id"), nullable=False
    )
    quantity = Column(Numeric(14, 3), nullable=False)
    price_at_sale = Column(Numeric(12, 2), nullable=False)

    order = relationship("Order", back_populates="items")
    product = relationship("Product", lazy="selectin")


class Payment(Base):
    """Journal de paiement (audit, retries, escrow, réconciliation)."""

    __tablename__ = "payments"
    __table_args__ = (
        Index("payments_order_idx", "order_id"),
        Index("payments_status_idx", "status"),
        Index("payments_provider_ref_unique", "provider_ref", unique=True),
        {"schema": "marketplace"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    order_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("marketplace.orders.id"), nullable=False
    )
    amount = Column(Numeric(14, 2), nullable=False)
    currency = Column(String, default="XOF", nullable=False)
    method = Column(String, default="CASH", nullable=False)
    status = Column(String, default="PENDING", nullable=False)
    provider = Column(String)
    provider_ref = Column(String)
    escrow_wallet_id = Column(PG_UUID(as_uuid=True))
    failure_reason = Column(Text)
    authorized_at = Column(DateTime)
    captured_at = Column(DateTime)
    refunded_at = Column(DateTime)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    order = relationship("Order", back_populates="payments")


class OrderStatusHistory(Base):
    """Traçabilité fine Commande→Paiement→Livraison→Confirmation."""

    __tablename__ = "order_status_history"
    __table_args__ = (
        Index("osh_order_idx", "order_id"),
        Index("osh_order_type_idx", "order_id", "status_type"),
        Index("osh_created_idx", "created_at"),
        {"schema": "marketplace"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    order_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("marketplace.orders.id"), nullable=False
    )
    status_type = Column(String, nullable=False)  # ORDER | PAYMENT | DELIVERY
    from_status = Column(String)
    to_status = Column(String, nullable=False)
    actor_id = Column(PG_UUID(as_uuid=True))
    note = Column(Text)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    order = relationship("Order", back_populates="status_history")


class OrderReminder(Base):
    """Relances automatiques (paiement dû, confirmation, avis)."""

    __tablename__ = "order_reminders"
    __table_args__ = (
        Index("order_reminders_order_idx", "order_id"),
        Index("order_reminders_due_idx", "status", "scheduled_at"),
        {"schema": "marketplace"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    order_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("marketplace.orders.id"), nullable=False
    )
    type = Column(String, nullable=False)
    channel = Column(String, default="WHATSAPP", nullable=False)
    status = Column(String, default="SCHEDULED", nullable=False)
    scheduled_at = Column(DateTime, nullable=False)
    sent_at = Column(DateTime)
    attempts = Column(Integer, default=0, nullable=False)
    last_error = Column(Text)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    order = relationship("Order", back_populates="reminders")


class OrderDispute(Base):
    __tablename__ = "order_disputes"
    __table_args__ = (
        Index("order_disputes_order_idx", "order_id"),
        Index("order_disputes_status_idx", "status"),
        Index("order_disputes_raised_by_idx", "raised_by_id"),
        Index("order_disputes_escrow_idx", "escrow_wallet_id"),
        {"schema": "marketplace"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    order_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("marketplace.orders.id"), nullable=False
    )
    escrow_wallet_id = Column(PG_UUID(as_uuid=True), nullable=True)
    raised_by_id = Column(PG_UUID(as_uuid=True), nullable=False)
    reason_category = Column(String, nullable=False)
    description = Column(Text, nullable=False)
    evidence_images = Column(
        PG_ARRAY(String), nullable=False, server_default=text("'{}'::text[]")
    )
    requested_solution = Column(String, nullable=False)
    disputed_amount = Column(Numeric(14, 2), default=0, nullable=False)
    escrow_payout_status = Column(String, default="HELD", nullable=False)
    status = Column(String, default="PENDING", nullable=False)
    resolution_notes = Column(Text)
    resolved_at = Column(DateTime)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Auction(Base):
    __tablename__ = "auctions"
    __table_args__ = (
        Index("auctions_status_idx", "status"),
        Index("auctions_buyer_idx", "buyer_id"),
        Index("auctions_escrow_status_idx", "escrow_status"),
        Index("auctions_zone_idx", "target_zone_id"),
        Index("auctions_deadline_idx", "deadline"),
        {"schema": "marketplace"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    buyer_id = Column(
        PG_UUID(as_uuid=True),
        ForeignKey("marketplace.buyer_profiles.id"),
        nullable=False,
    )
    sub_category_id = Column(
        PG_UUID(as_uuid=True),
        ForeignKey("governance.sub_categories.id"),
        nullable=False,
    )
    winner_bid_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.bids.id"))
    quantity = Column(Numeric(14, 3), nullable=False)
    unit = Column(String, default="TONNE", nullable=False)
    max_price_per_unit = Column(Numeric(12, 2), nullable=False)
    description = Column(Text)
    incoterm = Column(String, default="DDP", nullable=False)
    delivery_location = Column(String, nullable=False)
    delivery_deadline = Column(DateTime, nullable=False)
    quality_grading = Column(String)
    required_certifications = Column(
        PG_ARRAY(String), nullable=False, server_default=text("'{}'::text[]")
    )
    preferred_packaging = Column(String)
    # Photos de référence jointes par l'acheteur (ce qu'il recherche) — voir
    # services/database/auction.py::add_auction_photo.
    images = Column(
        PG_ARRAY(String), nullable=False, server_default=text("'{}'::text[]")
    )
    deadline = Column(DateTime, nullable=False)
    auto_extend = Column(Boolean, default=True, nullable=False)
    escrow_wallet_id = Column(PG_UUID(as_uuid=True), nullable=True)
    escrow_status = Column(String, default="NONE", nullable=False)
    status = Column(String, default="OPEN", nullable=False)
    cancellation_reason = Column(String)
    target_zone_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id"))
    version = Column(Integer, default=0, nullable=False)
    awarded_at = Column(DateTime)
    cancelled_at = Column(DateTime)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Bid(Base):
    __tablename__ = "bids"
    __table_args__ = (
        Index("bids_auction_producer_unique", "auction_id", "producer_id", unique=True),
        Index("bids_auction_idx", "auction_id"),
        Index("bids_producer_idx", "producer_id"),
        Index("bids_linked_stock_idx", "linked_stock_id"),
        Index("bids_status_idx", "status"),
        {"schema": "marketplace"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    auction_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("marketplace.auctions.id"), nullable=False
    )
    producer_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("marketplace.producers.id"), nullable=False
    )
    offered_price = Column(Numeric(12, 2), nullable=False)
    linked_stock_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.stocks.id"))
    is_winner = Column(Boolean, default=False, nullable=False)
    status = Column(String, default="PENDING", nullable=False)
    message = Column(Text)
    notified_at = Column(DateTime)
    valid_until = Column(DateTime)
    estimated_delivery_date = Column(DateTime)
    # Photos du lot proposé par le producteur — voir
    # services/database/auction.py::add_bid_photo. Indépendant de
    # `linked_stock_id` (jamais renseigné par `place_bid` en pratique) : le
    # `Stock` référencé n'a lui-même aucune colonne image.
    images = Column(
        PG_ARRAY(String), nullable=False, server_default=text("'{}'::text[]")
    )
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class MarketplaceRating(Base):
    __tablename__ = "marketplace_ratings"
    __table_args__ = (
        Index("mr_order_idx", "order_id"),
        Index("mr_author_idx", "author_id"),
        Index("mr_target_idx", "target_id"),
        Index("mr_order_author_unique", "order_id", "author_id", unique=True),
        {"schema": "marketplace"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    order_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("marketplace.orders.id"), nullable=False
    )
    author_type = Column(String, nullable=False)
    author_id = Column(PG_UUID(as_uuid=True), nullable=False)
    target_type = Column(String, nullable=False)
    target_id = Column(PG_UUID(as_uuid=True), nullable=False)
    rating_product_quality = Column(Integer)
    rating_packaging = Column(Integer)
    rating_reception_speed = Column(Integer)
    rating_communication = Column(Integer)
    rating_reliability = Column(Integer, nullable=False)
    global_rating = Column(Float, nullable=False)
    comment = Column(Text)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


__all__ = [
    "Delivery",
    "Order",
    "OrderItem",
    "Payment",
    "OrderStatusHistory",
    "OrderReminder",
    "OrderDispute",
    "Auction",
    "Bid",
    "MarketplaceRating",
]
