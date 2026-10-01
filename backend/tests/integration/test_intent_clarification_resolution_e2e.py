"""Étape 9C — résolution après clarification, replay E2E (2026-10-01). Même
méthode que `test_sales_publish_cross_flow_state_leak.py`/
`test_active_slot_answer_priority_e2e.py` (nœuds RÉELS, ordre réel,
reducers LangGraph réels) : `input_interpreter -> cognitive_guard ->
goal_planner -> memory_update -> validator`.

C'est le TEST PRINCIPAL de cette étape (mandat §22) :

    TURN 1
    user: "j'ai 90 L de miel"
    => CLARIFY_INTENT, facts préservés, candidats SALES/STOCK

    TURN 2
    user: "vendre"
    => current_goal=SALES_PUBLISH_PRODUCT, product=miel, quantity=90 L,
       pending_interaction consommé, prochaine question = PRICE

`cognitive_guard` ne verrouille JAMAIS `current_goal` lui-même (invariant
architectural vérifié par `tests/architecture/test_canonical_goal_api.py`
— seul `goal_planner` en est le propriétaire déclaré) : une résolution
réussie pose seulement `interpreted_event=NEW_TASK`/`detected_intent=
<goal choisi>`/`extracted_entities=<faits fusionnés>`, exactement ce qu'un
tour "je veux vendre 90 L de miel" tapé directement aurait posé — d'où la
nécessité de rejouer la chaîne COMPLÈTE (`goal_planner`/`memory_update`/
`validator`), pas seulement `cognitive_guard`, pour observer le résultat
final."""
from __future__ import annotations

import json
import typing
from typing import Any, Dict

from ladini.graphs.agents.market_coach.core.state import MarketAgentState
from ladini.graphs.agents.market_coach.interpreter.routing import (
    make_input_interpreter,
)
from ladini.graphs.agents.market_coach.nodes.cognitive import cognitive_guard
from ladini.graphs.agents.market_coach.nodes.memory import memory_update
from ladini.graphs.agents.market_coach.nodes.validation import validator
from tests.conftest import StubRuntime, _Completion, run

_HINTS = typing.get_type_hints(MarketAgentState, include_extras=True)


