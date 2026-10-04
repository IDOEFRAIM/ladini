"""B23 — matrice d'arbitrage PURE (sans graphe, sans LLM) : une attente guide l'interprétation, elle ne la remplace pas."""
from __future__ import annotations

import time

import pytest

from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    set_pending_interaction,
)
from ladini.graphs.agents.market_coach.interpreter import context_arbitration as ca
from ladini.graphs.agents.market_coach.interpreter.context_arbitration import ArbitrationKind as K


def _detail_state(*, created_at: float | None = None, actions=None):
    actions = actions or {"1": "REFRESH", "2": "LIST"}
    return {
        "current_goal": "GET_MY_NEEDS",
        **set_pending_interaction(InteractionKind.SELECTION_MENU, goal="GET_MY_NEEDS"),
        "working_memory": {"recurring_need_menu": {
            "mapping": {"1": "REFRESH:N1", "2": "LIST"}, "actions": actions,
            "created_at": created_at or time.time(),
            "title": "Écran du besoin récurrent « Boeuf »", "labels": {"1": "Rechercher maintenant", "2": "Retour"}}},
    }


def _decide(text, state=None, role="BUYER", **kw):
    return ca.resolve_conversation_context(state or _detail_state(), text, role=role, **kw)


# CURRENT CONTEXT = écran détail Bœuf (« 1. Rechercher maintenant  2. Retour »)
@pytest.mark.parametrize(
    "text,kind,intent,event",
    [
        ("1", K.ACTIVE_MENU_ACTION, "GET_MY_NEEDS", "SELECTION"),                      # réponse structurée -> REFRESH
        ("rechercher maintenant", K.ACTIVE_MENU_ACTION, "GET_MY_NEEDS", "SELECTION"),  # alias fermé -> REFRESH
        ("retour", K.ACTIVE_MENU_ACTION, "GET_MY_NEEDS", "SELECTION"),                 # alias fermé -> LIST
        ("mes besoins", K.INTERRUPT_WITH_NEW_GOAL, "GET_MY_NEEDS", "NEW_TASK"),        # navigation
        ("mes commandes", K.INTERRUPT_WITH_NEW_GOAL, "BUYER_LIST_ORDERS", "NEW_TASK"),
    ],
)
def test_closed_and_navigation_replies_are_deterministic(text, kind, intent, event):
    d = _decide(text)
    assert d.kind == kind and d.raw["detected_intent"] == intent and d.raw["interpreted_event"] == event


@pytest.mark.parametrize("text", ["je veux acheter 100 kg de tomates", "je veux vendre 100 kg d'oignons"])
def test_explicit_new_goals_supersede_the_menu(text):
    d = _decide(text, role="BUYER" if "acheter" in text else "PRODUCER")
    assert d.kind == K.INTERRUPT_WITH_NEW_GOAL and d.reclassify and d.purge


@pytest.mark.parametrize("text", ["j ai besoin de 2 chevres chaque semaine", "mets-en 3", "quel temps fera-t-il demain ?", "finalement 5"])
def test_open_free_text_is_never_turned_into_a_menu_action_by_python(text):
    """Python ne devine pas : un langage libre n'est ni une action de menu (pas de `raw`), ni capturé par une liste de
    phrases — il continue vers l'interprétation sémantique (micro-prompt avec le contexte comme prior)."""
    d = _decide(text)
    assert d.kind == K.ACTIVE_SLOT and d.raw is None


def test_live_menu_view_publishes_the_expectation():
    view = ca.live_menu_view(_detail_state())
    assert view == {"title": "Écran du besoin récurrent « Boeuf »", "labels": ["Rechercher maintenant", "Retour"],
                    "actions": {"1": "REFRESH", "2": "LIST"}}


def test_a_stale_or_foreign_menu_is_not_an_expectation():
    assert ca.live_menu_view(_detail_state(created_at=time.time() - 3600)) is None
    foreign = {**_detail_state(), **set_pending_interaction(InteractionKind.SELECTION_MENU, goal="BUYER_LIST_ORDERS")}
    assert ca.live_menu_view(foreign) is None
    assert ca.live_menu_view({}) is None


@pytest.mark.parametrize("action,flagged", [("CONFIRM", True), ("REJECT", True), ("REFRESH", False), ("LIST", False)])
def test_a_free_text_selection_cannot_mutate(action, flagged):
    state = _detail_state(actions={"1": action, "2": "LIST"})
    raw = {"interpreted_event": "SELECTION", "detected_intent": "UNKNOWN", "extracted_entities": {"selection_index": 1},
           "raw_analysis": {"path": "selection_microprompt"}}
    out = ca.guard_free_text_selection(state, raw)
    assert bool(out["extracted_entities"].get("closed_reply_required")) is flagged


def test_the_guard_ignores_non_selection_results_and_other_contexts():
    raw = {"interpreted_event": "NEW_TASK", "detected_intent": "CREATE_RECURRING_NEED", "extracted_entities": {}}
    assert ca.guard_free_text_selection(_detail_state(), raw) is raw
    sel = {"interpreted_event": "SELECTION", "extracted_entities": {"selection_index": 1}}
    assert ca.guard_free_text_selection({}, sel) is sel
