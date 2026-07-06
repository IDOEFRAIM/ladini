"""Action handlers for the Profile domain."""
from __future__ import annotations

from typing import Any, Dict, Mapping, Tuple

from agriconnect.graphs.agents.market_coach.registry import register_action
from agriconnect.graphs.agents.market_coach.actions.common import require, require_phone, normalize_quantity_to_kg

@register_action("PROFILE_GET_MCP_USER", mode="READ")
def prep_profile_get_mcp_user(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Prépare la résolution de profil par téléphone."""
    target_phone = str(require(payload, "phone"))
    return "get_user_by_phone", {"phone": target_phone}


@register_action("PROFILE_GET_TRUST", mode="READ")
def prep_profile_get_trust(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Prépare la consultation de la note de confiance.

    Outil MCP auto-enregistré : get_trust_score(user_id).
    Le résolveur de schéma mappe phone → user_id via _lookup_arg_value.
    """
    phone = require_phone(state)
    return "get_trust_score", {"user_id": phone}


@register_action("PROFILE_GET_CONTEXT", mode="READ")
def prep_profile_get_context(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Prépare la lecture du contexte conversationnel (Outil MCP : get_user_context).

    Auparavant manquant — l'absence faisait crasher le module au chargement.
    """
    phone = require_phone(state)
    return "get_user_context", {"user_id": phone}


@register_action("PROFILE_SET_GEO", mode="WRITE")
def prep_profile_set_geo(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    phone = require_phone(state)
    lat = float(require(payload, "latitude"))
    lon = float(require(payload, "longitude"))
    return "update_geo_location", {"user_id": phone, "lat": lat, "lon": lon}


@register_action("PROFILE_SET_PREFS", mode="WRITE")
def prep_profile_set_prefs(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    phone = require_phone(state)
    lang = str(require(payload, "language"))
    return "update_communication_prefs", {
        "user_id": phone,
        "advice_time": lang,
        "enabled": bool(payload.get("allow_voice", True)),
    }


@register_action("PROFILE_SWITCH_ROLE", mode="WRITE")
def prep_profile_switch_role(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    phone = require_phone(state)
    role = str(require(payload, "target_role")).upper().strip()
    return "create_agent_action", {
        "agent_name": "MarketCoach",
        "action_type": "PROFILE_SWITCH_ROLE",
        "payload": {"phone": phone, "target_role": role},
    }
