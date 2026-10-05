"""B27 — corpus d'évaluation du contrat sémantique/contexte (SANS réseau, SANS LLM).

Ce corpus n'est PAS une liste d'alias : aucune phrase n'est rattachée à une intention par ce module. Chaque cas décrit un
ÉCRAN (attente), une lecture du modèle (index, certitude, référence extraite) et le MESSAGE tel que l'utilisateur l'a écrit ;
il vérifie que les primitives DÉTERMINISTES (preuve de sélection, cohérence des nombres, date calculée par le domaine, entrée
fermée, relation au contexte) décident correctement — quelle que soit la formulation. Catégories couvertes : réponses fermées,
réponses libres, corrections, nouvelles tâches, interruptions, références, cibles ambiguës, négations, phrases composées, fautes,
contexte périmé, messages inter-domaines.
"""
from __future__ import annotations

import time
from datetime import date

import pytest

from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    set_pending_interaction,
)
from ladini.graphs.agents.market_coach.interpreter import context_arbitration as ca

TODAY = date(2026, 10, 5)

PROPOSAL = {
    "title": "Écran du besoin récurrent « Oignon » (livraison 6 octobre)",
    "labels": ["Accepter la proposition", "Refuser la proposition", "Retour"],
    "actions": {"1": "CONFIRM", "2": "REJECT", "3": "LIST"},
    "facts": {}, "shown_numbers": [75.0, 175.0, 13125.0, 2.0, 6.0],
}
TWINS = {
    "title": "Liste de vos besoins récurrents",
    "labels": [
        "Chevre — 3 UNITE/semaine — actif — démarré le 3 octobre — prochaine livraison 10 octobre",
        "Chevre — 3 UNITE/semaine — actif — démarré le 5 octobre — prochaine livraison 6 octobre",
        "Boeuf — 2 TETE/semaine — actif — prochaine livraison 8 octobre",
    ],
    "actions": {"1": "SELECT", "2": "SELECT", "3": "SELECT"},
    "facts": {"1": {"starts": ["2026-10-03"], "deliveries": ["2026-10-10"]}, "2": {"starts": ["2026-10-05"], "deliveries": ["2026-10-06"]},
              "3": {"starts": ["2026-09-20"], "deliveries": ["2026-10-08"]}},
    "shown_numbers": [],
}
CONFIRM_75 = {"raw_analysis": {"selection_confidence": 0.95}}


def _res(confidence=0.95):
    return {"raw_analysis": {} if confidence is None else {"selection_confidence": confidence}}


# ── 1. acceptation/refus NATURELS d'une entrée mutante (aucune phrase codée : le modèle a lu, la structure vérifie) ──────────
NATURAL_ACCEPTANCES = [
    "oui", "je confirme", "je prends", "ça me va", "ok", "vas-y", "je veux celui-là", "prends les 75", "d'accord pour celui-là",
    "on valide", "oui prends-les", "yes", "ça marche", "go", "c'est bon pour moi", "oui oui", "parfait", "je prends les 75 kg",
    "ok pour ce producteur", "oui je prends 75", "d accord", "oui svp", "je veu celui la", "ca me va", "ouais", "okay",
]


@pytest.mark.parametrize("text", NATURAL_ACCEPTANCES)
def test_a_certain_short_acceptance_without_new_values_is_a_candidate(text):
    assert ca.assess_natural_mutation(PROPOSAL, 1, _res(0.95), text) == "candidate"


NEW_VALUES = [
    "oui mais mets 100", "oui mais plutôt 100 kg", "ok mais 120", "je prends 80", "prends-en 50", "oui mais pour 30 jours",
    "je veux 76", "oui mais à 200 FCFA", "d'accord pour 12000", "oui mais 1,5 tonne", "ok 90", "oui mets 100",
]


@pytest.mark.parametrize("text", NEW_VALUES)
def test_an_acceptance_that_brings_an_unshown_number_is_a_correction_not_a_confirmation(text):
    assert ca.assess_natural_mutation(PROPOSAL, 1, _res(0.99), text) == "correction"


@pytest.mark.parametrize("confidence", [None, 0.0, 0.3, 0.6, 0.89])
def test_not_certain_enough_requires_a_closed_reply(confidence):
    assert ca.assess_natural_mutation(PROPOSAL, 1, _res(confidence), "je prends") == "closed_required"


@pytest.mark.parametrize("text", [
    "oui d'accord je veux bien prendre ça mais seulement si le producteur peut livrer avant midi sinon on verra",
    "alors là franchement je ne sais pas trop quoi te dire mais bon pourquoi pas on va essayer",
])
def test_a_long_message_is_never_a_natural_acceptance(text):
    assert ca.assess_natural_mutation(PROPOSAL, 1, _res(0.99), text) == "closed_required"


