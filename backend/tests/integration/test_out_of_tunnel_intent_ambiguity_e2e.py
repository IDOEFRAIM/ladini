"""Étape 9A/9B — ambiguïté hors tunnel, replay E2E (2026-10-01). Même
méthode que `test_sales_publish_cross_flow_state_leak.py`/
`test_active_slot_answer_priority_e2e.py` (nœuds RÉELS, ordre réel,
reducers LangGraph réels) : `input_interpreter -> cognitive_guard ->
goal_planner -> memory_update -> validator`.

Scénario exact du mandat :

    Aucun goal actif.
    User: "j'ai 90 L de miel"

Attendu : ni STOCK_REGISTER_HARVEST ni SALES_PUBLISH_PRODUCT automatique —
une clarification ciblée, les faits (product/quantity/unit) préservés,
aucun draft, aucune écriture `transaction_payload`."""
from __future__ import annotations

import json
import typing
from typing import Any, Dict

from ladini.graphs.agents.market_coach.core.state import MarketAgentState
from ladini.graphs.agents.market_coach.interpreter.routing import (
    make_input_interpreter,
)
from ladini.graphs.agents.market_coach.nodes.cognitive import cognitive_guard
from tests.conftest import StubRuntime, _Completion, run

_HINTS = typing.get_type_hints(MarketAgentState, include_extras=True)


class _AmbiguousDeclarationLLM:
    """Scripte l'unique appel LLM réel de ce scénario : le micro-prompt
    NEW_TASK (route NEW_TASK, aucun tunnel actif — voir `state_router.py`)
    répond AMBIGUOUS avec les 2 candidats plausibles, faits préservés."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        self.calls += 1
        payload: Dict[str, Any] = {
            "disposition": "AMBIGUOUS",
            "candidate_goals": ["SALES_PUBLISH_PRODUCT", "STOCK_REGISTER_HARVEST"],
            "confidence": 0.6,
            "entities": {"product": "miel", "quantity": 90.0, "unit": "LITRE"},
        }
        return _Completion(json.dumps(payload), model=kwargs.get("model"))


class _FalseConfidentStockLLM:
    """Scripte le PIRE CAS du mandat de clôture : le micro-prompt NEW_TASK
    NE rapporte PAS AMBIGUOUS lui-même — il reste confiant (0.99) sur UNE
    intention (STOCK_REGISTER_HARVEST) pour une déclaration nue. Le
    backstop déterministe de `cognitive_guard` doit rattraper ce cas SANS
    aucune aide du LLM."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        self.calls += 1
        payload: Dict[str, Any] = {
            "disposition": "NEW_TASK",
            "intent": "STOCK_REGISTER_HARVEST",
            "confidence": 0.99,
            "entities": {"product": "miel", "quantity": 90.0, "unit": "LITRE"},
        }
        return _Completion(json.dumps(payload), model=kwargs.get("model"))


def _reducer_for(field: str):
    ann = _HINTS.get(field)
    if ann is None:
        return None
    metadata = getattr(ann, "__metadata__", None)
    if not metadata:
        return None
    return metadata[0]


