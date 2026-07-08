"""Profile & farm loading — extracted from input_normalizer.

Provides typed helpers for loading user profiles and pre-caching farms
via the MCP gateways, keeping the normalizer node thin.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from agriconnect.graphs.agents.market_coach.services.mcp.gateway import (
    FarmGateway,
    ProfileGateway,
)

logger = logging.getLogger("AgriConnect.Market.ProfileLoader")


def _mask_phone(phone: Any) -> str:
    s = str(phone or "").strip()
    if len(s) >= 4:
        return f"***{s[-4:]}"
    return "***" if s else "(vide)"


async def load_user_profile(phone: str, mc_runtime: Any) -> Dict[str, Any]:
    """Call MCP to load a user profile and return a dict of state updates.

    Returns a dict with keys like ``user_name``, ``user_role``, ``user_id``,
    ``zone_name``, ``zone_id``, ``user_context_loaded``, and onboarding flags.
    On failure the dict contains ``user_context_loaded: False``.
    """
    profile_gw = ProfileGateway(mc_runtime)
    res_dict = await profile_gw.get_user_by_phone(str(phone))

    status = str(res_dict.get("status", "")).upper()
    updates: Dict[str, Any] = {}

    if status == "SUCCESS" and "data" in res_dict:
        profile = res_dict["data"] or {}
        updates["user_name"] = profile.get("name") or "N/A"
        updates["zone_name"] = profile.get("zone", {}).get("name")
        updates["zone_id"] = profile.get("zone", {}).get("id")
        updates["user_role"] = profile.get("role") or "PRODUCER"
        updates["user_context_loaded"] = True
        updates["is_onboarding"] = False
        updates["onboarding_step"] = None
        updates["onboarding_internal_step"] = None

        user_uuid = profile.get("id")
        if user_uuid:
            updates["user_id"] = str(user_uuid)
    elif status == "NEW_USER":
        updates["user_context_loaded"] = False
        updates["is_onboarding"] = True
        updates["_new_user"] = True
    else:
        logger.warning(
            "[ProfileLoader] Profil introuvable ou erreur status (%s) pour %s",
            status,
            _mask_phone(phone),
        )
        updates["user_context_loaded"] = False

    return updates


async def preload_farms(phone: str, mc_runtime: Any) -> List[Dict[str, Any]]:
    """Pre-cache the user's farms list via the FarmGateway."""
    farm_gw = FarmGateway(mc_runtime)
    return await farm_gw.list_farms(str(phone))


__all__ = ["load_user_profile", "preload_farms", "_mask_phone"]
