from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.types import Uuid
"""
SQLAlchemy Models — AgriConnect v3 (Multi-Schema).

SOURCE UNIQUE DE VÉRITÉ pour le schéma ORM côté agents.
Aligné sur le Drizzle schema frontend (auth / governance / marketplace / intelligence).

Convention :
  - UUID v4 pour tous les IDs
  - Timestamps created_at / updated_at partout
  - Foreign keys explicites cross-schema
"""

from sqlalchemy import (
    Column, String, DateTime, Boolean, Integer, Float,
    JSON, ForeignKey, Text, UniqueConstraint, Index,
)
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.sql import func

Base = declarative_base()


# ══════════════════════════════════════════════════════════════════
# AUTH SCHEMA
# ══════════════════════════════════════════════════════════════════

class User(Base):
    """auth.users — Utilisateurs de la plateforme."""
    __tablename__ = "users"
    __table_args__ = {"schema": "auth"}

    id = Column(Uuid(as_uuid=False), primary_key=True)
    name = Column(String)
    email = Column(String, unique=True)
    email_verified = Column("email_verified", DateTime(timezone=True))
    image = Column(String)
    password = Column(String)
    phone = Column(String, unique=True)
    role = Column(String, default="USER", nullable=False)
    zone_id = Column("zone_id", Uuid(as_uuid=False))
    deleted_at = Column("deleted_at", DateTime(timezone=True))
    created_at = Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    producer = relationship("Producer", back_populates="user", uselist=False, lazy="joined")
    trust_score = relationship("TrustScore", back_populates="user", uselist=False)
    conversations = relationship("Conversation", back_populates="user")
    audit_logs = relationship("AuditLog", back_populates="actor")

    @property
    def language(self) -> str:
        return "fr"

    @property
    def is_onboarded(self) -> bool:
        return self.deleted_at is None

    @property
    def voice_preference(self) -> str:
        return "fr-FR-HenriNeural"

    def to_dict(self):
        return {
            "id": self.id, "phone": self.phone, "name": self.name,
            "email": self.email, "role": self.role, "zone_id": self.zone_id,
        }


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint("provider", "provider_account_id", name="accounts_provider_unique"),
        {"schema": "auth"},
    )

    id = Column(Uuid(as_uuid=False), primary_key=True)
    user_id = Column("user_id", Uuid(as_uuid=False), ForeignKey("auth.users.id", ondelete="CASCADE"), nullable=False)
    type = Column(String, nullable=False)
    provider = Column(String, nullable=False)
    provider_account_id = Column("provider_account_id", Uuid(as_uuid=False), nullable=False)
    refresh_token = Column("refresh_token", String)
    access_token = Column("access_token", String)
    expires_at = Column("expires_at", Integer)
    token_type = Column("token_type", String)
    scope = Column(String)
    id_token = Column("id_token", String)
    session_state = Column("session_state", String)


class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = {"schema": "auth"}

    id = Column(Uuid(as_uuid=False), primary_key=True)
    session_token = Column("session_token", String, unique=True, nullable=False)
    user_id = Column("user_id", Uuid(as_uuid=False), ForeignKey("auth.users.id", ondelete="CASCADE"), nullable=False)
    expires = Column(DateTime(timezone=True), nullable=False)


# ══════════════════════════════════════════════════════════════════
# GOVERNANCE SCHEMA
# ══════════════════════════════════════════════════════════════════

class Organization(Base):
    __tablename__ = "organizations"
    __table_args__ = {"schema": "governance"}

    id = Column(Uuid(as_uuid=False), primary_key=True)
    name = Column(String, nullable=False)
    type = Column(String, nullable=False)
    tax_id = Column("tax_id", Uuid(as_uuid=False), unique=True)
    description = Column(Text)
    status = Column(String, default="PENDING", nullable=False)
    created_at = Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    users = relationship("UserOrganization", back_populates="organization")
    zones = relationship("Zone", back_populates="organization")
    work_zones = relationship("WorkZone", back_populates="organization")
    producers = relationship("Producer", back_populates="organization")
    stocks = relationship("Stock", back_populates="organization")
    batches = relationship("Batch", back_populates="organization")

    def to_dict(self):
        return {"id": self.id, "name": self.name, "type": self.type, "status": self.status}


