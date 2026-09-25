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

import pytest

from ladini.graphs.agents.market_coach.core.pending_interaction import (
    get_pending_interaction,
    to_tunnel_category,
)
from ladini.graphs.agents.market_coach.core.state import MarketAgentState
from ladini.graphs.agents.market_coach.domain.recurring_need_draft import (
    RecurringNeedDraft,
    RecurringNeedDraftStatus,
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


async def _run_turn_with_cognitive_guard(
    state: Dict[str, Any],
    interpreter,
    runtime: StubRuntime,
    *,
    text: str,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Même chaîne que `_run_turn`, avec `cognitive_guard` inséré entre
    l'interpréteur et `goal_planner` (bug réel production 2026-09-24,
    "14 coq et 57 moutons chèvres chaque semaine" retombant sur le
    catalogue) : `cognitive_guard` est le SEUL propriétaire de la décision
    d'interrompre un goal actif (`RÈGLE 4`, `nodes/cognitive.py`) — c'est
    LUI qui réécrit `interpreted_event` en "INTERRUPTION" quand une intention
    concurrente à confiance suffisante (`_DISAMBIGUATION_CONFIDENCE_
    THRESHOLD = 0.85`) doit primer sur un tunnel encore actif (ex: une
    ancienne interaction catalogue "produit non disponible... appel
    d'offres ? oui/non" encore en attente). L'omettre (comme le fait
    `_run_turn`, délibérément, pour les tests qui n'en ont pas besoin) sous-
    teste exactement ce mécanisme — indispensable ici pour reproduire le bug
    par le VRAI point d'entrée, pas seulement l'interpréteur seul."""
    from ladini.graphs.agents.market_coach.interpreter.goal_planner import (
        goal_planner,
    )
    from ladini.graphs.agents.market_coach.nodes.cognitive import cognitive_guard

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


def _stale_catalog_interaction_state() -> Dict[str, Any]:
    """Reproduit EXACTEMENT l'état laissé par `flows/buyer/procurement.py::
    buyer_request_resolver` (branche "aucun stock", ligne ~589-611) après un
    précédent "produit non disponible dans notre catalogue" — bug réel
    production 2026-09-24 : le PROCHAIN message de l'utilisateur ("14 coq et
    57 moutons chèvres chaque semaine"), pourtant sans aucun rapport, peut
    se retrouver traité comme une réponse à CETTE ancienne interaction
    catalogue plutôt que comme une nouvelle demande CREATE_RECURRING_NEED."""
    return {
        "current_goal": "BUYER_REQUEST",
        "status": "WAITING_INPUT",
        "user_phone": "+22670000000",
        "user_role": "BUYER",
        "transaction_payload": {"product": "mais"},
        "extracted_entities": {},
        "working_memory": {
            "buyer_request_catalog_checked": True,
            "buyer_request_waiting_choice": True,
            "buyer_request_last_product": "mais",
        },
        "recurring_need_draft": None,
        "pending_interaction": {
            "kind": "CONFIRM_ACTION",
            "goal": None,
            "context_ref": "confirmation",
            "status": "ACTIVE",
            "created_at": 0,
        },
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
        draft_id_before_cancel = (state.get("recurring_need_draft") or {}).get("draft_id")
        assert draft_id_before_cancel

        # "annuler" est reconnu par le filet déterministe REJECT
        # (`_REJECT_EXACT_PHRASES`) — aucun ScriptedLLM nécessaire ici.
        _turn2, state = run(_run_turn(state, interpreter, runtime, text="annuler"))
        assert len(created_needs) == 0, "aucune écriture DB ne doit avoir eu lieu sur un refus"
        assert to_tunnel_category(get_pending_interaction(state)) == "NONE", (
            "la PendingInteraction de confirmation doit être consommée par le refus, "
            f"pending={get_pending_interaction(state)}"
        )

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
        draft_id_after_new_request = (state.get("recurring_need_draft") or {}).get("draft_id")
        assert draft_id_after_new_request and draft_id_after_new_request != draft_id_before_cancel, (
            "une demande après annulation doit obtenir un NOUVEAU draft_id, jamais la réutilisation "
            f"de l'ancien ({draft_id_before_cancel!r} -> {draft_id_after_new_request!r})"
        )

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


class TestRecurringNeedMultipleProductsInOneMessage:
    """Chantier multi-produits (2026-09-23), suite du correctif state-leak ci-dessus : quand
    PLUSIEURS produits sont donnés dans le MÊME message ("10 kg de tomate et 20 kg d'oignon tous
    les jours sauf dimanche"), l'oignon ne doit plus jamais disparaître silencieusement — un SEUL
    draft doit porter les DEUX produits, et la confirmation doit déclencher UN SEUL appel MCP
    atomique `create_recurring_needs` (pluriel), jamais deux appels séparés `create_recurring_need`."""

    def test_a_single_message_with_two_products_creates_both_atomically(self):
        created_batches: List[Dict[str, Any]] = []

        def _create_recurring_needs(**kwargs: Any) -> Dict[str, Any]:
            items = kwargs.get("items") or []
            result_items = [
                {
                    "recurring_need_id": f"need-{len(created_batches)}-{i}",
                    "sub_category": it["product_query"],
                    "occurrences_created": 7,
                }
                for i, it in enumerate(items)
            ]
            created_batches.append(kwargs)
            return {"status": "success", "items": result_items}

        runtime = StubRuntime(responses={"create_recurring_needs": _create_recurring_needs})
        interpreter = make_input_interpreter("BUYER")
        state = _initial_state()

        runtime.llm = ScriptedLLM(
            {
                "disposition": "NEW_TASK",
                "intent": "CREATE_RECURRING_NEED",
                "confidence": 0.95,
                "entities": {
                    "product": "tomate",
                    "quantity": 10.0,
                    "unit": "KG",
                    "additional_items": [{"product": "oignon", "quantity": 20.0, "unit": "KG"}],
                    "recurrence_type": "DAILY",
                    "excluded_weekdays": [7],
                },
            }
        )
        turn1, state = run(
            _run_turn(
                state, interpreter, runtime,
                text="j ai besoin de 10 kg de tomate et 20 kg d oignon tous les jours sauf les dimanches",
            )
        )
        assert turn1["detected_intent"] == "CREATE_RECURRING_NEED", turn1.get("raw_analysis")
        assert state["status"] == "WAITING_INPUT"
        response = (state.get("final_response") or "").lower()
        assert "tomate" in response and "oignon" in response, (
            f"le récapitulatif doit lister les DEUX produits, jamais seulement la tomate : {response!r}"
        )

        draft = RecurringNeedDraft.from_dict(state.get("recurring_need_draft"))
        assert draft is not None
        assert draft.product == "tomate"
        assert draft.quantity == 10.0
        assert draft.additional_items == [{"product": "oignon", "quantity": 20.0, "unit": "KG"}]

        _turn2, state = run(_run_turn(state, interpreter, runtime, text="ok"))
        assert len(created_batches) == 1, "la confirmation doit déclencher UN SEUL appel atomique multi-produits"
        assert "create_recurring_need" not in runtime.calls, (
            "un draft multi-produits ne doit jamais passer par l'appel MCP singulier — "
            f"appels observés : {runtime.calls}"
        )
        batch_items = created_batches[0]["items"]
        assert [it["product_query"] for it in batch_items] == ["tomate", "oignon"]
        assert [it["quantity"] for it in batch_items] == [10.0, 20.0]
        assert state["status"] == "COMPLETED"


class TestStateIsFullyCleanAfterSuccess:
    """Mission lifecycle audit (2026-09-23, post multi-produit) : une confirmation réussie ne doit
    laisser AUCUN état transactionnel actif derrière elle — pas seulement un bon message utilisateur
    (mandat §5/§6/§9 de l'audit). Vérifie l'état complet, pas seulement `final_response`."""

    def test_every_transactional_field_is_cleared_after_a_successful_confirmation(self):
        created_needs: List[Dict[str, Any]] = []

        def _create_recurring_need(**kwargs: Any) -> Dict[str, Any]:
            created_needs.append(kwargs)
            return {"status": "success", "recurring_need_id": "need-1"}

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

        _turn2, state = run(_run_turn(state, interpreter, runtime, text="ok"))
        assert len(created_needs) == 1

        # État transactionnel COMPLET — pas seulement le texte envoyé à l'utilisateur.
        assert state.get("current_goal") is None, f"current_goal doit être nettoyé, reste : {state.get('current_goal')!r}"
        assert state.get("recurring_need_draft") is None, "le draft terminé (EXECUTED) ne doit jamais rester actif"
        assert to_tunnel_category(get_pending_interaction(state)) == "NONE", (
            f"aucune PendingInteraction ne doit survivre à une confirmation réussie : {state.get('pending_interaction')!r}"
        )
        assert state.get("transaction_payload") == {}, (
            f"transaction_payload doit être vidé, reste : {state.get('transaction_payload')!r}"
        )
        assert state.get("status") == "COMPLETED"


class TestRealCorrectionUpdatesTheSameDraft:
    """Mission scénario C (§8/§16) : "10 kg tomate" -> "Confirmer ?" -> "modifier" -> correction
    réelle -> "ok". Une VRAIE continuation doit préserver le MÊME `draft_id` de bout en bout, jamais
    en créer un nouveau — contrairement au cas NEW_TASK (state-leak, testé plus haut). Le LLM est
    scripté pour renvoyer UNKNOWN sur "modifier" ET sur la correction elle-même (pire cas réaliste :
    un classifieur sans aucun signal exploitable sur un message nu) — la garde déterministe
    d'extraction quantité+unité (`entities.py`) doit à elle seule appliquer la correction, sans
    jamais dépendre du jugement du LLM ici."""

    def test_modifier_then_a_real_correction_never_creates_a_new_draft(self):
        created_needs: List[Dict[str, Any]] = []

        def _create_recurring_need(**kwargs: Any) -> Dict[str, Any]:
            created_needs.append(kwargs)
            return {"status": "success", "recurring_need_id": "need-1"}

        runtime = StubRuntime(responses={"create_recurring_need": _create_recurring_need})
        interpreter = make_input_interpreter("BUYER")
        state = _initial_state()

        runtime.llm = ScriptedLLM(
            _new_task_payload("CREATE_RECURRING_NEED", product="tomate", quantity=10.0, unit="KG", excluded_weekdays=[7])
        )
        _turn1, state = run(
            _run_turn(state, interpreter, runtime, text="j ai besoin de 10 kg de tomate tous les jours sauf les dimanches")
        )
        draft_id_1 = (state.get("recurring_need_draft") or {}).get("draft_id")
        assert draft_id_1

        # "modifier" nu — aucune information exploitable, le classifieur ne DOIT rien inventer.
        runtime.llm = ScriptedLLM({"disposition": "UNKNOWN", "confidence": 0.3, "entities": {}})
        _turn2, state = run(_run_turn(state, interpreter, runtime, text="modifier"))
        assert len(created_needs) == 0, "aucune écriture DB avant confirmation explicite"
        draft_id_2 = (state.get("recurring_need_draft") or {}).get("draft_id")
        assert draft_id_2 == draft_id_1, "un simple 'modifier' nu ne doit jamais réinitialiser le draft"
        assert (state.get("recurring_need_draft") or {}).get("quantity") == 10.0, "rien n'a encore changé"

        # Correction réelle — LLM toujours UNKNOWN (pire cas) : seule l'extraction déterministe
        # doit permettre à "15 kg" d'atteindre le draft.
        runtime.llm = ScriptedLLM({"disposition": "UNKNOWN", "confidence": 0.3, "entities": {}})
        _turn3, state = run(_run_turn(state, interpreter, runtime, text="mets plutot 15 kg"))
        draft_id_3 = (state.get("recurring_need_draft") or {}).get("draft_id")
        assert draft_id_3 == draft_id_1, "une correction réelle continue le MÊME draft, n'en crée jamais un nouveau"
        assert (state.get("recurring_need_draft") or {}).get("quantity") == 15.0, (
            f"la correction '15 kg' n'a pas atteint le draft : {state.get('recurring_need_draft')!r}"
        )
        assert (state.get("recurring_need_draft") or {}).get("product") == "tomate", "le produit déjà connu doit survivre"

        _turn4, state = run(_run_turn(state, interpreter, runtime, text="ok"))
        assert len(created_needs) == 1, "une seule création, jamais une par tour intermédiaire"
        assert created_needs[0]["quantity"] == 15.0, "c'est la quantité CORRIGÉE qui doit être persistée, jamais l'originale"
        assert created_needs[0]["product_query"] == "tomate"


class TestMultiItemDraftNeverLeaksIntoTheNextSingleItemRequest:
    """Mission scénario D (§10/§16/§18) : un draft multi-produits confirmé (tomate + oignon) ne doit
    JAMAIS laisser `additional_items` (ni le produit principal) réapparaître dans une demande
    mono-produit ultérieure sans rapport — même invariant que le state-leak mono-produit historique,
    étendu à la nouvelle liste `additional_items`."""

    def test_a_new_single_item_request_after_a_confirmed_multi_item_draft_starts_fully_clean(self):
        created_batches: List[Dict[str, Any]] = []
        created_needs: List[Dict[str, Any]] = []

        def _create_recurring_needs(**kwargs: Any) -> Dict[str, Any]:
            items = kwargs.get("items") or []
            created_batches.append(kwargs)
            return {
                "status": "success",
                "items": [
                    {"recurring_need_id": f"multi-{i}", "sub_category": it["product_query"], "occurrences_created": 7}
                    for i, it in enumerate(items)
                ],
            }

        def _create_recurring_need(**kwargs: Any) -> Dict[str, Any]:
            created_needs.append(kwargs)
            return {"status": "success", "recurring_need_id": "need-carotte"}

        runtime = StubRuntime(
            responses={
                "create_recurring_needs": _create_recurring_needs,
                "create_recurring_need": _create_recurring_need,
            }
        )
        interpreter = make_input_interpreter("BUYER")
        state = _initial_state()

        runtime.llm = ScriptedLLM(
            {
                "disposition": "NEW_TASK",
                "intent": "CREATE_RECURRING_NEED",
                "confidence": 0.95,
                "entities": {
                    "product": "tomate",
                    "quantity": 10.0,
                    "unit": "KG",
                    "additional_items": [{"product": "oignon", "quantity": 20.0, "unit": "KG"}],
                    "recurrence_type": "DAILY",
                    "excluded_weekdays": [7],
                },
            }
        )
        _turn1, state = run(
            _run_turn(
                state, interpreter, runtime,
                text="j ai besoin de 10 kg de tomate et 20 kg d oignon tous les jours sauf les dimanches",
            )
        )
        draft = RecurringNeedDraft.from_dict(state.get("recurring_need_draft"))
        assert draft.additional_items == [{"product": "oignon", "quantity": 20.0, "unit": "KG"}]
        multi_draft_id = draft.draft_id

        _turn2, state = run(_run_turn(state, interpreter, runtime, text="ok"))
        assert len(created_batches) == 1, "la confirmation multi-produits doit déclencher l'appel atomique"
        assert state.get("recurring_need_draft") is None, "le draft multi-produits terminé ne doit jamais rester actif"

        # Nouvelle demande MONO-produit, sans rapport — ne doit JAMAIS voir tomate/oignon.
        runtime.llm = ScriptedLLM(
            {
                "disposition": "NEW_TASK",
                "intent": "CREATE_RECURRING_NEED",
                "confidence": 0.95,
                "entities": {
                    "product": "carotte",
                    "quantity": 30.0,
                    "unit": "KG",
                    "recurrence_type": "WEEKLY_DAYS",
                    "weekly_days": [5],
                },
            }
        )
        turn3, state = run(
            _run_turn(state, interpreter, runtime, text="j ai besoin de 30 kg de carotte tous les vendredis")
        )
        assert turn3["detected_intent"] == "CREATE_RECURRING_NEED", turn3.get("raw_analysis")
        response = (state.get("final_response") or "").lower()
        assert "tomate" not in response and "oignon" not in response, (
            f"contamination du draft multi-produits précédent détectée : {response!r}"
        )
        assert "carotte" in response

        carotte_draft = RecurringNeedDraft.from_dict(state.get("recurring_need_draft"))
        assert carotte_draft is not None
        assert carotte_draft.draft_id != multi_draft_id, "un NOUVEAU draft_id est attendu, jamais la réutilisation du draft multi-produits"
        assert carotte_draft.product == "carotte"
        assert carotte_draft.quantity == 30.0
        assert not carotte_draft.additional_items, (
            f"additional_items ne doit JAMAIS hériter de l'ancien draft tomate+oignon : {carotte_draft.additional_items!r}"
        )

        _turn4, state = run(_run_turn(state, interpreter, runtime, text="ok"))
        assert len(created_needs) == 1, "le besoin carotte doit passer par l'appel MCP singulier (mono-produit), jamais le pluriel"
        assert created_needs[0]["product_query"] == "carotte"
        assert len(created_batches) == 1, "aucun second appel multi-produits ne doit être déclenché pour une demande mono-produit"


# =====================================================================
# ANOMALIE RÉSIDUELLE (2026-09-24) : un rejet ("non"/"annuler") pendant la confirmation
# laissait le draft en `DRAFT`, orphelin (plus de `PendingInteraction` pour le référencer, mais
# jamais formellement `CANCELLED`) au lieu d'utiliser `CancelRecurringNeedDraft` (déjà existant,
# déjà correctement câblé pour TOUT le reste — CAS, persistance, protection contre une
# confirmation/modification tardive). Corrigé dans `resolve_domain_action` : REJECT ->
# `CancelRecurringNeedDraft()`. Les tests ci-dessous couvrent le contrat attendu :
#
#   Création -> rejet définitif -> CANCELLED -> aucune création DB -> state propre
#   CANCELLED -> confirmation/rejeu tardif -> aucune exécution, jamais de réactivation
#   CANCELLED -> nouvelle demande -> nouveau draft, totalement indépendant
# =====================================================================


class TestExplicitRejectionWordsBothCancelDefinitively:
    """Mission §7.1/§7.2 : "non" ET "annuler" sont deux mots DISTINCTS mais le MÊME
    `interpreted_event="REJECT"` côté interpréteur (`_REJECT_EXACT_PHRASES`) — les deux doivent
    produire le même résultat définitif, jamais une simple pause "doux" (contrairement à
    PROCUREMENT/PREORDER, voir `recurring_need_draft.py::CancelRecurringNeedDraft`)."""

    @pytest.mark.parametrize("rejection_word", ["non", "annuler"])
    def test_the_rejection_is_definitive_and_writes_nothing(self, rejection_word):
        created_needs: List[Dict[str, Any]] = []

        def _create_recurring_need(**kwargs: Any) -> Dict[str, Any]:
            created_needs.append(kwargs)
            return {"status": "success", "recurring_need_id": "need-1"}

        runtime = StubRuntime(responses={"create_recurring_need": _create_recurring_need})
        interpreter = make_input_interpreter("BUYER")
        state = _initial_state()

        runtime.llm = ScriptedLLM(
            _new_task_payload("CREATE_RECURRING_NEED", product="tomate", quantity=20.0, unit="KG")
        )
        _turn1, state = run(
            _run_turn(state, interpreter, runtime, text="j ai besoin de 20 kg de tomates tous les jours")
        )
        assert to_tunnel_category(get_pending_interaction(state)) == "CONFIRMATION"

        _turn2, state = run(_run_turn(state, interpreter, runtime, text=rejection_word))
        assert len(created_needs) == 0, f"aucune écriture DB après un rejet ({rejection_word!r})"
        assert state.get("recurring_need_draft") is None, "le draft rejeté ne doit jamais rester actif"
        assert to_tunnel_category(get_pending_interaction(state)) == "NONE"
        assert state.get("current_goal") is None
        assert state.get("transaction_payload") == {}


class TestLateConfirmationAfterCancellationNeverReactivates:
    """Mission §3 : "Draft A -> CANCELLED ; ancien message 'OK' -> aucune création, aucune
    réactivation du draft." Le "ok" tardif est un mot bare identique à celui qui aurait confirmé
    — seule la PendingInteraction/l'état ayant changé entre-temps doit empêcher la réactivation,
    jamais une comparaison de texte."""

    def test_ok_replayed_after_cancellation_creates_nothing(self):
        created_needs: List[Dict[str, Any]] = []

        def _create_recurring_need(**kwargs: Any) -> Dict[str, Any]:
            created_needs.append(kwargs)
            return {"status": "success", "recurring_need_id": "need-1"}

        runtime = StubRuntime(responses={"create_recurring_need": _create_recurring_need})
        interpreter = make_input_interpreter("BUYER")
        state = _initial_state()

        runtime.llm = ScriptedLLM(
            _new_task_payload("CREATE_RECURRING_NEED", product="tomate", quantity=20.0, unit="KG")
        )
        _turn1, state = run(
            _run_turn(state, interpreter, runtime, text="j ai besoin de 20 kg de tomates tous les jours")
        )
        _turn2, state = run(_run_turn(state, interpreter, runtime, text="non"))
        assert len(created_needs) == 0
        assert state.get("recurring_need_draft") is None

        # "ok" arrive APRÈS l'annulation — plus aucune PendingInteraction ne le rattache à
        # quoi que ce soit ; le classifieur n'a plus le contexte "confirmation en attente" et
        # ne doit rien halluciner de fiable (UNKNOWN, sans entité).
        runtime.llm = ScriptedLLM({"disposition": "UNKNOWN", "confidence": 0.3, "entities": {}})
        _turn3, state = run(_run_turn(state, interpreter, runtime, text="ok"))
        assert len(created_needs) == 0, "un \"ok\" tardif après annulation ne doit JAMAIS créer le besoin"
        assert state.get("recurring_need_draft") is None, "aucune réactivation du draft annulé"
        assert to_tunnel_category(get_pending_interaction(state)) == "NONE"


class TestRejectReplayIsIdempotent:
    """Mission §4 : une redélivrance webhook peut renvoyer DEUX FOIS le même événement REJECT
    contre le MÊME état de départ (c'est la définition d'une redélivrance — jamais un 2ᵉ tour
    distinct où le premier rejet a déjà fait avancer l'état). Le résultat final doit rester
    Draft A = CANCELLED, aucune opération métier exécutée, aucune interaction réactivée. Annuler
    n'a — contrairement à confirmer — aucun effet externe (pas d'appel MCP), donc aucun mécanisme
    de claim/dédup dédié n'est nécessaire ici : la pure transition d'état est déjà idempotente par
    construction (même état de départ -> même résultat)."""

    def test_a_redelivered_reject_never_creates_a_side_effect(self):
        created_needs: List[Dict[str, Any]] = []

        def _create_recurring_need(**kwargs: Any) -> Dict[str, Any]:
            created_needs.append(kwargs)
            return {"status": "success", "recurring_need_id": "need-1"}

        runtime = StubRuntime(responses={"create_recurring_need": _create_recurring_need})
        interpreter = make_input_interpreter("BUYER")
        state = _initial_state()

        runtime.llm = ScriptedLLM(
            _new_task_payload("CREATE_RECURRING_NEED", product="tomate", quantity=20.0, unit="KG")
        )
        _turn1, state_before_reject = run(
            _run_turn(state, interpreter, runtime, text="j ai besoin de 20 kg de tomates tous les jours")
        )
        assert to_tunnel_category(get_pending_interaction(state_before_reject)) == "CONFIRMATION"

        _redelivery_a, state_after_a = run(_run_turn(state_before_reject, interpreter, runtime, text="non"))
        assert len(created_needs) == 0
        assert state_after_a.get("recurring_need_draft") is None

        # Rejoué contre le MÊME état de départ (redélivrance webhook), pas contre state_after_a.
        _redelivery_b, state_after_b = run(_run_turn(state_before_reject, interpreter, runtime, text="non"))
        assert len(created_needs) == 0, "la redélivrance ne doit déclencher aucune opération métier"
        assert state_after_b.get("recurring_need_draft") is None
        assert to_tunnel_category(get_pending_interaction(state_after_b)) == "NONE", (
            "le nettoyage doit rester idempotent — jamais de PendingInteraction réactivée par le rejeu"
        )


class TestStaleConfirmationTargetNeverTouchesANewerDraft:
    """Mission §3 : "l'ancien événement de confirmation du draft A ne doit jamais confirmer ou
    modifier le draft B." Construit directement l'état pathologique (cible de confirmation
    périmée pointant vers A, alors que B est désormais le draft actif) — le scénario réel
    (PendingInteraction toujours réécrite pour B) ne le produit jamais naturellement, mais le
    garde-fou `ConfirmationTarget.matches` (draft_id ET version) doit le refuser structurellement
    si jamais un événement de confirmation périmé refaisait surface."""

    def test_a_stale_target_from_a_cancelled_draft_is_rejected_against_the_new_draft(self):
        from ladini.graphs.agents.market_coach.flows.buyer.recurring_need import (
            recurring_need_flow,
        )

        draft_b = RecurringNeedDraft.new(
            draft_id="draft-B-oignon", product="oignon", quantity=20.0, unit="KG", recurrence_type="DAILY"
        )
        state = {
            "current_goal": "CREATE_RECURRING_NEED",
            "user_phone": "+22670000000",
            "interpreted_event": "CONFIRM",
            "transaction_payload": {},
            "recurring_need_draft": draft_b.to_dict(),
            # Cible périmée : draft_id A, complètement étranger à B.
            "pending_interaction": {
                "kind": "CONFIRM_ACTION",
                "target": {"draft_id": "draft-A-tomate-cancelled", "draft_version": 3},
            },
        }
        runtime = StubRuntime()
        patch = run(recurring_need_flow(state, runtime))

        assert "create_recurring_need" not in runtime.calls, "jamais d'exécution sur une cible périmée"
        still_draft_b = RecurringNeedDraft.from_dict(patch.get("recurring_need_draft"))
        assert still_draft_b is not None
        assert still_draft_b.draft_id == "draft-B-oignon", "B doit rester intact, jamais touché par la cible de A"
        assert still_draft_b.status == RecurringNeedDraftStatus.DRAFT, "B ne doit jamais passer à EXECUTING"


class TestMultiItemDraftRejectedCreatesNothing:
    """Mission §6/§7.7 : tomate + oignon -> REJECT -> aucun des deux besoins ne doit être
    enregistré (ni via l'appel singulier ni via l'appel atomique pluriel), et une demande
    ultérieure ne doit récupérer aucun élément de `additional_items`."""

    def test_rejecting_a_multi_item_draft_creates_neither_product(self):
        created_needs: List[Dict[str, Any]] = []
        created_batches: List[Dict[str, Any]] = []

        def _create_recurring_need(**kwargs: Any) -> Dict[str, Any]:
            created_needs.append(kwargs)
            return {"status": "success", "recurring_need_id": "need-1"}

        def _create_recurring_needs(**kwargs: Any) -> Dict[str, Any]:
            created_batches.append(kwargs)
            return {"status": "success", "items": []}

        runtime = StubRuntime(
            responses={
                "create_recurring_need": _create_recurring_need,
                "create_recurring_needs": _create_recurring_needs,
            }
        )
        interpreter = make_input_interpreter("BUYER")
        state = _initial_state()

        runtime.llm = ScriptedLLM(
            {
                "disposition": "NEW_TASK",
                "intent": "CREATE_RECURRING_NEED",
                "confidence": 0.95,
                "entities": {
                    "product": "tomate",
                    "quantity": 10.0,
                    "unit": "KG",
                    "additional_items": [{"product": "oignon", "quantity": 20.0, "unit": "KG"}],
                    "recurrence_type": "DAILY",
                },
            }
        )
        _turn1, state = run(
            _run_turn(state, interpreter, runtime, text="j ai besoin de 10 kg de tomate et 20 kg d oignon tous les jours")
        )
        draft = RecurringNeedDraft.from_dict(state.get("recurring_need_draft"))
        assert draft.additional_items == [{"product": "oignon", "quantity": 20.0, "unit": "KG"}]

        _turn2, state = run(_run_turn(state, interpreter, runtime, text="annuler"))
        assert len(created_needs) == 0
        assert len(created_batches) == 0, "aucun appel atomique multi-produits après un rejet"
        assert state.get("recurring_need_draft") is None

        # Nouvelle demande mono-produit — ne doit récupérer AUCUN residu de tomate/oignon.
        runtime.llm = ScriptedLLM(
            {
                "disposition": "NEW_TASK",
                "intent": "CREATE_RECURRING_NEED",
                "confidence": 0.95,
                "entities": {"product": "carotte", "quantity": 30.0, "unit": "KG", "recurrence_type": "DAILY"},
            }
        )
        turn3, state = run(_run_turn(state, interpreter, runtime, text="j ai besoin de 30 kg de carotte tous les jours"))
        response = (state.get("final_response") or "").lower()
        assert "tomate" not in response and "oignon" not in response
        carotte_draft = RecurringNeedDraft.from_dict(state.get("recurring_need_draft"))
        assert not carotte_draft.additional_items, (
            f"additional_items ne doit jamais survivre à un rejet : {carotte_draft.additional_items!r}"
        )


# =====================================================================
# BUG RÉEL PRODUCTION (2026-09-24) : "Je veux 14 coq et 57 moutons chèvres chaque semaine" ->
# "Aucun produit disponible... Souhaitez-vous lancer un appel d'offres ?" au lieu de
# CREATE_RECURRING_NEED. Cause racine EXACTE, tracée pas à pas à travers le VRAI graphe
# (interpreter -> cognitive_guard -> goal_planner -> ...) : `nodes/cognitive.py::cognitive_guard`
# est le SEUL propriétaire de la décision d'interrompre un goal actif — une intention concurrente
# (ici CREATE_RECURRING_NEED) n'interrompt un tunnel encore actif (ici BUYER_REQUEST, coincé en
# attente "oui/non" pour un ancien appel d'offres jamais répondu) QUE si sa confiance dépasse
# `_DISAMBIGUATION_CONFIDENCE_THRESHOLD = 0.85` (`nodes/semantic_disambiguation.py`) — sous ce
# seuil, `goal_planner`'s RÈGLE 1quater (VERROUILLAGE PENDANT SLOT-FILLING) verrouille le tunnel
# PÉRIMÉ et écrase purement et simplement `detected_intent` par l'ancien goal. Ce seuil est un
# mécanisme GLOBAL, partagé par TOUS les tunnels de l'agent, réglé au fil d'incidents réels
# documentés dans `nodes/cognitive.py` — volontairement NON modifié ici (mandat §8 : ne pas
# élargir le périmètre, un changement de seuil global demande une décision produit à part
# entière, pas un correctif ponctuel pour un seul goal). Les tests ci-dessous PROUVENT que le
# mécanisme d'interruption fonctionne correctement pour une classification à confiance réaliste
# (>= 0.85, ce qu'un message clair et non ambigu doit produire), y compris avec une ancienne
# interaction catalogue encore active.
# =====================================================================


class TestWeeklyRecurrenceThroughTheRealOrchestrator:
    """Les 3 scénarios exigés, par le VRAI point d'entrée (interpreter -> cognitive_guard ->
    goal_planner -> memory_update -> validator -> buyer_context_resolver), jamais seulement
    l'interpréteur isolé."""

    def test_single_product_weekly_recurrence_is_recognized(self):
        state = _initial_state()
        runtime = StubRuntime(responses={"create_recurring_need": lambda **kw: {"status": "success", "recurring_need_id": "x"}})
        runtime.llm = ScriptedLLM({
            "disposition": "NEW_TASK", "intent": "CREATE_RECURRING_NEED", "confidence": 0.92,
            "entities": {"product": "coq", "quantity": 14.0, "unit": "unite", "recurrence_type": "WEEKLY"},
        })
        interpreter = make_input_interpreter("BUYER")
        turn1, state = run(_run_turn_with_cognitive_guard(state, interpreter, runtime, text="je veux 14 coqs chaque semaine"))
        assert turn1["detected_intent"] == "CREATE_RECURRING_NEED", turn1.get("raw_analysis")
        draft = RecurringNeedDraft.from_dict(state.get("recurring_need_draft"))
        assert draft is not None
        assert draft.product == "coq"
        assert draft.quantity == 14.0
        assert draft.recurrence_type == "WEEKLY"

    def test_two_products_weekly_recurrence_is_recognized(self):
        state = _initial_state()
        runtime = StubRuntime(responses={"create_recurring_needs": lambda **kw: {"status": "success", "items": []}})
        runtime.llm = ScriptedLLM({
            "disposition": "NEW_TASK", "intent": "CREATE_RECURRING_NEED", "confidence": 0.92,
            "entities": {
                "product": "coq", "quantity": 14.0, "unit": "unite",
                "additional_items": [{"product": "chevre", "quantity": 20.0, "unit": "unite"}],
                "recurrence_type": "WEEKLY",
            },
        })
        interpreter = make_input_interpreter("BUYER")
        turn1, state = run(
            _run_turn_with_cognitive_guard(state, interpreter, runtime, text="je veux 14 coqs et 20 chevres chaque semaine")
        )
        assert turn1["detected_intent"] == "CREATE_RECURRING_NEED", turn1.get("raw_analysis")
        draft = RecurringNeedDraft.from_dict(state.get("recurring_need_draft"))
        assert draft is not None
        assert draft.product == "coq" and draft.quantity == 14.0
        assert draft.additional_items == [{"product": "chevre", "quantity": 20.0, "unit": "UNITE"}]  # canonicalisé (commit 8, même normalisateur que le primaire)
        assert draft.recurrence_type == "WEEKLY"

    def test_ambiguous_quantity_across_two_products_asks_for_clarification(self):
        """« 57 moutons chèvres » — une seule quantité pour deux animaux, sans répartition. Ne
        doit ni fusionner en un produit incohérent, ni deviner un partage (57 de chacun) : une
        clarification explicite, jamais une invention."""
        state = _initial_state()
        runtime = StubRuntime(responses={"create_recurring_need": lambda **kw: {"status": "success", "recurring_need_id": "x"}})
        runtime.llm = ScriptedLLM({
            "disposition": "NEW_TASK", "intent": "CREATE_RECURRING_NEED", "confidence": 0.92,
            "entities": {
                "product": "coq", "quantity": 14.0, "unit": "unite", "recurrence_type": "WEEKLY",
                "ambiguous_groups": [{"quantity": 57.0, "unit": "unite", "candidates": ["mouton", "chevre"]}],
            },
        })
        interpreter = make_input_interpreter("BUYER")
        turn1, state = run(
            _run_turn_with_cognitive_guard(
                state, interpreter, runtime, text="je veux 14 coq et 57 moutons chevres chaque semaine"
            )
        )
        assert "create_recurring_need" not in runtime.calls, "aucune création tant que la quantité reste ambiguë"
        # Mandat lifecycle `ambiguous_groups` (2026-09-24, §10 "reprise du draft") : le draft
        # N'EST PLUS `None` ici — le produit principal (coq) est désormais persisté DÈS ce tour
        # (mandat §10), pour que la résolution de l'ambiguïté au tour suivant le retrouve intact
        # (`transaction_payload` est purgé sans condition par `goal_planner` RÈGLE 5 au tour
        # suivant — seul le draft, canal `replace_value`, survit fiablement). Ni fusion en un seul
        # produit incohérent, ni répartition inventée : `additional_items` reste vide tant que le
        # groupe ambigu n'est pas résolu.
        draft = RecurringNeedDraft.from_dict(state.get("recurring_need_draft"))
        assert draft is not None and draft.product == "coq" and draft.quantity == 14.0
        assert not draft.additional_items
        response = (state.get("final_response") or "").lower()
        assert "mouton" in response and "chevre" in response
        assert "57" in response
        # Ni fusion en un seul produit incohérent, ni répartition inventée silencieusement.
        assert "moutons chevres" not in response.replace("è", "e")


class TestWeeklyRecurrenceSurvivesAStaleCatalogInteraction:
    """Mêmes 3 scénarios, mais avec une ANCIENNE interaction catalogue encore active
    (`_stale_catalog_interaction_state`) — preuve que `cognitive_guard` interrompt correctement
    le tunnel BUYER_REQUEST périmé pour une classification CREATE_RECURRING_NEED à confiance
    suffisante, au lieu de le laisser écraser la nouvelle demande (bug réel production)."""

    def test_single_product_weekly_recurrence_interrupts_the_stale_catalog_tunnel(self):
        state = _stale_catalog_interaction_state()
        runtime = StubRuntime(responses={"create_recurring_need": lambda **kw: {"status": "success", "recurring_need_id": "x"}})
        runtime.llm = ScriptedLLM({
            "disposition": "NEW_TASK", "intent": "CREATE_RECURRING_NEED", "confidence": 0.92,
            "entities": {"product": "coq", "quantity": 14.0, "unit": "unite", "recurrence_type": "WEEKLY"},
        })
        interpreter = make_input_interpreter("BUYER")
        turn1, state = run(_run_turn_with_cognitive_guard(state, interpreter, runtime, text="je veux 14 coqs chaque semaine"))
        assert turn1["detected_intent"] == "CREATE_RECURRING_NEED", (
            f"le tunnel BUYER_REQUEST périmé a écrasé la nouvelle demande : {turn1.get('raw_analysis')}"
        )
        response = (state.get("final_response") or "").lower()
        assert "catalogue" not in response and "appel d'offres" not in response
        draft = RecurringNeedDraft.from_dict(state.get("recurring_need_draft"))
        assert draft is not None and draft.product == "coq" and draft.recurrence_type == "WEEKLY"

    def test_two_products_weekly_recurrence_interrupts_the_stale_catalog_tunnel(self):
        state = _stale_catalog_interaction_state()
        runtime = StubRuntime(responses={"create_recurring_needs": lambda **kw: {"status": "success", "items": []}})
        runtime.llm = ScriptedLLM({
            "disposition": "NEW_TASK", "intent": "CREATE_RECURRING_NEED", "confidence": 0.92,
            "entities": {
                "product": "coq", "quantity": 14.0, "unit": "unite",
                "additional_items": [{"product": "chevre", "quantity": 20.0, "unit": "unite"}],
                "recurrence_type": "WEEKLY",
            },
        })
        interpreter = make_input_interpreter("BUYER")
        turn1, state = run(
            _run_turn_with_cognitive_guard(state, interpreter, runtime, text="je veux 14 coqs et 20 chevres chaque semaine")
        )
        assert turn1["detected_intent"] == "CREATE_RECURRING_NEED", (
            f"le tunnel BUYER_REQUEST périmé a écrasé la nouvelle demande : {turn1.get('raw_analysis')}"
        )
        draft = RecurringNeedDraft.from_dict(state.get("recurring_need_draft"))
        assert draft is not None
        assert draft.additional_items == [{"product": "chevre", "quantity": 20.0, "unit": "UNITE"}]  # canonicalisé (commit 8, même normalisateur que le primaire)

    def test_ambiguous_quantity_interrupts_the_stale_catalog_tunnel_and_still_asks(self):
        state = _stale_catalog_interaction_state()
        runtime = StubRuntime(responses={"create_recurring_need": lambda **kw: {"status": "success", "recurring_need_id": "x"}})
        runtime.llm = ScriptedLLM({
            "disposition": "NEW_TASK", "intent": "CREATE_RECURRING_NEED", "confidence": 0.92,
            "entities": {
                "product": "coq", "quantity": 14.0, "unit": "unite", "recurrence_type": "WEEKLY",
                "ambiguous_groups": [{"quantity": 57.0, "unit": "unite", "candidates": ["mouton", "chevre"]}],
            },
        })
        interpreter = make_input_interpreter("BUYER")
        turn1, state = run(
            _run_turn_with_cognitive_guard(
                state, interpreter, runtime, text="je veux 14 coq et 57 moutons chevres chaque semaine"
            )
        )
        response = (state.get("final_response") or "").lower()
        assert "catalogue" not in response and "appel d'offres" not in response, (
            f"le tunnel BUYER_REQUEST périmé a écrasé la clarification attendue : {response!r}"
        )
        assert "mouton" in response and "chevre" in response
        assert "create_recurring_need" not in runtime.calls


class TestAdditionalItemWithoutALiteralUnitStillSurvives:
    """Bug réel (trouvé en auditant 4f796bd, jamais couvert par ses propres tests — ceux-ci
    fournissaient `unit: "unite"` pour "chevre" dans le script LLM, ce qu'un vrai appel Groq ne
    renvoie jamais pour "20 chevres" : `new_task_prompts.py` interdit explicitement de deviner une
    unité absente du texte littéral). Le slot `unit` de premier niveau a un défaut par produit
    (`default_unit_for_product`/`core/slots.py` — TETE pour l'élevage), mais
    `_clean_additional_items` (`flows/buyer/recurring_need.py`) exigeait une unité déjà présente
    sur CHAQUE item et rejetait silencieusement l'item sinon — "14 coqs et 20 chèvres chaque
    semaine" ne créait donc qu'un draft coq, la chèvre disparaissant sans aucun avertissement."""

    def test_a_livestock_additional_item_without_a_literal_unit_is_not_dropped(self):
        state = _initial_state()
        runtime = StubRuntime(responses={"create_recurring_needs": lambda **kw: {"status": "success", "items": []}})
        runtime.llm = ScriptedLLM({
            "disposition": "NEW_TASK", "intent": "CREATE_RECURRING_NEED", "confidence": 0.92,
            "entities": {
                "product": "coq", "quantity": 14.0, "unit": "unite",
                # Aucune unité littérale pour "chevre" dans le message ("20 chevres" ne contient
                # aucun mot d'unité) — exactement ce que le vrai micro-prompt renvoie ici.
                "additional_items": [{"product": "chevre", "quantity": 20.0, "unit": None}],
                "recurrence_type": "WEEKLY",
            },
        })
        interpreter = make_input_interpreter("BUYER")
        turn1, state = run(
            _run_turn_with_cognitive_guard(state, interpreter, runtime, text="je veux 14 coqs et 20 chevres chaque semaine")
        )
        assert turn1["detected_intent"] == "CREATE_RECURRING_NEED", turn1.get("raw_analysis")
        draft = RecurringNeedDraft.from_dict(state.get("recurring_need_draft"))
        assert draft is not None
        assert draft.additional_items == [{"product": "chevre", "quantity": 20.0, "unit": "TETE"}], (
            f"la chèvre ne doit jamais disparaître faute d'unité littérale : {draft.additional_items!r}"
        )
        assert "Chevre : 20 TETE" in draft.render_summary()


# =====================================================================
# LIFECYCLE DE CLARIFICATION `ambiguous_groups` (mandat 2026-09-24, suite) : la première question
# ("TOTAL ou DE CHAQUE ?") était déjà correcte, mais rien ne savait consommer sa réponse — la
# route NEW_TASK reclasse TOUJOURS ce genre de réponse libre comme un message indépendant
# (`ambiguous_quantity` n'est pas un slot enregistré, voir `interpreter/state_router.py`). Les 6
# scénarios ci-dessous couvrent exactement les TEST 1-6 du mandat, par le VRAI point d'entrée
# (interpreter -> cognitive_guard -> goal_planner -> memory_update -> validator ->
# buyer_context_resolver), même thread/état entre les tours.
# =====================================================================


def _ambiguous_turn1_llm() -> ScriptedLLM:
    return ScriptedLLM({
        "disposition": "NEW_TASK", "intent": "CREATE_RECURRING_NEED", "confidence": 0.92,
        "entities": {
            "product": "coq", "quantity": 14.0, "unit": "unite", "recurrence_type": "WEEKLY",
            "ambiguous_groups": [{"quantity": 57.0, "unit": "unite", "candidates": ["mouton", "chevre"]}],
        },
    })


def _unclassifiable_reply_llm() -> ScriptedLLM:
    """Une réponse comme "50 moutons et 7 chèvres" ne correspond à aucune intention du catalogue
    — un vrai appel LLM renverrait plausiblement UNKNOWN ici. `_resolve_ambiguous_group_reply`
    n'a de toute façon pas besoin d'une classification utile : il lit le texte brut directement,
    AVANT que `resolve_domain_action` ne s'appuie sur `detected_intent`."""
    return ScriptedLLM({"disposition": "UNKNOWN", "intent": None, "confidence": 0.0, "entities": {}})


class TestAmbiguousGroupClarificationLifecycle:
    def _turn1(self):
        state = _initial_state()
        runtime = StubRuntime(responses={"create_recurring_need": lambda **kw: {"status": "success", "recurring_need_id": "x"}})
        runtime.llm = _ambiguous_turn1_llm()
        interpreter = make_input_interpreter("BUYER")
        turn1, state = run(
            _run_turn_with_cognitive_guard(
                state, interpreter, runtime, text="je veux 14 coqs et 57 moutons chevres chaque semaine"
            )
        )
        return turn1, state, runtime, interpreter

    # ── structure exacte avant correction (LIVRABLE §1/§2) ────────────────

    def test_turn1_clarification_structure(self):
        turn1, state, _runtime, _interp = self._turn1()
        assert "57" in turn1["final_response"] and "TOTAL" in turn1["final_response"]
        pending = state.get("pending_interaction") or {}
        assert pending.get("kind") == "ENTER_FIELD"
        assert pending.get("field") == "ambiguous_quantity"
        target = pending.get("target") or {}
        assert target.get("total_quantity") == 57.0
        assert set(target.get("candidates") or []) == {"mouton", "chevre"}
        # Le produit principal est déjà persisté dans le draft (mandat §10 "reprise du draft") —
        # jamais seulement dans `transaction_payload`, purgé sans condition par `goal_planner`
        # RÈGLE 5 dès le tour suivant.
        draft = RecurringNeedDraft.from_dict(state.get("recurring_need_draft"))
        assert draft is not None and draft.product == "coq" and draft.quantity == 14.0
        assert draft.recurrence_type == "WEEKLY"
        assert not draft.additional_items

    # ── TEST 1 — quantités explicites ─────────────────────────────────────

    def test_explicit_quantities_resolve_and_confirm(self):
        _turn1, state, runtime, interpreter = self._turn1()
        runtime.llm = _unclassifiable_reply_llm()
        turn2, state = run(
            _run_turn_with_cognitive_guard(state, interpreter, runtime, text="50 moutons et 7 chevres")
        )
        draft = RecurringNeedDraft.from_dict(state.get("recurring_need_draft"))
        assert draft is not None
        assert draft.product == "coq" and draft.quantity == 14.0
        assert draft.additional_items == [
            {"product": "mouton", "quantity": 50.0, "unit": "TETE"},
            {"product": "chevre", "quantity": 7.0, "unit": "TETE"},
        ]
        assert state.get("pending_interaction", {}).get("kind") == "CONFIRM_ACTION"
        assert "confirmer" in turn2["final_response"].lower()

    # ── TEST 2 — "le reste" ────────────────────────────────────────────────

    def test_rest_of_the_total_resolves_and_confirms(self):
        _turn1, state, runtime, interpreter = self._turn1()
        runtime.llm = _unclassifiable_reply_llm()
        turn2, state = run(
            _run_turn_with_cognitive_guard(
                state, interpreter, runtime, text="c est 20 mouton et le reste pour les chevres"
            )
        )
        draft = RecurringNeedDraft.from_dict(state.get("recurring_need_draft"))
        assert draft.additional_items == [
            {"product": "mouton", "quantity": 20.0, "unit": "TETE"},
            {"product": "chevre", "quantity": 37.0, "unit": "TETE"},
        ]
        assert state.get("pending_interaction", {}).get("kind") == "CONFIRM_ACTION"
        assert "confirmer" in turn2["final_response"].lower()

    # ── TEST 3 — "de chaque" ───────────────────────────────────────────────

    def test_each_of_the_total_resolves_and_confirms(self):
        _turn1, state, runtime, interpreter = self._turn1()
        runtime.llm = _unclassifiable_reply_llm()
        turn2, state = run(
            _run_turn_with_cognitive_guard(state, interpreter, runtime, text="57 de chaque")
        )
        draft = RecurringNeedDraft.from_dict(state.get("recurring_need_draft"))
        assert draft.additional_items == [
            {"product": "mouton", "quantity": 57.0, "unit": "TETE"},
            {"product": "chevre", "quantity": 57.0, "unit": "TETE"},
        ]
        assert state.get("pending_interaction", {}).get("kind") == "CONFIRM_ACTION"

    # ── TEST 4 — somme invalide ────────────────────────────────────────────

    def test_an_invalid_sum_asks_for_a_targeted_correction_without_persisting_anything(self):
        _turn1, state, runtime, interpreter = self._turn1()
        runtime.llm = _unclassifiable_reply_llm()
        turn2, state = run(
            _run_turn_with_cognitive_guard(state, interpreter, runtime, text="20 moutons et 20 chevres")
        )
        assert "create_recurring_need" not in runtime.calls
        assert "create_recurring_needs" not in runtime.calls
        draft = RecurringNeedDraft.from_dict(state.get("recurring_need_draft"))
        # Le draft reste ACTIF (produit principal toujours là), mais sans les allocations
        # invalides — la clarification ciblée reste la seule issue de ce tour.
        assert draft is not None and draft.status.value == "DRAFT"
        assert not draft.additional_items
        pending = state.get("pending_interaction") or {}
        assert pending.get("kind") == "ENTER_FIELD" and pending.get("field") == "ambiguous_quantity"
        assert "40" in turn2["final_response"] and "57" in turn2["final_response"]

    # ── TEST 5 — interruption par une tâche autonome ───────────────────────

    def test_an_unrelated_autonomous_task_interrupts_the_clarification(self):
        _turn1, state, runtime, interpreter = self._turn1()
        runtime.llm = ScriptedLLM({
            "disposition": "NEW_TASK", "intent": "CREATE_RECURRING_NEED", "confidence": 0.92,
            "entities": {"product": "coq", "quantity": 14.0, "unit": "unite", "recurrence_type": "WEEKLY"},
        })
        turn2, state = run(
            _run_turn_with_cognitive_guard(state, interpreter, runtime, text="je veux 14 coqs chaque semaine")
        )
        response = turn2["final_response"].lower()
        assert "57" not in response and "moutons" not in response and "total" not in response
        draft = RecurringNeedDraft.from_dict(state.get("recurring_need_draft"))
        assert draft is not None and draft.product == "coq" and draft.quantity == 14.0
        assert not draft.additional_items, "l'ancien groupe ambigu ne doit pas survivre à la nouvelle tâche"
        assert state.get("pending_interaction", {}).get("field") != "ambiguous_quantity"

    # ── TEST 6 — abandon explicite ─────────────────────────────────────────

    def test_a_bare_abandon_cancels_the_draft_and_the_clarification(self):
        _turn1, state, runtime, interpreter = self._turn1()
        runtime.llm = _unclassifiable_reply_llm()
        turn2, state = run(
            _run_turn_with_cognitive_guard(state, interpreter, runtime, text="laisse tomber")
        )
        assert "create_recurring_need" not in runtime.calls
        draft = RecurringNeedDraft.from_dict(state.get("recurring_need_draft"))
        assert draft is None or draft.status.value == "CANCELLED"
        pending = state.get("pending_interaction")
        assert not pending or pending.get("kind") != "ENTER_FIELD"

    # ── mandat §13 : persistance atomique des 3 produits SEULEMENT après confirmation ──

    def test_confirming_after_resolution_creates_all_three_products_in_one_atomic_call(self):
        """Mandat §13 : aucun `recurring_need` permanent tant que l'ambiguïté n'est pas résolue ET
        confirmée — puis les 3 produits (coq, mouton, chèvre) dans un SEUL appel transactionnel
        (`create_recurring_needs`, pluriel — voir `services/database/recurring_supply.py`,
        rollback complet si l'un des 3 échoue), jamais 3 créations indépendantes."""
        _turn1, state, runtime, interpreter = self._turn1()
        assert "create_recurring_need" not in runtime.calls and "create_recurring_needs" not in runtime.calls

        captured_items = {}

        def _capture(**kw):
            captured_items.update(kw)
            return {"status": "success", "items": [{"recurring_need_id": f"id-{i}"} for i in range(3)]}

        runtime._responses["create_recurring_needs"] = _capture
        runtime.llm = _unclassifiable_reply_llm()
        turn2, state = run(
            _run_turn_with_cognitive_guard(state, interpreter, runtime, text="50 moutons et 7 chevres")
        )
        assert "create_recurring_needs" not in runtime.calls, "pas encore confirmé — aucune persistance"

        runtime.llm = ScriptedLLM({"disposition": "CONFIRM", "intent": None, "confidence": 0.95, "entities": {}})
        turn3, state = run(_run_turn_with_cognitive_guard(state, interpreter, runtime, text="oui"))

        assert runtime.calls.count("create_recurring_needs") == 1
        assert "create_recurring_need" not in runtime.calls, "jamais l'appel singulier une fois multi-produits"
        assert len(captured_items.get("items") or []) == 3
        products = {it["product_query"] for it in captured_items["items"]}
        assert products == {"coq", "mouton", "chevre"}
        assert "c'est noté" in turn3["final_response"].lower()