@pytest.mark.parametrize("text", ["oui 1", "je prends le 1", "oui option 3", "2 pas cette fois"])
def test_menu_indices_are_not_new_values(text):
    assert ca.assess_natural_mutation(PROPOSAL, 1, _res(0.95), text) == "candidate"


# ── 2. preuve d'un choix libre dans une liste (jamais un chiffre isolé, jamais un mot partagé) ────────────────────────────
EVIDENCE = [
    ("le boeuf", 3, True), ("mon boeuf", 3, True), ("les boeufs", 3, True),  # repli de pluriel
    ("le troisième", 3, True), ("le premier", 1, True), ("le deuxième", 2, True), ("la deuxième", 2, True), ("le second", 2, True),
    ("le deuxième", 1, False), ("le premier", 2, False), ("mets-en 3", 3, False), ("j'en veux 3", 3, False), ("5", 2, False),
    ("mes chèvres", 1, False), ("mes chèvres", 2, False), ("chevre", 1, False), ("le chevre", 2, False),
    ("la prochaine livraison 10 octobre", 1, False),  # « livraison » et « octobre » sont partagés ou trop faibles
    ("celui du boeuf", 3, True), ("le boeuf", 1, False), ("le boeuf", 2, False), ("boeuf", 3, True), ("BOEUF !", 3, True),
    ("le bœuf", 3, False),  # ligature : aucun jeton commun après repli ASCII -> pas de preuve (le modèle clarifie)
]


@pytest.mark.parametrize("text,index,expected", EVIDENCE)
def test_free_text_choice_evidence(text, index, expected):
    assert ca.selection_is_evidenced(TWINS, index, text) is expected


def test_with_a_single_option_any_label_word_is_evidence():
    single = {**TWINS, "labels": [TWINS["labels"][2]], "actions": {"1": "SELECT"}, "facts": {}}
    assert ca.selection_is_evidenced(single, 1, "et le boeuf alors")
    assert not ca.selection_is_evidenced(single, 1, "mets-en 3")


# ── 3. candidats d'une référence partagée ─────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,expected", [
    ("mes chèvres", ["1", "2"]), ("la chevre", ["1", "2"]), ("le boeuf", ["3"]), ("les chevres et le boeuf", ["1", "2", "3"]),
    ("tomates", []), ("mets-en 3", []),
])
def test_reference_candidates(text, expected):
    assert ca.reference_candidates(TWINS, text) == expected


# ── 4. dates : le DOMAINE calcule, le modèle n'écrit jamais une date ──────────────────────────────────────────────────────
DATES = [
    ({"offset_days": 1}, "celle de demain", "one", ["2"]),
    ({"offset_days": 1, "role": "DELIVERY"}, "celle de demain", "one", ["2"]),
    ({"offset_days": 0}, "celle d'aujourd'hui", "one", ["2"]),  # sans rôle : le démarrage (5 octobre) compte aussi
    ({"offset_days": 0, "role": "DELIVERY"}, "celle d'aujourd'hui", "none", []),  # livraison aujourd'hui : aucune
    ({"offset_days": 0, "role": "START"}, "celui qui commence aujourd'hui", "one", ["2"]),
    ({"offset_days": 3}, "dans trois jours", "one", ["3"]),
    ({"offset_days": 5}, "celle du 10", "one", ["1"]),
    ({"offset_days": -2, "role": "DELIVERY"}, "avant-hier", "none", []),
    ({"offset_days": -2, "role": "START"}, "celui de samedi", "one", ["1"]),
    ({"offset_days": 40}, "dans six semaines", "none", []),
    ({"day": 5}, "celui qui commence le 5", "one", ["2"]),
    ({"day": 5, "role": "START"}, "celui qui commence le 5", "one", ["2"]),
    ({"day": 5, "role": "DELIVERY"}, "livraison le 5", "none", []),
    ({"day": 5, "month": 10}, "celui du 5 octobre", "one", ["2"]),
    ({"day": 3, "month": 10}, "celui du 3 octobre", "one", ["1"]),
    ({"day": 3, "month": 11}, "celui du 3 novembre", "none", []),
    ({"day": 5, "month": 11}, "celui du 5 novembre", "none", []),
    ({"day": 6}, "celui du 6", "one", ["2"]),
    ({"day": 10}, "le 10", "one", ["1"]),
    ({"day": 8}, "livraison le 8", "one", ["3"]),
    ({"day": 20, "month": 9}, "le 20 septembre", "one", ["3"]),
    ({"day": 5}, "celui du début", "none", []),  # le 5 n'est pas DIT : jamais pris en compte
    ({"day": 7}, "le 5", "none", []),  # le modèle prétend 7, le message dit 5
    ({"day": 31}, "le 31", "none", []),
]