class UserOrganization(Base):
    __tablename__ = "user_organizations"
    __table_args__ = (
        UniqueConstraint("user_id", "organization_id", name="user_org_unique"),
        {"schema": "governance"},
    )

    id = Column(Uuid(as_uuid=False), primary_key=True)
    user_id = Column("user_id", Uuid(as_uuid=False), ForeignKey("auth.users.id"), nullable=False)
    organization_id = Column("organization_id", Uuid(as_uuid=False), ForeignKey("governance.organizations.id"), nullable=False)
    role = Column(String, default="FIELD_AGENT", nullable=False)
    role_id = Column("role_id", Uuid(as_uuid=False), ForeignKey("governance.role_definitions.id"))
    managed_zone_id = Column("managed_zone_id", Uuid(as_uuid=False), ForeignKey("governance.zones.id"))

    user = relationship("User")
    organization = relationship("Organization", back_populates="users")


class RoleDef(Base):
    __tablename__ = "role_definitions"
    __table_args__ = {"schema": "governance"}

    id = Column(Uuid(as_uuid=False), primary_key=True)
    name = Column(String, unique=True, nullable=False)
    description = Column(Text)
    permissions = Column(JSON, default=list)
    created_at = Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False)


class ClimaticRegion(Base):
    __tablename__ = "climatic_regions"
    __table_args__ = {"schema": "governance"}

    id = Column(Uuid(as_uuid=False), primary_key=True)
    name = Column(String, unique=True, nullable=False)
    description = Column(Text)
    created_at = Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    zones = relationship("Zone", back_populates="climatic_region")

    def to_dict(self):
        return {"id": self.id, "name": self.name, "description": self.description}


class Zone(Base):
    __tablename__ = "zones"
    __table_args__ = {"schema": "governance"}

    id = Column(Uuid(as_uuid=False), primary_key=True)
    name = Column(String, unique=True, nullable=False)
    code = Column(String, unique=True, nullable=False)
    climatic_region_id = Column("climatic_region_id", Uuid(as_uuid=False), ForeignKey("governance.climatic_regions.id"), nullable=False)
    organization_id = Column("organization_id", Uuid(as_uuid=False), ForeignKey("governance.organizations.id"))
    parent_id = Column("parent_id", Uuid(as_uuid=False), ForeignKey("governance.zones.id"))
    path = Column(String)
    depth = Column(Integer, default=0, nullable=False)
    latitude = Column(Float)
    longitude = Column(Float)
    is_active = Column("is_active", Boolean, default=True, nullable=False)
    created_at = Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    climatic_region = relationship("ClimaticRegion", back_populates="zones")
    organization = relationship("Organization", back_populates="zones")
    parent = relationship("Zone", remote_side="Zone.id", backref="children")
    producers = relationship("Producer", back_populates="zone")
    farms = relationship("Farm", back_populates="zone")
    warehouses = relationship("Warehouse", back_populates="zone")
    standard_prices = relationship("StandardPrice", back_populates="zone")
    auctions = relationship("Auction", back_populates="target_zone")

    def to_dict(self):
        return {
            "id": self.id, "name": self.name, "code": self.code,
            "latitude": self.latitude, "longitude": self.longitude,
            "is_active": self.is_active, "depth": self.depth,
            "parent_id": self.parent_id, "path": self.path,
        }


class WorkZone(Base):
    __tablename__ = "work_zones"
    __table_args__ = (
        UniqueConstraint("organization_id", "zone_id", name="work_zones_org_zone_unique"),
        {"schema": "governance"},
    )

    id = Column(Uuid(as_uuid=False), primary_key=True)
    organization_id = Column("organization_id", Uuid(as_uuid=False), ForeignKey("governance.organizations.id", ondelete="CASCADE"), nullable=False)
    zone_id = Column("zone_id", Uuid(as_uuid=False), ForeignKey("governance.zones.id", ondelete="CASCADE"), nullable=False)
    manager_id = Column("manager_id", Uuid(as_uuid=False), ForeignKey("auth.users.id"))
    role = Column(String)
    created_at = Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    organization = relationship("Organization", back_populates="work_zones")
    zone = relationship("Zone")
    manager = relationship("User")


class ZoneMetric(Base):
    __tablename__ = "zone_metrics"
    __table_args__ = {"schema": "governance"}

    id = Column(Uuid(as_uuid=False), primary_key=True)
    zone_id = Column("zone_id", Uuid(as_uuid=False), ForeignKey("governance.zones.id", ondelete="CASCADE"), nullable=False)
    date = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    metric_name = Column("metric_name", String, nullable=False)
    value = Column(Float, nullable=False)
    created_at = Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False)

    zone = relationship("Zone")


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = {"schema": "governance"}

    id = Column(Uuid(as_uuid=False), primary_key=True)
    name = Column(String, unique=True, nullable=False)
    description = Column(Text)
    created_at = Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    sub_categories = relationship("SubCategory", back_populates="category")

    def to_dict(self):
        return {"id": self.id, "name": self.name}