def apply_patch(state: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    new_state = dict(state)
    for key, value in patch.items():
        reducer = _reducer_for(key)
        if reducer is None:
            new_state[key] = value
            continue
        new_state[key] = reducer(state.get(key), value)
    return new_state


async def _run_turn(state: Dict[str, Any], interpreter, runtime: StubRuntime, *, text: str) -> Dict[str, Any]:
    from ladini.graphs.agents.market_coach.interpreter.goal_planner import (
        goal_planner,
    )

    state = dict(state)
    state["normalized_text"] = text
    state["user_query"] = text

    interp = await interpreter(state, runtime)
    state = apply_patch(state, interp)

    cg = await cognitive_guard(state, runtime)
    state = apply_patch(state, cg)

    # (2026-10-01) ASK_INTENT_SELECTION route DIRECTEMENT vers
    # `response_strategy` dans le graphe réel (`nodes/routing.py`) —
    # `goal_planner`/`memory_update`/`validator` ne tournent PAS ce
    # tour-ci. Ce replay les appelle quand même pour PROUVER
    # explicitement qu'ils seraient des no-op s'ils tournaient (double
    # filet de sécurité), jamais pour les exercer comme chemin réel.
    gp = await goal_planner(state, runtime)
    state_if_planner_ran = apply_patch(state, gp)

    return state, state_if_planner_ran


class TestMielAmbiguousDeclarationNeverAutoSelectsAGoal:
    def test_pilot_scenario_asks_targeted_clarification_without_side_effect(self):
        state: Dict[str, Any] = {
            "user_phone": "+22670000099",
            "user_role": "PRODUCER",
            "extracted_entities": {},
            "working_memory": {},
            "current_goal": None,
            "transaction_payload": {},
        }
        runtime = StubRuntime()
        interpreter = make_input_interpreter("PRODUCER")
        runtime.llm = _AmbiguousDeclarationLLM()

        state_after_cognitive_guard, state_if_planner_ran = run(
            _run_turn(state, interpreter, runtime, text="j'ai 90 L de miel")
        )

        # Ce que le graphe réel produit (cognitive_guard route directement
        # vers response_strategy, voir nodes/routing.py) :
        assert state_after_cognitive_guard.get("current_goal") is None
        assert state_after_cognitive_guard.get("transaction_payload") == {}
        assert state_after_cognitive_guard.get("sales_publish_draft") is None
        pending = state_after_cognitive_guard.get("pending_interaction") or {}
        assert pending.get("kind") == "CLARIFY_INTENT"
        facts = pending.get("target", {}).get("facts", {})
        assert facts.get("product") == "miel"
        assert facts.get("quantity") == 90.0
        assert facts.get("unit") == "LITRE"
        assert set(pending.get("target", {}).get("candidate_goals", [])) == {
            "SALES_PUBLISH_PRODUCT",
            "STOCK_REGISTER_HARVEST",
        }
        assert state_after_cognitive_guard.get("final_response")
        assert state_after_cognitive_guard["cognitive_decision"]["action"] == "ASK_INTENT_SELECTION"

        # Double filet : même si goal_planner tournait quand même par erreur
        # de câblage futur, il ne doit JAMAIS verrouiller un goal pour un
        # event="AMBIGUOUS" (aucune RÈGLE ne le reconnaît — repli par
        # défaut, current_goal reste None).
        assert state_if_planner_ran.get("current_goal") is None


class TestBackstopRescuesAFalseConfidentNewTaskOnTheRealNodeChain:
    """Clôture Étape 9A/9B — preuve E2E que le backstop déterministe
    fonctionne MÊME quand le LLM lui-même ne coopère pas (ne rapporte
    jamais AMBIGUOUS), sur la chaîne de nœuds réelle."""

    def test_stock_at_0_99_confidence_still_triggers_clarification(self):
        state: Dict[str, Any] = {
            "user_phone": "+22670000099",
            "user_role": "PRODUCER",
            "extracted_entities": {},
            "working_memory": {},
            "current_goal": None,
            "transaction_payload": {},
        }
        runtime = StubRuntime()
        interpreter = make_input_interpreter("PRODUCER")
        runtime.llm = _FalseConfidentStockLLM()

        state_after_cognitive_guard, _ = run(
            _run_turn(state, interpreter, runtime, text="j'ai 90 L de miel")
        )

        # Preuve que le LLM a bien répondu NEW_TASK (pas AMBIGUOUS) — le
        # backstop, pas le LLM, est responsable du résultat final.
        assert state_after_cognitive_guard["cognitive_decision"]["intent"] == "STOCK_REGISTER_HARVEST"
        assert state_after_cognitive_guard["cognitive_decision"]["confidence"] == 0.99

        assert state_after_cognitive_guard.get("current_goal") is None
        assert state_after_cognitive_guard.get("transaction_payload") == {}
        assert state_after_cognitive_guard.get("sales_publish_draft") is None
        pending = state_after_cognitive_guard.get("pending_interaction") or {}
        assert pending.get("kind") == "CLARIFY_INTENT"
        facts = pending.get("target", {}).get("facts", {})
        assert facts.get("product") == "miel"
        assert facts.get("quantity") == 90.0
        assert set(pending.get("target", {}).get("candidate_goals", [])) == {
            "SALES_PUBLISH_PRODUCT",
            "STOCK_REGISTER_HARVEST",
        }
        assert (
            state_after_cognitive_guard["cognitive_decision"]["action"]
            == "ASK_INTENT_SELECTION"
        )
        assert (
            state_after_cognitive_guard["cognitive_decision"]["reason"]
            == "out_of_tunnel_ambiguity_backstop"
        )
