from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple

from pydantic import BaseModel, ConfigDict, field_validator

from ladini.graphs.agents.market_coach.actions.common import (
    coalesce_entity_value,
    to_float,
)

_EMPTY_SLOT_VALUES: Tuple[object, ...] = (None, "", [], {})


class SalesUpdateProductPayload(BaseModel):
    product_id: str
    price: Optional[float] = None
    quantity: Optional[float] = None
    name: Optional[str] = None
    unit: Optional[str] = None
    # (2026-08-30) permet d'éditer/remplacer les paliers d'un produit déjà
    # publié — jusqu'ici SALES_UPDATE_PRODUCT ne touchait QUE les champs
    # scalaires, sans aucun moyen de corriger `pricing_tiers` après coup.
    # Validé et structuré par `domain/pricing_tiers.py::validate_pricing_tiers`
    # au moment de l'écriture (voir `services/database/producer.py`), jamais
    # ici — cette couche ne fait que transporter la liste brute.
    pricing_tiers: Optional[List[Dict[str, Any]]] = None

    model_config = ConfigDict(extra="ignore")

    @field_validator("product_id", mode="before")
    @classmethod
    def _normalize_product_id(cls, value: object) -> str:
        if value is None:
            raise ValueError("product_id is required")
        return str(value).strip()

    @classmethod
    def from_state_and_payload(
        cls,
        *,
        payload: Mapping[str, Any],
        entity: Mapping[str, Any],
    ) -> "SalesUpdateProductPayload":
        product_id = coalesce_entity_value(
            payload,
            entity,
            payload_keys=("product_id",),
            entity_keys=("product_id", "id"),
        )
        if not product_id:
            raise ValueError("Impossible d'identifier le produit à modifier.")

        price_raw = coalesce_entity_value(
            payload,
            entity,
            payload_keys=("price",),
            entity_keys=("price", "price_fcfa", "unit_price"),
        )
        quantity_raw = coalesce_entity_value(
            payload,
            entity,
            payload_keys=("quantity",),
            entity_keys=("quantity_for_sale", "quantity", "qty"),
        )

        price_value = (
            to_float(price_raw, field="price")
            if price_raw not in _EMPTY_SLOT_VALUES
            else None
        )
        quantity_value = (
            to_float(quantity_raw, field="quantity")
            if quantity_raw not in _EMPTY_SLOT_VALUES
            else None
        )
        name_raw = coalesce_entity_value(
            payload,
            entity,
            payload_keys=("product", "name"),
            entity_keys=("name",),
        )
        unit_raw = coalesce_entity_value(
            payload,
            entity,
            payload_keys=("unit",),
            entity_keys=("unit",),
        )
        tiers_raw = coalesce_entity_value(
            payload,
            entity,
            payload_keys=("pricing_tiers",),
            entity_keys=("pricing_tiers",),
        )

        return cls(
            product_id=str(product_id),
            price=price_value,
            quantity=quantity_value,
            name=(
                str(name_raw).strip() if name_raw not in _EMPTY_SLOT_VALUES else None
            ),
            unit=(
                str(unit_raw).strip() if unit_raw not in _EMPTY_SLOT_VALUES else None
            ),
            pricing_tiers=(
                tiers_raw
                if isinstance(tiers_raw, list) and tiers_raw
                else None
            ),
        )


class SalesPublishProductPayload(BaseModel):
    product: str
    quantity: float
    unit: Optional[str] = None
    price: float
    description: Optional[str] = None
    category_label: Optional[str] = None
    # Déclinaisons de prix/conditionnement (2026-08-27) — voir
    # domain/catalog/models.py::Product.pricing_tiers. `unit` y reste
    # LITTÉRAL (jamais uppercasé/normalisé ici, contrairement au `unit`
    # racine ci-dessus) : ce champ n'existe QUE pour préserver exactement ce
    # que l'utilisateur a dit.
    pricing_tiers: Optional[List[Dict[str, Any]]] = None

    model_config = ConfigDict(extra="ignore")

    @field_validator("product", mode="before")
    @classmethod
    def _normalize_product(cls, value: object) -> str:
        if value is None:
            raise ValueError("product is required")
        return str(value).strip()

    @field_validator("description", "category_label", mode="before")
    @classmethod
    def _normalize_optional_str(cls, value: object) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SalesPublishProductPayload":
        product_raw = payload.get("product")
        if product_raw in _EMPTY_SLOT_VALUES:
            raise ValueError("Le nom du produit est requis pour la publication.")

        quantity_raw = payload.get("quantity") or payload.get("quantity_for_sale")
        quantity_value = to_float(quantity_raw, field="quantity")
        if quantity_value is None:
            raise ValueError("La quantité est requise pour la publication du produit.")

        price_raw = payload.get("price") or payload.get("unit_price")
        price_value = to_float(price_raw, field="price")
        if price_value is None:
            raise ValueError("Le prix est requis pour la publication du produit.")

        unit_value = payload.get("unit")

        raw_tiers = payload.get("pricing_tiers")
        clean_tiers: Optional[List[Dict[str, Any]]] = None
        if isinstance(raw_tiers, list) and raw_tiers:
            clean_tiers = [tier for tier in raw_tiers if isinstance(tier, dict)] or None

        return cls(
            product=str(product_raw),
            quantity=quantity_value,
            unit=str(unit_value).strip().upper()
            if unit_value not in _EMPTY_SLOT_VALUES
            else None,
            price=price_value,
            description=payload.get("description"),
            category_label=payload.get("category_label"),
            pricing_tiers=clean_tiers,
        )


class SalesRecordDirectPayload(BaseModel):
    product: str
    quantity: float
    unit: Optional[str] = None
    price: float

    model_config = ConfigDict(extra="ignore")

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SalesRecordDirectPayload":
        base = SalesPublishProductPayload.from_payload(payload)
        return cls(
            product=base.product,
            quantity=base.quantity,
            unit=base.unit,
            price=base.price,
        )


class MarketGetRequestsPayload(BaseModel):
    status: Optional[str] = None
    product_name: Optional[str] = None
    zone_name: Optional[str] = None

    model_config = ConfigDict(extra="ignore")

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "MarketGetRequestsPayload":
        status_raw = payload.get("status")
        product_raw = payload.get("product") or payload.get("product_name")
        zone_raw = payload.get("zone") or payload.get("zone_name")

        status = (
            str(status_raw).strip() if status_raw not in _EMPTY_SLOT_VALUES else None
        )

        return cls(
            status=status,
            product_name=(
                str(product_raw).strip()
                if product_raw not in _EMPTY_SLOT_VALUES
                else None
            ),
            zone_name=(
                str(zone_raw).strip() if zone_raw not in _EMPTY_SLOT_VALUES else None
            ),
        )


class SalesListOrdersPayload(BaseModel):
    status: Optional[str] = None
    limit: Optional[int] = None

    model_config = ConfigDict(extra="ignore")

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SalesListOrdersPayload":
        status_raw = payload.get("status") or payload.get("order_status")
        status = (
            str(status_raw).upper().strip()
            if status_raw not in _EMPTY_SLOT_VALUES
            else None
        )

        limit_raw = payload.get("limit") or payload.get("top")
        limit_val: Optional[int] = None
        if limit_raw not in _EMPTY_SLOT_VALUES:
            try:
                limit_val = max(1, int(limit_raw))
            except (TypeError, ValueError):
                limit_val = None

        return cls(status=status, limit=limit_val)