class SubCategory(Base):
    __tablename__ = "sub_categories"
    __table_args__ = (
        UniqueConstraint("category_id", "name", name="sub_categories_cat_name_unique"),
        {"schema": "governance"},
    )

    id = Column(Uuid(as_uuid=False), primary_key=True)
    category_id = Column("category_id", Uuid(as_uuid=False), ForeignKey("governance.categories.id", ondelete="CASCADE"), nullable=False)
    name = Column(String, nullable=False)
    blocked_zone_ids = Column("blocked_zone_ids", JSON, default=list)
    created_at = Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    category = relationship("Category", back_populates="sub_categories")
    standard_prices = relationship("StandardPrice", back_populates="sub_category")
    products = relationship("Product", back_populates="sub_category")

    def to_dict(self):
        return {"id": self.id, "category_id": self.category_id, "name": self.name}


class StandardPrice(Base):
    __tablename__ = "standard_prices"
    __table_args__ = (
        UniqueConstraint("sub_category_id", "zone_id", name="standard_prices_sub_zone_unique"),
        {"schema": "governance"},
    )

    id = Column(Uuid(as_uuid=False), primary_key=True)
    sub_category_id = Column("sub_category_id", Uuid(as_uuid=False), ForeignKey("governance.sub_categories.id", ondelete="CASCADE"), nullable=False)
    zone_id = Column("zone_id", Uuid(as_uuid=False), ForeignKey("governance.zones.id"), nullable=False)
    price_per_unit = Column("price_per_unit", Float, nullable=False)
    unit = Column(String, default="KG", nullable=False)
    updated_by_id = Column("updated_by_id", Uuid(as_uuid=False), ForeignKey("auth.users.id"), nullable=False)
    created_at = Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    sub_category = relationship("SubCategory", back_populates="standard_prices")
    zone = relationship("Zone", back_populates="standard_prices")

    def to_dict(self):
        return {
            "id": self.id, "sub_category_id": self.sub_category_id,
            "zone_id": self.zone_id, "price_per_unit": self.price_per_unit,
            "unit": self.unit,
        }


class ZoneSetting(Base):
    __tablename__ = "zone_settings"
    __table_args__ = (
        UniqueConstraint("zone_id", "key", name="zone_settings_zone_key_unique"),
        {"schema": "governance"},
    )

    id = Column(Uuid(as_uuid=False), primary_key=True)
    zone_id = Column("zone_id", Uuid(as_uuid=False), ForeignKey("governance.zones.id"), nullable=False)
    key = Column(String, nullable=False)
    value = Column(JSON, nullable=False)
    created_at = Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class OverlayLayer(Base):
    __tablename__ = "overlay_layers"
    __table_args__ = (
        UniqueConstraint("zone_id", "key", name="overlay_layers_zone_key_unique"),
        {"schema": "governance"},
    )

    id = Column(Uuid(as_uuid=False), primary_key=True)
    zone_id = Column("zone_id", Uuid(as_uuid=False), ForeignKey("governance.zones.id"), nullable=False)
    key = Column(String, nullable=False)
    label = Column(String, nullable=False)
    enabled = Column(Boolean, default=False, nullable=False)
    settings = Column(JSON)
    created_at = Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


# ══════════════════════════════════════════════════════════════════
# MARKETPLACE SCHEMA
# ══════════════════════════════════════════════════════════════════

class Warehouse(Base):
    __tablename__ = "warehouses"
    __table_args__ = {"schema": "marketplace"}

    id = Column(Uuid(as_uuid=False), primary_key=True)
    name = Column(String, nullable=False)
    type = Column(String, nullable=False)
    capacity = Column(Float)
    location = Column(String)
    zone_id = Column("zone_id", Uuid(as_uuid=False), ForeignKey("governance.zones.id"), nullable=False)
    created_at = Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    zone = relationship("Zone", back_populates="warehouses")
    stocks = relationship("Stock", back_populates="warehouse")

    def to_dict(self):
        return {"id": self.id, "name": self.name, "type": self.type, "capacity": self.capacity, "zone_id": self.zone_id}


