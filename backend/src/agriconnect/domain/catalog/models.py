"""ORM Catalog — marketplace.warehouses/farms/market_offers/stocks/products.

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
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import relationship

from agriconnect.domain.orm_base import Base, _uuid4


class Warehouse(Base):
    __tablename__ = "warehouses"
    __table_args__ = (
        Index("warehouses_zone_idx", "zone_id"),
        {"schema": "marketplace"},
    )

    id = Column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    name = Column(String, nullable=False)
    type = Column(String, nullable=False)
    capacity = Column(Float)
    location = Column(String)
    zone_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id"))
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


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
    producer_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("marketplace.producers.id"), nullable=False
    )
    zone_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id"))
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

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

    id = Column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    producer_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("marketplace.producers.id"), nullable=False
    )
    farm_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.farms.id"))
    sub_category_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("governance.sub_categories.id")
    )

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
    updated_at = Column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    producer = relationship("Producer", back_populates="offers")
    farm = relationship("Farm", back_populates="offers")
    orders = relationship("Order", back_populates="offer")


# Backward-compat alias (call sites migrent progressivement vers MarketOffer)
CropCycle = MarketOffer


class Stock(Base):
    __tablename__ = "stocks"
    __table_args__ = {"schema": "marketplace"}

    id = Column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    farm_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("marketplace.farms.id"), index=True
    )
    warehouse_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("marketplace.warehouses.id"), index=True
    )
    verified_by_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("auth.users.id"), index=True
    )
    organization_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("governance.organizations.id")
    )
    item_name = Column(String, nullable=False)
    quantity = Column(Numeric(14, 3), default=0, nullable=False)
    unit = Column(String, default="KG", nullable=False)
    type = Column(String, default="HARVEST", nullable=False)
    verified_at = Column(DateTime)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    farm = relationship("Farm", back_populates="stocks")


class StockMovement(Base):
    __tablename__ = "stock_movements"
    __table_args__ = (
        Index("stock_movements_stock_idx", "stock_id"),
        Index("stock_movements_created_idx", "created_at"),
        {"schema": "marketplace"},
    )

    id = Column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    stock_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("marketplace.stocks.id"), nullable=False
    )
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

    id = Column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    stock_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("marketplace.stocks.id"), nullable=False
    )
    organization_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("governance.organizations.id"), nullable=False
    )
    batch_number = Column(String, unique=True, nullable=False)
    origin_farm_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.farms.id"))
    quantity = Column(Numeric(14, 3), nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Expense(Base):
    __tablename__ = "expenses"
    __table_args__ = (
        Index("expenses_farm_idx", "farm_id"),
        Index("expenses_category_idx", "category"),
        Index("expenses_date_idx", "date"),
        {"schema": "marketplace"},
    )

    id = Column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    farm_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("marketplace.farms.id"), nullable=False
    )
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
    sub_category_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("governance.sub_categories.id")
    )
    local_names = Column(JSONB)
    description = Column(Text)
    price = Column(Numeric(12, 2), nullable=False)
    unit = Column(String, default="KG", nullable=False)
    quantity_for_sale = Column(Numeric(14, 3), default=0, nullable=False)
    # Déclinaisons de prix/conditionnement pour UN MÊME produit (2026-08-27) :
    # ex. "500f le demi-litre en sachet et 600f le bidon" — deux tarifs
    # distincts, jamais fusionnables dans les colonnes plates ci-dessus.
    # Liste de {"quantity": float, "unit": str, "price": float,
    # "packaging": str|null} — `unit` est TOUJOURS la valeur LITTÉRALE saisie
    # par l'utilisateur (jamais passée par `_CANONICAL_UNIT_MAP`/
    # `normalize_quantity_to_kg`, voir services/domain/quantity_unit.py).
    # `price`/`unit`/`quantity_for_sale` ci-dessus restent renseignés avec le
    # PREMIER tier (compatibilité avec tout code existant qui ne connaît pas
    # encore `pricing_tiers`). NULL = produit à tarif unique (comportement
    # historique inchangé).
    pricing_tiers = Column(JSONB, nullable=True)
    images = Column(
        PG_ARRAY(String), nullable=False, server_default=text("'{}'::text[]")
    )
    audio_url = Column(String)
    quality_class = Column(String)
    min_order_quality = Column(String)
    packaging_type = Column(String)
    harvest_date = Column(DateTime)
    is_available = Column(Boolean, default=True, nullable=False)
    producer_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("marketplace.producers.id"), nullable=False
    )
    verified_at = Column(DateTime)
    verified_by_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id"))
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    producer = relationship("Producer", backref="products")
    sub_category = relationship(
        "SubCategory", primaryjoin="Product.sub_category_id == SubCategory.id"
    )


__all__ = [
    "Warehouse",
    "Farm",
    "MarketOffer",
    "CropCycle",
    "Stock",
    "StockMovement",
    "Batch",
    "Expense",
    "Product",
]
