"""Étape 7 — continuité conversationnelle ANSWER vs NEW_TASK, replay E2E
(2026-09-30). Même méthode que `test_sales_publish_cross_flow_state_leak.py`
(nœuds RÉELS, ordre réel, reducers LangGraph réels — jamais un
`dict.update()` naïf) : `input_interpreter -> cognitive_guard ->
goal_planner -> memory_update -> validator`.

Scénario exact du mandat :

    User: "je veux vendre mon miel"
    Agent: "Quelle quantité avez-vous ?"
    User: "j'ai environ 90 litres de miel en ce moment"   (réponse PHRASTIQUE,
                                                            pas un pur nombre —
                                                            le fast-path
                                                            numérique durci le
                                                            2026-09-30 s'abstient
                                                            déjà ici, voir
                                                            `is_pure_numeric_answer`)

Le scénario le PIRE CAS est simulé explicitement : le micro-prompt ACTIVE_SLOT
répond (à tort) `DEVIATION`, ce qui déclenche la reclassification NEW_TASK
existante — laquelle répond (à tort, exactement le risque nommé par le
mandat) `STOCK_REGISTER_HARVEST` à confiance élevée. Le correctif de
`cognitive_guard` doit quand même rattraper ce double faux-négatif en amont
en constatant que les entités extraites (`quantity`/`unit`) satisfont déjà le
slot QUANTITY attendu."""
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


