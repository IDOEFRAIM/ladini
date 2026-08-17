from __future__ import annotations

from typing import Any, Mapping, Optional, Tuple

from pydantic import BaseModel, ConfigDict, field_validator

_EMPTY_SLOT_VALUES: Tuple[object, ...] = (None, "", [], {})


class ProcurementCreateRequestPayload(BaseModel):
    product: str
    quantity: float
    price: float
    unit: Optional[str] = None
    zone_name: Optional[str] = None
    deadline: Optional[object] = None  # parsed in domain (str | datetime acceptable)
    delivery_location: Optional[str] = None
    delivery_deadline: Optional[object] = None  # parsed in domain
    incoterm: Optional[str] = "DDP"
    auto_extend: Optional[bool] = True

    model_config = ConfigDict(extra="ignore")

    @field_validator("product", mode="before")
    @classmethod
    def _normalize_product(cls, value: object) -> str:
        if value in _EMPTY_SLOT_VALUES:
            raise ValueError("product is required")
        return str(value).strip()

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, Any]
    ) -> "ProcurementCreateRequestPayload":
        product_raw = payload.get("product")
        if product_raw in _EMPTY_SLOT_VALUES:
            raise ValueError("Le produit recherché est requis pour créer une demande.")

        qty_raw = payload.get("quantity")
        if qty_raw in _EMPTY_SLOT_VALUES:
            raise ValueError(
                "La quantité recherchée est requise pour créer une demande."
            )

        price_raw = payload.get("price")
        if price_raw in _EMPTY_SLOT_VALUES:
            raise ValueError(
                "Le prix plafond proposé est requis pour créer une demande."
            )

        unit_raw = payload.get("unit")

        zone_raw = payload.get("zone_name") or payload.get("zone")

        return cls(
            product=str(product_raw).strip(),
            quantity=float(qty_raw),
            price=float(price_raw),
            unit=(
                str(unit_raw).strip().upper()
                if unit_raw not in _EMPTY_SLOT_VALUES
                else None
            ),
            zone_name=(
                str(zone_raw).strip() if zone_raw not in _EMPTY_SLOT_VALUES else None
            ),
            deadline=payload.get("deadline"),
            delivery_location=(
                str(payload.get("delivery_location")).strip()
                if payload.get("delivery_location") not in _EMPTY_SLOT_VALUES
                else None
            ),
            delivery_deadline=payload.get("delivery_deadline"),
            incoterm=(
                str(payload.get("incoterm")).strip().upper()
                if payload.get("incoterm") not in _EMPTY_SLOT_VALUES
                else "DDP"
            ),
            auto_extend=(
                bool(payload.get("auto_extend"))
                if payload.get("auto_extend") not in _EMPTY_SLOT_VALUES
                else True
            ),
        )


class ProcurementSelectWinnerPayload(BaseModel):
    auction_id: str
    bid_id: str

    model_config = ConfigDict(extra="ignore")

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, Any]
    ) -> "ProcurementSelectWinnerPayload":
        auction_id = payload.get("auction_id")
        bid_id = payload.get("bid_id")
        if auction_id in _EMPTY_SLOT_VALUES:
            raise ValueError("auction_id is required")
        if bid_id in _EMPTY_SLOT_VALUES:
            raise ValueError("bid_id is required")
        return cls(auction_id=str(auction_id), bid_id=str(bid_id))


class ProcurementAcceptOfferPayload(BaseModel):
    bid_id: str

    model_config = ConfigDict(extra="ignore")

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, Any]
    ) -> "ProcurementAcceptOfferPayload":
        bid_id = payload.get("bid_id")
        if bid_id in _EMPTY_SLOT_VALUES:
            raise ValueError("bid_id is required")
        return cls(bid_id=str(bid_id))
