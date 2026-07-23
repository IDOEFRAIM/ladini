import asyncio
import pytest

from agriconnect.graphs.nodes.market import MarketCoach, PRODUCER_ID


class MockMCP:
    def __init__(self):
        self.calls = []

    async def call_tool(self, name: str, args: dict):
        self.calls.append((name, args))
        # simulate DB returning created record id
        return {"id": "mock-record-1", "status": "created", "args": args}


@pytest.mark.asyncio
async def test_marketcoach_registers_surplus_with_producer_id():
    """Ensure MarketCoach calls MCP tool `register_surplus_offer` with the user/producer id."""
    mock = MockMCP()
    coach = MarketCoach(mcp_session=mock)

    payload = {
        "product": "maïs",
        "quantity": 50,
        "location": "Nouna",
        "user_id": PRODUCER_ID,
    }

    result = await coach._handle_transaction_execution(payload)

    # Assert MCP was called
    assert mock.calls, "Expected MCP call but none were recorded"
    name, args = mock.calls[0]
    assert name == "register_surplus_offer"
    assert args.get("user_id") == PRODUCER_ID
    assert args.get("commodity") == "maïs"
    assert args.get("quantity") == 50

    # Assert agent treated registration as success
    assert result["registration_status"] == "SUCCESS"
    assert "mcp_result" in result
    assert result["mcp_result"]["status"] == "created"
