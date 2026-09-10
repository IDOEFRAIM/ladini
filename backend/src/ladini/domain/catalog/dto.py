"""DTOs du contexte Catalog — frontière API + Tools injectés dans l'Agent IA."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import Field, field_validator

from ladini.domain.base_model import BaseMarketplaceModel


class FarmDTO(BaseMarketplaceModel):
    """↔ marketplace.farms"""

    id: Optional[str] = Field(
        default=None, description="Identifiant de l'exploitation."
    )
    name: str = Field(description="Nom de l'exploitation agricole.")
    location: Optional[str] = Field(
        default=None, description="Localisation textuelle (village, quartier)."
    )
    size: Optional[float] = Field(default=None, description="Superficie en hectares.")
    producer_id: str = Field(
        description="Identifiant du producteur propriétaire de cette exploitation."
    )
    zone_id: Optional[str] = Field(
        default=None, description="Zone géographique de rattachement."
    )


class MarketOfferDTO(BaseMarketplaceModel):
    """↔ marketplace.market_offers — offre immédiate ou production future."""

    id: Optional[str] = Field(default=None, description="Identifiant de l'offre.")
    producer_id: str = Field(
        description="Identifiant du producteur qui publie cette offre."
    )
    farm_id: Optional[str] = Field(
        default=None, description="Exploitation d'origine de cette offre."
    )
    sub_category_id: Optional[str] = Field(
        default=None, description="Sous-catégorie de produit (référence)."
    )

    product_label: str = Field(
        description="Nom du produit ou de l'espèce/race déclarée (ex: 'poussins', 'maïs')."
    )
    production_type: str = Field(
        default="CROP", description="CROP (culture) ou LIVESTOCK (élevage)."
    )
    species: Optional[str] = Field(
        default=None, description="Espèce précise, si applicable."
    )
    breed: Optional[str] = Field(default=None, description="Race précise, si élevage.")

    unit: str = Field(
        default="KG", description="Unité de mesure : KG, TONNE, SAC, TETE, UNITE..."
    )
    price_per_unit: Optional[Decimal] = Field(
        default=None, description="Prix proposé par unité, en FCFA."
    )
    available_quantity: Decimal = Field(
        default=Decimal("0"), description="Quantité totale annoncée disponible à terme."
    )
    reserved_quantity: Decimal = Field(
        default=Decimal("0"), description="Quantité déjà réservée par des précommandes."
    )
    current_stock: Decimal = Field(
        default=Decimal("0"),
        description="Quantité physiquement disponible immédiatement (0 si production future).",
    )

    is_public: bool = Field(
        default=False,
        description="True si l'offre est visible dans le catalogue public.",
    )
    preorder_enabled: bool = Field(
        default=False,
        description="True si les acheteurs peuvent précommander cette offre.",
    )
    estimated_available_at: Optional[datetime] = Field(
        default=None,
        description="Date estimée de disponibilité (OBLIGATOIRE si la production n'est pas encore prête).",
    )
    expected_harvest_date: Optional[datetime] = Field(
        default=None, description="Date prévue de récolte physique."
    )
    status: str = Field(
        default="DRAFT", description="DRAFT | AVAILABLE | SOLD_OUT | CLOSED"
    )

    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @field_validator(
        "price_per_unit", "available_quantity", "reserved_quantity", "current_stock"
    )
    @classmethod
    def _positive(cls, v):
        return cls._non_negative(v)


class ProductDTO(BaseMarketplaceModel):
    """↔ marketplace.products — produit du catalogue public, vente immédiate."""

    id: Optional[str] = Field(default=None, description="Identifiant du produit.")
    short_code: Optional[str] = Field(
        default=None, description="Code court affiché à l'acheteur (ex: #A1B2C3)."
    )
    name: str = Field(
        default="Produit", description="Nom du produit affiché au catalogue."
    )
    category_label: str = Field(
        description="Libellé de catégorie (ex: 'Céréales', 'Légumes')."
    )
    sub_category_id: Optional[str] = Field(
        default=None, description="Sous-catégorie précise (référence)."
    )
    description: Optional[str] = Field(
        default=None, description="Description libre du produit."
    )
    price: Decimal = Field(description="Prix unitaire de vente, en FCFA.")
    unit: str = Field(default="KG", description="Unité de mesure.")
    quantity_for_sale: Decimal = Field(
        default=Decimal("0"), description="Quantité actuellement disponible à la vente."
    )
    images: list[str] = Field(
        default_factory=list, description="URLs des photos du produit."
    )
    quality_class: Optional[str] = Field(
        default=None, description="Classe de qualité (ex: 'Extra', 'Standard')."
    )
    packaging_type: Optional[str] = Field(
        default=None, description="Type de conditionnement (sac, panier, vrac...)."
    )
    harvest_date: Optional[datetime] = Field(
        default=None, description="Date de récolte effective."
    )
    is_available: bool = Field(
        default=True,
        description="True si le produit est actuellement disponible à la vente.",
    )
    producer_id: str = Field(description="Identifiant du producteur vendeur.")
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @field_validator("price", "quantity_for_sale")
    @classmethod
    def _positive(cls, v):
        return cls._non_negative(v)


__all__ = ["FarmDTO", "MarketOfferDTO", "ProductDTO"]
