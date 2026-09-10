from __future__ import annotations

from typing import Any, Mapping, Optional, Tuple

from pydantic import BaseModel, ConfigDict, field_validator

_EMPTY_SLOT_VALUES: Tuple[object, ...] = (None, "", [], {})


class FarmGetMyListPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "FarmGetMyListPayload":
        # No fields required. Phone comes from DomainContext
        return cls()


class FarmCreatePayload(BaseModel):
    farm_name: str
    zone: str
    surface: Optional[float] = None

    model_config = ConfigDict(extra="ignore")

    @field_validator("farm_name", mode="before")
    @classmethod
    def _normalize_name(cls, value: object) -> str:
        if value in _EMPTY_SLOT_VALUES:
            raise ValueError("farm_name is required")
        return str(value).strip()

    @field_validator("zone", mode="before")
    @classmethod
    def _normalize_zone(cls, value: object) -> str:
        if value in _EMPTY_SLOT_VALUES:
            raise ValueError("zone is required")
        return str(value).strip()

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "FarmCreatePayload":
        name = (payload or {}).get("farm_name") or (payload or {}).get("name")
        zone = (payload or {}).get("zone") or (payload or {}).get("zone_id")
        if name in _EMPTY_SLOT_VALUES:
            raise ValueError("farm_name is required")
        if zone in _EMPTY_SLOT_VALUES:
            raise ValueError("zone is required")
        surface_raw = (payload or {}).get("surface") or (payload or {}).get("area_size")
        surface_val: Optional[float] = None
        if surface_raw not in _EMPTY_SLOT_VALUES:
            try:
                surface_val = float(surface_raw)
            except (TypeError, ValueError):
                surface_val = None
        return cls(
            farm_name=str(name).strip(), zone=str(zone).strip(), surface=surface_val
        )


class FarmUpdatePayload(BaseModel):
    farm_id: str
    farm_name: Optional[str] = None
    surface: Optional[float] = None

    model_config = ConfigDict(extra="ignore")

    @field_validator("farm_id", mode="before")
    @classmethod
    def _normalize_id(cls, value: object) -> str:
        if value in _EMPTY_SLOT_VALUES:
            raise ValueError("farm_id is required")
        return str(value).strip()

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "FarmUpdatePayload":
        farm_id = (payload or {}).get("farm_id") or (payload or {}).get("id")
        if farm_id in _EMPTY_SLOT_VALUES:
            raise ValueError("farm_id is required")
        name = (payload or {}).get("farm_name") or (payload or {}).get("name")
        surface_raw = (payload or {}).get("surface") or (payload or {}).get("area_size")
        surface_val: Optional[float] = None
        if surface_raw not in _EMPTY_SLOT_VALUES:
            try:
                surface_val = float(surface_raw)
            except (TypeError, ValueError):
                surface_val = None
        return cls(
            farm_id=str(farm_id).strip(),
            farm_name=(str(name).strip() if name not in _EMPTY_SLOT_VALUES else None),
            surface=surface_val,
        )