class Producer(Base):
    __tablename__ = "producers"
    __table_args__ = {"schema": "marketplace"}

    id = Column(Uuid(as_uuid=False), primary_key=True)
    user_id = Column("user_id", Uuid(as_uuid=False), ForeignKey("auth.users.id", ondelete="CASCADE"), unique=True, nullable=False)
    organization_id = Column("organization_id", Uuid(as_uuid=False), ForeignKey("governance.organizations.id"))
    business_name = Column("business_name", String)
    status = Column(String, default="PENDING", nullable=False)
    is_certified = Column("is_certified", Boolean, default=False, nullable=False)
    zone_id = Column("zone_id", Uuid(as_uuid=False), ForeignKey("governance.zones.id"))
    region = Column(String)
    province = Column(String)
    commune = Column(String)
    created_at = Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User", back_populates="producer")
    organization = relationship("Organization", back_populates="producers")
    zone = relationship("Zone", back_populates="producers")
    farms = relationship("Farm", back_populates="producer")
    products = relationship("Product", back_populates="producer")
    clients = relationship("Client", back_populates="producer")
    bids = relationship("Bid", back_populates="producer")

    def to_dict(self):
        return {
            "id": self.id, "user_id": self.user_id,
            "business_name": self.business_name, "status": self.status,
            "is_certified": self.is_certified, "zone_id": self.zone_id,
            "commune": self.commune,
        }


class Client(Base):
    __tablename__ = "clients"
    __table_args__ = {"schema": "marketplace"}

    id = Column(Uuid(as_uuid=False), primary_key=True)
    name = Column(String, nullable=False)
    phone = Column(String, nullable=False)
    email = Column(String)
    location = Column(String)
    total_orders = Column("total_orders", Integer, default=0, nullable=False)
    total_spent = Column("total_spent", Float, default=0, nullable=False)
    last_order_date = Column("last_order_date", DateTime(timezone=True))
    producer_id = Column("producer_id", Uuid(as_uuid=False), ForeignKey("marketplace.producers.id"), nullable=False)
    created_at = Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    producer = relationship("Producer", back_populates="clients")
    orders = relationship("Order", back_populates="client")

    def to_dict(self):
        return {
            "id": self.id, "name": self.name, "phone": self.phone,
            "producer_id": self.producer_id, "total_orders": self.total_orders,
        }


class Farm(Base):
    __tablename__ = "farms"
    __table_args__ = {"schema": "marketplace"}

    id = Column(Uuid(as_uuid=False), primary_key=True)
    name = Column(String, nullable=False)
    location = Column(String)
    size = Column(Float)
    soil_type = Column("soil_type", String)
    water_source = Column("water_source", String)
    zone_id = Column("zone_id", Uuid(as_uuid=False), ForeignKey("governance.zones.id"))
    producer_id = Column("producer_id", Uuid(as_uuid=False), ForeignKey("marketplace.producers.id", ondelete="CASCADE"), nullable=False)
    created_at = Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    producer = relationship("Producer", back_populates="farms")
    zone = relationship("Zone", back_populates="farms")
    stocks = relationship("Stock", back_populates="farm")
    expenses = relationship("Expense", back_populates="farm")
    crop_cycles = relationship("CropCycle", back_populates="farm")
    batches = relationship("Batch", back_populates="farm")

    def to_dict(self):
        return {
            "id": self.id, "name": self.name, "size": self.size,
            "soil_type": self.soil_type, "producer_id": self.producer_id,
        }


class CropCycle(Base):
    __tablename__ = "crop_cycles"
    __table_args__ = {"schema": "marketplace"}

    id = Column(Uuid(as_uuid=False), primary_key=True)
    farm_id = Column("farm_id", Uuid(as_uuid=False), ForeignKey("marketplace.farms.id"), nullable=False)
    crop_type = Column("crop_type", String, nullable=False)
    area_size = Column("area_size", Float, nullable=False)
    planted_at = Column("planted_at", DateTime(timezone=True), nullable=False)
    expected_harvest_date = Column("expected_harvest_date", DateTime(timezone=True), nullable=False)
    expected_yield = Column("expected_yield", Float, nullable=False)
    status = Column(String, nullable=False)
    created_at = Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    farm = relationship("Farm", back_populates="crop_cycles")

    def to_dict(self):
        return {
            "id": self.id, "farm_id": self.farm_id, "crop_type": self.crop_type,
            "area_size": self.area_size, "status": self.status,
            "expected_yield": self.expected_yield,
        }


