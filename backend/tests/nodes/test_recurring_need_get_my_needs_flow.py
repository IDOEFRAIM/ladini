"""`flows/buyer/recurring_need.py::_get_my_needs_flow` — liste avec disponibilité, détail d'un
besoin, navigation par menu numéroté réutilisant `PendingInteraction(SELECTION_MENU)` (mandat
Phase 4 §6/§23). Appels MCP passés par `StubRuntime` (tests/conftest.py), aucun réseau."""
from __future__ import annotations

import time

import pytest

from ladini.graphs.agents.market_coach.flows.buyer.recurring_need import (
    recurring_need_flow,
)
from ladini.services.database.recurring_supply import MATCH_RESPONSE_ACTIONS
from tests.conftest import StubRuntime, make_state, run

_NEEDS_RESPONSE = {
    "status": "success",
    "items": [
        {
            "recurring_need_id": "need-tomate",
            "product": "tomate",
            "quantity": 40,
            "unit": "KG",
            "recurrence_type": "DAILY",
            "weekly_days": None,
            "status": "ACTIVE",
            "next_occurrence_date": "2026-09-23",
            "next_occurrence_id": "occ-tomate",
            "requested_quantity": 40,
            "matched_quantity": 40,
        },
        {
            "recurring_need_id": "need-poulet",
            "product": "poulet",
            "quantity": 30,
            "unit": "UNITE",
            "recurrence_type": "WEEKLY",
            "weekly_days": None,
            "status": "ACTIVE",
            "next_occurrence_date": "2026-09-23",
            "next_occurrence_id": "occ-poulet",
            "requested_quantity": 30,
            "matched_quantity": 0,
        },
    ],
}

_DETAIL_RESPONSE = {
    "status": "success",
    "product": "tomate",
    "requested_quantity": 40,
    "unit": "KG",
    "occurrence_date": "2026-09-23",
    "allocations": [{"producer_label": "Coopérative A", "quantity": 40, "unit_price": 500, "unit": "KG"}],
}


def _run(state, responses):
    rt = StubRuntime(responses=responses)
    return run(recurring_need_flow(state, rt))


# ── première visite : liste avec disponibilité ────────────────────────────

def test_first_visit_renders_the_list_with_availability_and_sets_a_menu():
    state = make_state(current_goal="GET_MY_NEEDS")
    result = _run(state, {"list_my_recurring_needs": _NEEDS_RESPONSE})

    assert "Tomate" in result["final_response"] and "40/40 KG" in result["final_response"]
    assert "Poulet" in result["final_response"] and "0/30 UNITE" in result["final_response"]
    assert result["pending_interaction"]["kind"] == "SELECTION_MENU"
    assert result["pending_interaction"]["goal"] == "GET_MY_NEEDS"
    mapping = result["working_memory"]["recurring_need_menu"]["mapping"]
    assert mapping == {"1": "need-tomate", "2": "need-poulet"}


def test_never_promises_a_reservation_in_the_list():
    state = make_state(current_goal="GET_MY_NEEDS")
    result = _run(state, {"list_my_recurring_needs": _NEEDS_RESPONSE})
    forbidden = ("réservé", "garanti", "commande confirmée")
    assert not any(w in result["final_response"].lower() for w in forbidden)


# ── sélection d'un besoin → détail ─────────────────────────────────────────

def test_selecting_a_valid_index_shows_the_need_detail():
    pending_menu_state = _list_state_after_first_visit()
    result = _run(
        pending_menu_state,
        {"get_recurring_need_detail": _DETAIL_RESPONSE},
    )
    assert "Coopérative A" in result["final_response"]
    assert "40 KG — 500 FCFA/KG" in result["final_response"]
    assert "Total estimé : 20000 FCFA" in result["final_response"]
    # VS4 (pilote) : une disponibilité trouvée offre désormais explicitement de confirmer — le
    # mandat "jamais réservé/garanti avant confirmation" reste respecté (voir digest.py), seule
    # l'ABSENCE de bouton "confirmer" avant cette phase a changé.
    assert "1. Confirmer" in result["final_response"]
    assert "2. Pas cette fois" in result["final_response"]


