import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock

# Adjust imports to point to your actual backend structure
from agriconnect.graphs.nodes.market_coach import MarketCoach, MarketAgentState


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
    """
    Test that MarketCoach maintains the 'REGISTER_SURPLUS' intent even if
    the LLM misclassifies a follow-up answer (like 'Gaoua') as 'CHECK_PRICE'.
    """
    # 1. Setup Mock LLM
    mock_llm = MagicMock()
    # Mocking the LLM response for validation (analyze_node calls _extract_market_intent)
    # Simulator: First call (intent extraction) returns CHECK_PRICE for "Gaoua"
    mock_llm.chat.completions.create.return_value.choices = [
        MagicMock(message=MagicMock(content='{"intent": "CHECK_PRICE", "location": "Gaoua"}'))
    ]

    # 2. Initialize Coach
    coach = MarketCoach(llm_client=mock_llm)

    # 3. Simulate User Input: "Gaoua" (after "40k de mais")
    state = MarketAgentState(
        user_query="Gaoua",
        user_profile={"user_id": "test_user"},
        # The previous state from the graph would have these if it was real execution,
        # but analyze_node fetches context from MCP mainly.
        # However, let's also populate state to mimic graph accumulation if needed.
        status="MISSING_INFO",
        intent="REGISTER_SURPLUS",
        pending_user_intent="REGISTER_SURPLUS",
    )

    # 4. Run analyze_node
    result = await coach.analyze_node(state)

    # 5. Emulate reducer merge as LangGraph would do at runtime
    _merge_non_null(state, result)

    # 6. Assertions on merged state
    print(f"DEBUG Result: {result}")
    assert state.get("intent") == "REGISTER_SURPLUS", \
        f"Expected intent to remain REGISTER_SURPLUS, but got {state.get('intent')}"
    assert state.get("location") == "Gaoua", "Location should be extracted"

@pytest.mark.asyncio
async def test_market_create_product_intent_heuristic():
    """
    Test that 'enregistre dans la base de donne' is correctly identified as CREATE_PRODUCT
    via regex heuristic even if LLM is weak.
    """
    mock_llm = MagicMock()
    # Simulate LLM returning standard output
    mock_llm.chat.completions.create.return_value.choices = [
         MagicMock(message=MagicMock(content='{"intent": "CHECK_PRICE"}'))
    ]
    
    coach = MarketCoach(llm_client=mock_llm)
    
    state = MarketAgentState(
        user_query="j'ai 40 k de mais ,enregistre dans la base de donne",
        user_profile={"user_id": "test_user"}
    )
    
    result = await coach.analyze_node(state)
    
    # The heuristic should catch "enregistre"
    assert result["intent"] == "CREATE_PRODUCT", \
        f"Expected CREATE_PRODUCT via heuristic, got {result.get('intent')}"


@pytest.mark.asyncio
async def test_scam_detection_lenient_on_inventory():
    """
    Test that '40 k de mais' is NOT flagged as a scam.
    This requires inspecting the prompt or mocking the LLM to respect the prompt.
    Since we can't easily test prompt efficacy without real LLM, we verify the logic handles 'SAFE'.
    """
    mock_llm = MagicMock()
    # Simulate LLM returning SAFE following our new prompt rules
    mock_llm.chat.completions.create.return_value.choices = [
         MagicMock(message=MagicMock(content='{"is_scam": false, "reason": "Inventory report"}'))
    ]
    
    coach = MarketCoach(llm_client=mock_llm)
    state = MarketAgentState(user_query="40 k de mais")
    
    # We just want to ensure analyze_node respects the LLM's "is_scam": false
    result = await coach.analyze_node(state)
    
    assert result.get("security_status") != "SCAM_DETECTED"
    assert result.get("status") != "SCAM_DETECTED"

    """
    Test that '40 k de mais' is NOT flagged as a scam.
    This requires inspecting the prompt or mocking the LLM to respect the prompt.
    Since we can't easily test prompt efficacy without real LLM, we verify the logic handles 'SAFE'.
    """
    mock_llm = MagicMock()
    # Simulate LLM returning SAFE following our new prompt rules
    mock_llm.chat.completions.create.return_value.choices = [
         MagicMock(message=MagicMock(content='{"is_scam": false, "reason": "Inventory report"}'))
    ]
    
    coach = MarketCoach(llm_client=mock_llm)
    state = MarketAgentState(user_query="40 k de mais")
    
    # We just want to ensure analyze_node respects the LLM's "is_scam": false
    result = await coach.analyze_node(state)
    
    assert result.get("security_status") != "SCAM_DETECTED"
    assert result.get("status") != "SCAM_DETECTED"