class Stock(Base):
    __tablename__ = "stocks"
    __table_args__ = (
        Index("ix_stocks_farm_item", "farm_id", "item_name"),
        {"schema": "marketplace"},
    )

    id = Column(Uuid(as_uuid=False), primary_key=True)
    farm_id = Column("farm_id", Uuid(as_uuid=False), ForeignKey("marketplace.farms.id"))
    warehouse_id = Column("warehouse_id", Uuid(as_uuid=False), ForeignKey("marketplace.warehouses.id"))
    organization_id = Column("organization_id", Uuid(as_uuid=False), ForeignKey("governance.organizations.id"))
    item_name = Column("item_name", String, nullable=False)
    quantity = Column(Float, default=0, nullable=False)
    unit = Column(String, default="KG", nullable=False)
    type = Column(String, default="HARVEST", nullable=False)
    verified_at = Column("verified_at", DateTime(timezone=True))
    verified_by_id = Column("verified_by_id", Uuid(as_uuid=False), ForeignKey("auth.users.id"))
    created_at = Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    farm = relationship("Farm", back_populates="stocks")
    warehouse = relationship("Warehouse", back_populates="stocks")
    organization = relationship("Organization", back_populates="stocks")
    movements = relationship("StockMovement", back_populates="stock")
    batches = relationship("Batch", back_populates="stock")

    def to_dict(self):
        return {
            "id": self.id, "farm_id": self.farm_id, "item_name": self.item_name,
            "quantity": self.quantity, "unit": self.unit, "type": self.type,
        }


class StockMovement(Base):
    __tablename__ = "stock_movements"
    __table_args__ = {"schema": "marketplace"}

    id = Column(Uuid(as_uuid=False), primary_key=True)
    stock_id = Column("stock_id", Uuid(as_uuid=False), ForeignKey("marketplace.stocks.id", ondelete="CASCADE"), nullable=False)
    type = Column(String, nullable=False)
    quantity = Column(Float, nullable=False)
    reason = Column(String)
    created_at = Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False)

    stock = relationship("Stock", back_populates="movements")


class Batch(Base):
    __tablename__ = "batches"
    __table_args__ = {"schema": "marketplace"}

    id = Column(Uuid(as_uuid=False), primary_key=True)
    stock_id = Column("stock_id", Uuid(as_uuid=False), ForeignKey("marketplace.stocks.id", ondelete="CASCADE"), nullable=False)
    organization_id = Column("organization_id", Uuid(as_uuid=False), ForeignKey("governance.organizations.id"), nullable=False)
    batch_number = Column("batch_number", String, unique=True, nullable=False)
    origin_farm_id = Column("origin_farm_id", Uuid(as_uuid=False), ForeignKey("marketplace.farms.id"))
    quantity = Column(Float, nullable=False)
    created_at = Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    stock = relationship("Stock", back_populates="batches")
    organization = relationship("Organization", back_populates="batches")
    farm = relationship("Farm", back_populates="batches")