class _WorstCaseMisclassificationLLM:
    """Scripte, dans l'ordre, les 3 appels LLM réels possibles d'un tour :
    NEW_TASK (bootstrap du goal, tour 1), ACTIVE_SLOT (tour 2, répond à tort
    DEVIATION), puis NEW_TASK à nouveau (reclassification du message ORIGINAL
    après la déviation — répond à tort STOCK_REGISTER_HARVEST). Même
    technique de dispatch que `test_sales_publish_cross_flow_state_leak.py::
    _DeviationThenNewTaskLLM` (inspection du prompt), étendue à un 2e tour."""

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
        messages = kwargs.get("messages") or []
        blob = json.dumps(messages)
        if "DEVIATION" in blob:
            # Micro-prompt ACTIVE_SLOT — répond (à tort) DEVIATION, confiance
            # suffisante pour ne PAS retomber sur UNKNOWN (voir
            # `active_slot_micro.py::_outcome_for_decision`).
            payload: Dict[str, Any] = {
                "disposition": "DEVIATION",
                "extracted_entities": {},
                "confidence": 0.9,
            }
        elif self.calls == 1:
            payload = {
                "disposition": "NEW_TASK",
                "intent": "SALES_PUBLISH_PRODUCT",
                "confidence": 0.95,
                "entities": {"product": "miel"},
            }
        else:
            # Reclassification NEW_TASK après la DEVIATION ci-dessus — la
            # pire hypothèse nommée par le mandat : le message est
            # (mal) reclassé vers une intention concurrente à confiance
            # élevée, avec les bonnes entités quand même extraites.
            payload = {
                "disposition": "NEW_TASK",
                "intent": "STOCK_REGISTER_HARVEST",
                "confidence": 0.95,
                "entities": {"quantity": 90.0, "unit": "LITRE"},
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

    gp = await goal_planner(state, runtime)
    state = apply_patch(state, gp)

    mem = await memory_update(state, runtime)
    state = apply_patch(state, mem)

    val = await validator(state, runtime)
    state = apply_patch(state, val)

    return state


class TestMielSalesPublishSurvivesWorstCaseMisclassification:
    def test_pilot_scenario_never_switches_to_stock_register_harvest(self):
        state: Dict[str, Any] = {
            "user_phone": "+22670000099",
            "user_role": "PRODUCER",
            "extracted_entities": {},
            "working_memory": {},
        }
        runtime = StubRuntime()
        interpreter = make_input_interpreter("PRODUCER")
        runtime.llm = _WorstCaseMisclassificationLLM()

        # Tour 1 : "je veux vendre mon miel" -> goal=SALES_PUBLISH_PRODUCT,
        # product=miel, quantity/price manquants.
        state = run(_run_turn(state, interpreter, runtime, text="je veux vendre mon miel"))
        assert state.get("current_goal") == "SALES_PUBLISH_PRODUCT"
        assert (state.get("transaction_payload") or {}).get("product") == "miel"
        pending = state.get("pending_interaction") or {}
        assert pending.get("kind") == "ENTER_FIELD" and pending.get("field") == "quantity", (
            f"la 1ère question doit porter sur la quantité : {pending!r}"
        )

        # Tour 2 : réponse PHRASTIQUE (pas un pur nombre) au slot quantity,
        # avec le PIRE CAS scripté : ACTIVE_SLOT dit DEVIATION, puis NEW_TASK
        # dit (à tort) STOCK_REGISTER_HARVEST à confiance 0.95.
        state = run(
            _run_turn(
                state,
                interpreter,
                runtime,
                text="j'ai environ 90 litres de miel en ce moment",
            )
        )

        assert state.get("current_goal") == "SALES_PUBLISH_PRODUCT", (
            f"le tunnel miel n'aurait jamais dû basculer vers un autre goal : "
            f"current_goal={state.get('current_goal')!r}"
        )
        payload = state.get("transaction_payload") or {}
        assert payload.get("product") == "miel", payload
        assert payload.get("quantity") == 90.0, payload
        assert payload.get("unit") == "LITRE", payload
        missing = set(state.get("missing_fields") or [])
        assert "quantity" not in missing, f"quantity doit être satisfaite : {missing!r}"
        assert missing == {"price"}, f"seul price doit rester manquant : {missing!r}"
        new_pending = state.get("pending_interaction") or {}
        assert new_pending.get("field") == "price", (
            f"la question suivante doit porter sur le prix : {new_pending!r}"
        )
        assert state.get("sales_publish_draft") is None, "aucun draft prématuré"


class TestFreshFlowAfterAPreviousTransactionProperlyClosed:
    """Mandat §17 : rejoue le même scénario miel en partant d'un état qui
    représente une vente LAIT antérieure déjà terminée PROPREMENT (nettoyée
    par `state_cleaner`/`post_response_cleanup` — Étapes 5/58 déjà mergées) :
    `current_goal=None`, `pending_interaction` absent, `transaction_payload`
    purgé (`{"__reset__": True}` déjà appliqué par le cleaner). Le nouveau
    flow miel doit démarrer parfaitement propre, sans aucune trace de lait —
    preuve que le correctif de cette étape ne dépend d'aucun historique de
    conversation antérieur, seulement de l'état COURANT au moment du tour."""

    def test_new_miel_flow_starts_clean_after_a_prior_completed_lait_sale(self):
        state: Dict[str, Any] = {
            "user_phone": "+22670000099",
            "user_role": "PRODUCER",
            "extracted_entities": {},
            "working_memory": {},
            # Trace RÉSIDUELLE d'une vente lait déjà terminée — un cleaner
            # correct a déjà vidé `transaction_payload`/`current_goal`, mais
            # `stable_entities` (mémoire longue, distincte) peut légitimement
            # survivre : elle ne doit jamais contaminer le nouveau flow.
            "current_goal": None,
            "transaction_payload": {},
            "stable_entities": {"product": "lait", "unit": "LITRE"},
            "sales_publish_draft": None,
        }
        runtime = StubRuntime()
        interpreter = make_input_interpreter("PRODUCER")
        runtime.llm = _WorstCaseMisclassificationLLM()

        state = run(_run_turn(state, interpreter, runtime, text="je veux vendre mon miel"))
        assert state.get("current_goal") == "SALES_PUBLISH_PRODUCT"
        payload = state.get("transaction_payload") or {}
        assert payload.get("product") == "miel", payload
        assert "quantity" not in payload, f"aucune quantité héritée du lait : {payload!r}"
        pending = state.get("pending_interaction") or {}
        assert pending.get("field") == "quantity", pending

        state = run(
            _run_turn(
                state,
                interpreter,
                runtime,
                text="j'ai environ 90 litres de miel en ce moment",
            )
        )
        assert state.get("current_goal") == "SALES_PUBLISH_PRODUCT"
        payload = state.get("transaction_payload") or {}
        assert payload.get("product") == "miel", payload
        assert payload.get("quantity") == 90.0, payload
        assert payload.get("unit") == "LITRE", payload
        assert set(state.get("missing_fields") or []) == {"price"}, state.get("missing_fields")
