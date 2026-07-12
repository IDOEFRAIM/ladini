from typing import Any, Dict, Optional
from agriconnect.graphs.agents.market_coach.core.base import get_node_logger
from agriconnect.graphs.agents.market_coach.core.state import MarketAgentState

logger = get_node_logger("RoutingNodes")

# Tout statut de sécurité bloquant doit court-circuiter vers la stratégie
# (sinon la réponse de blocage est écrasée par l'interpréteur en aval).
_SECURITY_BLOCKING = frozenset({
    "SCAM_DETECTED",
    "ACCOUNT_BLOCKED",
    "PROHIBITED_PRODUCT",
    "PROFILE_UNAVAILABLE",
})


def _route_after_security(state: MarketAgentState) -> str:
    """Court-circuite vers la stratégie sur fraude, compte restreint, produit
    interdit ou profil indisponible ; sinon poursuit vers l'interpréteur."""
    if state.get("security_status") in _SECURITY_BLOCKING:
        return "to_strategy"
    if str(state.get("status") or "").upper() == "BLOCKED" and state.get("final_response"):
        return "to_strategy"
    return "to_interpreter"


def _route_after_planner(state: MarketAgentState) -> str:
    """Achemine vers le routeur si des informations critiques manquent."""
    status = str(state.get("status") or "").upper()
    if status == "WAITING_INPUT":
        return "to_strategy"
    return "to_memory"


def _route_after_resolver(state: MarketAgentState) -> str:
    """Redirige si l'état nécessite une interaction ou s'il est prêt pour confirmation."""
    # If a DRY form was activated by the resolver (e.g., procurement escalation),
    # jump directly to form_node to collect the next slot within the same turn.
    if state.get("active_form"):
        return "to_form"
    status = str(state.get("status") or "").upper()
    if status in {"WAITING_INPUT", "ERROR", "COMPLETED"}:
        return "to_strategy"
    # Nominal path: ensure farm is present or auto-created before confirmation
    return "to_farm_guard"


def _route_after_confirmation(state: MarketAgentState) -> str:
    """Achemine vers l'exécuteur MCP si l'opération a été validée par l'utilisateur."""
    status = str(state.get("status") or "").upper()
    if status == "EXECUTING":
        return "to_executor"
    return "to_strategy"


def _route_after_executor(state: MarketAgentState) -> str:
    """Post-execution routing — always proceeds to response strategy."""
    return "to_strategy"