def test_the_detail_screen_never_promises_a_reservation_before_confirmation():
    pending_menu_state = _list_state_after_first_visit()
    result = _run(pending_menu_state, {"get_recurring_need_detail": _DETAIL_RESPONSE})
    forbidden = ("réservé", "garanti")
    assert not any(w in result["final_response"].lower() for w in forbidden)


def test_the_detail_screen_sets_a_confirm_reject_menu_when_something_is_available():
    pending_menu_state = _list_state_after_first_visit()
    result = _run(pending_menu_state, {"get_recurring_need_detail": _DETAIL_RESPONSE})
    assert result["pending_interaction"]["kind"] == "SELECTION_MENU"
    assert result["working_memory"]["recurring_need_menu"]["mapping"] == {
        "1": "CONFIRM:need-tomate",
        "2": "REJECT:need-tomate",
        "3": "LIST",
    }


def test_the_detail_screen_offers_no_confirmation_when_nothing_is_available():
    pending_menu_state = _list_state_after_first_visit()
    empty_detail = {**_DETAIL_RESPONSE, "allocations": []}
    result = _run(pending_menu_state, {"get_recurring_need_detail": empty_detail})
    assert "Confirmer" not in result["final_response"]
    assert result["working_memory"]["recurring_need_menu"]["mapping"] == {"1": "LIST", "2": "LIST"}


# ── confirmer / refuser une proposition (VS4) ──────────────────────────────

def _detail_state_after_selection():
    return make_state(
        current_goal="GET_MY_NEEDS",
        transaction_payload={"selection_index": "1"},
        pending_interaction={"kind": "SELECTION_MENU", "goal": "GET_MY_NEEDS", "created_at": time.time()},
        working_memory={
            "recurring_need_menu": {
                "mapping": {"1": "CONFIRM:need-tomate", "2": "REJECT:need-tomate", "3": "LIST"},
                "created_at": time.time(),
            }
        },
    )


def test_confirming_calls_the_gateway_and_returns_a_success_message():
    state = _detail_state_after_selection()
    result = _run(
        state,
        {
            "accept_match_proposal": {
                "status": "success",
                "occurrence_id": "occ-tomate",
                "action": "ACCEPT",
                "order_ids": ["order-1"],
                "quantity_confirmed": 40,
            }
        },
    )
    assert "confirmé" in result["final_response"].lower()
    assert result["status"] == "COMPLETED"


def test_rejecting_calls_the_gateway_and_never_mentions_an_order():
    state = _detail_state_after_selection()
    state["transaction_payload"] = {"selection_index": "2"}
    result = _run(
        state,
        {"accept_match_proposal": {"status": "success", "occurrence_id": "occ-tomate", "action": "REJECT"}},
    )
    assert "besoin habituel reste actif" in result["final_response"]
    assert "commande" not in result["final_response"].lower()


def test_option_three_returns_to_the_list_without_calling_the_gateway():
    state = _detail_state_after_selection()
    state["transaction_payload"] = {"selection_index": "3"}
    # Aucune réponse stubée pour accept_match_proposal : si le code l'appelait quand même,
    # StubRuntime lèverait — la réussite du test prouve que l'appel n'a jamais eu lieu.
    result = _run(state, {"list_my_recurring_needs": _NEEDS_RESPONSE})
    assert "Vos approvisionnements" in result["final_response"]


# =====================================================================
# Mandat "2e mismatch CONFIRM vs ACCEPT" (2026-09-26) — tests A-G
#
# `_respond_to_match` (menu détail `GET_MY_NEEDS`, ci-dessus) passait `action` — littéralement
# "CONFIRM"/"REJECT", jamais mappé — à `RecurringSupplyGateway.accept_match_proposal`, alors que
# le service réel (`services/database/recurring_supply.py::MATCH_RESPONSE_ACTIONS = ("ACCEPT",
# "REJECT")`) n'accepte jamais "CONFIRM" (même bug que `_respond_to_digest_flow`, corrigé PR #12 —
# voir `tests/integration/test_recurring_supply_digest_routing.py::TestConfirmAcceptContract`).
# Corrigé en réutilisant la MÊME table partagée (`_MATCH_RESPONSE_TO_SERVICE_ACTION`/
# `_to_match_service_action`), jamais une 2e table dupliquée.
# =====================================================================


