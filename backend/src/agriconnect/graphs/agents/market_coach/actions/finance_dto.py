from __future__ import annotations

from typing import Any, Mapping, Optional, Tuple

from pydantic import BaseModel, ConfigDict, field_validator

from agriconnect.graphs.agents.market_coach.actions.common import to_float


_EMPTY_SLOT_VALUES: Tuple[object, ...] = (None, "", [], {})


class FinanceGetSummaryPayload(BaseModel):
    farm_id: str
    days: Optional[int] = None

    model_config = ConfigDict(extra="ignore")

    @field_validator("farm_id", mode="before")
    @classmethod
    def _normalize_farm_id(cls, value: object) -> str:
        if value in _EMPTY_SLOT_VALUES:
            raise ValueError("farm_id is required")
        return str(value).strip()

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "FinanceGetSummaryPayload":
        farm_id = payload.get("farm_id")
        if farm_id in _EMPTY_SLOT_VALUES:
            raise ValueError("farm_id is required")
        days_raw = payload.get("days")
        days_val: Optional[int] = None
        if days_raw not in _EMPTY_SLOT_VALUES:
            try:
                days_val = int(days_raw)
            except (TypeError, ValueError):
                days_val = None
        return cls(farm_id=str(farm_id), days=days_val)


class FinanceLogExpensePayload(BaseModel):
    farm_id: str
    amount: float
    label: Optional[str] = None
    category: Optional[str] = None

    model_config = ConfigDict(extra="ignore")

    @field_validator("farm_id", mode="before")
    @classmethod
    def _normalize_farm_id2(cls, value: object) -> str:
        if value in _EMPTY_SLOT_VALUES:
            raise ValueError("farm_id is required")
        return str(value).strip()

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "FinanceLogExpensePayload":
        farm_id = payload.get("farm_id")
        if farm_id in _EMPTY_SLOT_VALUES:
            raise ValueError("farm_id is required")

        # amount from price or amount
        amount_raw = payload.get("price")
        if amount_raw in _EMPTY_SLOT_VALUES:
            amount_raw = payload.get("amount")
        amount = to_float(amount_raw, field="amount")
        if amount is None:
            raise ValueError("amount is required")

        label_raw = payload.get("label")
        if label_raw in _EMPTY_SLOT_VALUES:
            label_raw = payload.get("product")
        label_val = None if label_raw in _EMPTY_SLOT_VALUES else str(label_raw).strip()

        category_raw = payload.get("category")
        category_val = None if category_raw in _EMPTY_SLOT_VALUES else str(category_raw).strip().upper()

        return cls(
            farm_id=str(farm_id),
            amount=amount,
            label=label_val,
            category=category_val,
        )
