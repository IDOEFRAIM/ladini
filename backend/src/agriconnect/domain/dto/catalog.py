"""DTO catalogue : Farm, MarketOffer, Product. Alignés 1-pour-1 sur le schéma."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import field_validator

from agriconnect.domain.base_model import BaseMarketplaceModel


class FarmModel(BaseMarketplaceModel):
    """↔ marketplace.farms"""

    id: Optional[str] = None
    name: str
    location: Optional[str] = None
    size: Optional[float] = None
    producer_id: str
    zone_id: Optional[str] = None


class MarketOfferModel(BaseMarketplaceModel):
    """↔ marketplace.market_offers (ex crop_cycles)"""

    id: Optional[str] = None
    producer_id: str
    farm_id: Optional[str] = None
    sub_category_id: Optional[str] = None

    product_label: str
    production_type: str = "CROP"
    species: Optional[str] = None
    breed: Optional[str] = None

    unit: str = "KG"
    price_per_unit: Optional[Decimal] = None
    available_quantity: Decimal = Decimal("0")
    reserved_quantity: Decimal = Decimal("0")
    current_stock: Decimal = Decimal("0")

    is_public: bool = False
    preorder_enabled: bool = False
    estimated_available_at: Optional[datetime] = None
    expected_harvest_date: Optional[datetime] = None
    status: str = "DRAFT"

    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @field_validator("price_per_unit", "available_quantity", "reserved_quantity", "current_stock")
    @classmethod
    def _positive(cls, v):
        return cls._non_negative(v)


class ProductModel(BaseMarketplaceModel):
    """↔ marketplace.products"""

    id: Optional[str] = None
    short_code: Optional[str] = None
    name: str = "Produit"
    category_label: str
    sub_category_id: Optional[str] = None
    description: Optional[str] = None
    price: Decimal
    unit: str = "KG"
    quantity_for_sale: Decimal = Decimal("0")
    images: list[str] = []
    quality_class: Optional[str] = None
    packaging_type: Optional[str] = None
    harvest_date: Optional[datetime] = None
    is_available: bool = True
    producer_id: str
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @field_validator("price", "quantity_for_sale")
    @classmethod
    def _positive(cls, v):
        return cls._non_negative(v)