class Expense(Base):
    __tablename__ = "expenses"
    __table_args__ = {"schema": "marketplace"}

    id = Column(Uuid(as_uuid=False), primary_key=True)
    farm_id = Column("farm_id", Uuid(as_uuid=False), ForeignKey("marketplace.farms.id", ondelete="CASCADE"), nullable=False)
    label = Column(String, nullable=False)
    amount = Column(Float, nullable=False)
    category = Column(String, default="OTHER", nullable=False)
    date = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    farm = relationship("Farm", back_populates="expenses")

    def to_dict(self):
        return {
            "id": self.id, "farm_id": self.farm_id, "label": self.label,
            "amount": self.amount, "category": self.category,
        }


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        Index("ix_products_producer_id", "producer_id"),
        Index("ix_products_name", "name"),
        {"schema": "marketplace"},
    )

    id = Column(Uuid(as_uuid=False), primary_key=True)
    short_code = Column("short_code", String, unique=True)
    name = Column(String, default="Produit", nullable=False)
    category_label = Column("category_label", String, nullable=False)
    sub_category_id = Column("sub_category_id", Uuid(as_uuid=False), ForeignKey("governance.sub_categories.id"))
    local_names = Column("local_names", JSON)
    description = Column(Text)
    price = Column(Float, nullable=False)
    unit = Column(String, default="KG", nullable=False)
    quantity_for_sale = Column("quantity_for_sale", Float, default=0, nullable=False)
    images = Column(ARRAY(String), default=list)
    audio_url = Column("audio_url", String)
    producer_id = Column("producer_id", Uuid(as_uuid=False), ForeignKey("marketplace.producers.id", ondelete="CASCADE"), nullable=False)
    verified_at = Column("verified_at", DateTime(timezone=True))
    verified_by_id = Column("verified_by_id", Uuid(as_uuid=False), ForeignKey("auth.users.id"))
    created_at = Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    producer = relationship("Producer", back_populates="products")
    sub_category = relationship("SubCategory", back_populates="products")
    order_items = relationship("OrderItem", back_populates="product")

    def to_dict(self):
        return {
            "id": self.id, "short_code": self.short_code,
            "name": self.name, "category_label": self.category_label,
            "price": self.price, "unit": self.unit,
            "quantity_for_sale": self.quantity_for_sale,
            "producer_id": self.producer_id,
        }


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = {"schema": "marketplace"}

    id = Column(Uuid(as_uuid=False), primary_key=True)
    buyer_id = Column("buyer_id", Uuid(as_uuid=False), ForeignKey("auth.users.id"))
    organization_id = Column("organization_id", Uuid(as_uuid=False), ForeignKey("governance.organizations.id"))
    customer_name = Column("customer_name", String)
    customer_phone = Column("customer_phone", String)
    zone_id = Column("zone_id", Uuid(as_uuid=False), ForeignKey("governance.zones.id"))
    payment_method = Column("payment_method", String, default="CASH", nullable=False)
    payment_status = Column("payment_status", String, default="PENDING", nullable=False)
    city = Column(String)
    gps_lat = Column("gps_lat", Float)
    gps_lng = Column("gps_lng", Float)
    delivery_desc = Column("delivery_desc", String)
    audio_url = Column("audio_url", String)
    status = Column(String, default="PENDING", nullable=False)
    source = Column(String, default="APP", nullable=False)
    whatsapp_id = Column("whatsapp_id", Uuid(as_uuid=False))
    total_amount = Column("total_amount", Float, nullable=False)
    is_agent_order = Column("is_agent_order", Boolean, default=False, nullable=False)
    client_id = Column("client_id", Uuid(as_uuid=False), ForeignKey("marketplace.clients.id"))
    created_at = Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    buyer = relationship("User")
    organization = relationship("Organization")
    zone = relationship("Zone")
    client = relationship("Client", back_populates="orders")
    items = relationship("OrderItem", back_populates="order")

    def to_dict(self):
        return {
            "id": self.id, "buyer_id": self.buyer_id,
            "customer_name": self.customer_name, "status": self.status,
            "payment_status": self.payment_status,
            "total_amount": self.total_amount, "source": self.source,
        }


class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = {"schema": "marketplace"}

    id = Column(Uuid(as_uuid=False), primary_key=True)
    order_id = Column("order_id", Uuid(as_uuid=False), ForeignKey("marketplace.orders.id", ondelete="CASCADE"), nullable=False)
    product_id = Column("product_id", Uuid(as_uuid=False), ForeignKey("marketplace.products.id"), nullable=False)
    quantity = Column(Float, nullable=False)
    price_at_sale = Column("price_at_sale", Float, nullable=False)

    order = relationship("Order", back_populates="items")
    product = relationship("Product", back_populates="order_items")


class Auction(Base):
    __tablename__ = "auctions"
    __table_args__ = {"schema": "marketplace"}

    id = Column(Uuid(as_uuid=False), primary_key=True)
    buyer_id = Column("buyer_id", Uuid(as_uuid=False), ForeignKey("auth.users.id"), nullable=False)
    sub_category_id = Column("sub_category_id", Uuid(as_uuid=False), nullable=False)
    quantity = Column(Float, nullable=False)
    unit = Column(String, default="TONNE", nullable=False)
    max_price_per_unit = Column("max_price_per_unit", Float, nullable=False)
    deadline = Column(DateTime(timezone=True), nullable=False)
    status = Column(String, default="OPEN", nullable=False)
    target_zone_id = Column("target_zone_id", Uuid(as_uuid=False), ForeignKey("governance.zones.id"))
    version = Column(Integer, default=0, nullable=False)
    created_at = Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    buyer = relationship("User")
    target_zone = relationship("Zone", back_populates="auctions")
    bids = relationship("Bid", back_populates="auction")

    def to_dict(self):
        return {
            "id": self.id, "buyer_id": self.buyer_id,
            "quantity": self.quantity, "status": self.status,
            "max_price_per_unit": self.max_price_per_unit,
        }


class Bid(Base):
    __tablename__ = "bids"
    __table_args__ = (
        UniqueConstraint("auction_id", "producer_id", name="bids_auction_producer_unique"),
        {"schema": "marketplace"},
    )

    id = Column(Uuid(as_uuid=False), primary_key=True)
    auction_id = Column("auction_id", Uuid(as_uuid=False), ForeignKey("marketplace.auctions.id", ondelete="CASCADE"), nullable=False)
    producer_id = Column("producer_id", Uuid(as_uuid=False), ForeignKey("marketplace.producers.id"), nullable=False)
    offered_price = Column("offered_price", Float, nullable=False)
    is_winner = Column("is_winner", Boolean, default=False, nullable=False)
    created_at = Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False)

    auction = relationship("Auction", back_populates="bids")
    producer = relationship("Producer", back_populates="bids")


