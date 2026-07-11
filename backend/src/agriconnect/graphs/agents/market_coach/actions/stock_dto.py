from __future__ import annotations

from typing import Any, Mapping, Optional, Tuple

from pydantic import BaseModel, ConfigDict, field_validator

from agriconnect.graphs.agents.market_coach.actions.common import (
    coalesce_entity_value,
    to_float,
)


_EMPTY_SLOT_VALUES: Tuple[object, ...] = (None, "", [], {})


class StockUpdateLevelPayload(BaseModel):
    """Typed payload for the STOCK_UPDATE_LEVEL intent.

    This model is the application-layer DTO sitting at the boundary
    between the raw graph payload/state and the domain command.
    """

    stock_id: str
    quantity: float
    unit: Optional[str] = None
    reason: Optional[str] = None

    model_config = ConfigDict(extra="ignore")

    @field_validator("stock_id", mode="before")
    @classmethod
    def _normalize_stock_id(cls, value: object) -> str:
        if value is None:
            raise ValueError("stock_id is required")
        return str(value).strip()

    @field_validator("reason", mode="before")
    @classmethod
    def _normalize_reason(cls, value: object) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @classmethod
    def from_state_and_payload(
        cls,
        *,
        payload: Mapping[str, Any],
        entity: Mapping[str, Any],
    ) -> "StockUpdateLevelPayload":
        """Build a DTO by coalescing values from payload and current entity.

        This concentrates all extraction/normalisation logic for the
        STOCK_UPDATE_LEVEL intent in a single, typed place.
        """

        stock_id = coalesce_entity_value(
            payload,
            entity,
            payload_keys=("stock_id",),
            entity_keys=("stock_id", "id"),
        )
        if not stock_id:
            raise ValueError("Impossible d'identifier le lot de stock à modifier.")

        quantity_raw = coalesce_entity_value(
            payload,
            entity,
            payload_keys=("quantity",),
            entity_keys=("quantity", "quantity_for_sale"),
        )
        quantity_value = to_float(quantity_raw, field="quantity")
        if quantity_value is None:
            raise ValueError("Aucune nouvelle quantité fournie pour la mise à jour du lot.")

        unit_value = coalesce_entity_value(
            payload,
            entity,
            payload_keys=("unit",),
            entity_keys=("unit",),
        )

        return cls(
            stock_id=str(stock_id),
            quantity=quantity_value,
            unit=str(unit_value) if unit_value not in _EMPTY_SLOT_VALUES else None,
            reason=payload.get("reason"),
        )
