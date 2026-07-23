import asyncio

from agriconnect.graphs.nodes.market import MarketCoach
from agriconnect.graphs.nodes.marketplace_background import MarketplaceBackgroundAgent


class FakeMCP:
    def __init__(self):
        self.calls = []

    def call_tool_sync(self, name, args=None):
        args = args or {}
        self.calls.append((name, args))

        if name == "upsert_user_context_state":
            return {"ok": True, "data": {"user_id": args.get("user_id"), "pending_intent": args.get("pending_intent")}}
        if name == "get_user_context_state":
            return {"ok": True, "data": {"user_id": args.get("user_id"), "pending_intent": "REGISTER_SURPLUS", "draft_data": {"product": "mais"}}}
        if name == "search_products":
            return {"ok": True, "data": [{"id": "prod-1", "buyer_id": "buyer-1", "zone_id": "zone-1"}]}
        if name == "get_open_auctions":
            return {"ok": True, "data": []}
        if name == "create_market_match":
            return {"ok": True, "data": {"id": "match-1", "product_id": args.get("product_id"), "score": args.get("score")}}

        return {"ok": True, "data": {}}


def test_market_agent_persists_pending_intent_when_info_missing():
    coach = MarketCoach(llm_client=None, mcp_session=FakeMCP())
    state = {
        "intent": "REGISTER_SURPLUS",
        "product": "mais",
        "status": "ANALYZED",
        # missing quantity/location/price => MISSING_INFO
        "user_profile": {"user_id": "fa987f63-fafa-4147-9676-52c9af0edc75"},
    }

    updates = coach._run_async(coach.validate_node(state))

    assert updates.get("status") == "MISSING_INFO"
    upserts = [c for c in coach.db_host.calls if c[0] == "upsert_user_context_state"]
    assert upserts, "Expected user context upsert call when info is missing"
    assert upserts[-1][1].get("pending_intent") == "REGISTER_SURPLUS"


def test_marketplace_background_generates_match_suggestions():
    fake = FakeMCP()
    agent = MarketplaceBackgroundAgent(mcp_session=fake)

    created = asyncio.run(agent.generate_matches_for_product("mais", zone_id="zone-1", limit=5))

    assert created, "Expected at least one created match"
    assert any(c[0] == "create_market_match" for c in fake.calls)
