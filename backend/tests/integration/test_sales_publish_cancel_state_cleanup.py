"""Étape 5 — hygiène d'état après CANCEL/REJECT/TIMEOUT sur SALES_PUBLISH_PRODUCT.

Réplique, au niveau du graphe (interpreter -> cognitive_guard -> goal_planner
-> memory_update -> validator -> confirmation_gate -> state_cleaner_node), le
scénario exact du mandat (§12) : une confirmation active sur un draft
« lait / 60 L / 500 FCFA le sachet de 2 L », annulée par un « annule » nu,
suivie d'un « je veux vendre mon miel » qui NE DOIT hériter d'aucune valeur
lait/60/sachet.

Root cause (voir `flows/producer/sales_confirmation.py` et `nodes/cognitive.py`
pour les correctifs) : deux bugs indépendants convertissaient silencieusement
ce CANCEL en UPDATE, empêchant le draft d'atteindre CANCELLED et donc
empêchant `state_cleaner_node` de déclencher son nettoyage terminal existant.
Ces tests prouvent le comportement de bout en bout, pas seulement au niveau
unitaire (déjà couvert par `tests/architecture/
test_sales_publish_draft_transactional_contract.py::
TestNodeLevelBareRejectTerminatesTheDraft` et `tests/nodes/
test_cognitive_guard_and_orchestrator.py::TestEntityCarryForward`)."""
from __future__ import annotations

import typing
from typing import Any, Dict, Optional

import pytest

from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    set_pending_interaction,
)
from ladini.graphs.agents.market_coach.core.state import MarketAgentState
from ladini.graphs.agents.market_coach.domain.sales_publish_draft import (
    SalesPublishDraft,
)
from ladini.graphs.agents.market_coach.interpreter.goal_planner import goal_planner
from ladini.graphs.agents.market_coach.interpreter.routing import (
    make_input_interpreter,
)
from ladini.graphs.agents.market_coach.nodes.cleaner import state_cleaner_node
from ladini.graphs.agents.market_coach.nodes.cognitive import cognitive_guard
from ladini.graphs.agents.market_coach.nodes.confirmation_gate import (
    confirmation_gate,
)
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
    import ladini.services.database.sales_publish_draft_store as mod

    store = _InMemoryDraftStore()
    monkeypatch.setattr(mod, "load", store.load)
    monkeypatch.setattr(mod, "insert", store.insert)
    monkeypatch.setattr(mod, "compare_and_swap", store.compare_and_swap)
    return store


async def _run_full_turn(state, interpreter, runtime, *, text):
    state = dict(state)
    state["normalized_text"] = text
    state["user_query"] = text

    state = _apply_patch(state, await interpreter(state, runtime))
    state = _apply_patch(state, await cognitive_guard(state, runtime))
    state = _apply_patch(state, await goal_planner(state, runtime))
    state = _apply_patch(state, await memory_update(state, runtime))
    state = _apply_patch(state, await validator(state, runtime))
    state = _apply_patch(state, await confirmation_gate(state, runtime))
    state = _apply_patch(state, await state_cleaner_node(state, runtime))
    return state


def _lait_offer(*, enriched: bool) -> Dict[str, Any]:
    package = {
        "package_type": "SACHET", "content_amount": 2.0, "content_unit": "LITRE",
        "status": "KNOWN", "source": "USER_EXPLICIT",
    }
    normalized = None
    if enriched:
        package = {**package, "count": None}
        normalized = {
            "quantity_amount": 60.0, "quantity_unit": "LITRE",
            "unit_price": 250.0, "unit_price_basis": "PER_BASE_UNIT",
        }
    return {
        "schema": 1, "product": "lait",
        "commercial_quantity": {"amount": 60.0, "unit": "LITRE", "source": "USER_EXPLICIT"},
        "inventory_quantity": {"amount": 60.0, "unit": "LITRE", "source": "USER_EXPLICIT"},
        "pricing": {
            "amount": 500.0, "basis": "PER_PACKAGE", "basis_unit": None, "currency": "FCFA",
            "source": "USER_EXPLICIT", "basis_source": "USER_EXPLICIT",
        },
        "package": package,
        "normalized": normalized,
    }


