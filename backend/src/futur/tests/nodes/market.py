import pytest
from unittest.mock import MagicMock

from agriconnect.graphs.nodes.market import MarketCoach, MarketAgentState


def test_market_analyze_node_basic():
    agent = MarketCoach()
    state: MarketAgentState = {"user_query": "Quel est le prix du maïs ?", "warnings": []}
    result = agent.analyze_node(state)
    assert result.get("status") == "ANALYZED"


def test_market_validate_and_compose_flow():
    agent = MarketCoach()
    # Simulate an intent that requires validation but missing fields
    state: MarketAgentState = {"intent": "SELL_PRODUCT", "status": "ANALYZED", "warnings": []}
    updates = agent.validate_node(state)
    assert isinstance(updates, dict)
