"""Profile & farm loading — extracted from input_normalizer.

Provides typed helpers for loading user profiles and pre-caching farms
via the MCP gateways, keeping the normalizer node thin.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

from ladini.graphs.agents.market_coach.services.mcp.gateway import (
    FarmGateway,
    ProfileGateway,
)

logger = logging.getLogger("Ladini.Market.ProfileLoader")


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

    # Cold-start : au TOUT premier message, le pool DB est froid — la 1re requête
    # peut échouer/timeouter (résultat vide/erreur), la 2e passe. On retente une
    # fois DANS le même tour pour que le transitoire se répare seul, au lieu
    # d'obliger l'utilisateur à réécrire. On ne retente PAS un NEW_USER ni un
    # profil déjà résolu (résultats décisifs).
    res_dict: Dict[str, Any] = {}
    status = ""
    profile: Any = None
    is_real_profile = False
    for attempt in range(2):
        raw = await profile_gw.get_user_by_phone(str(phone))
        res_dict = raw if isinstance(raw, dict) else {}
        status = str(res_dict.get("status", "")).upper()
        profile = res_dict.get("data")
        is_real_profile = (
            status == "SUCCESS"
            and isinstance(profile, dict)
            and bool(profile.get("id"))
        )
        decisive = is_real_profile or status == "NEW_USER"
        if decisive or attempt == 1:
            break
        logger.warning(
            "[ProfileLoader] Résolution indécise (status=%s) pour %s — nouvelle tentative (cold-start).",
            status or "(vide)",
            _mask_phone(phone),
        )
        await asyncio.sleep(0.4)

    updates: Dict[str, Any] = {}

    if is_real_profile:
        updates["user_name"] = profile.get("name") or "Client"
        zone = profile.get("zone") or {}
        updates["zone_name"] = zone.get("name") if isinstance(zone, dict) else None
        updates["zone_id"] = zone.get("id") if isinstance(zone, dict) else None
        updates["user_role"] = profile.get("role") or "PRODUCER"
        updates["user_context_loaded"] = True
        updates["is_onboarding"] = False
        updates["onboarding_step"] = None
        updates["onboarding_internal_step"] = None
        updates["user_id"] = str(profile.get("id"))

    elif status == "NEW_USER":
        # Aucun compte : parcours d'inscription.
        updates["user_context_loaded"] = False
        updates["is_onboarding"] = True
        updates["_new_user"] = True

    else:
        # SUCCESS avec data vide/None, ou statut d'erreur : échec technique de
        # résolution (ex: schéma non migré, DB indisponible). On NE simule pas
        # un utilisateur et on NE propose pas de transaction : on remonte l'échec.
        logger.warning(
            "[ProfileLoader] Résolution de profil non aboutie (status=%s, data_vide=%s) pour %s",
            status or "(vide)",
            profile in (None, {}, ""),
            _mask_phone(phone),
        )
        updates["user_context_loaded"] = False
        updates["_profile_unavailable"] = True

    return updates


async def preload_farms(phone: str, mc_runtime: Any) -> List[Dict[str, Any]]:
    """Pre-cache the user's farms list via the FarmGateway."""
    farm_gw = FarmGateway(mc_runtime)
    return await farm_gw.list_farms(str(phone))


__all__ = ["load_user_profile", "preload_farms", "_mask_phone"]
