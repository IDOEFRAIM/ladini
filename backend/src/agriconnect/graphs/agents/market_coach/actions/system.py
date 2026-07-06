"""Action handlers for the System domain."""
from __future__ import annotations

from typing import Any, Dict, Mapping, Tuple

from agriconnect.graphs.agents.market_coach.registry import register_action
from agriconnect.graphs.agents.market_coach.actions.common import require, require_phone, normalize_quantity_to_kg

@register_action("SYSTEM_GET_PENDING", mode="READ")
def prep_system_get_pending(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Prépare la consultation des actions en attente.

    Outil MCP : get_pending_actions(agent_name?, limit?).
    ATTENTION : le MCP n'utilise PAS phone. On passe le nom de l'agent.
    """
    require_phone(state)  # safety — user must be authenticated
    return "get_pending_actions", {"agent_name": "MarketCoach"}


@register_action("SYSTEM_REPORT_ANOMALY", mode="WRITE")
def prep_system_report_anomaly(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    require_phone(state)
    zone = str(payload.get("zone") or payload.get("zone_name") or "")
    description = str(require(payload, "description"))
    return "report_anomaly", {"zone_id": zone, "title": description[:80], "level": "MEDIUM"}


@register_action("SYSTEM_BIND_ZONE", mode="WRITE")
def prep_system_bind_zone(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    phone = require_phone(state)
    zone = str(require(payload, "zone"))
    return "create_agent_action", {
        "agent_name": "MarketCoach",
        "action_type": "SYSTEM_BIND_ZONE",
        "payload": {"phone": phone, "zone_name": zone},
    }


@register_action("SYSTEM_COMMIT_TRANSACTION", mode="WRITE")
def prep_system_commit_transaction(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    require_phone(state)
    staging_id = str(require(payload, "staging_id"))
    return "commit_staged_transaction", {"transaction_id": staging_id, "approved": True}
