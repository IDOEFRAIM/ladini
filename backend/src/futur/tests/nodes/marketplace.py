import pytest
from unittest.mock import MagicMock

from agriconnect.graphs.nodes.marketplace import MarketplaceAgent, MarketplaceState


@pytest.fixture
def marketplace_agent():
    agent = MarketplaceAgent()
    # stub the tool to avoid external side effects
    agent.tool = MagicMock()
    agent.tool.identify_or_create_user = MagicMock(return_value={"producer_id": None, "is_new": False})
    return agent


def test_identify_user_missing_phone(marketplace_agent):
    state: MarketplaceState = {"user_phone": ""}
    res = marketplace_agent.identify_user_node(state)
    assert res.get("status") == "ERROR"


def test_identify_user_success(marketplace_agent):
    state: MarketplaceState = {"user_phone": "+22670000000", "zone_id": None}
    res = marketplace_agent.identify_user_node(state)
    assert res.get("status") == "IDENTIFIED"
