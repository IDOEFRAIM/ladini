"""Interruption d'une confirmation active par une nouvelle intention explicite
(mandat 2026-09-30, Partie A).

Scénario réel : un producteur a un draft SALES_PUBLISH_PRODUCT(lait) en attente de
CONFIRM_ACTION (paliers sachet/bidon, jamais confirmé) et dit "je veux vendre mon
miel" — une NOUVELLE intention explicite, MÊME type de goal (SALES_PUBLISH_PRODUCT)
mais produit différent. Avant ce correctif, `nodes/cognitive.py::cognitive_guard`
excluait ce message de sa détection d'interruption générale (même nom de goal que
`current_goal`, condition pensée pour ne pas casser un tunnel sur sa propre
continuation) — le message retombait sur CONTINUE_ACTIVE_GOAL et ses entités
("product": "miel") atteignaient le fallthrough générique de
`domain/sales_publish_draft.py::resolve_domain_action`, qui les fusionnait
SILENCIEUSEMENT dans le draft lait actif (`UpdateSalesPublishDraft`) — jamais annulé,
jamais réellement démarré pour le miel.

Comportement cible (conservateur, mandat §A2/§A3) : ni switch automatique, ni silence
— une question fermée oui/non. « oui » annule proprement l'ancien draft (réutilise le
nettoyage transactionnel de l'Étape 5) puis démarre la nouvelle intention à partir des
entités déjà dites (product=miel), sans redemander au client de tout retaper. « non »
laisse l'ancien draft intact, mot pour mot."""
from __future__ import annotations

import typing
from typing import Any, Dict, Optional

import pytest

from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    get_pending_interaction,
    set_pending_interaction,
)
from ladini.graphs.agents.market_coach.core.state import (
    MarketAgentState,
    resolve_current_goal,
)
from ladini.graphs.agents.market_coach.domain.sales_publish_draft import (
    SalesPublishDraft,
)
from ladini.graphs.agents.market_coach.interpreter.goal_planner import goal_planner
from ladini.graphs.agents.market_coach.interpreter.routing import (
    make_input_interpreter,
)
from ladini.graphs.agents.market_coach.nodes.cleaner import state_cleaner_node
from ladini.graphs.agents.market_coach.nodes.cleanup import post_response_cleanup
from ladini.graphs.agents.market_coach.nodes.cognitive import cognitive_guard
from ladini.graphs.agents.market_coach.nodes.confirmation_gate import confirmation_gate
from ladini.graphs.agents.market_coach.nodes.memory import memory_update
from ladini.graphs.agents.market_coach.nodes.validation import validator
from tests.conftest import ForbiddenLLM, ScriptedLLM, StubRuntime, make_state, run

_HINTS = typing.get_type_hints(MarketAgentState, include_extras=True)


def _reducer_for(field: str):
    ann = _HINTS.get(field)
    if ann is None:
        return None
    metadata = getattr(ann, "__metadata__", None)
    if not metadata:
        return None
    return metadata[0]