def _seeded_lait_state(draft_store) -> Dict[str, Any]:
    raw_offer = _lait_offer(enriched=False)
    draft = SalesPublishDraft.new(
        draft_id="sd-cancel-replay", product="lait", quantity=60.0, unit="LITRE",
        price=500.0, commercial_offer=raw_offer,
    )
    run(draft_store.insert(draft, conversation_id="+22670000099"))

    state = make_state(
        current_goal="SALES_PUBLISH_PRODUCT",
        working_memory={"active_goal": "SALES_PUBLISH_PRODUCT"},
        transaction_payload={
            "product": "lait", "quantity": 60.0, "unit": "LITRE", "price": 500.0,
            "package_label": "SACHET", "package_size": 2.0, "package_unit": "LITRE",
            # ré-dérivé par le validator à chaque tour — enrichi, donc
            # structurellement différent du draft persisté (voir docstring).
            "commercial_offer": _lait_offer(enriched=True),
        },
        stable_entities={"product": "lait", "quantity": 60.0, "unit": "LITRE"},
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


class TestBareCancelFullyTerminatesAndDoesNotPolluteTheNextSale:
    def test_annule_reaches_a_terminal_completed_status(self, draft_store):
        state = _seeded_lait_state(draft_store)
        runtime = StubRuntime()
        runtime.llm = ForbiddenLLM()  # "annule" est résolu par le filet déterministe, jamais le LLM
        interpreter = make_input_interpreter("PRODUCER")

        after_cancel = run(_run_full_turn(state, interpreter, runtime, text="annule"))

        assert after_cancel.get("status") == "COMPLETED"
        assert after_cancel.get("current_goal") is None
        assert after_cancel.get("sales_publish_draft") is None
        assert after_cancel.get("transaction_payload") == {}
        assert after_cancel.get("stable_entities") == {}
        from ladini.graphs.agents.market_coach.core.pending_interaction import (
            InteractionKind,
            get_pending_interaction,
        )

        assert get_pending_interaction(after_cancel).kind == InteractionKind.NONE

    def test_the_next_unrelated_sale_starts_with_no_lait_residue(self, draft_store):
        state = _seeded_lait_state(draft_store)
        runtime = StubRuntime()
        runtime.llm = ForbiddenLLM()
        interpreter = make_input_interpreter("PRODUCER")

        after_cancel = run(_run_full_turn(state, interpreter, runtime, text="annule"))

        runtime.llm = ScriptedLLM({
            "disposition": "NEW_TASK", "intent": "SALES_PUBLISH_PRODUCT",
            "confidence": 0.95, "entities": {"product": "miel"},
        })
        after_new_sale = run(
            _run_full_turn(after_cancel, interpreter, runtime, text="je veux vendre mon miel")
        )

        payload = after_new_sale.get("transaction_payload") or {}
        stable = after_new_sale.get("stable_entities") or {}
        assert after_new_sale.get("sales_publish_draft") is None
        for values in (payload, stable):
            for key, value in values.items():
                assert value != "lait", f"{key} still carries the cancelled sale's product"
                assert value != 60.0, f"{key} still carries the cancelled sale's quantity"
                assert value != "LITRE", f"{key} still carries the cancelled sale's unit"


class TestTimeoutAbandonReachesTheSameCleanupAsCancel:
    """Mandat §15 : le nettoyage d'un tunnel abandonné pour max-retries
    (`core/conversation_reset.py::reset_abandoned_conversation_context`,
    déjà durci 2026-09-28) doit atteindre le même résultat qu'un CANCEL —
    aucune valeur transactionnelle ne doit survivre."""

    def test_abandon_wipes_transaction_payload_and_stable_entities_wholesale(self):
        from ladini.graphs.agents.market_coach.core.conversation_reset import (
            reset_abandoned_conversation_context,
        )

        state = make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            transaction_payload={"product": "lait", "quantity": 60.0, "unit": "LITRE"},
            stable_entities={
                "product": "lait", "quantity": 60.0, "unit": "LITRE",
                "item_name": "lait", "quantity_kg": 60.0, "unite": "LITRE",
            },
            sales_publish_draft={"draft_id": "sd-timeout", "status": "DRAFT"},
        )
        patch = reset_abandoned_conversation_context(
            state, intent_competition=[], cognitive_decision={}
        )
        merged = _apply_patch(state, patch)

        assert merged.get("transaction_payload") == {}
        assert merged.get("stable_entities") == {}
        assert merged.get("sales_publish_draft") is None
        assert merged.get("current_goal") is None


class TestMidFlowCarryForwardStillWorksAfterTheRejectCancelGuard:
    """Non-régression (mandat §18) : « tant que le flow n'est PAS terminé ou
    annulé, le carry-forward doit continuer à fonctionner ». Le correctif de
    `nodes/cognitive.py::_entity_carry_forward` ne doit désactiver le report
    d'entités QUE pour REJECT/CANCEL — un ANSWER (ex: « 500 » répondant à
    PRICE) doit toujours voir `product` disponible."""

    def test_answering_a_price_question_still_carries_the_locked_product(self, draft_store):
        state = _seeded_lait_state(draft_store)
        # Pas de confirmation active : on est en train de répondre à un champ
        # manquant (ENTER_FIELD), pas sur un CONFIRM_ACTION — cas nominal
        # du carry-forward, indépendant du chemin CANCEL testé plus haut.
        state["pending_interaction"] = None
        state["current_goal"] = "SALES_PUBLISH_PRODUCT"
        state["working_memory"] = {"active_goal": "SALES_PUBLISH_PRODUCT"}
        from ladini.graphs.agents.market_coach.nodes.cognitive import (
            _entity_carry_forward,
        )

        # Vérifie directement l'unité de décision plutôt que de rejouer tout
        # le graphe (déjà exercé ci-dessus pour le chemin CANCEL) : un tour
        # ANSWER dans le même tunnel doit toujours hériter product/unit.
        result = _entity_carry_forward(state, "SALES_PUBLISH_PRODUCT", True, "ANSWER")
        assert result is not None
        assert result.get("product") == "lait"
