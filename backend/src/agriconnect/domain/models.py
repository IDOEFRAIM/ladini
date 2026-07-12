"""Canonical SQLAlchemy ORM models for AgriConnect — Marketplace edition.

Single source-of-truth for ORM models used by the async database services.
Aligned 1-for-1 with the Drizzle schema (frontag/src/db/schema). All legacy
"conseil" models (agronomy, sensors, weather, recommendations) have been removed.

`crop_cycles` -> `market_offers`. A backward-compat alias `CropCycle` is kept
so in-flight imports do not crash during the migration of call sites.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
    Index,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import (
    JSONB,
    UUID as PG_UUID,
    ARRAY as PG_ARRAY,
    DOUBLE_PRECISION,
)
from sqlalchemy.inspection import inspect
from sqlalchemy.orm import declarative_base, relationship


class _ToDictMixin:
    def to_dict(self) -> dict:
        mapper = inspect(self).mapper
        data = {}
        for attr in mapper.column_attrs:
            key = attr.key
            val = getattr(self, key)
            data[key] = str(val) if isinstance(val, uuid.UUID) else val
        return data


Base = declarative_base(cls=_ToDictMixin)


def _uuid4():
    return str(uuid.uuid4())


# ══════════════════════════════════════════════════════════════════════════
# AUTH
# ══════════════════════════════════════════════════════════════════════════
class User(Base):
    __tablename__ = "users"
    __table_args__ = {"schema": "auth"}

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    name = Column(String)
    email = Column(String, unique=True, index=True)
    email_verified = Column(DateTime)
    image = Column(String)
    password = Column(String)
    phone = Column(String, unique=True, index=True)
    whatsapp_enabled = Column(Boolean, default=True)
    onboarding_completed = Column(Boolean, nullable=False, server_default=text("false"))
    latitude = Column(Float)
    longitude = Column(Float)
    cnib_number = Column(String, unique=True)
    role = Column(String, default="USER", nullable=False)
    identity_verified = Column(Boolean, default=False)
    zone_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id"))
    # Modération / abus : blocage (annulations répétées) & bannissement (produits interdits).
    account_status = Column(String, default="ACTIVE", nullable=False, server_default=text("'ACTIVE'"), index=True)
    blocked_reason = Column(Text)
    blocked_at = Column(DateTime)
    deleted_at = Column(DateTime)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    producer = relationship("Producer", back_populates="user", uselist=False)


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = {"schema": "auth"}

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    user_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id", ondelete="CASCADE"), nullable=False)
    type = Column(String, nullable=False)
    provider = Column(String, nullable=False)
    provider_account_id = Column(String, nullable=False)
    refresh_token = Column(Text)
    access_token = Column(Text)
    expires_at = Column(Integer)
    token_type = Column(String)
    scope = Column(String)
    id_token = Column(Text)
    session_state = Column(String)


class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = {"schema": "auth"}

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    session_token = Column(String, unique=True, nullable=False)
    user_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id", ondelete="CASCADE"), nullable=False)
    expires = Column(DateTime, nullable=False)


# ══════════════════════════════════════════════════════════════════════════
# GOVERNANCE
# ══════════════════════════════════════════════════════════════════════════
class Organization(Base):
    __tablename__ = "organizations"
    __table_args__ = {"schema": "governance"}

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    name = Column(String, nullable=False)
    type = Column(String, nullable=False)
    tax_id = Column(String, unique=True)
    description = Column(Text)
    status = Column(String, default="PENDING", nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class UserOrganization(Base):
    __tablename__ = "user_organizations"
    __table_args__ = (
        UniqueConstraint("user_id", "organization_id", name="user_org_unique"),
        {"schema": "governance"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id"), nullable=False)
    organization_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.organizations.id"), nullable=False)
    role = Column(String, default="FIELD_AGENT", nullable=False)
    role_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.role_definitions.id"))
    managed_zone_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id"))


class RoleDefinition(Base):
    __tablename__ = "role_definitions"
    __table_args__ = {"schema": "governance"}

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    name = Column(String, unique=True, nullable=False)
    description = Column(Text)
    permissions = Column(PG_ARRAY(String), nullable=False, server_default=text("'{}'::text[]"))
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class ClimaticRegion(Base):
    __tablename__ = "climatic_regions"
    __table_args__ = {"schema": "governance"}

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    name = Column(String, unique=True, nullable=False)
    description = Column(Text)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class Zone(Base):
    __tablename__ = "zones"
    __table_args__ = (
        Index("zones_region_idx", "climatic_region_id"),
        Index("zones_org_idx", "organization_id"),
        Index("zones_active_idx", "is_active"),
        Index("zones_parent_idx", "parent_id"),
        Index("zones_path_idx", "path"),
        {"schema": "governance"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    name = Column(String, unique=True, nullable=False)
    code = Column(String, unique=True, nullable=False)
    climatic_region_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.climatic_regions.id"), nullable=False)
    organization_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.organizations.id"))
    parent_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id"))
    path = Column(String)
    depth = Column(Integer, default=0, nullable=False)
    latitude = Column(Float)
    longitude = Column(Float)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class WorkZone(Base):
    __tablename__ = "work_zones"
    __table_args__ = (
        UniqueConstraint("organization_id", "zone_id", name="work_zones_org_zone_unique"),
        Index("work_zones_org_idx", "organization_id"),
        Index("work_zones_zone_idx", "zone_id"),
        {"schema": "governance"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    organization_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.organizations.id"), nullable=False)
    zone_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id"), nullable=False)
    manager_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id"))
    role = Column(String)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class ZoneMetric(Base):
    __tablename__ = "zone_metrics"
    __table_args__ = (
        Index("zone_metrics_composite_idx", "zone_id", "date", "metric_name"),
        {"schema": "governance"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    zone_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id"), nullable=False)
    date = Column(DateTime, server_default=func.now(), nullable=False)
    metric_name = Column(String, nullable=False)
    value = Column(Float, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = {"schema": "governance"}

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    name = Column(String, unique=True, nullable=False)
    description = Column(Text)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class SubCategory(Base):
    __tablename__ = "sub_categories"
    __table_args__ = (
        UniqueConstraint("category_id", "name", name="sub_categories_cat_name_unique"),
        {"schema": "governance"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    category_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.categories.id"), nullable=False)
    name = Column(String, nullable=False)
    blocked_zone_ids = Column(PG_ARRAY(String), nullable=False, server_default=text("'{}'::text[]"))
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class StandardPrice(Base):
    __tablename__ = "standard_prices"
    __table_args__ = (
        UniqueConstraint("sub_category_id", "zone_id", name="standard_prices_sub_zone_unique"),
        Index("standard_prices_zone_idx", "zone_id"),
        {"schema": "governance"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    sub_category_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.sub_categories.id"), nullable=False)
    zone_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id"), nullable=False)
    price_per_unit = Column(Float, nullable=False)
    unit = Column(String, default="KG", nullable=False)
    updated_by_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id"), nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class ZoneSetting(Base):
    __tablename__ = "zone_settings"
    __table_args__ = (
        UniqueConstraint("zone_id", "key", name="zone_settings_zone_key_unique"),
        Index("zone_settings_zone_idx", "zone_id"),
        {"schema": "governance"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    zone_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id"), nullable=False)
    key = Column(String, nullable=False)
    value = Column(JSONB, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class ProhibitedTerm(Base):
    """Liste noire des produits interdits (drogue, armes…) gérée par les admins."""

    __tablename__ = "prohibited_terms"
    __table_args__ = (
        Index("prohibited_terms_active_idx", "is_active"),
        {"schema": "governance"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    term = Column(String, unique=True, nullable=False)
    category = Column(String, default="ILLICIT", nullable=False, server_default=text("'ILLICIT'"))
    severity = Column(String, default="HIGH", nullable=False, server_default=text("'HIGH'"))
    is_active = Column(Boolean, default=True, nullable=False, server_default=text("true"))
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class OverlayLayer(Base):
    __tablename__ = "overlay_layers"
    __table_args__ = (
        UniqueConstraint("zone_id", "key", name="overlay_layers_zone_key_unique"),
        Index("overlay_layers_zone_idx", "zone_id"),
        {"schema": "governance"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    zone_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id"), nullable=False)
    key = Column(String, nullable=False)
    label = Column(String, nullable=False)
    enabled = Column(Boolean, default=False, nullable=False)
    settings = Column(JSONB)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


# ══════════════════════════════════════════════════════════════════════════
# MARKETPLACE
# ══════════════════════════════════════════════════════════════════════════
class Warehouse(Base):
    __tablename__ = "warehouses"
    __table_args__ = (Index("warehouses_zone_idx", "zone_id"), {"schema": "marketplace"})

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    name = Column(String, nullable=False)
    type = Column(String, nullable=False)
    capacity = Column(Float)
    location = Column(String)
    zone_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id"))
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class Producer(Base):
    __tablename__ = "producers"
    __table_args__ = (
        Index("producers_status_idx", "status"),
        Index("producers_org_idx", "organization_id"),
        Index("producers_zone_idx", "zone_id"),
        {"schema": "marketplace"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id"), nullable=False, unique=True)
    organization_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.organizations.id"))
    zone_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id"))
    business_name = Column(String)
    status = Column(String, default="PENDING", nullable=False)
    is_certified = Column(Boolean, default=False, nullable=False)
    region = Column(String)
    province = Column(String)
    commune = Column(String)
    logo_url = Column(String)
    phone_number = Column(String)
    rating = Column(Integer)
    reviews_count = Column(Integer, default=0, nullable=False)
    company_registration_number = Column(String)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User", back_populates="producer", lazy="joined", foreign_keys=[user_id])
    farms = relationship("Farm", back_populates="producer", cascade="all, delete-orphan")
    clients = relationship("Client", back_populates="producer", cascade="all, delete-orphan")
    offers = relationship("MarketOffer", back_populates="producer", cascade="all, delete-orphan")


class Client(Base):
    __tablename__ = "clients"
    __table_args__ = (
        Index("clients_phone_idx", "phone"),
        Index("clients_name_idx", "name"),
        Index("clients_producer_idx", "producer_id"),
        {"schema": "marketplace"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    producer_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.producers.id"))
    name = Column(String, nullable=False)
    phone = Column(String, nullable=False)
    email = Column(String)
    location = Column(String)
    total_orders = Column(Integer, default=0, nullable=False)
    total_spent = Column(Float, default=0.0, nullable=False)
    last_order_date = Column(DateTime)
    tax_id = Column(String)
    prefered_payement_method = Column(JSONB)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    producer = relationship("Producer", back_populates="clients", foreign_keys=[producer_id])


class BuyerType(Base):
    __tablename__ = "buyer_types"
    __table_args__ = {"schema": "marketplace"}

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    name = Column(String, unique=True, nullable=False)
    description = Column(Text)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class BuyerProfile(Base):
    __tablename__ = "buyer_profiles"
    __table_args__ = (
        Index("buyer_profiles_user_idx", "user_id"),
        Index("buyer_profiles_type_idx", "buyer_type_id"),
        Index("buyer_profiles_verified_idx", "is_verified"),
        {"schema": "marketplace"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id"), nullable=False, unique=True)
    buyer_type_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.buyer_types.id"))
    establishment_name = Column(String)
    default_delivery_address = Column(Text)
    is_verified = Column(Boolean, default=False, nullable=False)
    trust_badge = Column(String)
    rating = Column(Float)
    reviews_count = Column(Integer, default=0, nullable=False)
    company_registration_number = Column(String)
    verified_at = Column(DateTime)
    verified_by_id = Column(PG_UUID(as_uuid=True))
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User", foreign_keys=[user_id])


class DeliveryAgent(Base):
    __tablename__ = "delivery_agents"
    __table_args__ = {"schema": "marketplace"}

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id"), nullable=False, unique=True)
    vehicle_type = Column(String)
    license_number = Column(String)
    status = Column(String, default="OFFLINE", nullable=False)
    zone_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id"))
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User", foreign_keys=[user_id])


class Delivery(Base):
    __tablename__ = "deliveries"
    __table_args__ = (
        Index("deliveries_order_unique", "order_id", unique=True),
        Index("deliveries_agent_idx", "delivery_agent_id"),
        Index("deliveries_status_idx", "status"),
        Index("deliveries_agent_status_idx", "delivery_agent_id", "status"),
        {"schema": "marketplace"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    order_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.orders.id"), nullable=False, unique=True)
    delivery_agent_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.delivery_agents.id"))
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
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    agent = relationship("DeliveryAgent", foreign_keys=[delivery_agent_id])
    order = relationship("Order", back_populates="delivery", foreign_keys=[order_id])


class Farm(Base):
    __tablename__ = "farms"
    __table_args__ = (
        Index("farms_producer_idx", "producer_id"),
        Index("farms_zone_idx", "zone_id"),
        {"schema": "marketplace"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    name = Column(String, nullable=False)
    location = Column(String)
    size = Column(Float)
    producer_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.producers.id"), nullable=False)
    zone_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id"))
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    producer = relationship("Producer", back_populates="farms")
    stocks = relationship("Stock", back_populates="farm", cascade="all, delete-orphan")
    offers = relationship("MarketOffer", back_populates="farm")


class MarketOffer(Base):
    """Offre de vente / prévente (ex crop_cycles)."""

    __tablename__ = "market_offers"
    __table_args__ = (
        Index("market_offers_producer_idx", "producer_id"),
        Index("market_offers_farm_idx", "farm_id"),
        Index("market_offers_subcategory_idx", "sub_category_id"),
        Index("market_offers_available_at_idx", "estimated_available_at"),
        Index("market_offers_public_status_idx", "is_public", "status"),
        Index("market_offers_preorder_idx", "preorder_enabled"),
        {"schema": "marketplace"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    producer_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.producers.id"), nullable=False)
    farm_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.farms.id"))
    sub_category_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.sub_categories.id"))

    product_label = Column(String, nullable=False)
    production_type = Column(String, default="CROP", nullable=False)
    species = Column(String)
    breed = Column(String)

    unit = Column(String, default="KG", nullable=False)
    price_per_unit = Column(Numeric(12, 2))
    available_quantity = Column(Numeric(14, 3), default=0, nullable=False)
    reserved_quantity = Column(Numeric(14, 3), default=0, nullable=False)
    current_stock = Column(Numeric(14, 3), default=0, nullable=False)

    is_public = Column(Boolean, default=False, nullable=False)
    preorder_enabled = Column(Boolean, default=False, nullable=False)
    estimated_available_at = Column(DateTime)
    expected_harvest_date = Column(DateTime)
    status = Column(String, default="DRAFT", nullable=False)

    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    producer = relationship("Producer", back_populates="offers")
    farm = relationship("Farm", back_populates="offers")
    orders = relationship("Order", back_populates="offer")


# Backward-compat alias (call sites migrent progressivement vers MarketOffer)
CropCycle = MarketOffer


class Stock(Base):
    __tablename__ = "stocks"
    __table_args__ = {"schema": "marketplace"}

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    farm_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.farms.id"), index=True)
    warehouse_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.warehouses.id"), index=True)
    verified_by_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id"), index=True)
    organization_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.organizations.id"))
    item_name = Column(String, nullable=False)
    quantity = Column(Numeric(14, 3), default=0, nullable=False)
    unit = Column(String, default="KG", nullable=False)
    type = Column(String, default="HARVEST", nullable=False)
    verified_at = Column(DateTime)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    farm = relationship("Farm", back_populates="stocks")


class StockMovement(Base):
    __tablename__ = "stock_movements"
    __table_args__ = (
        Index("stock_movements_stock_idx", "stock_id"),
        Index("stock_movements_created_idx", "created_at"),
        {"schema": "marketplace"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    stock_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.stocks.id"), nullable=False)
    type = Column(String, nullable=False)
    quantity = Column(Numeric(14, 3), nullable=False)
    reason = Column(String)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class Batch(Base):
    __tablename__ = "batches"
    __table_args__ = (
        Index("batches_stock_idx", "stock_id"),
        Index("batches_org_idx", "organization_id"),
        {"schema": "marketplace"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    stock_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.stocks.id"), nullable=False)
    organization_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.organizations.id"), nullable=False)
    batch_number = Column(String, unique=True, nullable=False)
    origin_farm_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.farms.id"))
    quantity = Column(Numeric(14, 3), nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class Expense(Base):
    __tablename__ = "expenses"
    __table_args__ = (
        Index("expenses_farm_idx", "farm_id"),
        Index("expenses_category_idx", "category"),
        Index("expenses_date_idx", "date"),
        {"schema": "marketplace"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    farm_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.farms.id"), nullable=False)
    label = Column(String, nullable=False)
    amount = Column(Numeric(14, 2), nullable=False)
    category = Column(String, server_default="OTHER", nullable=False)
    date = Column(DateTime, server_default=func.now(), nullable=False)


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        Index("products_producer_idx", "producer_id"),
        Index("products_category_idx", "category_label"),
        Index("products_subcategory_idx", "sub_category_id"),
        Index("products_price_idx", "price"),
        Index("products_created_idx", "created_at"),
        Index("products_verifier_idx", "verified_by_id"),
        Index("products_producer_available_idx", "producer_id", "is_available"),
        Index("products_category_available_idx", "category_label", "is_available"),
        {"schema": "marketplace"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    short_code = Column(String, unique=True)
    name = Column(String, default="Produit", nullable=False)
    category_label = Column(String, nullable=False)
    sub_category_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.sub_categories.id"))
    local_names = Column(JSONB)
    description = Column(Text)
    price = Column(Numeric(12, 2), nullable=False)
    unit = Column(String, default="KG", nullable=False)
    quantity_for_sale = Column(Numeric(14, 3), default=0, nullable=False)
    images = Column(PG_ARRAY(String), nullable=False, server_default=text("'{}'::text[]"))
    audio_url = Column(String)
    quality_class = Column(String)
    min_order_quality = Column(String)
    packaging_type = Column(String)
    harvest_date = Column(DateTime)
    is_available = Column(Boolean, default=True, nullable=False)
    producer_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.producers.id"), nullable=False)
    verified_at = Column(DateTime)
    verified_by_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id"))
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    producer = relationship("Producer", backref="products")
    sub_category = relationship("SubCategory", primaryjoin="Product.sub_category_id == SubCategory.id")


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
    buyer_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.buyer_profiles.id"))
    client_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.clients.id"))
    organization_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.organizations.id"))
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
    market_offer_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.market_offers.id"))
    expected_fulfillment_date = Column(DateTime)
    preorder_converted_at = Column(DateTime)
    confirmed_at = Column(DateTime)
    auction_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.auctions.id"), unique=True)
    winning_bid_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.bids.id"))
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    items = relationship("OrderItem", back_populates="order", lazy="selectin")
    delivery = relationship("Delivery", back_populates="order", uselist=False)
    offer = relationship("MarketOffer", back_populates="orders")
    payments = relationship("Payment", back_populates="order", cascade="all, delete-orphan")
    status_history = relationship("OrderStatusHistory", back_populates="order", cascade="all, delete-orphan")
    reminders = relationship("OrderReminder", back_populates="order", cascade="all, delete-orphan")


class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = (
        Index("order_items_order_idx", "order_id"),
        Index("order_items_product_idx", "product_id"),
        {"schema": "marketplace"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    order_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.orders.id"), nullable=False)
    product_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.products.id"), nullable=False)
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
    order_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.orders.id"), nullable=False)
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
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

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
    order_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.orders.id"), nullable=False)
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
    order_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.orders.id"), nullable=False)
    type = Column(String, nullable=False)
    channel = Column(String, default="WHATSAPP", nullable=False)
    status = Column(String, default="SCHEDULED", nullable=False)
    scheduled_at = Column(DateTime, nullable=False)
    sent_at = Column(DateTime)
    attempts = Column(Integer, default=0, nullable=False)
    last_error = Column(Text)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

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
    order_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.orders.id"), nullable=False)
    escrow_wallet_id = Column(PG_UUID(as_uuid=True), nullable=True)
    raised_by_id = Column(PG_UUID(as_uuid=True), nullable=False)
    reason_category = Column(String, nullable=False)
    description = Column(Text, nullable=False)
    evidence_images = Column(PG_ARRAY(String), nullable=False, server_default=text("'{}'::text[]"))
    requested_solution = Column(String, nullable=False)
    disputed_amount = Column(Numeric(14, 2), default=0, nullable=False)
    escrow_payout_status = Column(String, default="HELD", nullable=False)
    status = Column(String, default="PENDING", nullable=False)
    resolution_notes = Column(Text)
    resolved_at = Column(DateTime)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


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
    buyer_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.buyer_profiles.id"), nullable=False)
    sub_category_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.sub_categories.id"), nullable=False)
    winner_bid_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.bids.id"))
    quantity = Column(Numeric(14, 3), nullable=False)
    unit = Column(String, default="TONNE", nullable=False)
    max_price_per_unit = Column(Numeric(12, 2), nullable=False)
    description = Column(Text)
    incoterm = Column(String, default="DDP", nullable=False)
    delivery_location = Column(String, nullable=False)
    delivery_deadline = Column(DateTime, nullable=False)
    quality_grading = Column(String)
    required_certifications = Column(PG_ARRAY(String), nullable=False, server_default=text("'{}'::text[]"))
    preferred_packaging = Column(String)
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
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


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
    auction_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.auctions.id"), nullable=False)
    producer_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.producers.id"), nullable=False)
    offered_price = Column(Numeric(12, 2), nullable=False)
    linked_stock_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.stocks.id"))
    is_winner = Column(Boolean, default=False, nullable=False)
    status = Column(String, default="PENDING", nullable=False)
    message = Column(Text)
    notified_at = Column(DateTime)
    valid_until = Column(DateTime)
    estimated_delivery_date = Column(DateTime)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


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
    order_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.orders.id"), nullable=False)
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


# ══════════════════════════════════════════════════════════════════════════
# INTELLIGENCE (persistance opérationnelle de l'agent + réputation)
# ══════════════════════════════════════════════════════════════════════════
class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("audit_logs_actor_idx", "actor_id"),
        Index("audit_logs_entity_idx", "entity_id"),
        Index("audit_logs_entity_time_idx", "entity_type", "created_at"),
        {"schema": "intelligence"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    actor_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id"), nullable=False)
    action = Column(Text, nullable=False)
    entity_id = Column(Text, nullable=False)
    entity_type = Column(Text, nullable=False)
    old_value = Column(JSONB)
    new_value = Column(JSONB)
    ip_address = Column(Text)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class AgentAction(Base):
    __tablename__ = "agent_actions"
    __table_args__ = (
        Index("agent_actions_status_idx", "status"),
        Index("agent_actions_batch_idx", "batch_id"),
        Index("agent_actions_name_idx", "agent_name"),
        Index("agent_actions_order_unique", "order_id", unique=True),
        Index("agent_actions_queue_idx", "status", "priority"),
        {"schema": "intelligence"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    agent_name = Column(Text, nullable=False)
    action_type = Column(Text, nullable=False)
    batch_id = Column(Text)
    payload = Column(JSONB)
    status = Column(String, default="PENDING", nullable=False)
    priority = Column(String, default="MEDIUM", nullable=False)
    order_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.orders.id"), unique=True)
    user_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id"))
    audit_trail_id = Column(Text)
    ai_reasoning = Column(Text)
    admin_notes = Column(Text)
    validated_by_id = Column(Text)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        Index("conversations_user_idx", "user_id"),
        Index("conversations_agent_idx", "agent_type"),
        Index("conversations_created_idx", "created_at"),
        Index("conversations_followup_idx", "needs_follow_up"),
        Index("conversations_audit_unique", "audit_trail_id", unique=True),
        {"schema": "intelligence"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    user_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id"), nullable=False)
    query = Column(Text, nullable=False)
    response = Column(Text)
    agent_type = Column(Text)
    zone_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id"))
    mode = Column(Text, default="text", nullable=False)
    audio_url = Column(Text)
    is_waiting_for_input = Column(Boolean, default=False, nullable=False)
    missing_slots = Column(JSONB)
    execution_path = Column(JSONB)
    confidence_score = Column(DOUBLE_PRECISION)
    user_intent = Column(Text)
    needs_follow_up = Column(Boolean, default=False, nullable=False)
    total_tokens_used = Column(Integer, default=0, nullable=False)
    response_time_ms = Column(Integer)
    audit_trail_id = Column(Text, unique=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class AgentContextMemory(Base):
    __tablename__ = "agent_context_memory"
    __table_args__ = (
        Index("acm_user_idx", "user_id"),
        Index("acm_key_idx", "context_key"),
        Index("acm_user_key_unique", "user_id", "context_key", unique=True),
        {"schema": "intelligence"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    user_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id"), nullable=False)
    market_offer_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.market_offers.id"))
    context_key = Column(Text, nullable=False)
    context_value = Column(JSONB, nullable=False)
    source = Column(Text, default="AGENT", nullable=False)
    confidence = Column(DOUBLE_PRECISION)
    expires_at = Column(DateTime)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class TrustScore(Base):
    __tablename__ = "trust_scores"
    __table_args__ = {"schema": "intelligence"}

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    user_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id"), nullable=False, unique=True)
    global_score = Column(DOUBLE_PRECISION, default=0.0, nullable=False)
    reliability_index = Column(DOUBLE_PRECISION, default=0.0, nullable=False)
    quality_index = Column(DOUBLE_PRECISION, default=0.0, nullable=False)
    compliance_index = Column(DOUBLE_PRECISION, default=0.0, nullable=False)
    resilience_bonus = Column(DOUBLE_PRECISION, default=0.0, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class AIRatingReasoning(Base):
    __tablename__ = "ai_rating_reasonings"
    __table_args__ = (
        Index("ai_rating_trust_idx", "trust_score_id"),
        Index("ai_rating_agent_idx", "agent_name"),
        {"schema": "intelligence"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    trust_score_id = Column(PG_UUID(as_uuid=True), ForeignKey("intelligence.trust_scores.id"), nullable=False)
    agent_name = Column(Text, nullable=False)
    justification = Column(Text, nullable=False)
    data_points = Column(JSONB, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class ModerationEvent(Base):
    """Journal auditable des strikes de modération (produits interdits, scams).

    Le nombre de strikes d'un utilisateur = COUNT sur cette table.
    """

    __tablename__ = "moderation_events"
    __table_args__ = (
        Index("moderation_events_phone_idx", "phone"),
        Index("moderation_events_user_idx", "user_id"),
        Index("moderation_events_kind_idx", "kind"),
        {"schema": "intelligence"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    user_id = Column(PG_UUID(as_uuid=True))
    phone = Column(Text, nullable=False)
    kind = Column(String, nullable=False)  # PROHIBITED_PRODUCT | SCAM
    matched_term = Column(Text)
    excerpt = Column(Text)
    action_taken = Column(String)  # WARNED | BANNED
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class DemandSignal(Base):
    """Demande non satisfaite : produits recherchés en vain, agrégés par terme."""

    __tablename__ = "demand_signals"
    __table_args__ = (
        Index("demand_signals_term_unique", "normalized_term", unique=True),
        Index("demand_signals_occurrences_idx", "occurrences"),
        {"schema": "intelligence"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    normalized_term = Column(Text, nullable=False)
    raw_query = Column(Text, nullable=False)
    phone = Column(Text)
    user_id = Column(PG_UUID(as_uuid=True))
    zone_id = Column(PG_UUID(as_uuid=True))
    occurrences = Column(Integer, default=1, nullable=False, server_default=text("1"))
    resolved = Column(Boolean, default=False, nullable=False, server_default=text("false"))
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class Solicitation(Base):
    """Sollicitation proactive (enchère→producteur, nouveau produit→acheteur).

    Porte l'état métier + l'idempotence. Taux de conversion = RESPONDED/NOTIFIED.
    """

    __tablename__ = "solicitations"
    __table_args__ = (
        Index("solicitations_auction_producer_uq", "auction_id", "target_producer_id", unique=True),
        Index("solicitations_offer_buyer_uq", "market_offer_id", "target_buyer_id", unique=True),
        Index("solicitations_kind_status_idx", "kind", "status"),
        Index("solicitations_auction_idx", "auction_id"),
        {"schema": "intelligence"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    kind = Column(String, nullable=False)  # AUCTION_INVITE | NEW_PRODUCT_ALERT
    auction_id = Column(PG_UUID(as_uuid=True))
    market_offer_id = Column(PG_UUID(as_uuid=True))
    target_producer_id = Column(PG_UUID(as_uuid=True))
    target_buyer_id = Column(PG_UUID(as_uuid=True))
    sub_category_id = Column(PG_UUID(as_uuid=True))
    zone_id = Column(PG_UUID(as_uuid=True))
    status = Column(String, default="PENDING", nullable=False, server_default=text("'PENDING'"))
    notified_at = Column(DateTime)
    responded_at = Column(DateTime)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class NotificationOutbox(Base):
    """File d'attente durable de messages (Outbox Pattern). Écrite par les crons
    métier, lue/envoyée par le dispatcher — découplage total de la notification.
    """

    __tablename__ = "notification_outbox"
    __table_args__ = (
        Index("outbox_dedupe_uq", "dedupe_key", unique=True),
        Index("outbox_due_idx", "status", "next_attempt_at"),
        Index("outbox_solicitation_idx", "solicitation_id"),
        {"schema": "intelligence"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    solicitation_id = Column(PG_UUID(as_uuid=True))
    channel = Column(String, nullable=False)  # WHATSAPP | EMAIL | PUSH | IN_APP
    recipient_user_id = Column(PG_UUID(as_uuid=True))
    recipient_phone = Column(Text)
    template_key = Column(String, nullable=False)
    payload = Column(JSONB, nullable=False)
    dedupe_key = Column(Text, nullable=False)
    status = Column(String, default="PENDING", nullable=False, server_default=text("'PENDING'"))
    attempts = Column(Integer, default=0, nullable=False, server_default=text("0"))
    max_attempts = Column(Integer, default=5, nullable=False, server_default=text("5"))
    next_attempt_at = Column(DateTime, server_default=func.now(), nullable=False)
    last_error = Column(Text)
    sent_at = Column(DateTime)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


__all__ = [
    "Base",
    "_uuid4",
    # auth
    "User", "Account", "Session",
    # governance
    "Organization", "UserOrganization", "RoleDefinition", "ClimaticRegion",
    "Zone", "WorkZone", "ZoneMetric", "Category", "SubCategory",
    "StandardPrice", "ZoneSetting", "OverlayLayer", "ProhibitedTerm",
    # marketplace
    "Warehouse", "Producer", "Client", "BuyerType", "BuyerProfile",
    "DeliveryAgent", "Delivery", "Farm", "MarketOffer", "CropCycle",
    "Stock", "StockMovement", "Batch", "Expense", "Product",
    "Order", "OrderItem", "Payment", "OrderStatusHistory", "OrderReminder",
    "OrderDispute", "Auction", "Bid", "MarketplaceRating",
    # intelligence
    "AuditLog", "AgentAction", "Conversation", "AgentContextMemory",
    "TrustScore", "AIRatingReasoning", "ModerationEvent", "DemandSignal",
    "Solicitation", "NotificationOutbox",
]