@pytest.mark.parametrize("ref,text,kind,hits", DATES)
def test_date_reference_resolution(ref, text, kind, hits):
    assert ca.resolve_date_reference(TWINS, ref, text, today=TODAY) == (kind, hits)


def test_two_options_on_the_same_date_are_many():
    twins = {**TWINS, "facts": {**TWINS["facts"], "3": {"starts": [], "deliveries": ["2026-10-06"]}}}
    assert ca.resolve_date_reference(twins, {"offset_days": 1}, "demain", today=TODAY) == ("many", ["2", "3"])


def test_invalid_dates_in_facts_are_ignored_not_fatal():
    broken = {**TWINS, "facts": {"1": {"starts": ["pas une date"], "deliveries": ["2026-10-06"]}}}
    assert ca.resolve_date_reference(broken, {"offset_days": 1}, "demain", today=TODAY) == ("one", ["1"])


# ── 5. entrée FERMÉE : un numéro entier ou « option/numéro N » — jamais « contient un chiffre » ───────────────────────────
CLOSED = [
    ("1", "1"), ("01", "1"), ("1.", "1"), ("1)", "1"), ("option 2", "2"), ("choix 3", "3"), ("numero 4", "4"), ("n 5", "5"), ("12", "12"),
    ("je veux 1 chevre", None), ("mets-en 1", None), ("j en veux 1 de plus", None), ("123", None), ("un", None), ("", None),
    ("1 kg", None), ("oui 1", None), ("3 chevres", None), ("option", None), ("celui du 5", None),
]


@pytest.mark.parametrize("text,expected", CLOSED)
def test_closed_menu_index(text, expected):
    assert ca.closed_menu_index(ca.fold(text)) == expected


# ── 6. relation au contexte : dérivée de signaux STRUCTURÉS, jamais du texte ──────────────────────────────────────────────
def _live_state(view):
    return {**set_pending_interaction(InteractionKind.SELECTION_MENU, goal="GET_MY_NEEDS"),
            "working_memory": {"recurring_need_menu": {
                "created_at": time.time(), "title": view["title"], "actions": view["actions"],
                "labels": {k: lbl for k, lbl in zip(view["actions"], view["labels"], strict=False)},
                "facts": view.get("facts") or {}, "shown_numbers": view.get("shown_numbers") or [],
                "target": {"type": "RECURRING_NEED", "id": "N1", "product": "Oignon"}}}}


RELATIONS = [
    ({"interpreted_event": "SELECTION", "detected_intent": "UNKNOWN"}, "ANSWER"),
    ({"interpreted_event": "CONFIRM", "detected_intent": "GET_MY_NEEDS"}, "ANSWER"),
    ({"interpreted_event": "REJECT", "detected_intent": "GET_MY_NEEDS"}, "ANSWER"),
    ({"interpreted_event": "UPDATE", "detected_intent": "UPDATE_RECURRING_NEED"}, "CORRECTION"),
    ({"interpreted_event": "NEW_TASK", "detected_intent": "UPDATE_RECURRING_NEED"}, "CORRECTION"),
    ({"interpreted_event": "NEW_TASK", "detected_intent": "REFRESH_RECURRING_MATCHING"}, "ANSWER"),
    ({"interpreted_event": "NEW_TASK", "detected_intent": "CREATE_RECURRING_NEED"}, "NEW_TASK"),
    ({"interpreted_event": "NEW_TASK", "detected_intent": "BUYER_REQUEST"}, "NEW_TASK"),
    ({"interpreted_event": "NEW_TASK", "detected_intent": "SALES_PUBLISH_PRODUCT"}, "NEW_TASK"),
    ({"interpreted_event": "NEW_TASK", "detected_intent": "BUYER_LIST_ORDERS"}, "INTERRUPTION"),
    ({"interpreted_event": "NEW_TASK", "detected_intent": "GET_MY_NEEDS"}, "INTERRUPTION"),
    ({"interpreted_event": "UNKNOWN", "detected_intent": "UNKNOWN"}, "AMBIGUOUS"),
    ({"interpreted_event": "AMBIGUOUS", "detected_intent": "UNKNOWN"}, "AMBIGUOUS"),
    ({"interpreted_event": "OUT_OF_SCOPE", "detected_intent": "UNKNOWN"}, "UNRELATED"),
    ({"interpreted_event": "NEW_TASK", "detected_intent": "BUYER_REQUEST", "interruption_unresolved": True}, "AMBIGUOUS"),
]