class TransactionStaging(Base):
    __tablename__ = "transaction_staging"
    __table_args__ = {"schema": "marketplace"}

    id = Column(String, primary_key=True)
    transaction_id = Column("transaction_id", String, unique=True, nullable=False)
    payload = Column(JSON, nullable=False)
    status = Column(String, default="PENDING", nullable=False)
    created_at = Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at = Column("expires_at", DateTime(timezone=True))

    def to_dict(self):
        return {
            "id": self.id,
            "transaction_id": self.transaction_id,
            "payload": self.payload,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


# ══════════════════════════════════════════════════════════════════
# INTELLIGENCE SCHEMA
# ══════════════════════════════════════════════════════════════════

class AgentAction(Base):
    __tablename__ = "agent_actions"
    __table_args__ = {"schema": "intelligence"}

    id = Column(Uuid(as_uuid=False), primary_key=True)
    agent_name = Column("agent_name", String, nullable=False)
    action_type = Column("action_type", String, nullable=False)
    batch_id = Column("batch_id", Uuid(as_uuid=False))
    payload = Column(JSON, nullable=False)
    status = Column(String, default="PENDING", nullable=False)
    priority = Column(String, default="MEDIUM", nullable=False)
    order_id = Column("order_id", Uuid(as_uuid=False), unique=True)
    user_id = Column("user_id", Uuid(as_uuid=False), ForeignKey("auth.users.id"))
    audit_trail_id = Column("audit_trail_id", Uuid(as_uuid=False))
    ai_reasoning = Column("ai_reasoning", Text)
    admin_notes = Column("admin_notes", Text)
    validated_by_id = Column("validated_by_id", Uuid(as_uuid=False))
    created_at = Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User")

    def to_dict(self):
        return {
            "id": self.id, "agent_name": self.agent_name,
            "action_type": self.action_type, "status": self.status,
            "priority": self.priority, "ai_reasoning": self.ai_reasoning,
        }


class AgentTelemetry(Base):
    __tablename__ = "agent_telemetry"
    __table_args__ = {"schema": "intelligence"}

    id = Column(Uuid(as_uuid=False), primary_key=True)
    user_id = Column("user_id", Uuid(as_uuid=False), ForeignKey("auth.users.id"), nullable=False)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    battery = Column(Integer)
    signal = Column(String)
    created_at = Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False)

    user = relationship("User")


class ExternalContext(Base):
    __tablename__ = "external_context_files"
    __table_args__ = {"schema": "intelligence"}

    id = Column(Uuid(as_uuid=False), primary_key=True)
    file_name = Column("file_name", String, nullable=False)
    file_type = Column("file_type", String, nullable=False)
    file_url = Column("file_url", String, nullable=False)
    category = Column(String)
    zone_id = Column("zone_id", Uuid(as_uuid=False), ForeignKey("governance.zones.id"))
    is_vectorized = Column("is_vectorized", Boolean, default=False, nullable=False)
    mcp_server_id = Column("mcp_server_id", Uuid(as_uuid=False))
    created_at = Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False)


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = {"schema": "intelligence"}

    id = Column(Uuid(as_uuid=False), primary_key=True)
    user_id = Column("user_id", Uuid(as_uuid=False), ForeignKey("auth.users.id", ondelete="CASCADE"), nullable=False)
    query = Column(Text, nullable=False)
    response = Column(Text)
    agent_type = Column("agent_type", String)
    crop = Column(String)
    zone_id = Column("zone_id", Uuid(as_uuid=False), ForeignKey("governance.zones.id"))
    mode = Column(String, default="text", nullable=False)
    audio_url = Column("audio_url", String)
    is_waiting_for_input = Column("is_waiting_for_input", Boolean, default=False, nullable=False)
    missing_slots = Column("missing_slots", JSON)
    execution_path = Column("execution_path", JSON)
    confidence_score = Column("confidence_score", Float)
    total_tokens_used = Column("total_tokens_used", Integer, default=0, nullable=False)
    response_time_ms = Column("response_time_ms", Integer)
    audit_trail_id = Column("audit_trail_id", Uuid(as_uuid=False), unique=True)
    anomaly_id = Column("anomaly_id", Uuid(as_uuid=False), unique=True)
    created_at = Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User", back_populates="conversations")


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = {"schema": "intelligence"}

    id = Column(Uuid(as_uuid=False), primary_key=True)
    actor_id = Column("actor_id", Uuid(as_uuid=False), ForeignKey("auth.users.id"), nullable=False)
    action = Column(String, nullable=False)
    entity_id = Column("entity_id", Uuid(as_uuid=False), nullable=False)
    entity_type = Column("entity_type", String, nullable=False)
    old_value = Column("old_value", JSON)
    new_value = Column("new_value", JSON)
    ip_address = Column("ip_address", String)
    created_at = Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False)

    actor = relationship("User", back_populates="audit_logs")


