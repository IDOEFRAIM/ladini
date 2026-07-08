from __future__ import annotations

from typing import Any, Mapping, Optional, Tuple

from pydantic import BaseModel, ConfigDict, field_validator


_EMPTY_SLOT_VALUES: Tuple[object, ...] = (None, "", [], {})


class SystemGetPendingPayload(BaseModel):
    limit: Optional[int] = None

    model_config = ConfigDict(extra="ignore")

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SystemGetPendingPayload":
        raw = (payload or {}).get("limit")
        value: Optional[int] = None
        if raw not in _EMPTY_SLOT_VALUES:
            try:
                value = max(1, int(raw))
            except (TypeError, ValueError):
                value = None
        return cls(limit=value)


class SystemReportAnomalyPayload(BaseModel):
    description: str
    zone: Optional[str] = None
    anomaly_type: Optional[str] = None
    target_id: Optional[str] = None

    model_config = ConfigDict(extra="ignore")

    @field_validator("description", mode="before")
    @classmethod
    def _normalize_description(cls, value: object) -> str:
        if value in _EMPTY_SLOT_VALUES:
            raise ValueError("description is required")
        return str(value).strip()

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SystemReportAnomalyPayload":
        description = (payload or {}).get("description")
        if description in _EMPTY_SLOT_VALUES:
            raise ValueError("description is required")
        zone_val = (payload or {}).get("zone") or (payload or {}).get("zone_name")
        anomaly_type = (payload or {}).get("anomaly_type")
        target_id = (payload or {}).get("target_id")
        return cls(
            description=str(description).strip(),
            zone=(str(zone_val).strip() if zone_val not in _EMPTY_SLOT_VALUES else None),
            anomaly_type=(str(anomaly_type).strip() if anomaly_type not in _EMPTY_SLOT_VALUES else None),
            target_id=(str(target_id).strip() if target_id not in _EMPTY_SLOT_VALUES else None),
        )


class SystemBindZonePayload(BaseModel):
    zone: str

    model_config = ConfigDict(extra="ignore")

    @field_validator("zone", mode="before")
    @classmethod
    def _normalize_zone(cls, value: object) -> str:
        if value in _EMPTY_SLOT_VALUES:
            raise ValueError("zone is required")
        return str(value).strip()

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SystemBindZonePayload":
        zone = (payload or {}).get("zone")
        if zone in _EMPTY_SLOT_VALUES:
            raise ValueError("zone is required")
        return cls(zone=str(zone).strip())


class SystemCommitTransactionPayload(BaseModel):
    staging_id: str

    model_config = ConfigDict(extra="ignore")

    @field_validator("staging_id", mode="before")
    @classmethod
    def _normalize_staging_id(cls, value: object) -> str:
        if value in _EMPTY_SLOT_VALUES:
            raise ValueError("staging_id is required")
        return str(value).strip()

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SystemCommitTransactionPayload":
        staging_id = (payload or {}).get("staging_id")
        if staging_id in _EMPTY_SLOT_VALUES:
            raise ValueError("staging_id is required")
        return cls(staging_id=str(staging_id).strip())