def _confirm_state(selection_index="1"):
    return make_state(
        current_goal="GET_MY_NEEDS",
        transaction_payload={"selection_index": selection_index},
        pending_interaction={"kind": "SELECTION_MENU", "goal": "GET_MY_NEEDS", "created_at": time.time()},
        working_memory={
            "recurring_need_menu": {
                "mapping": {"1": "CONFIRM:need-tomate", "2": "REJECT:need-tomate", "3": "LIST"},
                "created_at": time.time(),
            }
        },
    )


class TestMatchResponseServiceContract:
    def test_A_confirming_sends_the_canonical_accept_action_to_the_service(self):
        seen = []

        def _accept(**kwargs):
            seen.append(kwargs)
            return {"status": "success", "occurrence_id": "occ-tomate", "order_ids": ["order-1"]}

        result = run(recurring_need_flow(_confirm_state("1"), StubRuntime(responses={"accept_match_proposal": _accept})))
        assert len(seen) == 1
        assert seen[0]["action"] == "ACCEPT"
        assert seen[0]["action"] in MATCH_RESPONSE_ACTIONS
        assert "confirmé" in result["final_response"].lower()

    def test_B_rejecting_sends_the_canonical_reject_action_to_the_service(self):
        seen = []

        def _accept(**kwargs):
            seen.append(kwargs)
            return {"status": "success", "occurrence_id": "occ-tomate"}

        result = run(recurring_need_flow(_confirm_state("2"), StubRuntime(responses={"accept_match_proposal": _accept})))
        assert len(seen) == 1
        assert seen[0]["action"] == "REJECT"
        assert seen[0]["action"] in MATCH_RESPONSE_ACTIONS
        assert "besoin habituel reste actif" in result["final_response"]

    def test_D_an_unmapped_conversational_action_fails_fast_without_any_db_call(self):
        """Défense en profondeur (mandat §5) : `_resolve_menu_reply` ne peut structurellement
        produire que "CONFIRM"/"REJECT" pour ce `kind` — ce test appelle `_respond_to_match`
        DIRECTEMENT avec une valeur hors de ce contrat fermé pour prouver que la frontière service
        échoue fort plutôt que de transmettre un mot inconnu."""
        import asyncio

        from ladini.graphs.agents.market_coach.flows.buyer.recurring_need import (
            _respond_to_match,
        )

        state = _confirm_state("1")
        rt = StubRuntime(responses={})

        async def go():
            with pytest.raises(AssertionError):
                await _respond_to_match(state, rt, recurring_need_id="need-tomate", action="MAYBE")

        asyncio.run(go())
        # `_to_match_service_action` lève AVANT tout appel gateway — `rt.calls` (peuplé par
        # `call_db`, voir `tests/conftest.py::StubRuntime`) le prouve directement, sans dépendre
        # d'un comportement de rejet côté double MCP.
        assert rt.calls == [], "aucun appel service ne doit partir avant la résolution de l'action"

    def test_F_two_different_needs_each_independently_use_accept(self):
        """Mandat §6.F ("plusieurs matches") transposé à `_respond_to_match` (une confirmation à la
        fois, depuis l'écran détail, jamais un lot comme le digest) : deux confirmations
        SUCCESSIVES sur deux besoins différents envoient chacune "ACCEPT", jamais "CONFIRM"."""
        seen = []

        def _accept(**kwargs):
            seen.append(kwargs)
            return {"status": "success"}

        rt = StubRuntime(responses={"accept_match_proposal": _accept})
        state_a = _confirm_state("1")
        run(recurring_need_flow(state_a, rt))

        state_b = make_state(
            current_goal="GET_MY_NEEDS",
            transaction_payload={"selection_index": "1"},
            pending_interaction={"kind": "SELECTION_MENU", "goal": "GET_MY_NEEDS", "created_at": time.time()},
            working_memory={
                "recurring_need_menu": {
                    "mapping": {"1": "CONFIRM:need-oignon", "2": "REJECT:need-oignon", "3": "LIST"},
                    "created_at": time.time(),
                }
            },
        )
        run(recurring_need_flow(state_b, rt))

        assert len(seen) == 2
        assert {c["recurring_need_id"] for c in seen} == {"need-tomate", "need-oignon"}
        assert all(c["action"] == "ACCEPT" for c in seen)

    def test_G_parity_with_the_digest_flow_same_canonical_mapping_function(self):
        """Mandat §6.G : les deux flows (digest et détail `GET_MY_NEEDS`) doivent appliquer
        EXACTEMENT le même contrat service — vérifié ici en important la MÊME fonction de mapping
        que celle exercée côté digest (`tests/integration/test_recurring_supply_digest_routing.py`),
        preuve qu'il n'existe qu'une seule source de vérité, pas deux tables qui pourraient diverger."""
        from ladini.graphs.agents.market_coach.flows.buyer.recurring_need import (
            _to_match_service_action,
        )

        assert _to_match_service_action("CONFIRM") == "ACCEPT"
        assert _to_match_service_action("REJECT") == "REJECT"


