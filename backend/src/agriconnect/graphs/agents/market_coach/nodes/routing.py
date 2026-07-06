from typing import Any, Dict, Optional
from agriconnect.graphs.agents.market_coach.core.base import get_node_logger
from agriconnect.graphs.agents.market_coach.core.state import MarketAgentState

logger = get_node_logger("RoutingNodes")

def _route_after_security(state: MarketAgentState) -> str:
    """Achemine vers le routeur de stratégie si une fraude est détectée."""
    if state.get("security_status") == "SCAM_DETECTED":
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
    """Routage post-exécution agentique.

    Si l'executor a détecté un champ manquant via self-healing (status=WAITING_INPUT),
    il route vers response_strategy pour poser la question.
    Si erreur récupérable avec retry possible, re-route vers validator pour correction.
    Sinon : stratégie normale.
    """
    status = str(state.get("status") or "").upper()
    # Self-healing a routé vers ASK_MISSING_FIELD
    if status == "WAITING_INPUT":
        return "to_strategy"
    # TODO: future feedback loop — route to validator on recoverable errors
    return "to_strategy"
