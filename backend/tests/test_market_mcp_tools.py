import asyncio
import json
import pytest
import sys
import os

# Ensure backend/src is on sys.path so tests run without external PYTHONPATH
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agriconnect.graphs.nodes.market import MarketCoach, MarketAgentState, PRODUCER_ID


class MockMCP:
    def __init__(self):
        self.calls = []

    async def call_tool(self, name: str, args: dict):
        # record call and return a predictable GenericResult-like dict
        self.calls.append((name, args))
        # simple success payload mimicking db_tools GenericResult.data
        if name == "register_surplus_offer":
            return {"record_type": "surplus_offer", "offer": {"id": "offer-1"}}
        if name == "get_farm_stocks":
            return [{"item_name": "maïs - sac", "quantity": 100}]
        if name == "list_products":
            return [{"id": "p1", "name": "maïs"}]
        return {"ok": True, "name": name, "args": args}


@pytest.mark.asyncio
async def test_marketcoach_uses_mcp_tools_and_requests_missing_params():
    mcp = MockMCP()
    coach = MarketCoach(mcp_session=mcp)

    # Scenario 1: missing fields -> agent should request missing info
    incomplete_state = {
        "intent": "REGISTER_SURPLUS",
        "product": None,
        "quantity_mentioned": None,
        "unit_mentioned": None,
        "location": None,
        "user_profile": {"phone": "+250000"},
    }

    updates = coach.validate_node(incomplete_state)
    # When missing, agent sets status to MISSING_INFO and includes missing_fields
    assert updates.get("status") == "MISSING_INFO" or updates.get("missing_fields")

    # Scenario 2: complete payload -> execute and ensure MCP tools called
    complete_state = {
        "intent": "REGISTER_SURPLUS",
        "product": "maïs",
        "quantity_mentioned": 50,
        "unit_mentioned": "kg",
        "location": "Nouna",
        "user_profile": {"user_id": PRODUCER_ID},
    }

    validate_updates = coach.validate_node(complete_state)
    assert validate_updates.get("waiting_for_confirmation") is True

    payload = validate_updates.get("transaction_payload")
    assert payload["user_id"] == PRODUCER_ID

    # Execute transaction (uses _handle_transaction_execution which will call wrapper->mcp)
    result = await coach._handle_transaction_execution(payload)

    # Ensure register_surplus_offer was called on MCP
    called_names = [c[0] for c in mcp.calls]
    assert "register_surplus_offer" in called_names

    # Ensure persist_conversation was also invoked for audit (wrapper calls persist)
    assert any(n == "persist_conversation" for n in called_names)

    # Scenario 3: call other wrappers to ensure presence
    coach.mcp_list_products(PRODUCER_ID)
    coach.mcp_get_farm_stocks(PRODUCER_ID)
    # The mock stores calls via async call_tool only; we invoked wrappers synchronously which use
    # the internal _call_mcp_tool that runs the async call_tool via the bridge. Ensure calls logged.
    assert any(n == "list_products" for n in [c[0] for c in mcp.calls])
    assert any(n == "get_farm_stocks" for n in [c[0] for c in mcp.calls])

    # Basic sanity: at least three distinct MCP tools should have been invoked
    distinct = set([c[0] for c in mcp.calls])
    assert len(distinct) >= 3