# ── retour depuis le détail ────────────────────────────────────────────────

def test_returning_from_the_detail_screen_shows_the_list_again():
    state = make_state(
        current_goal="GET_MY_NEEDS",
        transaction_payload={"selection_index": "1"},
        pending_interaction={"kind": "SELECTION_MENU", "goal": "GET_MY_NEEDS", "created_at": time.time()},
        working_memory={"recurring_need_menu": {"mapping": {"1": "LIST", "2": "LIST"}, "created_at": time.time()}},
    )
    result = _run(state, {"list_my_recurring_needs": _NEEDS_RESPONSE})
    assert "Vos approvisionnements" in result["final_response"]


def test_a_free_text_retour_reply_also_returns_to_the_list():
    state = _list_state_after_first_visit()
    state["transaction_payload"] = {}
    state["normalized_text"] = "retour"
    result = _run(state, {"list_my_recurring_needs": _NEEDS_RESPONSE})
    assert "Vos approvisionnements" in result["final_response"]


# ── protections ────────────────────────────────────────────────────────────

def test_an_invalid_index_re_renders_the_list_with_a_notice():
    state = _list_state_after_first_visit()
    state["transaction_payload"] = {"selection_index": "99"}
    result = _run(state, {"list_my_recurring_needs": _NEEDS_RESPONSE})
    assert "Je n'ai pas compris ce choix." in result["final_response"]
    assert "Vos approvisionnements" in result["final_response"]


def test_a_stale_menu_past_the_ttl_is_treated_as_a_fresh_visit():
    state = _list_state_after_first_visit()
    state["transaction_payload"] = {"selection_index": "1"}
    state["pending_interaction"]["created_at"] = time.time() - 3600  # 1h — largement expiré
    result = _run(state, {"list_my_recurring_needs": _NEEDS_RESPONSE})
    # Une sélection périmée ne doit JAMAIS être appliquée : on retombe sur la liste, pas le détail.
    assert "Vos approvisionnements" in result["final_response"]
    assert "Coopérative A" not in result["final_response"]


def test_a_pending_interaction_from_an_unrelated_tunnel_is_never_touched():
    """Un menu SELECTION_MENU actif pour un AUTRE goal ne doit jamais être interprété comme une
    réponse à CE menu — la liste s'affiche fraîche, sans toucher à l'interaction en cours ailleurs."""
    state = make_state(
        current_goal="GET_MY_NEEDS",
        transaction_payload={"selection_index": "1"},
        pending_interaction={"kind": "SELECTION_MENU", "goal": "BUYER_LIST_AUCTIONS", "created_at": time.time()},
        working_memory={},
    )
    result = _run(state, {"list_my_recurring_needs": _NEEDS_RESPONSE})
    assert "Vos approvisionnements" in result["final_response"]


def _list_state_after_first_visit():
    return make_state(
        current_goal="GET_MY_NEEDS",
        transaction_payload={"selection_index": "1"},
        pending_interaction={"kind": "SELECTION_MENU", "goal": "GET_MY_NEEDS", "created_at": time.time()},
        working_memory={"recurring_need_menu": {"mapping": {"1": "need-tomate", "2": "need-poulet"}, "created_at": time.time()}},
    )