class TerritoryEvent(Base):
    __tablename__ = "territory_events"
    __table_args__ = {"schema": "intelligence"}

    id = Column(Uuid(as_uuid=False), primary_key=True)
    zone_id = Column("zone_id", Uuid(as_uuid=False), ForeignKey("governance.zones.id"), nullable=False)
    event_type = Column("event_type", String, nullable=False)
    payload = Column(JSON)
    meta = Column(JSON)
    status = Column(String, default="NEW", nullable=False)
    created_at = Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False)
    processed_at = Column("processed_at", DateTime(timezone=True))
    processed_by_id = Column("processed_by_id", Uuid(as_uuid=False), ForeignKey("auth.users.id"))

    zone = relationship("Zone")


class Anomaly(Base):
    __tablename__ = "anomalies"
    __table_args__ = {"schema": "intelligence"}

    id = Column(Uuid(as_uuid=False), primary_key=True)
    zone_id = Column("zone_id", Uuid(as_uuid=False), ForeignKey("governance.zones.id"), nullable=False)
    source = Column(String)
    level = Column(String, nullable=False)
    title = Column(String, nullable=False)
    message = Column(Text)
    details = Column(JSON)
    is_resolved = Column("is_resolved", Boolean, default=False, nullable=False)
    resolved_by_id = Column("resolved_by_id", Uuid(as_uuid=False), ForeignKey("auth.users.id"))
    resolved_at = Column("resolved_at", DateTime(timezone=True))
    created_at = Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    zone = relationship("Zone")

    def to_dict(self):
        return {
            "id": self.id, "zone_id": self.zone_id, "level": self.level,
            "title": self.title, "is_resolved": self.is_resolved,
        }


class TrustScore(Base):
    __tablename__ = "trust_scores"
    __table_args__ = {"schema": "intelligence"}

    id = Column(Uuid(as_uuid=False), primary_key=True)
    user_id = Column("user_id", Uuid(as_uuid=False), ForeignKey("auth.users.id", ondelete="CASCADE"), unique=True, nullable=False)
    global_score = Column("global_score", Float, default=0.0, nullable=False)
    reliability_index = Column("reliability_index", Float, default=0.0, nullable=False)
    quality_index = Column("quality_index", Float, default=0.0, nullable=False)
    compliance_index = Column("compliance_index", Float, default=0.0, nullable=False)
    resilience_bonus = Column("resilience_bonus", Float, default=0.0, nullable=False)
    created_at = Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User", back_populates="trust_score")
    reasoning = relationship("AIRatingReasoning", back_populates="trust_score")

    def to_dict(self):
        return {
            "id": self.id, "user_id": self.user_id,
            "global_score": self.global_score,
            "reliability_index": self.reliability_index,
            "quality_index": self.quality_index, "compliance_index": self.compliance_index, "resilience_bonus": self.resilience_bonus,
        }


class AIRatingReasoning(Base):
    __tablename__ = "ai_rating_reasonings"
    __table_args__ = {"schema": "intelligence"}

    id = Column(Uuid(as_uuid=False), primary_key=True)
    trust_score_id = Column("trust_score_id", Uuid(as_uuid=False), ForeignKey("intelligence.trust_scores.id", ondelete="CASCADE"), nullable=False)
    agent_name = Column("agent_name", String, nullable=False)
    justification = Column(Text, nullable=False)
    data_points = Column("data_points", JSON, nullable=False)
    created_at = Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False)

    trust_score = relationship("TrustScore", back_populates="reasoning")


# ══════════════════════════════════════════════════════════════════
# MEMORY MODELS (imported for Base.metadata discovery)
# ══════════════════════════════════════════════════════════════════
try:
    from agriconnect.services.memory.user_profile import UserFarmProfileModel
    from agriconnect.services.memory.episodic_memory import EpisodicMemoryModel
except ImportError:
    pass
