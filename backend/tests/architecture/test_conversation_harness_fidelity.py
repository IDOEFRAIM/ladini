"""Le harnais canonique exécute bien le moteur RÉEL (et pas une sous-chaîne recomposée).

Si un de ces tests casse, les tests multi-tours bâtis sur `tests/harness` ne prouvent plus
rien sur la production — voir la docstring de `tests/harness/conversation.py`.
"""
from __future__ import annotations

import json

import pytest

from tests.harness import ConversationHarness, new_task

_COQ = new_task("CREATE_RECURRING_NEED", product="coq", quantity=14.0, recurrence_type="WEEKLY")

_FULL_TURN_SPINE = [
    "input_normalizer",
    "security_moderation",
    "session_bootstrap",
    "input_interpreter",
    "cognitive_guard",
    "goal_planner",
    "memory_update",
    "validator",
    "context_resolver",
    "response_strategy",
    "state_cleaner",
    "final_response",
    "post_response_cleanup",
]


@pytest.mark.parametrize("channel", ["whatsapp", "webchat"])
def test_a_turn_runs_the_whole_compiled_graph_in_order(channel):
    with ConversationHarness(channel=channel) as conv:
        turn = conv.send("je veux 14 coqs chaque semaine", llm=_COQ)
    visited = turn.visited
    positions = [visited.index(node) for node in _FULL_TURN_SPINE]
    assert positions == sorted(positions), visited


def test_the_next_turn_reads_state_back_through_the_json_store():
    with ConversationHarness() as conv:
        conv.send("je veux 14 coqs chaque semaine", llm=_COQ)
        row = conv.store.rows[conv.phone]
        assert isinstance(row["langgraph_state"], str)
        json.loads(row["langgraph_state"])  # sérialisable, comme en base
        assert conv.state()["recurring_need_draft"]["product"] == "coq"


def test_both_channels_reach_the_same_business_outcome():
    outcomes = []
    for channel in ("whatsapp", "webchat"):
        with ConversationHarness(channel=channel) as conv:
            conv.runtime.responses["create_recurring_need"] = lambda **kw: {"status": "success", "recurring_need_id": "n"}
            t1 = conv.send("je veux 14 coqs chaque semaine", llm=_COQ)
            t2 = conv.send("oui")
            draft = t1.draft()
            outcomes.append((
                t1.event, t1.intent, t1.decision.get("action"), t1.goal_after, t1.pending_after.kind.value,
                draft["product"], draft["quantity"], draft["unit"], draft["recurrence_type"],
                t2.event, t2.goal_after, [tool for tool, _ in t2.mcp_calls if tool.startswith("create_")],
            ))
    assert outcomes[0] == outcomes[1]


def test_an_unscripted_llm_call_is_visible_not_silent():
    with ConversationHarness() as conv:
        turn = conv.send("bonjour")  # aucun script : le LLM ne doit pas « répondre » par hasard
    assert turn.llm_calls >= 1
    assert turn.error is None, "une panne LLM ne doit jamais faire échouer le tour"
