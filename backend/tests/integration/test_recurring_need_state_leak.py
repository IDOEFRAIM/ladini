"""Full REAL node chain for the `recurring_need` state-leak bug (2026-09-23) —
même méthode que `test_tier_selection_full_node_chain.py` (nœuds réels, ordre
réel, reducers LangGraph réels, pas un `dict.update()` naïf qui masquerait
exactement la classe de bug traquée ici).

Scénario signalé : "10 kg de tomate tous les jours sauf dimanche" -> confirmé
("ok") -> "20 kg d'oignon tous les jours sauf dimanche" réutilise l'ancien
état tomate au lieu d'un nouveau draft propre.

Chaîne : input_interpreter -> goal_planner -> memory_update -> validator ->
buyer_context_resolver (dispatch réel vers `recurring_need_flow`) ->
state_cleaner_node -> post_response_cleanup, pour chaque tour. `cognitive_guard`
est délibérément omis (comme dans le test de référence) : pour une
classification nette (confiance correcte, pas de disambiguation en
compétition), son rôle est un simple passe-plat vers `goal_planner` — ce
n'est pas la zone d'état investiguée ici.
"""
from __future__ import annotations

import typing
from typing import Any, Dict, List, Optional

from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    get_pending_interaction,
    to_tunnel_category,
)
from ladini.graphs.agents.market_coach.core.state import MarketAgentState
from ladini.graphs.agents.market_coach.domain.recurring_need_draft import (
    RecurringNeedDraft,
)
from ladini.graphs.agents.market_coach.flows.buyer.flow import (
    buyer_context_resolver,
)
from ladini.graphs.agents.market_coach.interpreter.routing import (
    make_input_interpreter,
)
from ladini.graphs.agents.market_coach.nodes.cleaner import state_cleaner_node
from ladini.graphs.agents.market_coach.nodes.cleanup import post_response_cleanup
from ladini.graphs.agents.market_coach.nodes.memory import memory_update
from ladini.graphs.agents.market_coach.nodes.validation import validator
from tests.conftest import ScriptedLLM, StubRuntime, run

_HINTS = typing.get_type_hints(MarketAgentState, include_extras=True)


def _reducer_for(field: str):
    ann = _HINTS.get(field)
    if ann is None:
        return None
    metadata = getattr(ann, "__metadata__", None)
    if not metadata:
        return None
    return metadata[0]


