from __future__ import annotations

from typing import Any, Mapping, Optional, Tuple

from pydantic import BaseModel, ConfigDict, field_validator


_EMPTY_SLOT_VALUES: Tuple[object, ...] = (None, "", [], {})


class ProfileGetMcpUserPayload(BaseModel):
    phone: str

    model_config = ConfigDict(extra="ignore")

    @field_validator("phone", mode="before")
    @classmethod
    def _normalize_phone(cls, value: object) -> str:
        if value in _EMPTY_SLOT_VALUES:
            raise ValueError("phone is required")
        return str(value).strip()

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ProfileGetMcpUserPayload":
        phone = payload.get("phone")
        if phone in _EMPTY_SLOT_VALUES:
            raise ValueError("phone is required")
        return cls(phone=str(phone))


class ProfileGetTrustPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ProfileGetTrustPayload":
        # No payload fields; phone comes from DomainContext
        return cls()


class ProfileGetContextPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ProfileGetContextPayload":
        # No payload fields; phone comes from DomainContext
        return cls()


class ProfileSetGeoPayload(BaseModel):
    latitude: float
    longitude: float

    model_config = ConfigDict(extra="ignore")

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ProfileSetGeoPayload":
        lat = payload.get("latitude")
        lon = payload.get("longitude")
        if lat in _EMPTY_SLOT_VALUES:
            raise ValueError("latitude is required")
        if lon in _EMPTY_SLOT_VALUES:
            raise ValueError("longitude is required")
        return cls(latitude=float(lat), longitude=float(lon))


class ProfileSetPrefsPayload(BaseModel):
    language: str
    allow_voice: Optional[bool] = True

    model_config = ConfigDict(extra="ignore")

    @field_validator("language", mode="before")
    @classmethod
    def _normalize_language(cls, value: object) -> str:
        if value in _EMPTY_SLOT_VALUES:
            raise ValueError("language is required")
        return str(value).strip()

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ProfileSetPrefsPayload":
        language = payload.get("language")
        if language in _EMPTY_SLOT_VALUES:
            raise ValueError("language is required")
        allow_voice = payload.get("allow_voice")
        allow_voice_val: Optional[bool]
        if allow_voice in _EMPTY_SLOT_VALUES:
            allow_voice_val = True
        else:
            allow_voice_val = bool(allow_voice)
        return cls(language=str(language).strip(), allow_voice=allow_voice_val)


class ProfileSwitchRolePayload(BaseModel):
    target_role: str

    model_config = ConfigDict(extra="ignore")

    @field_validator("target_role", mode="before")
    @classmethod
    def _normalize_role(cls, value: object) -> str:
        if value in _EMPTY_SLOT_VALUES:
            raise ValueError("target_role is required")
        return str(value).strip().upper()

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ProfileSwitchRolePayload":
        role = payload.get("target_role")
        if role in _EMPTY_SLOT_VALUES:
            raise ValueError("target_role is required")
        return cls(target_role=str(role).strip().upper())
