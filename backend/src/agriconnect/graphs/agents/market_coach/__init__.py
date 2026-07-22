"""MarketCoach package facade.

Architecture (post-refactor) :
- `core/`         : state, graph_builder, base
- `interpreter/`  : intent, prompts, routing NLU, response_strategy
- `flows/buyer/`  : tunnel transactionnel acheteur (panier, précommande, négo)
- `flows/producer/` : actions, farm_logic, onboarding, flow producteur
- `nodes/`        : nodes purement techniques et réutilisables
- `services/`     : entrée MCP/DB

Les attributs lazy-loaded ci-dessous restent supportés pour préserver les
imports historiques (`from agriconnect.graphs.agents.market_coach import
onboarding_node`, etc.).
"""

from agriconnect.graphs.agents.market_coach.registry import (
    load_all_actions,
    validate_integrity,
)


load_all_actions()
validate_integrity()


__all__ = [
    "onboarding_node",
    "memory_update",
    "mcp_tool_executor",
    "validator",
    "ensure_farm_node",
]


def __getattr__(name):
    if name == "onboarding_node":
        from agriconnect.graphs.agents.market_coach.flows.common.onboarding import onboarding_node
        return onboarding_node
    if name == "memory_update":
        from agriconnect.graphs.agents.market_coach.nodes.memory import memory_update
        return memory_update
    if name == "mcp_tool_executor":
        from agriconnect.graphs.agents.market_coach.nodes.executor import mcp_tool_executor
        return mcp_tool_executor
    if name == "validator":
        from agriconnect.graphs.agents.market_coach.nodes.validation import validator
        return validator
    if name == "ensure_farm_node":
        from agriconnect.graphs.agents.market_coach.flows.producer.farm_logic import ensure_farm_node
        return ensure_farm_node
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
