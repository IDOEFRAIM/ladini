import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))

import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock
from agriconnect.graphs.agents.market_coach.core.state import MarketAgentState
from agriconnect.graphs.agents.market_coach.adapter import get_agent_graph as MarketCoach


def _merge_non_null(target: dict, updates: dict) -> None:
    """Emulate reducer merge used by LangGraph for direct node unit tests."""
    for key, value in updates.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        # Special-case intent stickiness to reflect `merge_intent` reducer
        if key == "intent":
            transactional_intents = {"REGISTER_SURPLUS", "CREATE_PRODUCT", "BUY_OFFER"}
            if str(value).upper() == "CHECK_PRICE" and str(target.get("intent")).upper() in transactional_intents:
                # keep previous transactional intent
                continue
        target[key] = value

@pytest.mark.asyncio
async def test_market_coach_intent_stickiness():
    # 1. Setup Mock LLM
    mock_llm = MagicMock()
    # Mocking the LLM response for validation (analyze_node calls _extract_market_intent)
    # Simulator: First call (intent extraction) returns CHECK_PRICE for "Gaoua"
    mock_llm.chat.completions.create.return_value.choices = [
        MagicMock(message=MagicMock(content='{"intent": "CHECK_PRICE", "location": "Gaoua"}'))
    ]

    # 2. Initialize Coach (stateless node + carried state)
    coach = MarketCoach(llm_client=mock_llm)

    # 4. Simulate User Input: "Gaoua" (after "40k de mais")
    state = MarketAgentState(
        user_query="Gaoua",
        user_profile={"user_id": "test_user"},
        status="MISSING_INFO",
        intent="REGISTER_SURPLUS",
        pending_user_intent="REGISTER_SURPLUS",
    )

    # 5. Run analyze_node
    # We expect the logic to override "CHECK_PRICE" with "REGISTER_SURPLUS"
    result = await coach.analyze_node(state)

    # 6. Emulate reducer merge as LangGraph would do at runtime
    _merge_non_null(state, result)

    # 7. Assertions on merged state
    print(f"DEBUG Result: {result}")
    assert state.get("intent") == "REGISTER_SURPLUS"
    assert state.get("location") == "Gaoua"

@pytest.mark.asyncio
async def test_market_create_product_intent_heuristic():
    mock_llm = MagicMock()
    mock_llm.chat.completions.create.return_value.choices = [
         MagicMock(message=MagicMock(content='{"intent": "CHECK_PRICE"}'))
    ]
    coach = MarketCoach(llm_client=mock_llm)
    state = MarketAgentState(
        user_query="j'ai 40 k de mais ,enregistre dans la base de donne",
        user_profile={"user_id": "test_user"}
    )
    result = await coach.analyze_node(state)
    assert result["intent"] == "CREATE_PRODUCT"

@pytest.mark.asyncio
async def test_scam_detection_lenient_on_inventory():
    mock_llm = MagicMock()
    mock_llm.chat.completions.create.return_value.choices = [
         MagicMock(message=MagicMock(content='{"is_scam": false, "reason": "Inventory report"}'))
    ]
    coach = MarketCoach(llm_client=mock_llm)
    state = MarketAgentState(user_query="40 k de mais")
    result = await coach.analyze_node(state)
    assert result.get("security_status") != "SCAM_DETECTED"


@pytest.mark.asyncio
async def test_followup_price_and_location_completes_surplus_registration():
    coach = MarketCoach(llm_client=None)

    state = MarketAgentState(
        user_query="c est a gaoua ; le kg coute 300 FCFA",
        user_profile={"user_id": "test_user"},
        status="MISSING_INFO",
        intent="REGISTER_SURPLUS",
        pending_user_intent="REGISTER_SURPLUS",
        product="riz",
        quantity_mentioned=40.0,
        unit_mentioned="kg",
    )

    analyzed = await coach.analyze_node(state)
    _merge_non_null(state, analyzed)
    validated = await coach.validate_node(state)
    state.update(validated)
    composed = await coach.compose_node(state)

    assert state.get("intent") == "REGISTER_SURPLUS"
    assert state.get("missing_fields") == []
    assert state.get("status") == "PROPOSAL_READY"
    assert "register_surplus_offer" in composed.get("final_response", "")


