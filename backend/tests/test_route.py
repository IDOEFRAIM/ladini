import json
import os
import types

import pytest

from agriconnect.graphs.route import Router


def _real_llm():
    # Use the configured SDK client from our RAG components. This will
    # raise a clear error if the environment isn't configured for a real LLM.
    from agriconnect.rag.components import get_groq_sdk

    return get_groq_sdk()


def test_analyze_needs_greeting_returns_structure():
    llm = _real_llm()
    router = Router(llm=llm, ctx=None)

    state = {"requete_utilisateur": "Bonjour AgriBot, comment ça va ?"}
    out = router.analyze_needs(state)
    needs = out.get("needs") or {}

    assert "intent" in needs
    assert isinstance(needs.get("selected_experts", []), list)


def test_analyze_needs_compost_returns_structure():
    llm = _real_llm()
    router = Router(llm=llm, ctx=None)

    state = {"requete_utilisateur": "Explique-moi comment faire un compost simple."}
    out = router.analyze_needs(state)
    needs = out.get("needs") or {}

    assert "intent" in needs
    assert isinstance(needs.get("selected_experts", []), list)


# --- Mock-based unit tests (fast, deterministic) -----------------
class FakeCompletions:
    def __init__(self, content: str):
        self._content = content

    def create(self, *args, **kwargs):
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=self._content))])


class FakeLLM:
    def __init__(self, json_obj):
        self.chat = types.SimpleNamespace(completions=FakeCompletions(json.dumps(json_obj)))


def test_analyze_needs_chat_polite_no_expert_mock():
    resp = {"intent": "CHAT", "selected_experts": [], "reason": "Simple politesse"}
    llm = FakeLLM(resp)
    router = Router(llm=llm, ctx=None)

    state = {"requete_utilisateur": "Bonjour AgriBot, comment ça va ?"}
    out = router.analyze_needs(state)
    needs = out["needs"]

    assert needs["intent"].upper() == "CHAT"
    assert needs.get("selected_experts") in ([], None)


def test_analyze_needs_chat_explain_routes_to_formation_mock():
    resp = {"intent": "CHAT", "selected_experts": [], "reason": ""}
    llm = FakeLLM(resp)
    router = Router(llm=llm, ctx=None)

    state = {"requete_utilisateur": "Explique-moi comment faire un compost simple."}
    out = router.analyze_needs(state)
    needs = out["needs"]

    assert needs["intent"].upper() == "CHAT"
    assert needs.get("selected_experts") == ["formation"]


def test_select_experts_with_manifests_scores_and_mapping():
    # Build synthetic manifests to exercise scoring and mapping
    manifests = [
        {"agent_id": "formation_coach", "intents": ["how_to", "learn"], "capabilities": ["tutorial"], "avg_response_ms": 200},
        {"agent_id": "market_coach", "intents": ["price", "market"], "capabilities": ["pricing"], "avg_response_ms": 300},
        {"agent_id": "climate_sentinel", "intents": ["weather", "disease"], "capabilities": ["alert"], "avg_response_ms": 1000},
    ]

    router = Router()

    # Query explicitly asking for explanation should prefer formation
    selected = router.select_experts("Explique-moi comment faire un compost", manifests, limit=2)
    assert "formation" in selected

    # Query about prix/market should prefer market
    selected = router.select_experts("Quel est le prix du maïs aujourd'hui?", manifests, limit=2)
    assert "market" in selected


def test_map_selected_agents_and_dedupe():
    router = Router()
    agent_ids = ["formation_coach", "formation", "market_coach", "marketplace_agent", "marketplace_agent"]
    mapped = router._map_selected_agents(agent_ids)
    # expect deduped mapped keys and known mappings present
    assert "formation" in mapped
    assert "marketplace" in mapped


def test_analyze_needs_reason_token_mapping_to_selected_experts():
    # LLM returns empty selected_experts but reason mentions marketplace
    resp = {"intent": "CHAT", "selected_experts": [], "reason": "recommend marketplace_agent for listings"}
    llm = FakeLLM(resp)
    router = Router(llm=llm, ctx=None)

    state = {"requete_utilisateur": "Je veux vendre mon produit"}
    analysis = router.analyze_needs(state)["needs"]
    sel = analysis.get("selected_experts", [])
    assert sel and "marketplace" in sel


def test_route_flow_flags_and_intents():
    router = Router()

    # REJECT intent should map to REJECT
    state = {"needs": {"intent": "REJECT"}}
    assert router.route_flow(state) == "REJECT"

    # CHAT with explicit selected expert -> SOLO_<EXPERT>
    state = {"needs": {"intent": "CHAT", "selected_experts": ["formation"]}}
    assert router.route_flow(state) == "SOLO_FORMATION"

    # Reason hint mapping
    state = {"needs": {"intent": "CHAT", "reason": "marketplace"}}
    assert router.route_flow(state) == "SOLO_MARKETPLACE"

    # Multi-need flags -> PARALLEL_EXPERTS
    state = {"needs": {"needs_formation": True, "needs_market": True}}
    assert router.route_flow(state) == "PARALLEL_EXPERTS"


def test_route_flow_single_and_parallel():
    router = Router(llm=None, ctx=None)

    # Single selected expert -> SOLO_<EXPERT>
    state = {"needs": {"selected_experts": ["market"], "intent": None}}
    assert router.route_flow(state) == "SOLO_MARKET"

    # Multiple selected experts -> PARALLEL_EXPERTS
    state = {"needs": {"selected_experts": ["market", "sentinelle"], "intent": None}}
    assert router.route_flow(state) == "PARALLEL_EXPERTS"


def test_map_agent_id_to_key_variants():
    router = Router()
    assert router._map_agent_id_to_key("formation_coach") == "formation"
    assert router._map_agent_id_to_key("marketplace_agent") == "marketplace"
    assert router._map_agent_id_to_key("climate_sentinel") == "sentinelle"


# Integration test using the real LLM SDK. This runs only when the
# environment variable `TEST_REAL_LLM` is set (1/true/yes). It avoids
# asserting exact expert selections since models can change; instead
# it checks that the LLM returns a valid analysis structure.
@pytest.mark.skipif(os.getenv("TEST_REAL_LLM", "").lower() not in ("1", "true", "yes"), reason="Real LLM tests disabled")
def test_analyze_needs_with_real_llm():
    from agriconnect.rag.components import get_groq_sdk

    llm = get_groq_sdk()
    router = Router(llm=llm, ctx=None)

    state = {"requete_utilisateur": "Explique-moi comment faire un compost simple."}
    out = router.analyze_needs(state)
    needs = out.get("needs") or {}

    assert "intent" in needs
    assert isinstance(needs.get("selected_experts", []), list)
