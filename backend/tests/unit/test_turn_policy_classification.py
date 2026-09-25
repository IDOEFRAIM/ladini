"""`classify_turn` (Phase 2 hardening, commit 6) — vocabulaire canonique d'un tour, en mode
SHADOW : cette fonction n'est consommée par AUCUN routeur de production aujourd'hui (voir
`test_turn_policy_is_shadow_only_not_authoritative` ci-dessous), elle documente seulement,
dans un vocabulaire unique, ce que les couches existantes ont déjà décidé. Ces tests fixent
son étiquetage sur les scénarios réels de `test_conversation_characterization.py` (incidents
A-O) pour qu'un futur chantier d'unification parte d'une matrice déjà vérifiée."""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

from ladini.graphs.agents.market_coach.core.conversation_decision import (
    ConversationAction,
)
from ladini.graphs.agents.market_coach.core.turn_policy import TurnAction, classify_turn


def test_an_approved_interruption_is_always_labelled_interrupt():
    """Incident I ("maïs" pendant un goal actif, cognitive_guard autorise) : le label ne
    doit JAMAIS dépendre de `interpreted_event` une fois l'interruption approuvée — c'est
    précisément le bug B3 (`is_short`) que ce vocabulaire doit rendre impossible à
    réintroduire silencieusement."""
    result = classify_turn(
        interpreted_event="NEW_TASK",
        cognitive_decision={"action": ConversationAction.INTERRUPT_ACTIVE_GOAL},
        goal_before="CREATE_RECURRING_NEED",
        goal_after="BUYER_ADD_TO_CART",
        normalized_text="maïs",
    )
    assert result.action is TurnAction.INTERRUPT


def test_a_bare_confirm_event_is_confirm():
    result = classify_turn(
        interpreted_event="CONFIRM",
        cognitive_decision=None,
        goal_before="CREATE_RECURRING_NEED",
        goal_after="CREATE_RECURRING_NEED",
        normalized_text="oui",
    )
    assert result.action is TurnAction.CONFIRM


def test_a_bare_reject_event_is_reject_not_cancel():
    """Décision produit B : "non" à une porte de confirmation -> REJECT terminal du
    proposal courant, distinct d'une annulation explicite."""
    result = classify_turn(
        interpreted_event="REJECT",
        cognitive_decision=None,
        goal_before="CREATE_RECURRING_NEED",
        goal_after=None,
        normalized_text="non",
    )
    assert result.action is TurnAction.REJECT


def test_an_explicit_abandon_phrase_reject_is_cancel():
    """Décision produit A : "laisse tomber"/"annule"/"oublie ça" -> CANCEL terminal, pas un
    simple REJECT du proposal en cours."""
    for phrase in ("laisse tomber", "annule", "oublie ça", "stop"):
        result = classify_turn(
            interpreted_event="REJECT",
            cognitive_decision=None,
            goal_before="CREATE_RECURRING_NEED",
            goal_after=None,
            normalized_text=phrase,
        )
        assert result.action is TurnAction.CANCEL, phrase


def test_a_new_task_with_no_prior_goal_is_new_task():
    result = classify_turn(
        interpreted_event="NEW_TASK",
        cognitive_decision=None,
        goal_before=None,
        goal_after="CREATE_RECURRING_NEED",
        normalized_text="je veux 14 coqs chaque semaine",
    )
    assert result.action is TurnAction.NEW_TASK


def test_a_new_task_replacing_a_different_goal_is_new_task():
    """Incident : ancien tunnel catalogue actif -> nouvelle demande récurrente. Le goal a
    RÉELLEMENT changé (le planner l'a laissé changer), donc NEW_TASK, pas CONTINUE."""
    result = classify_turn(
        interpreted_event="NEW_TASK",
        cognitive_decision=None,
        goal_before="BROWSE_CATALOG",
        goal_after="CREATE_RECURRING_NEED",
        normalized_text="je veux 14 coqs chaque semaine",
    )
    assert result.action is TurnAction.NEW_TASK


