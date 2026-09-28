"""ORM Orders — marketplace.orders/order_items/payments/auctions/bids/deliveries.

Extrait de l'ancien `domain/models.py` (voir `orm_base.py` pour la justification
du split — même `Base` partagé, zéro changement de schéma/colonnes/index).
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import relationship

from ladini.domain.orm_base import Base, _uuid4


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
        ForeignKey("marketplace.orders.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    delivery_agent_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("marketplace.delivery_agents.id", ondelete="SET NULL")
    )
    status = Column(Text, default="PENDING", nullable=False, server_default=text("'PENDING'"))
    delivery_code = Column(Text)
    origin_gps_lat = Column(Float)
    origin_gps_lng = Column(Float)
    destination_gps_lat = Column(Float)
    destination_gps_lng = Column(Float)
    destination_desc = Column(Text)
    estimated_distance_km = Column(Float)
    actual_distance_km = Column(Float)
    shipping_condition = Column(Text)
    proof_of_delivery_url = Column(Text)
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
        Index("orders_client_idx", "client_id"),
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
        CheckConstraint(
            "award_pricing_snapshot IS NULL OR (jsonb_typeof(award_pricing_snapshot) = 'object' AND award_pricing_snapshot ? 'schema_version')",
            name="orders_award_pricing_snapshot_chk",
        ),
        Index("orders_buyer_status_idx", "buyer_id", "status"),
        Index("orders_payment_status_idx", "payment_status"),
        Index("ix_orders_paydunya_token", "paydunya_invoice_token", unique=True, postgresql_where=text("paydunya_invoice_token IS NOT NULL")),
        Index("ix_orders_payment_expires_at", "payment_expires_at", postgresql_where=text("payment_status = 'PENDING'")),
        Index("ix_orders_checkout_group", "checkout_group_id", postgresql_where=text("checkout_group_id IS NOT NULL")),
        {"schema": "marketplace"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4, server_default=text("gen_random_uuid()"))
    buyer_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("marketplace.buyer_profiles.id")
    )
    client_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.clients.id"))
    organization_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("governance.organizations.id")
    )
    zone_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id"))
    customer_name = Column(Text)
    customer_phone = Column(Text)
    payment_method = Column(Text, default="CASH", nullable=False, server_default=text("'CASH'"))
    payment_status = Column(Text, default="PENDING", nullable=False, server_default=text("'PENDING'"))
    city = Column(Text)
    # REAL (float4) : type réel en base (créé par Drizzle `real()`), aligné ici.
    gps_lat = Column(Float(24))
    gps_lng = Column(Float(24))
    delivery_desc = Column(Text)
    audio_url = Column(Text)
    status = Column(Text, default="PENDING", nullable=False, server_default=text("'PENDING'"))
    delivery_status = Column(Text, default="PENDING", nullable=False, server_default=text("'PENDING'"))
    source = Column(Text, default="APP", nullable=False, server_default=text("'APP'"))
    order_type = Column(Text, default="STANDARD", nullable=False, server_default=text("'STANDARD'"))
    whatsapp_id = Column(Text)
    total_amount = Column(Numeric(14, 2), nullable=False)
    is_agent_order = Column(Boolean, default=False, nullable=False, server_default=text("false"))
    delivery_date = Column(DateTime)
    subtotal = Column(Numeric(14, 2), default=0, nullable=False, server_default=text("'0'"))
    tax_amount = Column(Numeric(14, 2), default=0, nullable=False, server_default=text("'0'"))
    currency = Column(Text, default="XOF", nullable=False, server_default=text("'XOF'"))
    delivery_fee = Column(Numeric(14, 2), default=0, nullable=False, server_default=text("'0'"))
    cancellation_role = Column(Text)
    escrow_wallet_id = Column(PG_UUID(as_uuid=True), nullable=True)
    # --- Escrow Paydunya ---
    # `payment_status` (ci-dessus) porte désormais aussi : ESCROWED (payé,
    # fonds bloqués), PAID_OUT (livraison confirmée par OTP, fonds débloqués),
    # REFUNDED — en plus des valeurs existantes PENDING/PAID/CANCELLED.
    # Colonnes déclarées dans Drizzle (source de vérité du schéma) ; migrations Drizzle.
    paydunya_invoice_token = Column(Text, nullable=True)
    delivery_otp = Column(Text, nullable=True)
    payment_expires_at = Column(DateTime, nullable=True)
    locked_amount = Column(Numeric(14, 2), nullable=True)
    # Anti-force-brute du code de livraison (audit sécurité 2026-09-10) :
    # `delivery_otp` ne fait que 4 chiffres et son unique vérificateur
    # (`EscrowMixin.verify_delivery_otp`) n'avait AUCUN compteur de tentatives —
    # un producteur pouvait donc deviner un code et faire passer une commande
    # en PAID_OUT/DELIVERED (déblocage de fonds) sans que l'acheteur ait
    # jamais confirmé la livraison. Compteur PERSISTÉ (pas en mémoire process :
    # il doit survivre aux redéploiements et être partagé par tous les workers).
    delivery_otp_attempts = Column(Integer, default=0, nullable=False, server_default=text("0"))
    delivery_otp_locked_until = Column(DateTime, nullable=True)
    market_offer_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("marketplace.market_offers.id")
    )
    expected_fulfillment_date = Column(DateTime)
    preorder_converted_at = Column(DateTime)
    confirmed_at = Column(DateTime)
    # (2026-09-05, Phase 6A) Corrélation de checkout — une commande par
    # producteur. NULL = commande hors checkout groupé (RFQ, vente directe)
    # OU commande antérieure à ce modèle (grandfathering) : dans les deux cas
    # « groupe d'une seule commande ». Ne porte AUCUN état : chaque commande
    # garde son propre cycle de vie après confirmation.
    checkout_group_id = Column(PG_UUID(as_uuid=True), nullable=True)
    auction_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("marketplace.auctions.id"), unique=True
    )
    winning_bid_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.bids.id"))
    # Phase B2a — instantané IMMUABLE du prix attribué d'une commande d'appel d'offres (elle n'a pas de
    # `order_items`, faute de produit). JSONB versionné, écrit une fois à l'attribution.
    award_pricing_snapshot = Column(JSONB)
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
        CheckConstraint('pricing_snapshot_version IS NOT NULL OR (commercial_price_amount IS NULL AND price_basis IS NULL AND price_unit IS NULL AND package_type IS NULL AND package_content_amount IS NULL AND package_content_unit IS NULL AND normalized_unit_price IS NULL AND normalized_unit IS NULL AND quantity_unit IS NULL AND currency IS NULL)', name="order_items_snapshot_all_null_chk"),
        CheckConstraint("pricing_snapshot_version IS NULL OR (pricing_snapshot_version >= 1 AND commercial_price_amount IS NOT NULL AND commercial_price_amount > 0 AND quantity_unit IS NOT NULL AND currency IS NOT NULL AND price_basis IS NOT NULL AND price_basis IN ('PER_BASE_UNIT','PER_PACKAGE','TOTAL_LOT') AND (price_basis <> 'PER_BASE_UNIT' OR price_unit IS NOT NULL) AND (price_basis = 'PER_BASE_UNIT' OR price_unit IS NULL) AND (price_basis <> 'PER_PACKAGE' OR (package_type IS NOT NULL AND package_content_amount IS NOT NULL AND package_content_amount > 0 AND package_content_unit IS NOT NULL)) AND (price_basis = 'PER_PACKAGE' OR (package_type IS NULL AND package_content_amount IS NULL AND package_content_unit IS NULL)) AND (normalized_unit_price IS NULL OR (normalized_unit_price > 0 AND normalized_unit IS NOT NULL)))", name="order_items_snapshot_chk"),
        {"schema": "marketplace"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4, server_default=text("gen_random_uuid()"))
    order_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("marketplace.orders.id"), nullable=False
    )
    product_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("marketplace.products.id"), nullable=False
    )
    quantity = Column(Numeric(14, 3), nullable=False)
    price_at_sale = Column(Numeric(12, 2), nullable=False)
    # (2026-08-30) Support des paliers de prix/conditionnement multiples
    # (`Product.pricing_tiers`) — voir domain/pricing_tiers.py. NULL sur
    # TOUTE commande sans palier (produit sans pricing_tiers, ou commande
    # créée avant cette colonne) : traçabilité complète du palier acheté
    # sans casser aucune ligne de commande existante.
    tier_id = Column(Text, nullable=True)
    # Quantité déjà convertie dans l'unité de BASE du produit (ex: 3 bidons
    # de 10L => 30, en LITRE) — c'est CETTE valeur qu'il faut débiter de
    # `Product.quantity_for_sale`, jamais `quantity` telle quelle dès qu'un
    # palier est impliqué (voir domain/pricing_tiers.py::resolve_stock_debit,
    # le SEUL point de calcul du débit de stock).
    base_unit_quantity = Column(Numeric(14, 3), nullable=True)
    # Phase B2a — INSTANTANÉ IMMUABLE de la sémantique commerciale (domain/commercial_pricing_snapshot.py).
    # NULL partout sur une ligne antérieure : sa base de prix est INCONNUE, jamais rétro-inférée.
    # Gelé par trigger (order_items_snapshot_immutable_trg) une fois `pricing_snapshot_version` posée.
    quantity_unit = Column(Text)
    commercial_price_amount = Column(Numeric(14, 2))
    price_basis = Column(Text)
    price_unit = Column(Text)
    package_type = Column(Text)
    package_content_amount = Column(Numeric(14, 3))
    package_content_unit = Column(Text)
    normalized_unit_price = Column(Numeric(18, 4))
    normalized_unit = Column(Text)
    currency = Column(Text)
    pricing_snapshot_version = Column(Integer)

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

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4, server_default=text("gen_random_uuid()"))
    order_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("marketplace.orders.id"), nullable=False
    )
    amount = Column(Numeric(14, 2), nullable=False)
    currency = Column(Text, default="XOF", nullable=False, server_default=text("'XOF'"))
    method = Column(Text, default="CASH", nullable=False, server_default=text("'CASH'"))
    status = Column(Text, default="PENDING", nullable=False, server_default=text("'PENDING'"))
    provider = Column(Text)
    provider_ref = Column(Text)
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

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4, server_default=text("gen_random_uuid()"))
    order_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("marketplace.orders.id"), nullable=False
    )
    status_type = Column(Text, nullable=False)  # ORDER | PAYMENT | DELIVERY
    from_status = Column(Text)
    to_status = Column(Text, nullable=False)
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

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4, server_default=text("gen_random_uuid()"))
    order_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("marketplace.orders.id"), nullable=False
    )
    type = Column(Text, nullable=False)
    channel = Column(Text, default="WHATSAPP", nullable=False, server_default=text("'WHATSAPP'"))
    status = Column(Text, default="SCHEDULED", nullable=False, server_default=text("'SCHEDULED'"))
    scheduled_at = Column(DateTime, nullable=False)
    sent_at = Column(DateTime)
    attempts = Column(Integer, default=0, nullable=False, server_default=text("0"))
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

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4, server_default=text("gen_random_uuid()"))
    order_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("marketplace.orders.id"), nullable=False
    )
    escrow_wallet_id = Column(PG_UUID(as_uuid=True), nullable=True)
    raised_by_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id", ondelete="RESTRICT"), nullable=False)
    reason_category = Column(Text, nullable=False)
    description = Column(Text, nullable=False)
    evidence_images = Column(
        PG_ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    requested_solution = Column(Text, nullable=False)
    disputed_amount = Column(Numeric(14, 2), default=0, nullable=False, server_default=text("'0'"))
    escrow_payout_status = Column(Text, default="HELD", nullable=False, server_default=text("'HELD'"))
    status = Column(Text, default="PENDING", nullable=False, server_default=text("'PENDING'"))
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
        Index("auctions_subcategory_idx", "sub_category_id"),
        Index("auctions_buyer_idx", "buyer_id"),
        Index("auctions_escrow_status_idx", "escrow_status"),
        Index("auctions_zone_idx", "target_zone_id"),
        Index("auctions_deadline_idx", "deadline"),
        {"schema": "marketplace"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4, server_default=text("gen_random_uuid()"))
    buyer_id = Column(
        PG_UUID(as_uuid=True),
        ForeignKey("marketplace.buyer_profiles.id", ondelete="RESTRICT"),
        nullable=False,
    )
    sub_category_id = Column(
        PG_UUID(as_uuid=True),
        ForeignKey("governance.sub_categories.id", ondelete="RESTRICT"),
        nullable=False,
    )
    winner_bid_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.bids.id", ondelete="RESTRICT"))
    quantity = Column(Numeric(14, 3), nullable=False)
    unit = Column(Text, default="TONNE", nullable=False, server_default=text("'TONNE'"))
    max_price_per_unit = Column(Numeric(12, 2), nullable=False)
    description = Column(Text)
    incoterm = Column(Text, default="DDP", nullable=False, server_default=text("'DDP'"))
    delivery_location = Column(Text, nullable=False)
    delivery_deadline = Column(DateTime, nullable=False)
    quality_grading = Column(Text)
    required_certifications = Column(
        PG_ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    preferred_packaging = Column(Text)
    # Photos de référence jointes par l'acheteur (ce qu'il recherche) — voir
    # services/database/auction.py::add_auction_photo.
    images = Column(
        PG_ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    deadline = Column(DateTime, nullable=False)
    auto_extend = Column(Boolean, default=True, nullable=False, server_default=text("true"))
    escrow_wallet_id = Column(PG_UUID(as_uuid=True), nullable=True)
    escrow_status = Column(Text, default="NONE", nullable=False, server_default=text("'NONE'"))
    status = Column(Text, default="OPEN", nullable=False, server_default=text("'OPEN'"))
    cancellation_reason = Column(Text)
    target_zone_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id", ondelete="SET NULL"))
    # DEPRECATED / UNUSED (2026-09-04, audit fonctionnel/transactionnel
    # Auction↔Bid — voir docs/AUCTION_BID_TRANSACTIONAL_AUDIT_2026-09-04.md
    # section L, et son suivi WINNER_AND_ORDER_LIFECYCLE audit) :
    # aucun code de ce dépôt ne lit plus cette colonne (elle n'a jamais
    # servi de verrou optimiste — toute la concurrence Auction/Bid est
    # protégée par `SELECT...FOR UPDATE`, pas par CAS de version). Elle
    # avait un unique écrivain (`services/database/buyer.py::update_negotiation_offer`,
    # un compteur "nombre de corrections de prix" jamais consommé), retiré
    # à cette même date. Colonne conservée en base : suppression = migration
    # Drizzle destructive (EXPAND/CONTRACT, voir docs/runbooks/migrations.md), et un éventuel
    # lecteur hors de ce dépôt (tableau de bord admin externe) ne peut pas
    # être exclu depuis ici. Ne pas réutiliser cette colonne pour un
    # nouveau besoin sans revérifier cette conclusion.
    version = Column(Integer, default=0, nullable=False, server_default=text("0"))
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
        Index("bids_one_winner_per_auction_uq", "auction_id", unique=True, postgresql_where=text("is_winner = true")),
        CheckConstraint("offered_price_basis IS NULL OR offered_price_basis IN ('PER_BASE_UNIT','PER_PACKAGE','TOTAL_LOT','LEGACY_UNSPECIFIED')", name="bids_price_basis_chk"),
        CheckConstraint("pricing_snapshot_version IS NULL OR (pricing_snapshot_version >= 1 AND offered_price > 0 AND offered_price_currency IS NOT NULL AND offered_price_basis IS NOT NULL AND offered_price_basis IN ('PER_BASE_UNIT','PER_PACKAGE','TOTAL_LOT') AND (offered_price_basis <> 'PER_BASE_UNIT' OR offered_price_unit IS NOT NULL) AND (offered_price_basis = 'PER_BASE_UNIT' OR offered_price_unit IS NULL) AND (offered_price_basis <> 'PER_PACKAGE' OR (package_type IS NOT NULL AND package_content_amount IS NOT NULL AND package_content_amount > 0 AND package_content_unit IS NOT NULL)) AND (offered_price_basis = 'PER_PACKAGE' OR (package_type IS NULL AND package_content_amount IS NULL AND package_content_unit IS NULL)) AND (normalized_unit_price IS NULL OR (normalized_unit_price > 0 AND normalized_unit IS NOT NULL)))", name="bids_snapshot_chk"),
        {"schema": "marketplace"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4, server_default=text("gen_random_uuid()"))
    auction_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("marketplace.auctions.id", ondelete="RESTRICT"), nullable=False
    )
    producer_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("marketplace.producers.id", ondelete="RESTRICT"), nullable=False
    )
    offered_price = Column(Numeric(12, 2), nullable=False)
    # Phase B2a — CONTRAT DE PRIX du bid. NULL = bid antérieur : base de prix INCONNUE (jamais
    # « par unité de l'enchère »). Bid nouveau : base obligatoire (domain/commercial_pricing_snapshot).
    offered_price_basis = Column(Text)
    offered_price_unit = Column(Text)
    offered_price_currency = Column(Text)
    package_type = Column(Text)
    package_content_amount = Column(Numeric(14, 3))
    package_content_unit = Column(Text)
    normalized_unit_price = Column(Numeric(18, 4))
    normalized_unit = Column(Text)
    pricing_snapshot_version = Column(Integer)
    linked_stock_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.stocks.id", ondelete="SET NULL"))
    is_winner = Column(Boolean, default=False, nullable=False, server_default=text("false"))
    status = Column(Text, default="PENDING", nullable=False, server_default=text("'PENDING'"))
    message = Column(Text)
    notified_at = Column(DateTime)
    valid_until = Column(DateTime)
    estimated_delivery_date = Column(DateTime)
    # Photos du lot proposé par le producteur — voir
    # services/database/auction.py::add_bid_photo. Indépendant de
    # `linked_stock_id` (jamais renseigné par `place_bid` en pratique) : le
    # `Stock` référencé n'a lui-même aucune colonne image.
    images = Column(
        PG_ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )




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
]
