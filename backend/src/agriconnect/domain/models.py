"""Canonical SQLAlchemy ORM models for AgriConnect.

This file is the single source-of-truth for ORM models used by the async
database services. It intentionally includes a small `to_dict()` helper
because the service mixins return dictionaries.
"""

from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    Time,
    func,
    text,Index, UniqueConstraint,JSON
)


from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID, ARRAY as PG_ARRAY,DOUBLE_PRECISION
from sqlalchemy.inspection import inspect
from sqlalchemy.orm import declarative_base, relationship
from datetime import datetime

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

##**************************************
## Auth -User
###*************************************


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
    daily_advice_time = Column(Time, server_default="07:00", nullable=False) # Utiliser func ou un objet time
    latitude = Column(Float)
    longitude = Column(Float)
    cnib_number = Column(String, unique=True)
    role = Column(String, default="USER", nullable=False)
    identity_verified = Column(Boolean, default=False)
    
    zone_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id"))
    
    deleted_at = Column(DateTime)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    # Relations
    recommendations = relationship(
        "AIRecommendation",
        back_populates="user",
        lazy="selectin",
    )

    producer = relationship(
        "Producer",
        back_populates="user",
        uselist=False  # Indique à SQLAlchemy qu'un User n'a qu'UN SEUL profil Producer
    )
   


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = {"schema": "auth"}

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    # Correction : Toujours préciser le schéma dans la ForeignKey
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


from sqlalchemy import Column, String, Float, DateTime, ForeignKey, Index, Boolean, Text, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.sql import func