class _AmbiguousThenClarificationReplyLLM:
    """Scripte les 2 tours : tour 1 — NEW_TASK micro-prompt répond
    AMBIGUOUS (miel, 90 L, candidats SALES/STOCK) ; tour 2 — n'importe quel
    appel (la réponse "vendre" est résolue AVANT tout appel LLM par le
    radical lexical de `_resolve_intent_clarification`, mais un script de
    repli est fourni par robustesse si jamais l'interpréteur était quand
    même consulté en amont)."""

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
        if self.calls == 1:
            payload: Dict[str, Any] = {
                "disposition": "AMBIGUOUS",
                "candidate_goals": ["SALES_PUBLISH_PRODUCT", "STOCK_REGISTER_HARVEST"],
                "confidence": 0.6,
                "entities": {"product": "miel", "quantity": 90.0, "unit": "LITRE"},
            }
        else:
            payload = {
                "disposition": "NEW_TASK",
                "intent": "SALES_PUBLISH_PRODUCT",
                "confidence": 0.9,
                "entities": {},
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


async def _run_turn(
    state: Dict[str, Any], interpreter, runtime: StubRuntime, *, text: str
) -> Dict[str, Any]:
    """Un tour complet : interpreter -> cognitive_guard -> goal_planner ->
    memory_update -> validator (même chaîne que `test_sales_publish_
    cross_flow_state_leak.py`/`test_active_slot_answer_priority_e2e.py`).
    Nécessaire ICI précisément parce que `cognitive_guard` ne verrouille
    plus lui-même `current_goal` (voir la docstring de module) — c'est
    `goal_planner` qui le fait, normalement, dans ce même tour."""
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

    # ASK_INTENT_SELECTION/RESOLVE_INTENT_CLARIFICATION (cancel) routent
    # directement vers response_strategy dans le graphe réel — ni
    # goal_planner ni memory_update/validator n'ont besoin de tourner pour
    # ces deux actions-là, mais les rejouer quand même ici est un no-op
    # sûr (RULE 1quater/RULE 2 les laissent inchangés) et prouve qu'aucun
    # effet de bord inattendu n'apparaîtrait s'ils tournaient.
    gp = await goal_planner(state, runtime)
    state = apply_patch(state, gp)

    mem = await memory_update(state, runtime)
    state = apply_patch(state, mem)

    val = await validator(state, runtime)
    state = apply_patch(state, val)

    return state


class TestMielSalesClarificationResolutionE2E:
    """Mandat §22 — test principal de l'étape."""

    def test_vendre_resolves_sales_with_facts_preserved_and_price_next(self):
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
        runtime.llm = _AmbiguousThenClarificationReplyLLM()

        # TURN 1
        state = run(_run_turn(state, interpreter, runtime, text="j'ai 90 L de miel"))
        assert state.get("current_goal") is None
        pending = state.get("pending_interaction") or {}
        assert pending.get("kind") == "CLARIFY_INTENT"
        assert pending["target"]["facts"]["product"] == "miel"
        assert pending["target"]["facts"]["quantity"] == 90.0
        assert set(pending["target"]["candidate_goals"]) == {
            "SALES_PUBLISH_PRODUCT",
            "STOCK_REGISTER_HARVEST",
        }

        # TURN 2
        state = run(_run_turn(state, interpreter, runtime, text="vendre"))
        assert state.get("current_goal") == "SALES_PUBLISH_PRODUCT"
        payload = state.get("transaction_payload") or {}
        assert payload.get("product") == "miel"
        assert payload.get("quantity") == 90.0
        assert payload.get("unit") == "LITRE"
        new_pending = state.get("pending_interaction") or {}
        assert new_pending.get("kind") == "ENTER_FIELD"
        assert new_pending.get("field") == "price"
        assert set(state.get("missing_fields") or []) == {"price"}


class TestStockClarificationResolutionE2E:
    """Mandat §23."""

    def test_stock_starts_the_stock_flow_with_the_same_facts(self):
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
        runtime.llm = _AmbiguousThenClarificationReplyLLM()

        state = run(_run_turn(state, interpreter, runtime, text="j'ai 90 L de miel"))
        assert state.get("pending_interaction", {}).get("kind") == "CLARIFY_INTENT"

        state = run(_run_turn(state, interpreter, runtime, text="stock"))
        assert state.get("current_goal") == "STOCK_REGISTER_HARVEST"
        payload = state.get("transaction_payload") or {}
        assert payload.get("product") == "miel"
        assert payload.get("quantity") == 90.0


class TestEnrichedQuantityClarificationResolutionE2E:
    """Mandat §24."""

    def test_vendre_seulement_50_l_uses_the_corrected_quantity(self):
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
        runtime.llm = _AmbiguousThenClarificationReplyLLM()

        state = run(_run_turn(state, interpreter, runtime, text="j'ai 90 L de miel"))
        assert state.get("pending_interaction", {}).get("kind") == "CLARIFY_INTENT"

        # Le texte lui-même porte le radical "vendre" (résolution lexicale,
        # aucun appel LLM requis pour CHOISIR le goal) ; la quantité
        # corrigée est extraite par l'interpréteur RÉEL de ce harnais
        # (fast-path déterministe inclus — même chemin qu'un tour normal).
        state = run(
            _run_turn(state, interpreter, runtime, text="je veux en vendre seulement 50 L")
        )
        assert state.get("current_goal") == "SALES_PUBLISH_PRODUCT"
        payload = state.get("transaction_payload") or {}
        assert payload.get("product") == "miel"
        assert payload.get("quantity") == 50.0, (
            f"la quantité corrigée (50 L) n'a pas été reprise : {payload!r}"
        )