@pytest.mark.parametrize("result,expected", RELATIONS)
def test_relation_to_context(result, expected):
    assert ca.derive_relation(_live_state(PROPOSAL), result).value == expected


def test_a_recovery_notification_is_a_context_target_for_a_search_again():
    state = {"recovery_context": {"recurring_need_ids": ["N-1"], "occurrence_ids": ["O-1"], "dates": ["2026-10-27"]}}
    result = {"interpreted_event": "NEW_TASK", "detected_intent": "REFRESH_RECURRING_MATCHING"}
    assert ca.derive_relation(state, result) == ca.RelationToContext.ANSWER
    assert ca.recovery_target(state) == {"type": "RECURRING_NEED", "id": "N-1", "occurrence_id": "O-1", "source": "recovery_notification"}


@pytest.mark.parametrize("recovery", [None, {}, {"recurring_need_ids": []}, {"recurring_need_ids": ["A", "B"]}, "x"])
def test_no_or_several_recovery_needs_is_never_a_target(recovery):
    assert ca.recovery_target({"recovery_context": recovery}) is None


# ── 7. la garde de bout en bout sur une vue vivante ───────────────────────────────────────────────────────────────────────
def _sel(index, confidence=0.95, **extra):
    return {"interpreted_event": "SELECTION", "detected_intent": "UNKNOWN", "interpreter_confidence": 0.95,
            "extracted_entities": {"selection_index": index},
            "raw_analysis": {"path": "selection_microprompt", "selection_confidence": confidence, **extra}}


GUARDS = [
    # (vue, résultat du modèle, texte, garde attendue)
    (PROPOSAL, _sel(1), "je prends les 75 kg", ca.GUARD_NATURAL_CONFIRMATION),
    (PROPOSAL, _sel(2), "pas celui-là", ca.GUARD_NATURAL_CONFIRMATION),
    (PROPOSAL, _sel(1), "oui mais mets 100", ca.GUARD_FREE_TEXT_ACTION),
    (PROPOSAL, _sel(1, 0.4), "je prends", ca.GUARD_MUTATION_CLOSED_REPLY),
    (PROPOSAL, _sel(3), "retour s'il te plait", ca.GUARD_FREE_TEXT_ACTION),
    (TWINS, _sel(3), "mets-en 3", ca.GUARD_FREE_TEXT_ACTION),
    (TWINS, _sel(1), "mes chèvres", ca.GUARD_FREE_TEXT_ACTION),
    (TWINS, _sel(3), "le boeuf", None),
    (TWINS, _sel(2), "le deuxième", None),
    (TWINS, _sel(2, reference_resolved="date"), "celui du 5", None),
]


@pytest.mark.parametrize("view,result,text,guard", GUARDS)
def test_guard_free_text_selection(view, result, text, guard):
    out = ca.guard_free_text_selection(_live_state(view), result, text)
    assert (out["raw_analysis"].get("guard") if out is not result else None) == guard


def test_a_shared_word_narrows_the_clarification_to_the_matching_options():
    out = ca.guard_free_text_selection(_live_state(TWINS), _sel(1), "mes chèvres")
    assert out["raw_analysis"]["reference_candidates"] == ["1", "2"]


def test_the_targeted_clarification_lists_only_the_matching_options():
    state = {**_live_state(TWINS), "extracted_entities": {"reference_candidates": ["1", "2"]}}
    text = ca.targeted_menu_clarification(state)
    assert text is not None and "Plusieurs options correspondent" in text and "Boeuf" not in text and "1. Chevre" in text


def test_non_selection_results_and_foreign_menus_are_untouched():
    unknown = {"interpreted_event": "UNKNOWN", "extracted_entities": {}, "raw_analysis": {}}
    assert ca.guard_free_text_selection(_live_state(PROPOSAL), unknown, "x") is unknown
    assert ca.guard_free_text_selection({}, _sel(1), "je prends") == _sel(1)


# ── 8. nombres : lecture structurelle ─────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,expected", [
    ("75 kg", [75.0]), ("1,5 tonne", [1.5]), ("1.5", [1.5]), ("de 3 à 5", [3.0, 5.0]), ("rien", []), ("", []), ("100 FCFA et 20 kg", [100.0, 20.0]),
    ("le 5 octobre", [5.0]), ("2026", [2026.0]),
])
def test_numbers_in(text, expected):
    assert ca.numbers_in(text) == expected