class UserCulture(Base):
    __tablename__ = "user_cultures"
    __table_args__ = (
        Index('user_cultures_user_idx', 'user_id'),
        {"schema": "auth"}
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id", ondelete="CASCADE"), nullable=False)
    culture_name = Column(String, nullable=False)
    planting_date = Column(DateTime, nullable=False)
    is_association = Column(Boolean, default=False)
    status = Column(String, default="active")

class DailyAdviceLog(Base):
    __tablename__ = "daily_advice_logs"
    __table_args__ = {"schema": "auth"}

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id", ondelete="CASCADE"), nullable=False)
    culture_name = Column(String, nullable=False)
    advice_content = Column(Text, nullable=False)
    sent_at = Column(DateTime, server_default=func.now(), nullable=False)
    is_useful = Column(Boolean)


##**************************************
## Governance
###*************************************

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
        UniqueConstraint('user_id', 'organization_id', name='user_org_unique'),
        {"schema": "governance"}
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
        Index('zones_region_idx', 'climatic_region_id'),
        Index('zones_org_idx', 'organization_id'),
        Index('zones_active_idx', 'is_active'),
        Index('zones_parent_idx', 'parent_id'),
        Index('zones_path_idx', 'path'),
        {"schema": "governance"}
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
        UniqueConstraint('organization_id', 'zone_id', name='work_zones_org_zone_unique'),
        Index('work_zones_org_idx', 'organization_id'),
        Index('work_zones_zone_idx', 'zone_id'),
        {"schema": "governance"}
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
        Index('zone_metrics_composite_idx', 'zone_id', 'date', 'metric_name'),
        {"schema": "governance"}
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
        UniqueConstraint('category_id', 'name', name='sub_categories_cat_name_unique'),
        {"schema": "governance"}
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    category_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.categories.id"), nullable=False)
    name = Column(String, nullable=False)
    blocked_zone_ids = Column(PG_ARRAY(String), nullable=False, server_default=text("'{}'::text[]"))
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    crop_cycles = relationship("CropCycle", back_populates="sub_category")

class StandardPrice(Base):
    __tablename__ = "standard_prices"
    __table_args__ = (
        UniqueConstraint('sub_category_id', 'zone_id', name='standard_prices_sub_zone_unique'),
        Index('standard_prices_zone_idx', 'zone_id'),
        {"schema": "governance"}
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
        UniqueConstraint('zone_id', 'key', name='zone_settings_zone_key_unique'),
        Index('zone_settings_zone_idx', 'zone_id'),
        {"schema": "governance"}
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    zone_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id"), nullable=False)
    key = Column(String, nullable=False)
    value = Column(JSONB, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

class OverlayLayer(Base):
    __tablename__ = "overlay_layers"
    __table_args__ = (
        UniqueConstraint('zone_id', 'key', name='overlay_layers_zone_key_unique'),
        Index('overlay_layers_zone_idx', 'zone_id'),
        {"schema": "governance"}
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    zone_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id"), nullable=False)
    key = Column(String, nullable=False)
    label = Column(String, nullable=False)
    enabled = Column(Boolean, default=False, nullable=False)
    settings = Column(JSONB)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

##**************************************
## Marketplace
###*************************************
from sqlalchemy import Column, String, Float, Integer, Boolean, DateTime, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID, JSONB
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship

# ── 1. WAREHOUSE ──────────────────────────────────────────────────────────
class Warehouse(Base):
    __tablename__ = "warehouses"
    __table_args__ = (Index('warehouses_zone_idx', 'zone_id'), {"schema": "marketplace"})

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    name = Column(String, nullable=False)
    type = Column(String, nullable=False)
    capacity = Column(Float)
    location = Column(String)
    zone_id = Column(PG_UUID(as_uuid=True), ForeignKey('governance.zones.id'))
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

# ── 2. PRODUCER ───────────────────────────────────────────────────────────
class Producer(Base):
    __tablename__ = "producers"
    __table_args__ = (
        Index('producers_status_idx', 'status'),
        Index('producers_org_idx', 'organization_id'),
        Index('producers_zone_idx', 'zone_id'),
        {"schema": "marketplace"}
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

    
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User", back_populates="producer", lazy="joined", foreign_keys=[user_id])
    farms = relationship("Farm", back_populates="producer", cascade="all, delete-orphan")
    clients = relationship("Client", back_populates="producer", cascade="all, delete-orphan")

# ── 3. CLIENT ─────────────────────────────────────────────────────────────
class Client(Base):
    __tablename__ = "clients"
    __table_args__ = (
        Index('clients_phone_idx', 'phone'),
        Index('clients_name_idx', 'name'),
        Index('clients_producer_idx', 'producer_id'),
        {"schema": "marketplace"}
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

# ── 4. BUYER PROFILES ─────────────────────────────────────────────────────

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
        Index('buyer_profiles_user_idx', 'user_id'),
        Index('buyer_profiles_type_idx', 'buyer_type_id'),
        Index('buyer_profiles_verified_idx', 'is_verified'),
        {"schema": "marketplace"}
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

# ── 5. DELIVERY AGENT ─────────────────────────────────────────────────────
class DeliveryAgent(Base):
    __tablename__ = "delivery_agents"
    __table_args__ = {"schema": "marketplace"}

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id"), nullable=False, unique=True)
    vehicle_type = Column(String)
    license_number = Column(String)
    zone_id = Column(PG_UUID(as_uuid=True))
    status = Column(String, default="OFFLINE", nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    zone_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id"))

    user = relationship("User", foreign_keys=[user_id])

# ── 6. DELIVERY ───────────────────────────────────────────────────────────
class Delivery(Base):
    __tablename__ = "deliveries"
    __table_args__ = (
        Index('deliveries_order_unique', 'order_id', unique=True),
        Index('deliveries_agent_idx', 'delivery_agent_id'),
        Index('deliveries_status_idx', 'status'),
        {"schema": "marketplace"}
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
    shipping_condition = Column(String)
    actual_distance_km = Column(Float)
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
    __table_args__ = {"schema": "marketplace"}

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    name = Column(String, nullable=False)
    location = Column(String)
    size = Column(Float)
    soil_type = Column(String)
    water_source = Column(String)
    producer_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.producers.id"), nullable=False)
    zone_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id"))

    cycles = relationship("CropCycle", back_populates="farm", cascade="all, delete-orphan")
    # Relations manquantes pour fermer la boucle avec les enfants
    soil_profiles = relationship("SoilProfile", back_populates="farm", cascade="all, delete-orphan")
    telemetry_history = relationship("SensorTelemetryHistory", back_populates="farm", cascade="all, delete-orphan")
    sensor_summary = relationship("SensorDataSummary", back_populates="farm", uselist=False, cascade="all, delete-orphan")
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    stocks = relationship("Stock", back_populates="farm", cascade="all, delete-orphan")
    producer =  relationship("Producer", back_populates="farms")


#### crop
##################################


class CropCycle(Base):
    __tablename__ = 'crop_cycles'
    __table_args__ = (
        Index('crop_cycles_farm_idx', 'farm_id'),
        Index('crop_cycles_status_idx', 'status'),
        {"schema": "marketplace"}
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    
    # Clé étrangère vers la ferme
    farm_id = Column(PG_UUID(as_uuid=True), ForeignKey('marketplace.farms.id'), nullable=False, index=True)
    sub_category_id = Column(PG_UUID(as_uuid=True), ForeignKey('governance.sub_categories.id'))
    
    crop_type = Column(String, nullable=False)
    area_size = Column(Float, nullable=False)
    planted_at = Column(DateTime, nullable=False)
    expected_harvest_date = Column(DateTime, nullable=False)
    target_yield = Column(Float)
    expected_yield = Column(Float)
    status = Column(String, nullable=False)
    variety = Column(String)
    farming_method = Column(String, server_default='conventional')
    soil_type = Column(String)
    last_intervention_date = Column(DateTime)
    actual_harvested_yield = Column(Float)
    actual_harvest_date = Column(DateTime)
    destruction_reason = Column(Text)
    
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relations ORM
    # Assurez-vous que Farm a bien 'cycles = relationship("CropCycle", back_populates="farm")'
    farm = relationship("Farm", back_populates="cycles")
    sub_category = relationship("SubCategory", back_populates="crop_cycles")
    preorders = relationship("Order", back_populates="crop_cycle")
    growth_logs = relationship("CropGrowthLog", back_populates="crop_cycle", cascade="all, delete-orphan")
    # Assurez-vous que FieldIntervention a bien 'crop_cycle = relationship("CropCycle", back_populates="interventions")'
    interventions = relationship("FieldIntervention", back_populates="crop_cycle", cascade="all, delete-orphan")
    
# ── 2. FIELD INTERVENTIONS ────────────────────────────────────────────────
class FieldIntervention(Base):
    __tablename__ = 'field_interventions'
    __table_args__ = {"schema": "marketplace"}
    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    crop_cycle_id = Column(PG_UUID(as_uuid=True), ForeignKey('marketplace.crop_cycles.id'), nullable=False, index=True)
    type = Column(String, nullable=False, index=True)
    description = Column(String)
    input_used = Column(String)
    quantity = Column(Float)
    unit = Column(String)
    cost_per_unit = Column(Float, default=0.0)
    machinery_used = Column(String)
    fuel_consumption = Column(Float)
    hours_worked = Column(Float)
    observed_bbch_stage = Column(Integer)
    performed_at = Column(DateTime, nullable=False, index=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    crop_cycle = relationship("CropCycle", back_populates="interventions")

# ── 3. SENSOR DATA SUMMARY ────────────────────────────────────────────────
class SensorDataSummary(Base):
    __tablename__ = 'sensor_data_summary'
    __table_args__ = {"schema": "marketplace"}
    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    farm_id = Column(PG_UUID(as_uuid=True), ForeignKey('marketplace.farms.id'), nullable=False, unique=True)
    soil_moisture = Column(Float)
    last_rainfall = Column(Float)
    accumulated_gdd = Column(Integer)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    farm = relationship("Farm", back_populates="sensor_summary")

# ── 4. AGRONOMIC STANDARDS ────────────────────────────────────────────────
class AgronomicStandard(Base):
    __tablename__ = 'agronomic_standards'
    __table_args__ = (
        UniqueConstraint('crop_type', 'variety_type', name='as_crop_variety_unique'),
        {"schema": "marketplace"}
    )
    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    crop_type = Column(String, nullable=False, index=True)
    variety_type = Column(String)
    nitrogen_needs = Column(Float)
    phosphorus_needs = Column(Float)
    potassium_needs = Column(Float)
    base_temperature = Column(Float)
    gdd_to_harvest = Column(Integer)
    min_humidity_threshold = Column(Float)
    max_wind_speed_treatment = Column(Float, server_default="19.0")
    version = Column(String)

# ── 5. PEST & DISEASE CATALOG ─────────────────────────────────────────────
class PestDiseaseCatalog(Base):
    __tablename__ = 'pest_disease_catalog'
    __table_args__ = {"schema": "marketplace"}
    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    name = Column(String, nullable=False, index=True)
    type = Column(String, nullable=False, index=True)
    target_crops = Column(PG_ARRAY(String), nullable=False)
    symptoms_description = Column(Text)
    critical_stage_bbch = Column(Integer)
    weather_triggers = Column(JSONB)
    treatment_threshold = Column(String)
    recommended_molecules = Column(PG_ARRAY(String))
    bio_solutions = Column(PG_ARRAY(String))
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

# ── 6. SOIL PROFILES ──────────────────────────────────────────────────────
class SoilProfile(Base):
    __tablename__ = 'soil_profiles'
    __table_args__ = {"schema": "marketplace"}
    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    farm_id = Column(PG_UUID(as_uuid=True), ForeignKey('marketplace.farms.id'), nullable=False, index=True)
    clay_percentage = Column(Float)
    sand_percentage = Column(Float)
    silt_percentage = Column(Float)
    organic_matter = Column(Float)
    ph_value = Column(Float)
    water_retention_capacity = Column(Float)
    sampling_date = Column(DateTime, nullable=False, index=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    farm = relationship("Farm", back_populates="soil_profiles")

# ── 7. SENSOR TELEMETRY HISTORY ──────────────────────────────────────────
class SensorTelemetryHistory(Base):
    __tablename__ = 'sensor_telemetry_history'
    __table_args__ = (
        Index('telemetry_farm_time_idx', 'farm_id', 'timestamp'),
        {"schema": "marketplace"}
    )
    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    farm_id = Column(PG_UUID(as_uuid=True), ForeignKey('marketplace.farms.id'), nullable=False)
    soil_moisture = Column(Float)
    temperature = Column(Float)
    humidity = Column(Float)
    solar_radiation = Column(Float)
    timestamp = Column(DateTime, server_default=func.now(), nullable=False)

    farm = relationship("Farm", back_populates="telemetry_history")



class CropGrowthLog(Base):
    __tablename__ = 'crop_growth_logs'
    __table_args__ = {"schema": "marketplace"}

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    crop_cycle_id = Column(PG_UUID(as_uuid=True), ForeignKey('marketplace.crop_cycles.id'), nullable=False, index=True)
    stage_code = Column(Integer, nullable=False)
    observed_at = Column(DateTime, server_default=func.now())
    image_snapshot_url = Column(String)
    accumulated_gdd_at_stage = Column(Integer)

    crop_cycle = relationship("CropCycle", back_populates="growth_logs")


class CropGrowthStage(Base):
    __tablename__ = 'crop_growth_stages'
    __table_args__ = (
        UniqueConstraint('crop_type', 'stage_code', name='cgs_crop_stage_unique'),
        Index('cgs_crop_type_idx', 'crop_type'),
        {"schema": "marketplace"}
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    crop_type = Column(String, nullable=False)
    stage_code = Column(Integer, nullable=False)
    stage_name = Column(String, nullable=False)
    gdd_threshold = Column(Integer, nullable=False)
    nitrogen_need_kgha = Column(Float, server_default='0')
    water_need_mmday = Column(Float, server_default='0')
    agronomic_advice = Column(Text)
    version = Column(String, server_default='v1')




class Stock(Base):
    __tablename__ = "stocks"
    __table_args__ = {"schema": "marketplace"}

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    
    # Relation vers la ferme (déjà présente, mais consolidée ici)
    farm_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.farms.id"), index=True)
    farm = relationship("Farm", back_populates="stocks")

    # Relation vers l'entrepôt
    warehouse_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.warehouses.id"), index=True)
    
    # Relation vers l'utilisateur ayant vérifié le stock
    verified_by_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id"), index=True)
    
    organization_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.organizations.id"))
    item_name = Column(String, nullable=False)
    quantity = Column(Float, default=0.0, nullable=False)
    unit = Column(String, default="KG", nullable=False)
    type = Column(String, default="HARVEST", nullable=False)
    verified_at = Column(DateTime)
    
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class StockMovement(Base):
    __tablename__ = "stock_movements"
    __table_args__ = (
        Index('stock_movements_stock_idx', 'stock_id'),
        Index('stock_movements_created_idx', 'created_at'),
        {"schema": "marketplace"}
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    stock_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.stocks.id"), nullable=False)
    # Remplacez String par votre objet Enum si vous en avez un défini (ex: Enum('IN', 'OUT', name='movement_type'))
    type = Column(String, nullable=False) 
    quantity = Column(DOUBLE_PRECISION, nullable=False)
    reason = Column(String)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

class Batch(Base):
    __tablename__ = "batches"
    __table_args__ = (
        Index('batches_stock_idx', 'stock_id'),
        Index('batches_org_idx', 'organization_id'),
        {"schema": "marketplace"}
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    stock_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.stocks.id"), nullable=False)
    organization_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.organizations.id"), nullable=False)
    batch_number = Column(String, unique=True, nullable=False)
    origin_farm_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.farms.id"))
    quantity = Column(DOUBLE_PRECISION, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

class Expense(Base):
    __tablename__ = "expenses"
    __table_args__ = (
        Index('expenses_farm_idx', 'farm_id'),
        Index('expenses_category_idx', 'category'),
        Index('expenses_date_idx', 'date'),
        {"schema": "marketplace"}
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    farm_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.farms.id"), nullable=False)
    label = Column(String, nullable=False)
    amount = Column(DOUBLE_PRECISION, nullable=False)
    # Si votre Enum est défini ailleurs, remplacez String par votre objet Enum
    category = Column(String, server_default='OTHER', nullable=False)
    date = Column(DateTime, server_default=func.now(), nullable=False)

# ── 2. PRODUCTS ───────────────────────────────────────────────────────────
class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        Index('products_producer_idx', 'producer_id'),
        Index('products_category_idx', 'category_label'),
        Index('products_subcategory_idx', 'sub_category_id'),
        Index('products_price_idx', 'price'),
        Index('products_created_idx', 'created_at'),
        Index('products_verifier_idx', 'verified_by_id'),
        {"schema": "marketplace"}
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    short_code = Column(String, unique=True)
    name = Column(String, default="Produit", nullable=False)
    category_label = Column(String, nullable=False)
    sub_category_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.sub_categories.id"))
    local_names = Column(JSONB)
    description = Column(Text)
    price = Column(Float, nullable=False)
    unit = Column(String, default="KG", nullable=False)
    quantity_for_sale = Column(Float, default=0.0, nullable=False)
    images = Column(PG_ARRAY(String), nullable=False, server_default=text("'{}'::text[]"))
    audio_url = Column(String)
    
    # Nouveaux champs de test synchronisés avec Drizzle Web
    quality_class = Column(String)
    min_order_quality = Column(String)
    packaging_type = Column(String)
    harvest_date = Column(DateTime)
    is_available = Column(Boolean, default=True, nullable=False)

    producer_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.producers.id"), nullable=False)
    verified_at = Column(DateTime)
    verified_by_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id"))
    
    # Relations Python
    producer = relationship("Producer", backref="products")
    sub_category = relationship("SubCategory", primaryjoin="Product.sub_category_id == SubCategory.id")
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


# ── 3. ORDERS ─────────────────────────────────────────────────────────────
class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        Index('orders_buyer_idx', 'buyer_id'),
        Index('orders_org_idx', 'organization_id'),
        Index('orders_status_idx', 'status'),
        Index('orders_delivery_status_idx', 'delivery_status'),
        Index('orders_zone_idx', 'zone_id'),
        Index('orders_created_idx', 'created_at'),
        Index('orders_phone_idx', 'customer_phone'),
        Index('orders_type_idx', 'order_type'),
        Index('orders_crop_cycle_idx', 'crop_cycle_id'),
        Index('orders_auction_unique', 'auction_id', unique=True),
        Index('orders_winning_bid_idx', 'winning_bid_id'),
        {"schema": "marketplace"}
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
    total_amount = Column(Float, nullable=False)
    is_agent_order = Column(Boolean, default=False, nullable=False)
    
    # Nouveaux champs financiers et enchères synchronisés avec Drizzle Web
    delivery_date = Column(DateTime)
    subtotal = Column(Float, default=0.0, nullable=False)
    tax_amount = Column(Float, default=0.0, nullable=False)
    currency = Column(String, default="XOF", nullable=False)
    delivery_fee = Column(Float, default=0.0, nullable=False)
    cancellation_role = Column(String)
    escrow_wallet_id = Column(PG_UUID(as_uuid=True), nullable=True)
    crop_cycle_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.crop_cycles.id"))
    expected_fulfillment_date = Column(DateTime)
    preorder_converted_at = Column(DateTime)
    
    auction_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.auctions.id"), unique=True)  
    winning_bid_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.bids.id"))
    
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    
    # Relations Python
    items = relationship("OrderItem", back_populates="order", lazy="selectin")
    delivery = relationship("Delivery", back_populates="order", uselist=False)
    crop_cycle = relationship("CropCycle", back_populates="preorders")


# ── 4. ORDER ITEMS ────────────────────────────────────────────────────────
class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = (
        Index('order_items_order_idx', 'order_id'),
        Index('order_items_product_idx', 'product_id'),
        {"schema": "marketplace"}
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    order_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.orders.id"), nullable=False)
    product_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.products.id"), nullable=False)
    
    quantity = Column(Float, nullable=False)
    price_at_sale = Column(Float, nullable=False)
    
    # Relations Python
    order = relationship("Order", back_populates="items")
    product = relationship("Product", lazy="selectin")


# ── 5. ORDER DISPUTES ─────────────────────────────────────────────────────
class OrderDispute(Base):
    __tablename__ = "order_disputes"
    __table_args__ = (
        Index('order_disputes_order_idx', 'order_id'),
        Index('order_disputes_status_idx', 'status'),
        Index('order_disputes_raised_by_idx', 'raised_by_id'),
        Index('order_disputes_escrow_idx', 'escrow_wallet_id'),
        {"schema": "marketplace"}
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    order_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.orders.id"), nullable=False)
    escrow_wallet_id = Column(PG_UUID(as_uuid=True), nullable=True)
    raised_by_id = Column(PG_UUID(as_uuid=True), nullable=False) # Lié à l'ID auteur du litige
    reason_category = Column(String, nullable=False)
    description = Column(Text, nullable=False)
    evidence_images = Column(PG_ARRAY(String), nullable=False, server_default=text("'{}'::text[]"))
    requested_solution = Column(String, nullable=False)
    disputed_amount = Column(Float, default=0.0, nullable=False)
    escrow_payout_status = Column(String, default="HELD", nullable=False)
    status = Column(String, default="PENDING", nullable=False)
    resolution_notes = Column(Text)
    
    resolved_at = Column(DateTime)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


# ── 6. AUCTIONS ───────────────────────────────────────────────────────────
class Auction(Base):
    __tablename__ = "auctions"
    __table_args__ = (
        Index('auctions_status_idx', 'status'),
        Index('auctions_buyer_idx', 'buyer_id'),
        Index('auctions_escrow_status_idx', 'escrow_status'),
        Index('auctions_zone_idx', 'target_zone_id'),
        Index('auctions_deadline_idx', 'deadline'),
        Index('auctions_delivery_deadline_idx', 'delivery_deadline'),
        {"schema": "marketplace"}
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    buyer_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.buyer_profiles.id"), nullable=False)
    sub_category_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.sub_categories.id"), nullable=False)
    winner_bid_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.bids.id")) # Géré circulairement
    
    quantity = Column(Float, nullable=False)
    unit = Column(String, default="TONNE", nullable=False)
    max_price_per_unit = Column(Float, nullable=False)
    description = Column(Text)
    
    # Métriques Logistique, Qualité & Sécurité (Ajouts synchronisés)
    incoterm = Column(String, default="DDP", nullable=False)
    delivery_location = Column(String, nullable=False)
    delivery_deadline = Column(DateTime, nullable=False)
    quality_grading = Column(String)
    required_certifications = Column(PG_ARRAY(String), nullable=False, server_default=text("'{}'::text[]"))
    preferred_packaging = Column(String)
    
    deadline = Column(DateTime, nullable=False)
    auto_extend = Column(Boolean, default=True, nullable=False)
    escrow_wallet_id = Column(PG_UUID(as_uuid=True),nullable=True)
    escrow_status = Column(String, default="NONE", nullable=False)
    status = Column(String, default="OPEN", nullable=False)
    
    cancellation_reason = Column(String)
    target_zone_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id"))
    version = Column(Integer, default=0, nullable=False)
    
    awarded_at = Column(DateTime)
    cancelled_at = Column(DateTime)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


# ── 7. BIDS ───────────────────────────────────────────────────────────────
class Bid(Base):
    __tablename__ = "bids"
    __table_args__ = (
        Index('bids_auction_producer_unique', 'auction_id', 'producer_id', unique=True),
        Index('bids_auction_idx', 'auction_id'),
        Index('bids_producer_idx', 'producer_id'),
        Index('bids_linked_stock_idx', 'linked_stock_id'),
        Index('bids_status_idx', 'status'),
        {"schema": "marketplace"}
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    auction_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.auctions.id"), nullable=False)
    producer_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.producers.id"), nullable=False)
    offered_price = Column(Float, nullable=False)
    linked_stock_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.stocks.id")) # Pourra se lier à marketplace.stocks.id
    is_winner = Column(Boolean, default=False, nullable=False)
    status = Column(String, default="PENDING", nullable=False)
    message = Column(Text)
    notified_at = Column(DateTime)
    
    # Nouveaux champs de test synchronisés avec Drizzle Web
    valid_until = Column(DateTime)
    estimated_delivery_date = Column(DateTime)
    
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


# ── 8. MARKETPLACE RATINGS ────────────────────────────────────────────────
class MarketplaceRating(Base):
    __tablename__ = "marketplace_ratings"
    __table_args__ = (
        Index('mr_order_idx', 'order_id'),
        Index('mr_author_idx', 'author_id'),
        Index('mr_target_idx', 'target_id'),
        Index('mr_order_author_unique', 'order_id', 'author_id', unique=True),
        {"schema": "marketplace"}
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    order_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.orders.id"), nullable=False)
    
    author_type = Column(String, nullable=False) # 'BUYER' ou 'PRODUCER'
    author_id = Column(PG_UUID(as_uuid=True), nullable=False)
    target_type = Column(String, nullable=False) # 'BUYER' ou 'PRODUCER'
    target_id = Column(PG_UUID(as_uuid=True), nullable=False)

    # Critères spécifiques producteurs
    rating_product_quality = Column(Integer)
    rating_packaging = Column(Integer)
    
    # Critères spécifiques acheteurs
    rating_reception_speed = Column(Integer)
    rating_communication = Column(Integer)

    # Critères communs
    rating_reliability = Column(Integer, nullable=False)
    global_rating = Column(Float, nullable=False)
    comment = Column(Text)
    
    created_at = Column(DateTime, server_default=func.now(), nullable=False)



##**************************************
## Intelligence
###*************************************


# ── 1. Audit Logs ────────────────────────────────────────────────────────
class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index('audit_logs_actor_idx', 'actor_id'),
        Index('audit_logs_entity_idx', 'entity_id'),
        {"schema": "intelligence"} # Isolation stricte du schéma
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    actor_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id"), nullable=False)
    action = Column(String, nullable=False)
    entity_type = Column(String, nullable=False)
    entity_id = Column(String, nullable=False)
    old_value = Column(JSONB)
    new_value = Column(JSONB)
    ip_address = Column(String)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


# ── 2. Agent Actions (Sécurisé avec Guardrails & HITL) ──────────────────
class AgentAction(Base):
    __tablename__ = "agent_actions"
    __table_args__ = (
        Index('agent_actions_status_idx', 'status'),
        Index('agent_actions_batch_idx', 'batch_id'),
        Index('agent_actions_name_idx', 'agent_name'),
        {"schema": "intelligence"}
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    agent_name = Column(String, nullable=False)
    action_type = Column(String, nullable=False)
    batch_id = Column(String) # Conservé en String pour compatibilité migration
    payload = Column(JSONB)
    status = Column(String, default="PENDING", nullable=False)
    priority = Column(String, default="MEDIUM", nullable=False)
    
    # Sécurité IA & Validation humaine (HITL)
    is_human_required = Column(Boolean, default=False, nullable=False)
    guardrail_status = Column(String, default="PASSED", nullable=False)
    error_message = Column(Text)
    
    # Clés étrangères multi-schémas explicites
    order_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.orders.id"), unique=True)
    user_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id"))
    
    validated_by_id = Column(String) # Conservé en String pour compatibilité migration
    audit_trail_id = Column(String)
    ai_reasoning = Column(Text)
    admin_notes = Column(Text)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


# ── 3. Agent Telemetry ───────────────────────────────────────────────────
class AgentTelemetry(Base):
    __tablename__ = "agent_telemetry"
    __table_args__ = (
        Index('agent_telemetry_user_idx', 'user_id'),
        {"schema": "intelligence"}
    )
    
    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    user_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id"), nullable=False)
    latitude = Column(Float)
    longitude = Column(Float)
    battery = Column(Integer)
    signal = Column(String)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


# ── 4. External Context (Base de connaissances RAG) ─────────────────────
class ExternalContextFile(Base):
    __tablename__ = "external_context_files"
    __table_args__ = {"schema": "intelligence"}

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    file_name = Column(String, nullable=False)
    file_type = Column(String, nullable=False)
    file_url = Column(String, nullable=False)
    category = Column(String)
    zone_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id"))
    is_vectorized = Column(Boolean, default=False, nullable=False)
    
    # Tracking de l'indexation vectorielle
    vector_index_name = Column(String)
    chunk_count = Column(Integer)
    file_size = Column(Integer)
    
    farm_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.farms.id"))
    mcp_server_id = Column(String)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


# ── 5. Conversations (Gestion des fils de discussion et tracking LLM) ───
class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        Index('conversations_user_idx', 'user_id'),
        Index('conversations_parent_idx', 'parent_conversation_id'),
        Index('conversations_agent_idx', 'agent_type'),
        Index('conversations_created_idx', 'created_at'),
        Index('conversations_audit_trail_idx', 'audit_trail_id'),
        {"schema": "intelligence"}
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    user_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id"), nullable=False)
    parent_conversation_id = Column(PG_UUID(as_uuid=True), ForeignKey("intelligence.conversations.id")) # Chaînage des messages pour continuité
    
    query = Column(Text, nullable=False)
    response = Column(Text)
    agent_type = Column(String)
    crop = Column(String)
    zone_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id"))
    
    mode = Column(String, default="text", nullable=False)
    audio_url = Column(String)
    audio_duration_ms = Column(Integer) # Monitoring coût STT (Whisper)
    
    is_waiting_for_input = Column(Boolean, default=False, nullable=False)
    missing_slots = Column(JSONB)
    
    execution_path = Column(JSONB)
    confidence_score = Column(Float)
    total_tokens_used = Column(Integer, default=0, nullable=False)
    response_time_ms = Column(Integer)
    
    # Feedback RLHF utilisateur
    feedback_rating = Column(Integer)
    feedback_comment = Column(Text)
    
    audit_trail_id = Column(String, unique=True)
    anomaly_id = Column(PG_UUID(as_uuid=True), ForeignKey("intelligence.anomalies.id"))
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relations Python (Utile pour l'ORM)
    user = relationship("User", foreign_keys=[user_id])


# ── 6. Territory Events ──────────────────────────────────────────────────
class TerritoryEvent(Base):
    __tablename__ = "territory_events"
    __table_args__ = (
        Index('territory_events_zone_idx', 'zone_id'),
        Index('territory_events_type_idx', 'event_type'),
        {"schema": "intelligence"}
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    zone_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id"), nullable=False)
    event_type = Column(String, nullable=False)
    payload = Column(JSONB)
    meta = Column(JSONB)
    status = Column(String, default="NEW", nullable=False)
    expires_at = Column(DateTime) # Auto-expiration des événements éphémères
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    processed_at = Column(DateTime)
    processed_by_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id"))


# ── 7. Anomalies (Détection des fraudes et alertes terrain) ──────────────
class Anomaly(Base):
    __tablename__ = "anomalies"
    __table_args__ = (
        Index('anomalies_zone_idx', 'zone_id'),
        Index('anomalies_category_idx', 'category'),
        Index('anomalies_resolved_idx', 'is_resolved'),
        {"schema": "intelligence"}
    )
    
    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    zone_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id"), nullable=False)
    source = Column(String)
    category = Column(String, default="GENERAL", nullable=False) 
    level = Column(String, nullable=False)
    title = Column(String, nullable=False)
    message = Column(Text)
    details = Column(JSONB)
    is_resolved = Column(Boolean, default=False, nullable=False)
    false_positive_justification = Column(Text) # Apprentissage de l'IA sur ses faux positifs
    resolved_by_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id"))
    resolved_at = Column(DateTime)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


# ── 8. Trust Scores ──────────────────────────────────────────────────────
class TrustScore(Base):
    __tablename__ = "trust_scores"
    __table_args__ = {"schema": "intelligence"}

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    user_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id"), nullable=False, unique=True)
    global_score = Column(Float, default=0.0, nullable=False)
    reliability_index = Column(Float, default=0.0, nullable=False)
    quality_index = Column(Float, default=0.0, nullable=False)
    compliance_index = Column(Float, default=0.0, nullable=False)
    resilience_bonus = Column(Float, default=0.0, nullable=False)
    
    # Contexte pour la fiabilité du score
    transaction_count_evaluated = Column(Integer, default=0, nullable=False)
    score_trend = Column(String, default="STABLE", nullable=False)
    
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


# ── 9. AI Rating Reasonings ──────────────────────────────────────────────
class AIRatingReasoning(Base):
    __tablename__ = "ai_rating_reasonings"
    __table_args__ = (
        Index('ai_rating_trust_idx', 'trust_score_id'),
        Index('ai_rating_agent_idx', 'agent_name'),
        {"schema": "intelligence"}
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    trust_score_id = Column(PG_UUID(as_uuid=True), ForeignKey("intelligence.trust_scores.id"), nullable=False)
    agent_name = Column(String, nullable=False)
    justification = Column(Text, nullable=False)
    data_points = Column(JSONB, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


# ── 10. Cerveau IA : Recommandations stockées ────────────────────────────
class AIRecommendation(Base):
    __tablename__ = "ai_recommendations"
    __table_args__ = (
        Index('ai_rec_farm_idx', 'farm_id'),
        Index('ai_rec_crop_cycle_idx', 'crop_cycle_id'),
        Index('ai_rec_user_idx', 'user_id'),
        Index('ai_rec_type_idx', 'recommendation_type'),
        Index('ai_rec_status_idx', 'status'),
        Index('ai_rec_created_idx', 'created_at'),
        {"schema": "intelligence"}
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    user_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id"), nullable=False)
    crop_cycle_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.crop_cycles.id"))
    farm_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.farms.id"))
    agent_name = Column(String, nullable=False)
    recommendation_type = Column(String, nullable=False)
    title = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    confidence_score = Column(Float)
    data_sources_used = Column(JSONB)
    priority = Column(String, default="MEDIUM", nullable=False)
    status = Column(String, default="PENDING", nullable=False)

    user = relationship("User", back_populates="recommendations")
    crop_cycle = relationship("CropCycle", foreign_keys=[crop_cycle_id])
    farm = relationship("Farm", foreign_keys=[farm_id])
    applied_at = Column(DateTime)
    expires_at = Column(DateTime)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


# ── 11. Cerveau IA : Cache météo externe ( NASA / Open-Meteo ) ───────────
class WeatherDataLog(Base):
    __tablename__ = "weather_data_logs"
    __table_args__ = (
        Index('wdl_zone_idx', 'zone_id'),
        Index('wdl_farm_idx', 'farm_id'),
        Index('wdl_date_idx', 'record_date'),
        Index('wdl_farm_date_source_unique', 'farm_id', 'record_date', 'source', unique=True),
        {"schema": "intelligence"}
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    zone_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id"))
    farm_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.farms.id"))
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    record_date = Column(DateTime, nullable=False)
    temp_min = Column(Float)
    temp_max = Column(Float)
    temp_mean = Column(Float)
    precipitation_mm = Column(Float)
    humidity_percent = Column(Float)
    wind_speed_kmh = Column(Float)
    solar_radiation = Column(Float)
    evapotranspiration = Column(Float)
    gdd_contribution = Column(Float)
    
    forecast_horizon_days = Column(Integer, default=0, nullable=False) # 0 = Réel historique, >0 = Prévision J+N
    
    source = Column(String, default="OPEN_METEO", nullable=False)
    raw_payload = Column(JSONB)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


# ── 12. Cerveau IA : Mémoire contextuelle de l'agent ─────────────────────

class AgentContextMemory(Base):
    __tablename__ = "agent_context_memory"
    __table_args__ = (
        Index('acm_user_idx', 'user_id'),
        Index('acm_farm_idx', 'farm_id'),
        Index('acm_key_idx', 'context_key'),
        Index('acm_user_farm_key_unique', 'user_id', 'farm_id', 'context_key', unique=True),
        {"schema": "intelligence"}
    )

    # Utilisation de gen_random_uuid() pour éviter les erreurs de génération côté Python
    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    
    # Ajout de ForeignKeys pour assurer l'intégrité des données de contexte
    user_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id"), nullable=False, index=True)
    farm_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.farms.id"), index=True)
    crop_cycle_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.crop_cycles.id"), index=True)
    
    context_key = Column(String, nullable=False)
    context_value = Column(JSONB, nullable=False)
    source = Column(String, default="AGENT", nullable=False)
    
    is_long_term = Column(Boolean, default=False, nullable=False)
    confidence = Column(Float)
    expires_at = Column(DateTime)
    
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


##**************************************
## Inventory
###*************************************


class SeedAllocation(Base):
    __tablename__ = "seed_allocations"
    __table_args__ = (
        Index('seed_allocations_org_idx', 'organization_id'),
        Index('seed_allocations_zone_idx', 'zone_id'),
        Index('seed_allocations_seedtype_idx', 'seed_type'),
        {"schema": "inventory"}
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    organization_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.organizations.id"), nullable=False)
    zone_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id"), nullable=False)
    seed_type = Column(String, nullable=False)
    total_quantity = Column(Integer, nullable=False)
    remaining_quantity = Column(Integer, nullable=False)
    unit = Column(String, default="KG", nullable=False)
    allocated_by_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id"))
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, Index, Boolean, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID, JSONB
from sqlalchemy.sql import func

class SeedDistribution(Base):
    __tablename__ = "seed_distributions"
    __table_args__ = (
        Index('seed_distributions_alloc_idx', 'allocation_id'),
        Index('seed_distributions_producer_idx', 'producer_id'),
        Index('seed_distributions_agent_idx', 'agent_id'),
        Index('seed_distributions_status_idx', 'status'),
        {"schema": "inventory"}
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    allocation_id = Column(PG_UUID(as_uuid=True), ForeignKey("inventory.seed_allocations.id"), nullable=False)
    producer_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.producers.id"), nullable=False)
    agent_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id"), nullable=False)
    organization_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.organizations.id"), nullable=False)
    zone_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id"), nullable=False)
    quantity = Column(Integer, nullable=False)
    cnib_provided = Column(String)
    verification_code_hash = Column(String)
    verification_code_expires_at = Column(DateTime)
    verification_channel = Column(String, default="IN_APP")
    attempts_count = Column(Integer, default=0, nullable=False)
    status = Column(String, default="PENDING", nullable=False)
    receipt_at = Column(DateTime)
    
    # CORRECTION : Renommé pour éviter le conflit avec l'attribut réservé SQLAlchemy
    info_json = Column(JSONB) 
    
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

class SeedDistributionAttempt(Base):
    __tablename__ = "seed_distribution_attempts"
    __table_args__ = (
        Index('sda_distribution_idx', 'distribution_id'),
        Index('sda_actor_idx', 'actor_id'),
        {"schema": "inventory"}
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    distribution_id = Column(PG_UUID(as_uuid=True), ForeignKey("inventory.seed_distributions.id"), nullable=False)
    actor_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id"))
    attempt_type = Column(String)
    success = Column(Boolean, default=False, nullable=False)
    ip_address = Column(String)
    
    # CORRECTION : Renommé également ici
    info_json = Column(JSONB)
    
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

##**************************************
## unrelated
###*************************************

class UserContextState(Base):
    __tablename__ = "user_context_states"

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    user_id = Column(String, unique=True, nullable=False)
    last_intent = Column(String)
    pending_intent = Column(String)
    draft_data = Column(JSONB)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class MarketMatch(Base):
    __tablename__ = "market_matches"

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    product_id = Column(String)
    buyer_id = Column(String)
    score = Column(Float, default=0.0, nullable=False)
    status = Column(String, default="SUGGESTED", nullable=False)
    meta = Column(JSONB)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class TransactionStaging(Base):
    __tablename__ = "transaction_staging"

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    transaction_id = Column(String, unique=True, nullable=False)
    payload = Column(JSONB)
    status = Column(String, default="PENDING", nullable=False)
    expires_at = Column(DateTime)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class SurplusOffer(Base):
    __tablename__ = "surplus_offers"
    __table_args__ = {"schema": "marketplace"}

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    user_id = Column(String)
    product_name = Column(String, nullable=False)
    quantity_kg = Column(Float, nullable=False)
    price_kg = Column(Float)
    zone_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id"))
    location = Column(String)
    channel = Column(String, default="api")
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


__all__ = [
    "Base",
    "_uuid4",
    "User",
    "Account",
    "Session",
    "Organization",
    "UserOrganization",
    "RoleDefinition",
    
    "ClimaticRegion",
    "Zone",
    "WorkZone",
    "Producer",
    "BuyerType",
    "BuyerProfile",
    "DeliveryAgent",
    "Delivery",
    "Farm",
    "Product",
    "Category",
    "SubCategory",
    "ZoneSetting",
    "OverlayLayer",
    "UserCulture",
    "DailyAdviceLog",
    "ExternalContextFile",
    "Stock",
    "StockMovement",
    "Order",
    "OrderItem",
    "Client",
    "Expense",
    "CropCycle",
    "SurplusOffer",
    "Warehouse",
    "TransactionStaging",
    "Auction",
    "Bid",
    "Batch",
    "SeedAllocation",
    "SeedDistribution",
    "SeedDistributionAttempt",
    "StandardPrice",
    "AgentAction",
    "AgentTelemetry",
    "Conversation",
    "AuditLog",
    "TrustScore",
    "AIRatingReasoning",
    "AIRecommendation",
    "MarketplaceRating",
    "TerritoryEvent",
    "ZoneMetric",
    "UserContextState",
    "MarketMatch",
    "Anomaly",
]