@pytest.mark.asyncio
async def test_followup_uses_carried_state_when_store_unavailable():
    coach = MarketCoach(llm_client=None)

    # Turn 1: user starts a surplus registration without full data.
    state1 = MarketAgentState(
        user_query="j ai cultive 40 kg de riz",
        user_profile={"user_id": "test_user"},
    )
    analyzed1 = await coach.analyze_node(state1)
    state1.update(analyzed1)
    validated1 = await coach.validate_node(state1)
    state1.update(validated1)
    assert state1.get("status") == "MISSING_INFO"

    # Turn 2: user gives missing fields only (location + price), with carried state.
    state2 = MarketAgentState(
        user_query="c est a bobo , le kg coute 600 fcfa",
        user_profile={"user_id": "test_user"},
        status=state1.get("status"),
        intent=state1.get("intent"),
        pending_user_intent=state1.get("pending_user_intent"),
        product=state1.get("product"),
        quantity_mentioned=state1.get("quantity_mentioned"),
        unit_mentioned=state1.get("unit_mentioned"),
    )
    analyzed2 = await coach.analyze_node(state2)
    _merge_non_null(state2, analyzed2)
    validated2 = await coach.validate_node(state2)
    state2.update(validated2)

    assert state2.get("intent") == "REGISTER_SURPLUS"
    assert state2.get("product") in ["riz", "Riz"]
    assert state2.get("quantity_mentioned") == 40.0
    assert state2.get("location") == "Bobo"
    assert state2.get("price_mentioned") == 600.0
    assert state2.get("status") == "PROPOSAL_READY"


@pytest.mark.asyncio
async def test_manioc_cultive_detects_surplus():
    coach = MarketCoach(llm_client=None)
    state = MarketAgentState(
        user_query="J AI CULTIVE 10 SAC DE MANIOC",
        user_profile={"user_id": "test_user"},
    )

    analyzed = await coach.analyze_node(state)
    # We expect intent to be REGISTER_SURPLUS and product 'manioc' detected
    assert analyzed.get("intent") == "REGISTER_SURPLUS"
    assert analyzed.get("product") == "manioc"
    assert analyzed.get("quantity_mentioned") == 10.0 or analyzed.get("quantity_mentioned") == 10
    assert analyzed.get("unit_mentioned") in ("sacs", "sac")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query,expected_product",
    [
        ("j ai recolte 2 tonnes de sesame", "sesame"),
        ("J’AI CULTIVE 15 KG DE MAÏS", "mais"),
        ("jai 7 sacs de niébé", "niebe"),
    ],
)
async def test_inventory_variants_are_normalized_and_classified(query, expected_product):
    coach = MarketCoach(llm_client=None)
    state = MarketAgentState(
        user_query=query,
        user_profile={"user_id": "test_user"},
    )

    analyzed = await coach.analyze_node(state)
    assert analyzed.get("intent") == "REGISTER_SURPLUS"
    assert analyzed.get("product") == expected_product
    assert analyzed.get("quantity_mentioned") is not None

if __name__ == "__main__":
    async def main():
        print("Running tests manually...")
        await test_scam_detection_lenient_on_inventory()
        print("PASS: test_scam_detection_lenient_on_inventory")
        await test_market_create_product_intent_heuristic()
        print("PASS: test_market_create_product_intent_heuristic")
        await test_market_coach_intent_stickiness()
        print("PASS: test_market_coach_intent_stickiness")
    
    loop = asyncio.new_event_loop()
    loop.run_until_complete(main())
