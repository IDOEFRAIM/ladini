"""Unified identity resolution for all AgriConnect agents.

Eliminates the duplicated phone-first / user_id fallback pattern
found in both Formation (load_profile_node) and Market (onboarding_node).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .gateway import DataGateway

logger = logging.getLogger("AgriConnect.Agents.Identity")


@dataclass
class UserIdentity:
    """Resolved user identity — the canonical representation."""

    user_id: Optional[str] = None
    phone: Optional[str] = None
    name: Optional[str] = None
    role: Optional[str] = None
    zone_name: Optional[str] = None
    zone_id: Optional[str] = None
    raw_profile: Dict[str, Any] = field(default_factory=dict)
    farms: List[Dict[str, Any]] = field(default_factory=list)
    declared_crops: List[str] = field(default_factory=list)
    is_new_user: bool = False
    resolved: bool = False

    @property
    def has_identity(self) -> bool:
        return bool(self.phone or self.user_id)

    def to_state_dict(self) -> Dict[str, Any]:
        """Export identity fields for merging into agent state."""
        return {
            "user_id": self.user_id,
            "user_phone": self.phone,
            "user_name": self.name,
            "user_role": self.role,
            "zone_name": self.zone_name,
            "zone_id": self.zone_id,
            "user_farms_cache": self.farms if self.farms else None,
            "declared_crops": self.declared_crops,
            "user_context_loaded": self.resolved,
        }


def extract_phone_from_state(state: Dict[str, Any]) -> Optional[str]:
    """Extract phone number from any known state location."""
    candidates = [
        state.get("user_phone"),
        state.get("phone"),
        state.get("phone_number"),
        (state.get("learner_profile") or {}).get("phone"),
        (state.get("learner_profile") or {}).get("user_phone"),
        (state.get("learner_profile") or {}).get("whatsapp"),
        (state.get("transaction_payload") or {}).get("phone"),
    ]
    for c in candidates:
        if c and str(c).strip():
            return str(c).strip()
    return None


def extract_user_id_from_state(state: Dict[str, Any]) -> Optional[str]:
    """Extract user_id from any known state location."""
    candidates = [
        state.get("user_id"),
        state.get("db_user_id"),
        (state.get("learner_profile") or {}).get("user_id"),
        (state.get("learner_profile") or {}).get("id"),
    ]
    for c in candidates:
        if c and str(c).strip():
            return str(c).strip()
    return None


async def resolve_identity(
    gateway: DataGateway,
    *,
    phone: Optional[str] = None,
    user_id: Optional[str] = None,
    fetch_farms: bool = True,
    fetch_crops: bool = True,
) -> UserIdentity:
    """Resolve a user's full identity from DB using phone-first strategy.

    This is THE canonical identity resolution used by all agents.
    """
    identity = UserIdentity(phone=phone, user_id=user_id)

    if not identity.has_identity:
        identity.is_new_user = True
        return identity

    if not gateway.is_available:
        logger.info("resolve_identity: no data backend — returning bare identity")
        return identity

    # --- Step 1: Phone-first profile resolution ---
    if phone:
        try:
            user_data = await gateway.call("get_user_by_phone", phone=phone)
            if user_data:
                _apply_profile(identity, user_data)
                identity.resolved = True
            else:
                identity.is_new_user = True
        except Exception as exc:
            logger.warning("resolve_identity: get_user_by_phone failed: %s", exc)

    elif user_id:
        try:
            user_data = await gateway.call("get_user_by_id", user_id=user_id)
            if user_data:
                _apply_profile(identity, user_data)
                identity.resolved = True
            else:
                identity.is_new_user = True
        except Exception as exc:
            logger.warning("resolve_identity: get_user_by_id failed: %s", exc)

    # --- Step 2: Fetch farms ---
    if fetch_farms and identity.resolved:
        identity.farms = await _fetch_farms(gateway, identity)

    # --- Step 3: Infer declared crops from stocks ---
    if fetch_crops and identity.farms:
        identity.declared_crops = await _fetch_crops(gateway, identity.farms)

    return identity


async def _fetch_farms(
    gateway: DataGateway, identity: UserIdentity
) -> List[Dict[str, Any]]:
    """Fetch user farms with phone-first, user_id fallback."""
    farms: List[Dict[str, Any]] = []

    if identity.phone:
        try:
            result = await gateway.call("get_producer_farm", phone=identity.phone)
            if isinstance(result, list):
                farms = result
        except Exception as exc:
            logger.debug("_fetch_farms: get_producer_farm failed: %s", exc)

    if not farms and identity.user_id:
        try:
            result = await gateway.call("get_farm_stocks", user_id=identity.user_id)
            if isinstance(result, list):
                farms = result
        except Exception as exc:
            logger.debug("_fetch_farms: get_farm_stocks failed: %s", exc)

    return farms


async def _fetch_crops(gateway: DataGateway, farms: List[Dict[str, Any]]) -> List[str]:
    """Infer declared crops from farm stocks."""
    crops: List[str] = []
    for plot in farms:
        farm_id = plot.get("id") or plot.get("farm_id")
        if not farm_id:
            continue
        try:
            stocks = await gateway.call("get_stocks", farm_id=farm_id)
            if isinstance(stocks, list):
                for s in stocks:
                    crop = s.get("product_name") or s.get("crop_type") or ""
                    if crop and crop not in crops:
                        crops.append(crop)
        except Exception:
            pass
    return crops


def _apply_profile(identity: UserIdentity, data: Dict[str, Any]) -> None:
    """Apply DB profile data to identity object."""
    identity.raw_profile = data
    identity.user_id = str(data.get("id") or identity.user_id or "")
    identity.phone = data.get("phone") or identity.phone
    identity.name = data.get("name") or identity.name
    identity.role = data.get("role") or identity.role

    zone = data.get("zone") or {}
    if isinstance(zone, dict):
        identity.zone_name = zone.get("name") or data.get("zone_name")
        identity.zone_id = str(zone.get("id")) if zone.get("id") else None
    else:
        identity.zone_name = data.get("zone_name") or str(zone)