def _apply_patch(state: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    new_state = dict(state)
    for key, value in patch.items():
        reducer = _reducer_for(key)
        new_state[key] = value if reducer is None else reducer(state.get(key), value)
    return new_state


class _InMemoryDraftStore:
    def __init__(self) -> None:
        self.rows: Dict[str, SalesPublishDraft] = {}

    async def load(self, draft_id: str) -> Optional[SalesPublishDraft]:
        return self.rows.get(draft_id)

    async def insert(self, draft: SalesPublishDraft, *, conversation_id: str) -> bool:
        self.rows[draft.draft_id] = draft
        return True

    async def compare_and_swap(self, draft_id, *, expected_version, new_draft) -> bool:
        current = self.rows.get(draft_id)
        if current is None or current.version != expected_version:
            return False
        self.rows[draft_id] = new_draft
        return True


@pytest.fixture()
def draft_store(monkeypatch):
    import ladini.graphs.agents.market_coach.domain.sales_publish_draft as sd_mod
    import ladini.services.database.sales_publish_draft_store as mod

    store = _InMemoryDraftStore()
    monkeypatch.setattr(mod, "load", store.load)
    monkeypatch.setattr(mod, "insert", store.insert)
    monkeypatch.setattr(mod, "compare_and_swap", store.compare_and_swap)
    # `claim_once` (core/idempotency.py) hits a REAL Redis instance with a 1h TTL —
    # without this, re-running this file within the same hour makes CONFIRM
    # non-deterministically fail (`ALREADY_EXECUTING`) on a draft_id reused from a
    # previous run. Same pattern as
    # tests/architecture/test_sales_publish_draft_transactional_contract.py.
    monkeypatch.setattr(sd_mod, "claim_once", lambda key: True)
    return store


async def _run_full_turn(state, interpreter, runtime, *, text):
    """Rejoue un tour COMPLET — y compris `post_response_cleanup` (cette hygiène
    de fin de tour, qui réinitialise notamment `extracted_entities`/`current_goal`,
    tourne sur CHAQUE tour en production ; l'omettre ferait porter à tort un bug de
    harnais de test comme un bug produit — piège déjà rencontré en construisant ce
    test)."""
    state = dict(state)
    state["normalized_text"] = text
    state["user_query"] = text

    state = _apply_patch(state, await interpreter(state, runtime))
    state = _apply_patch(state, await cognitive_guard(state, runtime))
    if (state.get("cognitive_decision") or {}).get("action") == "ASK_SWITCH_CONFIRMATION":
        # Matches `_COGNITIVE_ACTION_ROUTES`: routes straight "to_strategy",
        # bypassing goal_planner/memory_update/validator/confirmation_gate — the
        # active draft must not be touched at all while the question is pending.
        state = _apply_patch(state, await state_cleaner_node(state, runtime))
        state = _apply_patch(state, await post_response_cleanup(state, runtime))
        return state
    state = _apply_patch(state, await goal_planner(state, runtime))
    state = _apply_patch(state, await memory_update(state, runtime))
    state = _apply_patch(state, await validator(state, runtime))
    state = _apply_patch(state, await confirmation_gate(state, runtime))
    state = _apply_patch(state, await state_cleaner_node(state, runtime))
    state = _apply_patch(state, await post_response_cleanup(state, runtime))
    return state


def _seeded_lait_state(draft_store, *, draft_id: str) -> Dict[str, Any]:
    draft = SalesPublishDraft.new(
        draft_id=draft_id, product="lait", quantity=55.0, unit="LITRE", price=500.0,
        pricing_tiers=[
            {"quantity": 0.5, "unit": "LITRE", "price": 500.0, "packaging": "sachet"},
            {"quantity": 0.5, "unit": "LITRE", "price": 600.0, "packaging": "bidon"},
        ],
    )
    run(draft_store.insert(draft, conversation_id="+22670000099"))

    state = make_state(
        current_goal="SALES_PUBLISH_PRODUCT",
        working_memory={"active_goal": "SALES_PUBLISH_PRODUCT"},
        transaction_payload={
            "product": "lait", "quantity": 55.0, "unit": "LITRE", "price": 500.0,
            "pricing_tiers": list(draft.pricing_tiers or ()),
        },
        stable_entities={"product": "lait", "quantity": 55.0, "unit": "LITRE"},
        sales_publish_draft=draft.to_dict(),
        user_role="PRODUCER", user_phone="+22670000099",
    )
    state.update(
        set_pending_interaction(
            InteractionKind.CONFIRM_ACTION, context_ref="confirmation",
            target={"draft_id": draft.draft_id, "draft_version": draft.version},
        )
    )
    return state


def _new_task_miel_llm() -> ScriptedLLM:
    return ScriptedLLM({
        "disposition": "NEW_TASK", "intent": "SALES_PUBLISH_PRODUCT",
        "confidence": 0.95, "entities": {"product": "miel"},
    })


class TestNewTaskDuringConfirmationAsksBeforeSwitching:
    """Mandat §A5, Test 1 : le draft lait ne doit ni se confirmer, ni se
    modifier, ni basculer silencieusement — une question fermée est posée."""

    def test_asks_instead_of_silently_merging_or_switching(self, draft_store):
        state = _seeded_lait_state(draft_store, draft_id="sd-t1")
        runtime = StubRuntime()
        runtime.llm = _new_task_miel_llm()
        interpreter = make_input_interpreter("PRODUCER")

        after = run(_run_full_turn(state, interpreter, runtime, text="je veux vendre mon miel"))

        assert resolve_current_goal(after) == "SALES_PUBLISH_PRODUCT"
        assert (after.get("sales_publish_draft") or {}).get("product") == "lait"
        assert (after.get("sales_publish_draft") or {}).get("version") == 1
        final_response = after.get("final_response") or ""
        assert "lait" in final_response
        assert "miel" in final_response
        pending = get_pending_interaction(after)
        assert pending.kind == InteractionKind.CONFIRM_ACTION
        assert pending.context_ref == "confirmation_switch"


class TestConfirmingTheSwitchCancelsOldAndStartsNew:
    """Mandat §A5, Test 2 : « oui » annule le lait (persistance comprise) et
    démarre le miel à partir de l'entité déjà dite, sans redemander le produit."""

    def test_yes_cancels_old_draft_and_starts_new_goal_from_stated_entities(self, draft_store):
        state = _seeded_lait_state(draft_store, draft_id="sd-t2")
        runtime = StubRuntime()
        runtime.llm = _new_task_miel_llm()
        interpreter = make_input_interpreter("PRODUCER")
        asked = run(_run_full_turn(state, interpreter, runtime, text="je veux vendre mon miel"))

        runtime.llm = ForbiddenLLM()  # "oui" est résolu par le filet déterministe
        after = run(_run_full_turn(asked, interpreter, runtime, text="oui"))

        assert resolve_current_goal(after) == "SALES_PUBLISH_PRODUCT"
        assert after.get("sales_publish_draft") is None
        payload = after.get("transaction_payload") or {}
        assert payload.get("product") == "miel"
        assert (after.get("stable_entities") or {}).get("product") == "miel"
        pending = get_pending_interaction(after)
        assert pending.kind == InteractionKind.ENTER_FIELD
        assert "quantity" in (after.get("missing_fields") or [])
        # Le premier champ manquant EST la quantité (mandat : "expected=QUANTITY").
        assert (after.get("missing_fields") or [None])[0] == "quantity"

    def test_yes_persists_the_old_draft_as_cancelled_in_the_store(self, draft_store):
        state = _seeded_lait_state(draft_store, draft_id="sd-t2b")
        runtime = StubRuntime()
        runtime.llm = _new_task_miel_llm()
        interpreter = make_input_interpreter("PRODUCER")
        asked = run(_run_full_turn(state, interpreter, runtime, text="je veux vendre mon miel"))

        runtime.llm = ForbiddenLLM()
        run(_run_full_turn(asked, interpreter, runtime, text="oui"))

        persisted = run(draft_store.load("sd-t2b"))
        assert persisted is not None
        assert persisted.status.value == "CANCELLED"


class TestRejectingTheSwitchKeepsTheOldDraftIntact:
    """Mandat §A5, Test 3 : « non » laisse le lait WAITING_CONFIRMATION, aucun
    flow miel actif."""

    def test_no_restores_the_original_confirmation_untouched(self, draft_store):
        state = _seeded_lait_state(draft_store, draft_id="sd-t3")
        runtime = StubRuntime()
        runtime.llm = _new_task_miel_llm()
        interpreter = make_input_interpreter("PRODUCER")
        asked = run(_run_full_turn(state, interpreter, runtime, text="je veux vendre mon miel"))

        runtime.llm = ForbiddenLLM()
        after = run(_run_full_turn(asked, interpreter, runtime, text="non"))

        assert resolve_current_goal(after) == "SALES_PUBLISH_PRODUCT"
        draft = after.get("sales_publish_draft") or {}
        assert draft.get("product") == "lait"
        assert draft.get("draft_id") == "sd-t3"
        assert draft.get("version") == 1
        assert draft.get("status") == "DRAFT"
        pending = get_pending_interaction(after)
        assert pending.kind == InteractionKind.CONFIRM_ACTION
        assert pending.context_ref == "confirmation"
        assert pending.target == {"draft_id": "sd-t3", "draft_version": 1}


class TestCorrectionDuringConfirmationNeverTriggersTheSwitchAsk:
    """Mandat §A5, Test 4 : une correction du draft ACTIF ("finalement 600 FCFA
    le bidon") ne doit jamais être lue comme une nouvelle intention — aucun nom
    de produit n'est prononcé, donc rien à comparer/basculer."""

    def test_a_pricing_correction_without_a_product_name_stays_a_correction(self, draft_store):
        state = _seeded_lait_state(draft_store, draft_id="sd-t4")
        runtime = StubRuntime()
        runtime.llm = ScriptedLLM({
            "disposition": "NEW_TASK", "intent": "SALES_PUBLISH_PRODUCT", "confidence": 0.9,
            "entities": {
                "pricing_tiers": [{"quantity": 0.5, "unit": "LITRE", "price": 600.0, "packaging": "bidon"}]
            },
        })
        interpreter = make_input_interpreter("PRODUCER")

        after = run(_run_full_turn(state, interpreter, runtime, text="finalement 600 FCFA le bidon"))

        assert (after.get("cognitive_decision") or {}).get("action") != "ASK_SWITCH_CONFIRMATION"
        assert get_pending_interaction(after).context_ref != "confirmation_switch"


class TestBareConfirmDuringConfirmationIsUnaffected:
    """Mandat §A5, Test 5 : un « ok » nu confirme normalement — le nouveau
    mécanisme n'interfère jamais avec le chemin CONFIRM standard."""

    async def _run_up_to_confirmation_gate(self, state, interpreter, runtime, *, text):
        state = dict(state)
        state["normalized_text"] = text
        state["user_query"] = text
        state = _apply_patch(state, await interpreter(state, runtime))
        state = _apply_patch(state, await cognitive_guard(state, runtime))
        state = _apply_patch(state, await goal_planner(state, runtime))
        state = _apply_patch(state, await memory_update(state, runtime))
        state = _apply_patch(state, await validator(state, runtime))
        state = _apply_patch(state, await confirmation_gate(state, runtime))
        return state

    def test_ok_still_confirms_normally(self, draft_store):
        state = _seeded_lait_state(draft_store, draft_id="sd-t5")
        runtime = StubRuntime()
        runtime.llm = ForbiddenLLM()
        interpreter = make_input_interpreter("PRODUCER")

        after = run(self._run_up_to_confirmation_gate(state, interpreter, runtime, text="ok"))

        assert after.get("status") == "EXECUTING"
        assert after.get("execution_authorized") is True