def apply_patch(state: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """Merge `patch` into `state` using each field's REAL LangGraph reducer —
    voir `test_tier_selection_full_node_chain.py::apply_patch` (même
    fonction, dupliquée ici pour ne pas créer de dépendance inter-fichiers de
    test)."""
    new_state = dict(state)
    for key, value in patch.items():
        reducer = _reducer_for(key)
        if reducer is None:
            new_state[key] = value
            continue
        new_state[key] = reducer(state.get(key), value)
    return new_state


def _new_task_payload(
    intent: str,
    *,
    product: str,
    quantity: float,
    unit: str,
    recurrence_type: str = "DAILY",
    excluded_weekdays: Optional[List[int]] = None,
) -> Dict[str, Any]:
    return {
        "disposition": "NEW_TASK",
        "intent": intent,
        "confidence": 0.95,
        "entities": {
            "product": product,
            "quantity": quantity,
            "unit": unit,
            "recurrence_type": recurrence_type,
            "excluded_weekdays": excluded_weekdays or [],
        },
    }


async def _run_turn(
    state: Dict[str, Any],
    interpreter,
    runtime: StubRuntime,
    *,
    text: str,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Un tour complet : interpreter -> goal_planner -> memory_update ->
    validator -> buyer_context_resolver -> state_cleaner -> post_response_cleanup.

    Retourne `(turn_state, next_state)` : `turn_state` est l'état juste après
    `buyer_context_resolver` (avant que les nœuds de fin de tour n'effacent
    les champs éphémères mono-tour comme `detected_intent` — voir
    `nodes/cleanup.py::_EPHEMERAL_REPLACE_FIELDS`), `next_state` est l'état
    RÉELLEMENT transmis au tour suivant (après nettoyage complet)."""
    from ladini.graphs.agents.market_coach.interpreter.goal_planner import (
        goal_planner,
    )

    state = dict(state)
    state["normalized_text"] = text
    state["user_query"] = text

    interp = await interpreter(state, runtime)
    state = apply_patch(state, interp)

    gp = await goal_planner(state, runtime)
    state = apply_patch(state, gp)

    mem = await memory_update(state, runtime)
    state = apply_patch(state, mem)

    val = await validator(state, runtime)
    state = apply_patch(state, val)

    ctx = await buyer_context_resolver(state, runtime)
    turn_state = apply_patch(state, ctx)

    clean = await state_cleaner_node(turn_state, runtime)
    next_state = apply_patch(turn_state, clean)

    post = await post_response_cleanup(next_state, runtime)
    next_state = apply_patch(next_state, post)

    return turn_state, next_state


def _initial_state() -> Dict[str, Any]:
    return {
        "current_goal": None,
        "status": "",
        "user_phone": "+22670000000",
        "user_role": "BUYER",
        "transaction_payload": {},
        "extracted_entities": {},
        "working_memory": {},
        "recurring_need_draft": None,
        **{"pending_interaction": None},
    }


class TestRecurringNeedStateLeak:
    def test_two_consecutive_operations_do_not_contaminate_each_other(self):
        """Scénario exact du bug rapporté, mandat §13 : 4 tours, tomate PUIS
        oignon, aucune trace de tomate dans les tours 3/4, 2 recurring_needs
        DISTINCTS créés en base, sans mélange."""
        created_needs: List[Dict[str, Any]] = []

        def _create_recurring_need(**kwargs: Any) -> Dict[str, Any]:
            need_id = f"need-{len(created_needs) + 1}"
            created_needs.append({"recurring_need_id": need_id, **kwargs})
            return {"status": "success", "recurring_need_id": need_id, "sub_category": kwargs.get("product_query")}

        runtime = StubRuntime(responses={"create_recurring_need": _create_recurring_need})
        interpreter = make_input_interpreter("BUYER")
        state = _initial_state()

        # ---------------- TURN 1 : "10 kg tomate tous les jours sauf dimanche" ----
        runtime.llm = ScriptedLLM(
            _new_task_payload(
                "CREATE_RECURRING_NEED", product="tomate", quantity=10.0, unit="KG",
                excluded_weekdays=[7],
            )
        )
        turn1, state = run(
            _run_turn(
                state, interpreter, runtime,
                text="j ai besoin de 10 kg de tomate tous les jours sauf les dimanches",
            )
        )
        assert turn1["detected_intent"] == "CREATE_RECURRING_NEED", turn1.get("raw_analysis")
        assert state["status"] == "WAITING_INPUT"
        draft_a = RecurringNeedDraft.from_dict(state.get("recurring_need_draft"))
        assert draft_a is not None
        assert draft_a.product == "tomate"
        assert draft_a.quantity == 10.0
        assert draft_a.unit == "KG"
        # Pas de doublon d'unité dans le récapitulatif affiché à l'utilisateur.
        assert "KG KG" not in (state.get("final_response") or "")
        assert "10 kg" in (state.get("final_response") or "").lower()
        assert to_tunnel_category(get_pending_interaction(state)) == "CONFIRMATION"

        # ---------------- TURN 2 : "ok" ----------------
        turn2, state = run(_run_turn(state, interpreter, runtime, text="ok"))
        assert len(created_needs) == 1, (
            f"la confirmation n'a pas déclenché l'écriture DB — "
            f"final_response={state.get('final_response')!r}"
        )
        assert created_needs[0]["product_query"] == "tomate"
        assert created_needs[0]["quantity"] == 10.0
        assert created_needs[0]["excluded_weekdays"] == [7]
        assert state["status"] == "COMPLETED"
        assert to_tunnel_category(get_pending_interaction(state)) == "NONE", (
            "la PendingInteraction de confirmation doit être consommée — "
            f"pending={get_pending_interaction(state)}"
        )

        # ---------------- TURN 3 : "20 kg d'oignon tous les jours sauf dimanche" --
        runtime.llm = ScriptedLLM(
            _new_task_payload(
                "CREATE_RECURRING_NEED", product="oignon", quantity=20.0, unit="KG",
                excluded_weekdays=[7],
            )
        )
        turn3, state = run(
            _run_turn(
                state, interpreter, runtime,
                text="j ai besoin de 20kg d oignon tous les jours sauf les dimanches",
            )
        )
        assert turn3["detected_intent"] == "CREATE_RECURRING_NEED", turn3.get("raw_analysis")
        response = (state.get("final_response") or "").lower()
        assert "tomate" not in response, f"contamination tomate détectée : {response!r}"
        assert "catalogue" not in response and "appel d'offres" not in response, (
            f"repli désambiguïsation catalogue/appel d'offres au lieu du draft oignon : {response!r}"
        )
        assert "oignon" in response, f"le récapitulatif ne porte pas sur l'oignon : {response!r}"
        draft_b = RecurringNeedDraft.from_dict(state.get("recurring_need_draft"))
        assert draft_b is not None
        assert draft_b.draft_id != draft_a.draft_id, "un NOUVEAU draft_id est attendu, pas la réutilisation du draft A"
        assert draft_b.product == "oignon"
        assert draft_b.quantity == 20.0

        # ---------------- TURN 4 : "ok" ----------------
        turn4, state = run(_run_turn(state, interpreter, runtime, text="ok"))
        assert len(created_needs) == 2, "le second besoin (oignon) n'a jamais été créé en base"
        assert created_needs[1]["product_query"] == "oignon"
        assert created_needs[1]["quantity"] == 20.0
        assert created_needs[1]["excluded_weekdays"] == [7]

        # Aucun mélange entre les deux besoins créés.
        assert created_needs[0]["product_query"] != created_needs[1]["product_query"]


class TestRecurringNeedCancellation:
    def test_a_cancelled_draft_never_resurfaces_on_the_next_operation(self):
        """Mandat §14 : "10 kg tomate..." -> confirmation -> "annuler" -> "20
        kg oignon..." — la tomate ne doit JAMAIS réapparaître, et aucun
        `recurring_need` tomate ne doit avoir été créé en base."""
        created_needs: List[Dict[str, Any]] = []

        def _create_recurring_need(**kwargs: Any) -> Dict[str, Any]:
            created_needs.append(kwargs)
            return {"status": "success", "recurring_need_id": "need-x"}

        runtime = StubRuntime(responses={"create_recurring_need": _create_recurring_need})
        interpreter = make_input_interpreter("BUYER")
        state = _initial_state()

        runtime.llm = ScriptedLLM(
            _new_task_payload("CREATE_RECURRING_NEED", product="tomate", quantity=10.0, unit="KG", excluded_weekdays=[7])
        )
        _turn1, state = run(
            _run_turn(state, interpreter, runtime, text="j ai besoin de 10 kg de tomate tous les jours sauf les dimanches")
        )
        assert to_tunnel_category(get_pending_interaction(state)) == "CONFIRMATION"

        # "annuler" est reconnu par le filet déterministe REJECT
        # (`_REJECT_EXACT_PHRASES`) — aucun ScriptedLLM nécessaire ici.
        _turn2, state = run(_run_turn(state, interpreter, runtime, text="annuler"))
        assert len(created_needs) == 0, "aucune écriture DB ne doit avoir eu lieu sur un refus"

        runtime.llm = ScriptedLLM(
            _new_task_payload("CREATE_RECURRING_NEED", product="oignon", quantity=20.0, unit="KG", excluded_weekdays=[7])
        )
        turn3, state = run(
            _run_turn(state, interpreter, runtime, text="j ai besoin de 20kg d oignon tous les jours sauf les dimanches")
        )
        assert turn3["detected_intent"] == "CREATE_RECURRING_NEED", turn3.get("raw_analysis")
        response = (state.get("final_response") or "").lower()
        assert "tomate" not in response, f"contamination tomate après annulation : {response!r}"
        assert "oignon" in response

        _turn4, state = run(_run_turn(state, interpreter, runtime, text="ok"))
        assert len(created_needs) == 1
        assert created_needs[0]["product_query"] == "oignon"


class TestRecurringNeedModificationIsPreserved:
    """Mandat §15 : le correctif du state-leak (repli sur un draft neuf quand
    `interpreted_event == "NEW_TASK"`) ne doit JAMAIS s'appliquer à une VRAIE
    continuation du même draft (`interpreted_event` autre que "NEW_TASK",
    ex: "ANSWER" — produit par la route ACTIVE_SLOT quand un champ est
    explicitement attendu, jamais par le micro-prompt NEW_TASK). Testé au
    niveau du domaine directement : c'est cette distinction précise
    (`interpreted_event`), pas une reclassification LLM incertaine, qui
    décide — voir `flows/buyer/recurring_need.py::_create_flow`."""

    def test_an_answer_event_updates_the_same_draft_in_place(self):
        from ladini.graphs.agents.market_coach.flows.buyer.recurring_need import (
            recurring_need_flow,
        )

        existing = RecurringNeedDraft.new(
            draft_id="draft-tomate",
            product="tomate",
            quantity=10.0,
            unit="KG",
            recurrence_type="DAILY",
            excluded_weekdays=[7],
        )
        state = {
            "current_goal": "CREATE_RECURRING_NEED",
            "user_phone": "+22670000000",
            "interpreted_event": "ANSWER",
            "transaction_payload": {"quantity": 15.0},
            "recurring_need_draft": existing.to_dict(),
            "pending_interaction": {
                "kind": "CONFIRM_ACTION",
                "target": {"draft_id": "draft-tomate", "draft_version": existing.version},
            },
        }
        runtime = StubRuntime()
        patch = run(recurring_need_flow(state, runtime))

        updated = RecurringNeedDraft.from_dict(patch.get("recurring_need_draft"))
        assert updated is not None
        assert updated.draft_id == "draft-tomate", "même draft — une continuation ne crée jamais un nouveau draft_id"
        assert updated.product == "tomate", "le produit déjà connu doit survivre à une correction partielle"
        assert updated.quantity == 15.0
        assert updated.version > existing.version


class TestRecurringNeedConfirmationIdempotency:
    def test_a_redelivered_ok_never_creates_a_second_recurring_need(self):
        """Mandat §16 : un webhook redélivré applique le MÊME message ("ok")
        DEUX FOIS contre le MÊME état de départ (c'est la définition d'une
        redélivraison — jamais un 2e tour distinct où le premier "ok" a déjà
        fait avancer l'état/consommé la PendingInteraction, ce qui ne teste
        plus rien d'idempotent). Ne doit jamais produire une double écriture ;
        un message suivant sans rapport (oignon) doit ensuite fonctionner
        normalement."""
        created_needs: List[Dict[str, Any]] = []
        seen_idempotency_keys: Dict[str, Dict[str, Any]] = {}

        def _create_recurring_need(**kwargs: Any) -> Dict[str, Any]:
            # Simule la dédup RÉELLE côté serveur (`infrastructure/mcp/
            # runtime.py`, `services/database/mcp_idempotency_store.py` —
            # claim/REPLAY sur `_idempotency_key`, appliquée AVANT le
            # handler) : `StubRuntime.call_db` (doublure de test) bypasse
            # entièrement cette couche MCP réelle, donc un test d'idempotence
            # doit la reproduire ici pour rester représentatif — sinon il ne
            # teste que `claim_once` (Redis, best-effort, fail-open sans
            # Redis — voir le log "IDEMPOTENCY_REDIS_ERROR" observé en local),
            # jamais la garantie RÉELLE qui protège la production.
            key = kwargs.get("idempotency_key")
            if key and key in seen_idempotency_keys:
                return seen_idempotency_keys[key]
            result = {"status": "success", "recurring_need_id": f"need-{len(created_needs) + 1}"}
            created_needs.append(kwargs)
            if key:
                seen_idempotency_keys[key] = result
            return result

        runtime = StubRuntime(responses={"create_recurring_need": _create_recurring_need})
        interpreter = make_input_interpreter("BUYER")
        state = _initial_state()

        runtime.llm = ScriptedLLM(
            _new_task_payload("CREATE_RECURRING_NEED", product="tomate", quantity=10.0, unit="KG", excluded_weekdays=[7])
        )
        _turn1, state_before_confirm = run(
            _run_turn(state, interpreter, runtime, text="j ai besoin de 10 kg de tomate tous les jours sauf les dimanches")
        )
        assert to_tunnel_category(get_pending_interaction(state_before_confirm)) == "CONFIRMATION"

        # Redélivraison webhook : le MÊME "ok" est rejoué deux fois contre le
        # MÊME état de départ (jamais l'état déjà avancé par le 1er appel).
        _redelivery_a, state_after_a = run(
            _run_turn(state_before_confirm, interpreter, runtime, text="ok")
        )
        assert len(created_needs) == 1
        _redelivery_b, _state_after_b = run(
            _run_turn(state_before_confirm, interpreter, runtime, text="ok")
        )
        assert len(created_needs) == 1, "une redélivraison du même \"ok\" ne doit jamais recréer le besoin"

        state = state_after_a
        runtime.llm = ScriptedLLM(
            _new_task_payload("CREATE_RECURRING_NEED", product="oignon", quantity=20.0, unit="KG", excluded_weekdays=[7])
        )
        _turn3, state = run(
            _run_turn(state, interpreter, runtime, text="j ai besoin de 20kg d oignon tous les jours sauf les dimanches")
        )
        _turn4, state = run(_run_turn(state, interpreter, runtime, text="ok"))
        assert len(created_needs) == 2
        assert created_needs[1]["product_query"] == "oignon"
