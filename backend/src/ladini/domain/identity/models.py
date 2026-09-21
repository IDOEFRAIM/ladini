"""ORM Identity — auth.users/accounts/sessions + marketplace.producers/clients/
buyer_types/buyer_profiles/delivery_agents + intelligence.trust_scores.

Extrait de l'ancien `domain/models.py` (1215 lignes, un seul fichier) — split
par grand groupe métier, zéro changement de schéma/colonnes/index.
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
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import DOUBLE_PRECISION, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import relationship

from ladini.domain.orm_base import Base, _uuid4


# ══════════════════════════════════════════════════════════════════════════
# AUTH
# ══════════════════════════════════════════════════════════════════════════
class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        Index("users_created_idx", "created_at"),
        Index("users_role_idx", "role"),
        Index("users_zone_idx", "zone_id"),
        {"schema": "auth"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4, server_default=text("gen_random_uuid()"))
    name = Column(Text)
    email = Column(Text, unique=True, index=True)
    email_verified = Column(DateTime)
    image = Column(Text)
    password = Column(Text)
    phone = Column(Text, unique=True, index=True)
    whatsapp_enabled = Column(Boolean, default=True, server_default=text("true"))
    onboarding_completed = Column(Boolean, nullable=False, server_default=text("false"))
    latitude = Column(Float)
    longitude = Column(Float)
    # Horodatage de la dernière mise à jour GPS (nullable — l'absence de
    # position ne doit jamais bloquer un profil). Alimenté par l'ingestion
    # native Twilio (message de localisation WhatsApp) ou par mise à jour
    # manuelle ultérieure (menu dédié).
    location_updated_at = Column(DateTime, nullable=True)
    cnib_number = Column(Text, unique=True)
    role = Column(Text, default="USER", nullable=False, server_default=text("'USER'"))
    identity_verified = Column(Boolean, default=False, server_default=text("false"))
    zone_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id", ondelete="SET NULL"))
    # Modération / abus : blocage (annulations répétées) & bannissement (produits interdits).
    account_status = Column(
        Text,
        default="ACTIVE",
        nullable=False,
        server_default=text("'ACTIVE'"),
        index=True,
    )
    blocked_reason = Column(Text)
    blocked_at = Column(DateTime)
    deleted_at = Column(DateTime)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    producer = relationship("Producer", back_populates="user", uselist=False)


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        Index("accounts_provider_unique", "provider", "provider_account_id", unique=True),
        Index("accounts_user_idx", "user_id"),
        {"schema": "auth"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4, server_default=text("gen_random_uuid()"))
    user_id = Column(
        PG_UUID(as_uuid=True),
        ForeignKey("auth.users.id", ondelete="CASCADE"),
        nullable=False,
    )
    type = Column(Text, nullable=False)
    provider = Column(Text, nullable=False)
    provider_account_id = Column(Text, nullable=False)
    refresh_token = Column(Text)
    access_token = Column(Text)
    expires_at = Column(Integer)
    token_type = Column(Text)
    scope = Column(Text)
    id_token = Column(Text)
    session_state = Column(Text)


class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        Index("sessions_user_idx", "user_id"),
        {"schema": "auth"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4, server_default=text("gen_random_uuid()"))
    session_token = Column(Text, unique=True, nullable=False)
    user_id = Column(
        PG_UUID(as_uuid=True),
        ForeignKey("auth.users.id", ondelete="CASCADE"),
        nullable=False,
    )
    expires = Column(DateTime, nullable=False)


# ══════════════════════════════════════════════════════════════════════════
# MARKETPLACE — identité (producteurs, acheteurs, livreurs)
# ══════════════════════════════════════════════════════════════════════════
class Producer(Base):
    __tablename__ = "producers"
    __table_args__ = (
        Index("producers_status_idx", "status"),
        Index("producers_org_idx", "organization_id"),
        Index("producers_zone_idx", "zone_id"),
        {"schema": "marketplace"},
    )

    id = Column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("auth.users.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    organization_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("governance.organizations.id", ondelete="SET NULL")
    )
    zone_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id", ondelete="SET NULL"))
    business_name = Column(Text)
    status = Column(Text, default="PENDING", nullable=False, server_default=text("'PENDING'"))
    is_certified = Column(Boolean, default=False, nullable=False, server_default=text("false"))
    region = Column(Text)
    province = Column(Text)
    commune = Column(Text)
    logo_url = Column(Text)
    phone_number = Column(Text)
    rating = Column(Integer)
    reviews_count = Column(Integer, default=0, nullable=False, server_default=text("0"))
    company_registration_number = Column(Text)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user = relationship(
        "User", back_populates="producer", lazy="joined", foreign_keys=[user_id]
    )
    farms = relationship(
        "Farm", back_populates="producer", cascade="all, delete-orphan"
    )
    clients = relationship(
        "Client", back_populates="producer", cascade="all, delete-orphan"
    )
    offers = relationship(
        "MarketOffer", back_populates="producer", cascade="all, delete-orphan"
    )


class Client(Base):
    __tablename__ = "clients"
    __table_args__ = (
        Index("clients_phone_idx", "phone"),
        Index("clients_name_idx", "name"),
        Index("clients_producer_idx", "producer_id"),
        {"schema": "marketplace"},
    )

    id = Column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    producer_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.producers.id", ondelete="SET NULL"))
    name = Column(Text, nullable=False)
    phone = Column(Text, nullable=False)
    email = Column(Text)
    location = Column(Text)
    total_orders = Column(Integer, default=0, nullable=False, server_default=text("0"))
    total_spent = Column(Float, default=0.0, nullable=False, server_default=text("0"))
    last_order_date = Column(DateTime)
    tax_id = Column(Text)
    prefered_payement_method = Column(JSONB)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    producer = relationship(
        "Producer", back_populates="clients", foreign_keys=[producer_id]
    )


class BuyerType(Base):
    __tablename__ = "buyer_types"
    __table_args__ = {"schema": "marketplace"}

    id = Column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    name = Column(Text, unique=True, nullable=False)
    description = Column(Text)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class BuyerProfile(Base):
    __tablename__ = "buyer_profiles"
    __table_args__ = (
        Index("buyer_profiles_user_idx", "user_id"),
        Index("buyer_profiles_type_idx", "buyer_type_id"),
        Index("buyer_profiles_verified_idx", "is_verified"),
        {"schema": "marketplace"},
    )

    id = Column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("auth.users.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    buyer_type_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("marketplace.buyer_types.id", ondelete="SET NULL")
    )
    establishment_name = Column(Text)
    default_delivery_address = Column(Text)
    is_verified = Column(Boolean, default=False, nullable=False, server_default=text("false"))
    trust_badge = Column(Text)
    rating = Column(Float)
    reviews_count = Column(Integer, default=0, nullable=False, server_default=text("0"))
    company_registration_number = Column(Text)
    verified_at = Column(DateTime)
    verified_by_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id", ondelete="SET NULL"))
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user = relationship("User", foreign_keys=[user_id])


class DeliveryAgent(Base):
    __tablename__ = "delivery_agents"
    __table_args__ = (
        Index("delivery_agents_status_idx", "status"),
        Index("delivery_agents_zone_idx", "zone_id"),
        {"schema": "marketplace"},
    )

    id = Column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("auth.users.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    vehicle_type = Column(Text)
    license_number = Column(Text)
    status = Column(Text, default="OFFLINE", nullable=False, server_default=text("'OFFLINE'"))
    zone_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id", ondelete="SET NULL"))
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user = relationship("User", foreign_keys=[user_id])


# ══════════════════════════════════════════════════════════════════════════
# INTELLIGENCE — réputation (rattachée à l'identité)
# ══════════════════════════════════════════════════════════════════════════
class TrustScore(Base):
    __tablename__ = "trust_scores"
    __table_args__ = {"schema": "intelligence"}

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4, server_default=text("gen_random_uuid()"))
    user_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("auth.users.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    global_score = Column(DOUBLE_PRECISION, default=0.0, nullable=False, server_default=text("0"))
    reliability_index = Column(DOUBLE_PRECISION, default=0.0, nullable=False, server_default=text("0"))
    quality_index = Column(DOUBLE_PRECISION, default=0.0, nullable=False, server_default=text("0"))
    compliance_index = Column(DOUBLE_PRECISION, default=0.0, nullable=False, server_default=text("0"))
    resilience_bonus = Column(DOUBLE_PRECISION, default=0.0, nullable=False, server_default=text("0"))
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


__all__ = [
    "User",
    "Account",
    "Session",
    "Producer",
    "Client",
    "BuyerType",
    "BuyerProfile",
    "DeliveryAgent",
    "TrustScore",
]