def test_a_new_task_event_on_the_same_goal_is_a_correction():
    """Incident H ("non plutôt 23 boeufs") : `cognitive_guard` n'a pas jugé nécessaire
    d'interrompre (pas de INTERRUPT_ACTIVE_GOAL), et le goal n'a pas bougé -> le domaine du
    goal courant absorbe ceci comme une correction du draft, jamais une perte."""
    result = classify_turn(
        interpreted_event="NEW_TASK",
        cognitive_decision={"action": ConversationAction.CONTINUE_ACTIVE_GOAL},
        goal_before="CREATE_RECURRING_NEED",
        goal_after="CREATE_RECURRING_NEED",
        normalized_text="non plutôt 23 boeufs",
    )
    assert result.action is TurnAction.CORRECT


def test_repeated_confirmation_on_the_same_goal_is_answer_pending():
    result = classify_turn(
        interpreted_event="ANSWER",
        cognitive_decision=None,
        goal_before="CREATE_RECURRING_NEED",
        goal_after="CREATE_RECURRING_NEED",
        normalized_text="oui je confirme",
    )
    assert result.action is TurnAction.ANSWER_PENDING


def test_disambiguate_and_clarify_and_recovery_all_map_to_clarify():
    for action in (
        ConversationAction.DISAMBIGUATE,
        ConversationAction.CLARIFY,
        ConversationAction.RECOVER_ACTIVE_GOAL,
        ConversationAction.ABANDON_ACTIVE_GOAL,
    ):
        result = classify_turn(
            interpreted_event="NEW_TASK",
            cognitive_decision={"action": action},
            goal_before="CREATE_RECURRING_NEED",
            goal_after="CREATE_RECURRING_NEED",
            normalized_text="50 moutons et 7 chèvres",
        )
        assert result.action is TurnAction.CLARIFY, action


def test_unknown_event_is_clarify():
    result = classify_turn(
        interpreted_event="UNKNOWN",
        cognitive_decision=None,
        goal_before=None,
        goal_after=None,
        normalized_text="???",
    )
    assert result.action is TurnAction.CLARIFY


def test_missing_event_defaults_to_unknown_and_is_classified_deterministically():
    result = classify_turn(
        interpreted_event=None,
        cognitive_decision=None,
        goal_before=None,
        goal_after=None,
    )
    assert result.action is TurnAction.CLARIFY  # UNKNOWN -> CLARIFY, jamais une exception


def test_turn_policy_is_shadow_only_not_authoritative():
    """Garde architecturale : `classify_turn`/`TurnClassification` ne doit être IMPORTÉ,
    aujourd'hui, que par ce test et par `core/turn_trace.py` (commit 11 — la seule
    consommation légitime : peupler `TurnTrace.turn_decision` pour l'observabilité, jamais
    pour router/décider quoi que ce soit, voir `turn_trace.py::capture_pre_cleanup` et sa
    propre docstring de module) — jamais par un nœud/flow qui route un tour en production.
    Si ce test casse en ajoutant un import ailleurs, c'est le signal qu'on bascule
    `decide_turn` en autoritaire SANS l'avoir décidé explicitement (mandat Phase 2 §21-22 :
    passage en 2 temps)."""
    from ladini.graphs.agents.market_coach import core as _core_pkg

    core_dir = Path(inspect.getfile(_core_pkg)).parent
    package_root = core_dir.parent
    allowed_importers = {"turn_policy.py", "turn_trace.py"}
    offenders = []
    for path in package_root.rglob("*.py"):
        if path.name in allowed_importers:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and "turn_policy" in node.module:
                offenders.append(str(path.relative_to(package_root)))
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if "turn_policy" in alias.name:
                        offenders.append(str(path.relative_to(package_root)))
    assert offenders == [], offenders
