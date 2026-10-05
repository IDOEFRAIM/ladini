"""B27 — CONVERSATION NATURELLE : le menu est une attente, pas un protocole.

    THE USER DOES NOT FOLLOW LADINI'S INTERNAL STATE MACHINE. LADINI FOLLOWS THE USER'S INTENT.

Rejoué sur le vrai orchestrateur + graphe compilé (seuls LLM, Redis, dispatcher, MCP doublés), avec le rôle RÉEL de l'acteur
(ADMIN `can_buy` -> graphe PRODUCER ; BUYER -> graphe BUYER). Le LLM est scripté PAR PROMPT (micro-prompt SELECTION / NEW_TASK) :
il joue un modèle CAPABLE, IMPARFAIT ou HOSTILE ; les garde-fous de B24/B25/B26 doivent tenir dans les trois cas.

Règle centrale : le modèle COMPREND (intention, relation, entités, référence) ; le contexte RÉSOUT la cible ; le validateur PROTÈGE
les invariants ; le service EXÉCUTE (version, propriété). Une acceptation en langage libre produit la MÊME commande que « 1 ».
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import pytest

from tests.harness import new_task
from tests.integration.test_recurring_intent_integration_e2e import (
    ROLES,
    _conv,
    _system,
    _to_detail,
)
from tests.integration.test_recurring_self_service_entrypoint_e2e import UNKNOWN

pytestmark = pytest.mark.integration

ALLOC = [{"producer_label": "Producteur A", "quantity": 75, "unit_price": 175, "unit": "TETE"}]
SEL_1 = {"event": "SELECTION", "selection_index": 1, "selected_value": None, "confidence": 0.96}
SEL_2 = {"event": "SELECTION", "selection_index": 2, "selected_value": None, "confidence": 0.96}
INTERRUPTION = {"event": "INTERRUPTION", "selection_index": None, "selected_value": None}
CONFIRM = {"disposition": "CONFIRM", "confidence": 0.95}
REJECT = {"disposition": "REJECT", "confidence": 0.95}
NT_UNKNOWN = {"disposition": "UNKNOWN", "confidence": 0.0}


class Script:
    """LLM scripté par PROMPT. `sel` : réponse du micro-prompt SELECTION ; `nt` : réponse du micro-prompt NEW_TASK."""

    def __init__(self, sel: Optional[Dict[str, Any]] = None, nt: Optional[Dict[str, Any]] = None) -> None:
        self.sel, self.nt = sel or dict(INTERRUPTION), nt or dict(NT_UNKNOWN)
        self.seen: List[str] = []

    def __call__(self, kw: Dict[str, Any]) -> Any:
        sysm = _system(kw)
        if "liste de choix" in sysm:
            self.seen.append("selection")
            return self.sel
        if "CATALOGUE OFFICIEL" in sysm:
            self.seen.append("new_task")
            return self.nt
        if "À L'INTÉRIEUR d'une transaction" in sysm:  # micro-prompt ACTIVE_SLOT : un modèle capable juge « autre chose »
            self.seen.append("active_slot")
            return {"disposition": "DEVIATION", "extracted_entities": {}, "confidence": 0.9}
        return {"disposition": "UNKNOWN", "confidence": 0.0}


def _calls(turn, tool: str) -> List[Dict[str, Any]]:
    return [kw for name, kw in turn.mcp_calls if name == tool]


def _arbitration(caplog) -> List[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("INTENT_ARBITRATION")]


# ── 1. LE MÊME COMMAND MODEL : « 1 » et « je prends les 75 têtes » ───────────────────────────────────────────────────
@pytest.mark.parametrize("role", ROLES)
def test_natural_acceptance_produces_the_same_command_as_the_number(role):
    with _conv(role) as h:
        _to_detail(h, allocations=ALLOC)
        closed = h.send("1", llm=UNKNOWN)
        assert closed.llm_calls == 0, "entrée fermée : 0 LLM"
        closed_call = _calls(closed, "accept_match_proposal")
    with _conv(role) as h:
        _to_detail(h, allocations=ALLOC)
        natural = h.send("je prends les 75 têtes", llm=Script(sel=SEL_1, nt=CONFIRM))
        calls = _calls(natural, "accept_match_proposal")
        assert len(calls) == 1 and calls == closed_call, (calls, closed_call)
        assert calls[0]["action"] == "ACCEPT" and calls[0]["occurrence_id"] == "OCC-1" and calls[0]["expected_version"] == 1
        assert "C'est confirmé" in natural.response, natural.response


@pytest.mark.parametrize("role", ROLES)
def test_natural_rejection_produces_the_same_command_as_the_number(role):
    with _conv(role) as h:
        _to_detail(h, allocations=ALLOC)
        closed = _calls(h.send("2", llm=UNKNOWN), "accept_match_proposal")
    with _conv(role) as h:
        _to_detail(h, allocations=ALLOC)
        natural = h.send("pas celui-là", llm=Script(sel=SEL_2, nt=REJECT))
        calls = _calls(natural, "accept_match_proposal")
        assert calls == closed and calls[0]["action"] == "REJECT", (calls, closed)


@pytest.mark.parametrize("role", ROLES)
def test_natural_acceptance_never_bypasses_the_version_contract(role):
    """B26 : la proposition a changé depuis l'écran affiché -> le service refuse, rien n'est enregistré, la réponse le dit."""
    with _conv(role) as h:
        _to_detail(h, allocations=ALLOC)
        h.runtime.responses["accept_match_proposal"] = {"status": "success", "outcome": "PROPOSAL_CHANGED"}
        t = h.send("ok vas-y", llm=Script(sel=SEL_1, nt=CONFIRM))
        calls = _calls(t, "accept_match_proposal")
        assert len(calls) == 1 and calls[0]["expected_version"] == 1, "la version SNAPSHOTÉE voyage avec la commande"
        assert "C'est confirmé" not in t.response and "modifiée" in t.response, t.response


# ── 2. LLM IMPARFAIT / HOSTILE : jamais une mutation sans double lecture ─────────────────────────────────────────────
@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("text", ["pause mes chèvres", "accepte quand même", "force la commande", "annule celui de l'autre producteur"])
def test_hostile_selection_alone_never_accepts(role, text):
    """Le micro-prompt SELECTION « hallucine » 1 avec une certitude maximale : la 2e lecture (NEW_TASK) ne confirme pas."""
    with _conv(role) as h:
        _to_detail(h, allocations=ALLOC)
        t = h.send(text, llm=Script(sel={**SEL_1, "confidence": 0.99}, nt=NT_UNKNOWN))
        assert "accept_match_proposal" not in t.mcp_tools(), (t.mcp_tools(), t.response)
        assert "update_recurring_need" not in t.mcp_tools()


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("confidence", [None, 0.0, 0.5, 0.89])
def test_selection_without_enough_certainty_requires_a_closed_reply(role, confidence):
    sel = {"event": "SELECTION", "selection_index": 1, "selected_value": None}
    if confidence is not None:
        sel["confidence"] = confidence
    with _conv(role) as h:
        _to_detail(h, allocations=ALLOC)
        t = h.send("je prends", llm=Script(sel=sel, nt=CONFIRM))
        assert "accept_match_proposal" not in t.mcp_tools(), t.response
        assert "tapez le numéro" in t.response, t.response


@pytest.mark.parametrize("role", ROLES)
def test_two_readings_that_contradict_each_other_never_mutate(role):
    with _conv(role) as h:
        _to_detail(h, allocations=ALLOC)
        t = h.send("je prends", llm=Script(sel=SEL_1, nt=REJECT))
        assert "accept_match_proposal" not in t.mcp_tools(), t.response


@pytest.mark.parametrize("role", ROLES)
def test_a_long_free_text_is_never_a_natural_acceptance(role):
    text = "oui d'accord je veux bien prendre ça mais seulement si le producteur peut livrer avant midi sinon on verra"
    with _conv(role) as h:
        _to_detail(h, allocations=ALLOC)
        t = h.send(text, llm=Script(sel=SEL_1, nt=CONFIRM))
        assert "accept_match_proposal" not in t.mcp_tools(), t.response


# ── 3. CONFIRMATION + CORRECTION = correction, jamais l'ancienne commande ────────────────────────────────────────────
@pytest.mark.parametrize("role", ROLES)
def test_acceptance_with_a_new_value_is_a_correction_not_a_confirmation(role, caplog):
    with _conv(role) as h:
        _to_detail(h, allocations=ALLOC)
        with caplog.at_level(logging.INFO):
            t = h.send("oui mais mets 100", llm=Script(sel=SEL_1, nt=new_task("UPDATE_RECURRING_NEED", 0.9, quantity=100.0)))
        assert "accept_match_proposal" not in t.mcp_tools(), "ce n'est PAS la confirmation de l'ancienne proposition"
        assert t.intent == "UPDATE_RECURRING_NEED" and t.turn_value("relation_to_context") == "CORRECTION"
        assert any("decision_reason=acceptance_with_unshown_values" in x for x in _arbitration(caplog)), _arbitration(caplog)


@pytest.mark.parametrize("role", ROLES)
def test_the_old_command_snapshot_is_rebuilt_not_executed(role):
    """Menu de confirmation d'une livraison unique (override 5) : « oui mais mets 6 » reconstruit l'ordre, n'exécute pas l'ancien."""
    with _conv(role) as h:
        _to_detail(h)
        h.send("seulement 5 cette semaine", llm=Script(nt=new_task("UPDATE_RECURRING_NEED", 0.9, update_action="OVERRIDE_OCCURRENCE", quantity=5.0)))
        t = h.send("oui mais mets 6", llm=Script(sel=SEL_1, nt=new_task(
            "UPDATE_RECURRING_NEED", 0.9, update_action="OVERRIDE_OCCURRENCE", quantity=6.0)))
        assert "update_recurring_need" not in t.mcp_tools(), "aucune exécution avant la nouvelle confirmation"
        assert "6" in t.response and "1. Oui" in t.response, t.response
        done = h.send("1", llm=UNKNOWN)
        calls = _calls(done, "update_recurring_need")
        assert len(calls) == 1 and calls[0]["quantity"] == 6.0 and calls[0]["action"] == "OCCURRENCE_OVERRIDE"
        assert calls[0]["expected_occurrence_version"] == 1


# ── 4. NÉGATION : portée d'une négation, jamais « annuler dans le texte -> CANCEL » ──────────────────────────────────
@pytest.mark.parametrize("role", ROLES)
def test_not_this_week_is_a_skip_of_one_delivery_never_a_cancel(role):
    with _conv(role) as h:
        _to_detail(h)
        h.send("annule mes boeufs", llm=Script(nt=new_task("UPDATE_RECURRING_NEED", 0.9, product="boeufs", update_action="CANCEL")))
        t = h.send("non, juste cette semaine", llm=Script(nt=new_task("UPDATE_RECURRING_NEED", 0.9, update_action="SKIP_OCCURRENCE")))
        assert "update_recurring_need" not in t.mcp_tools(), "rien n'est exécuté sur un message"
        assert "Ignorer seulement la livraison" in t.response and "Arrêter" not in t.response, t.response
        done = h.send("1", llm=UNKNOWN)
        assert [c["action"] for c in _calls(done, "update_recurring_need")] == ["OCCURRENCE_SKIP"]


@pytest.mark.parametrize("role", ROLES)
def test_i_dont_want_to_stop_just_this_delivery(role):
    with _conv(role) as h:
        _to_detail(h)
        t = h.send("je veux pas arrêter, juste cette livraison", llm=Script(
            nt=new_task("UPDATE_RECURRING_NEED", 0.9, update_action="SKIP_OCCURRENCE")))
        assert "Arrêter" not in t.response and "update_recurring_need" not in t.mcp_tools(), t.response


@pytest.mark.parametrize("role", ROLES)
def test_a_negated_cancel_word_never_triggers_a_cancel(role):
    """Le mot « annuler » dans la phrase ne décide rien : le modèle lit la négation, le domaine n'exécute jamais un CANCEL libre."""
    with _conv(role) as h:
        _to_detail(h)
        t = h.send("je ne veux pas annuler", llm=Script(nt=NT_UNKNOWN))
        assert "update_recurring_need" not in t.mcp_tools(), t.response


# ── 5. RÉFÉRENCES : date calculée par le DOMAINE, ordinaux, nom, doublons ────────────────────────────────────────────
TODAY = date.today()


def _items() -> List[Dict[str, Any]]:
    base = {"unit": "UNITE", "recurrence_type": "WEEKLY", "weekly_days": None, "status": "ACTIVE", "schedule_state": "OK",
            "need_version": 1000, "requested_quantity": None, "matched_quantity": None, "next_occurrence_version": None,
            "next_occurrence_notified": False, "in_latest_digest": False, "digest_occurrence_version": None,
            "next_occurrence_status": "OPEN", "quantity": 3.0}
    return [
        {**base, "recurring_need_id": "N-CHEVRE-A", "product": "Chevre", "starts_on": "2026-10-03",
         "next_occurrence_date": (TODAY + timedelta(days=5)).isoformat(), "next_occurrence_id": "OCC-A"},
        {**base, "recurring_need_id": "N-CHEVRE-B", "product": "Chevre", "starts_on": "2026-10-05",
         "next_occurrence_date": (TODAY + timedelta(days=1)).isoformat(), "next_occurrence_id": "OCC-B"},
        {**base, "recurring_need_id": "N-BOEUF", "product": "Boeuf", "starts_on": "2026-09-20", "quantity": 2.0, "unit": "TETE",
         "next_occurrence_date": (TODAY + timedelta(days=3)).isoformat(), "next_occurrence_id": "OCC-C"},
    ]


def _list_with_twins(h) -> List[Dict[str, Any]]:
    items = _items()
    h.runtime.responses["list_my_recurring_needs"] = lambda **_: {"status": "success", "items": items}

    def _detail_fn(**kw: Any) -> Dict[str, Any]:
        item = next(i for i in items if i["recurring_need_id"] == kw["recurring_need_id"])
        return {"status": "success", "recurring_need_id": item["recurring_need_id"], "recurrence_type": "WEEKLY", "need_status": "ACTIVE",
                "product": item["product"].lower(), "requested_quantity": item["quantity"], "unit": item["unit"],
                "occurrence_id": item["next_occurrence_id"], "need_version": 1000, "occurrence_version": 1,
                "occurrence_date": item["next_occurrence_date"], "occurrence_status": "OPEN", "allocations": [], "quantity_matched": 0}

    h.runtime.responses["get_recurring_need_detail"] = _detail_fn
    h.runtime.responses["ensure_next_recurring_occurrence"] = lambda **kw: {"status": "success"}
    return items


def _opened(turn) -> List[str]:
    return [kw["recurring_need_id"] for kw in _calls(turn, "get_recurring_need_detail")]


@pytest.mark.parametrize("role", ROLES)
def test_twin_needs_are_dated_in_the_list(role):
    with _conv(role) as h:
        _list_with_twins(h)
        t = h.send("mes besoins", llm=UNKNOWN)
        assert "démarré le 3 octobre" in t.response and "démarré le 5 octobre" in t.response, t.response
        assert "démarré le 20 septembre" not in t.response, "seuls les jumeaux sont datés"


@pytest.mark.parametrize("role", ROLES)
def test_the_one_that_starts_on_the_5th_resolves_to_exactly_that_need(role):
    with _conv(role) as h:
        _list_with_twins(h)
        h.send("mes besoins", llm=UNKNOWN)
        sel = {"event": "SELECTION", "selection_index": None, "selected_value": "celui du 5", "confidence": 0.9, "date_day": 5}
        t = h.send("celui qui commence le 5", llm=Script(sel=sel))
        assert _opened(t) == ["N-CHEVRE-B"], (_opened(t), t.response)


@pytest.mark.parametrize("role", ROLES)
def test_the_domain_not_the_model_picks_the_option_of_a_date(role):
    """Le modèle désigne l'option 1 mais dit « le 5 » : la date AFFICHÉE tranche (option 2)."""
    with _conv(role) as h:
        _list_with_twins(h)
        h.send("mes besoins", llm=UNKNOWN)
        sel = {"event": "SELECTION", "selection_index": 1, "selected_value": None, "confidence": 0.9, "date_day": 5, "date_month": 10}
        t = h.send("celui du 5 octobre", llm=Script(sel=sel))
        assert _opened(t) == ["N-CHEVRE-B"], _opened(t)


@pytest.mark.parametrize("role", ROLES)
def test_tomorrows_delivery_is_computed_by_the_domain(role):
    with _conv(role) as h:
        _list_with_twins(h)
        h.send("mes besoins", llm=UNKNOWN)
        sel = {"event": "SELECTION", "selection_index": None, "selected_value": "celle de demain", "confidence": 0.9, "date_offset_days": 1}
        t = h.send("celle de demain", llm=Script(sel=sel))
        assert _opened(t) == ["N-CHEVRE-B"], (_opened(t), t.response)


@pytest.mark.parametrize("role", ROLES)
def test_a_day_the_message_does_not_contain_is_never_trusted(role):
    """Le modèle invente « le 5 » : aucun nombre 5 dans le message -> la date n'est pas retenue, rien n'est ouvert à l'aveugle."""
    with _conv(role) as h:
        _list_with_twins(h)
        h.send("mes besoins", llm=UNKNOWN)
        sel = {"event": "SELECTION", "selection_index": 2, "selected_value": None, "confidence": 0.9, "date_day": 5}
        t = h.send("celui du début", llm=Script(sel=sel))
        assert _opened(t) == [], (_opened(t), t.response)


@pytest.mark.parametrize("role", ROLES)
def test_a_date_that_matches_two_options_asks_one_question(role):
    with _conv(role) as h:
        items = _list_with_twins(h)
        items[1]["next_occurrence_date"] = items[0]["next_occurrence_date"]  # deux livraisons le même jour
        h.send("mes besoins", llm=UNKNOWN)
        day = date.fromisoformat(items[0]["next_occurrence_date"]).day
        sel = {"event": "SELECTION", "selection_index": None, "selected_value": "x", "confidence": 0.9, "date_day": day}
        t = h.send(f"celui du {day}", llm=Script(sel=sel))
        assert _opened(t) == [] and "Plusieurs options correspondent" in t.response, t.response
        assert "Boeuf" not in t.response, "seules les options qui correspondent sont proposées"


@pytest.mark.parametrize("role", ROLES)
def test_my_goats_with_two_goat_needs_asks_which_one_and_never_picks(role):
    with _conv(role) as h:
        _list_with_twins(h)
        h.send("mes besoins", llm=UNKNOWN)
        # le modèle « choisit » l'option 1 sur un mot que les DEUX besoins partagent : aucune preuve
        t = h.send("mes chèvres", llm=Script(sel={**SEL_1}, nt=new_task("GET_MY_NEEDS", 0.9, product="chevres")))
        assert _opened(t) == [], (_opened(t), t.response)
        assert "2 besoins" in t.response and "Boeuf" not in t.response, t.response
        # puis la précision naturelle : la date de démarrage
        sel = {"event": "SELECTION", "selection_index": None, "selected_value": "celui du 5", "confidence": 0.9, "date_day": 5}
        pick = h.send("celui commencé le 5", llm=Script(sel=sel))
        assert _opened(pick) == ["N-CHEVRE-B"], (_opened(pick), pick.response)


@pytest.mark.parametrize("role", ROLES)
def test_pause_my_goats_with_two_targets_is_a_targeted_clarification_never_a_mutation(role):
    with _conv(role) as h:
        _list_with_twins(h)
        t = h.send("pause mes chèvres", llm=Script(nt=new_task("UPDATE_RECURRING_NEED", 0.9, product="chevres", update_action="PAUSE")))
        assert "update_recurring_need" not in t.mcp_tools(), t.mcp_tools()
        assert "2 besoins récurrents de Chevre" in t.response and "Lequel" in t.response, t.response
        assert t.turn_value("relation_to_context") == "AMBIGUOUS"
        sel = {"event": "SELECTION", "selection_index": None, "selected_value": "celui du 5", "confidence": 0.9, "date_day": 5}
        pick = h.send("celle commencée le 5", llm=Script(sel=sel))
        assert _opened(pick) == ["N-CHEVRE-B"], (_opened(pick), pick.response)
        assert "update_recurring_need" not in pick.mcp_tools(), "choisir le besoin n'exécute pas la pause"


@pytest.mark.parametrize("role", ROLES)
def test_a_quantity_is_never_a_menu_index(role):
    """Liste de 3 besoins ; « mets-en 3 » : le 3 est une quantité. Un modèle imparfait qui répond « option 3 » n'ouvre rien."""
    with _conv(role) as h:
        _list_with_twins(h)
        h.send("mes besoins", llm=UNKNOWN)
        sel3 = {"event": "SELECTION", "selection_index": 3, "selected_value": None, "confidence": 0.95}
        t = h.send("mets-en 3", llm=Script(sel=sel3, nt=new_task("UPDATE_RECURRING_NEED", 0.9, quantity=3.0)))
        assert _opened(t) == [], "le « 3 » n'a pas ouvert le 3e besoin"
        assert "update_recurring_need" not in t.mcp_tools(), "plusieurs cibles possibles : jamais d'héritage silencieux"


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("answer,expected", [("1", "N-CHEVRE-A"), ("2", "N-CHEVRE-B"), ("3", "N-BOEUF"), ("02", "N-CHEVRE-B")])
def test_closed_numbers_still_resolve_without_llm(role, answer, expected):
    with _conv(role) as h:
        _list_with_twins(h)
        h.send("mes besoins", llm=UNKNOWN)
        t = h.send(answer, llm=UNKNOWN)
        assert _opened(t) == [expected] and t.llm_calls == 0, (_opened(t), t.llm_calls)


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("word,index,expected", [("le deuxième", 2, "N-CHEVRE-B"), ("le premier", 1, "N-CHEVRE-A"), ("le troisième", 3, "N-BOEUF")])
def test_ordinals_resolve_a_list_choice(role, word, index, expected):
    with _conv(role) as h:
        _list_with_twins(h)
        h.send("mes besoins", llm=UNKNOWN)
        sel = {"event": "SELECTION", "selection_index": index, "selected_value": None, "confidence": 0.9}
        t = h.send(word, llm=Script(sel=sel))
        assert _opened(t) == [expected], (_opened(t), t.response)


@pytest.mark.parametrize("role", ROLES)
def test_and_for_the_onions_opens_the_named_need_when_there_is_one(role):
    with _conv(role) as h:
        items = _list_with_twins(h)
        items.append({**items[2], "recurring_need_id": "N-OIGNON", "product": "Oignon", "next_occurrence_id": "OCC-O"})
        t = h.send("et pour les oignons ?", llm=Script(nt=new_task("GET_MY_NEEDS", 0.9, product="oignons")))
        assert _opened(t) == ["N-OIGNON"] and "Oignon" in t.response, (_opened(t), t.response)


@pytest.mark.parametrize("role", ROLES)
def test_and_for_the_onions_from_another_context(role):
    with _conv(role) as h:
        items = _list_with_twins(h)
        items.append({**items[2], "recurring_need_id": "N-OIGNON", "product": "Oignon", "next_occurrence_id": "OCC-O"})
        h.send("mes besoins", llm=UNKNOWN)
        h.send("3", llm=UNKNOWN)  # écran Bœuf
        t = h.send("et pour les oignons ?", llm=Script(sel=INTERRUPTION, nt=new_task("GET_MY_NEEDS", 0.9, product="oignons")))
        assert _opened(t) == ["N-OIGNON"], (_opened(t), t.response)


@pytest.mark.parametrize("role", ROLES)
def test_and_for_the_tomatoes_when_there_are_none_says_so(role):
    with _conv(role) as h:
        _list_with_twins(h)
        t = h.send("et pour les tomates ?", llm=Script(nt=new_task("GET_MY_NEEDS", 0.9, product="tomates")))
        assert _opened(t) == [] and "Je ne trouve pas de besoin récurrent « tomates »" in t.response, t.response


# ── 6. LISTE / DÉTAIL : jamais deux vérités en silence ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("role", ROLES)
def test_list_and_detail_of_the_same_occurrence_agree_without_a_warning(role):
    with _conv(role) as h:
        _list_with_twins(h)
        h.send("mes besoins", llm=UNKNOWN)
        t = h.send("3", llm=UNKNOWN)
        assert "La liste annonçait" not in t.response


@pytest.mark.parametrize("role", ROLES)
def test_a_detail_that_contradicts_the_list_says_so(role, caplog):
    with _conv(role) as h:
        items = _list_with_twins(h)
        h.send("mes besoins", llm=UNKNOWN)
        inner = h.runtime.responses["get_recurring_need_detail"]
        h.runtime.responses["get_recurring_need_detail"] = lambda **kw: {**inner(**kw), "occurrence_date": "2027-01-04", "occurrence_id": "OCC-OTHER"}
        with caplog.at_level(logging.WARNING):
            t = h.send("3", llm=UNKNOWN)
        assert "La liste annonçait la livraison du" in t.response and "4 janvier" in t.response, t.response
        assert any("RECURRING_LIST_DETAIL_MISMATCH" in r.getMessage() for r in caplog.records)
        assert items[2]["recurring_need_id"] == "N-BOEUF"


# ── 7. RÉCUPÉRATION APRÈS ÉCHEC PRODUCTEUR ───────────────────────────────────────────────────────────────────────────
def _with_recovery(h, need_id: str = "N-BOEUF") -> None:
    h.runtime.responses["get_last_interactive_outbound"] = {
        "status": "success", "interactive": None,
        "recovery": {"sent_at": 4102444800.0, "recurring_need_ids": [need_id], "occurrence_ids": ["OCC-C"], "dates": ["2026-10-27"]}}


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("text", ["trouve-moi quelqu'un d'autre", "cherche ailleurs", "réessaie", "il y en a un autre ?", "je veux quelqu'un d'autre"])
def test_find_someone_else_after_a_producer_failure_targets_the_failed_delivery(role, text, caplog):
    with _conv(role) as h:
        _list_with_twins(h)
        _with_recovery(h)
        h.runtime.responses["refresh_recurring_need_matching"] = {"status": "success", "outcome": "MATCHED", "changed": True, "occurrence_version": 2}
        with caplog.at_level(logging.INFO):
            t = h.send(text, llm=Script(nt=new_task("REFRESH_RECURRING_MATCHING", 0.93)))
        calls = _calls(t, "refresh_recurring_need_matching")
        assert t.intent == "REFRESH_RECURRING_MATCHING" and [c["recurring_need_id"] for c in calls] == ["N-BOEUF"], (calls, t.response)
        assert t.turn_value("relation_to_context") == "ANSWER"
        assert any("target_resolution=recovery" in x for x in _arbitration(caplog)), _arbitration(caplog)


@pytest.mark.parametrize("role", ROLES)
def test_too_late_is_the_domains_answer_not_the_languages(role):
    with _conv(role) as h:
        _list_with_twins(h)
        _with_recovery(h)
        h.runtime.responses["refresh_recurring_need_matching"] = {
            "status": "success", "outcome": "NOT_MATCHABLE", "occurrence_status": "UNFULFILLED"}
        t = h.send("trouve-moi quelqu'un d'autre", llm=Script(nt=new_task("REFRESH_RECURRING_MATCHING", 0.93)))
        assert "ne peut plus être relancée" in t.response, t.response


@pytest.mark.parametrize("role", ROLES)
def test_a_recovery_context_that_is_not_the_buyers_need_is_never_a_target(role):
    """Identifiant forgé / d'un autre acheteur : absent de la liste de l'acheteur courant -> jamais une cible."""
    with _conv(role) as h:
        _list_with_twins(h)
        _with_recovery(h, need_id="N-OF-SOMEONE-ELSE")
        t = h.send("trouve-moi quelqu'un d'autre", llm=Script(nt=new_task("REFRESH_RECURRING_MATCHING", 0.93)))
        assert "refresh_recurring_need_matching" not in [n for n, kw in t.mcp_calls if kw.get("recurring_need_id") == "N-OF-SOMEONE-ELSE"]


@pytest.mark.parametrize("role", ROLES)
def test_without_any_context_find_someone_else_with_several_needs_asks_which(role):
    with _conv(role) as h:
        _list_with_twins(h)
        t = h.send("trouve-moi quelqu'un d'autre", llm=Script(nt=new_task("REFRESH_RECURRING_MATCHING", 0.93)))
        assert "refresh_recurring_need_matching" not in t.mcp_tools(), t.response
        assert "Vous avez plusieurs besoins actifs" in t.response, t.response


@pytest.mark.parametrize("role", ROLES)
def test_a_detail_screen_is_enough_context_for_search_again(role):
    with _conv(role) as h:
        _to_detail(h)
        t = h.send("cherche encore pour celui-là", llm=Script(sel=INTERRUPTION, nt=new_task("REFRESH_RECURRING_MATCHING", 0.93)))
        assert [c["recurring_need_id"] for c in _calls(t, "refresh_recurring_need_matching")] == [h.server.created[0]["recurring_need_id"]]


# ── 8. SÉMANTIQUE ≠ AUTORISATION ≠ VALIDATION ≠ CIBLE ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("role", ROLES)
def test_a_hallucinated_target_is_rejected_by_the_resolver(role):
    """Le modèle « connaît » un besoin que l'acheteur ne possède pas (identifiant halluciné dans le contexte) : jamais une cible."""
    with _conv(role) as h:
        _to_detail(h)
        t = h.send("mets-en 3", llm=Script(nt=new_task("UPDATE_RECURRING_NEED", 0.9, quantity=3.0)))
        needs = [c["recurring_need_id"] for c in _calls(t, "update_recurring_need")]
        assert needs == [h.server.created[0]["recurring_need_id"]], "la cible vient du contexte revalidé, jamais du modèle"


@pytest.mark.parametrize("role", ROLES)
def test_an_invalid_quantity_is_the_validators_problem_not_a_masked_intent(role):
    with _conv(role) as h:
        _to_detail(h)
        t = h.send("mets -5", llm=Script(nt=new_task("UPDATE_RECURRING_NEED", 0.9, quantity=-5.0)))
        assert "update_recurring_need" not in t.mcp_tools(), t.response
        assert t.intent == "UPDATE_RECURRING_NEED", "l'intention reste comprise ; c'est la validation qui refuse"


# ── 9. CONTEXTE = PRIOR, PAS PRISON : matrice inter-domaines ─────────────────────────────────────────────────────────
BUY = new_task("BUYER_REQUEST", 0.95, product="tomates", quantity=20.0, unit="KG")
SELL = new_task("SALES_PUBLISH_PRODUCT", 0.95, product="oignons", quantity=100.0, unit="KG")
CREATE_CHEVRE = new_task("CREATE_RECURRING_NEED", 0.95, product="chevre", quantity=2.0, unit="UNITE", recurrence_type="WEEKLY")


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("text,payload,intent", [
    ("je veux acheter 20 kg de tomate", BUY, "BUYER_REQUEST"),
    ("je veux vendre 100 kg d'oignon", SELL, "SALES_PUBLISH_PRODUCT"),
    ("je veux aussi 2 chèvres par semaine", CREATE_CHEVRE, "CREATE_RECURRING_NEED"),
    ("montre mes commandes", new_task("BUYER_LIST_ORDERS", 0.9), "BUYER_LIST_ORDERS"),
])
def test_recurring_screen_never_imprisons_the_user(role, text, payload, intent):
    with _conv(role) as h:
        _to_detail(h, allocations=ALLOC)
        t = h.send(text, llm=Script(sel=INTERRUPTION, nt=payload))
        assert t.intent == intent, (t.intent, t.response)
        assert "accept_match_proposal" not in t.mcp_tools() and "refresh_recurring_need_matching" not in t.mcp_tools()


@pytest.mark.parametrize("role", ROLES)
def test_buyer_context_to_recurring_creation(role):
    with _conv(role) as h:
        h.send("je veux acheter 20 kg de tomate", llm=Script(nt=BUY))
        t = h.send("en fait je veux 2 chèvres chaque semaine", llm=Script(sel=INTERRUPTION, nt=CREATE_CHEVRE))
        assert t.intent in {"CREATE_RECURRING_NEED"}, (t.intent, t.response)


def test_producer_context_to_buyer_purchase_semantic_intent_is_not_distorted_by_capability():
    """Intention ≠ autorisation : un compte producteur seul qui dit « je veux acheter » est compris comme BUYER_REQUEST ; c'est la
    capacité/validation qui décide ensuite de ce qui est permis (jamais un classifieur qui déforme l'intention)."""
    with _conv("PRODUCER") as h:
        h.send("je veux vendre 100 kg d'oignon", llm=Script(nt=SELL))
        t = h.send("je veux acheter 20 kg de tomate", llm=Script(sel=INTERRUPTION, nt=BUY))
        assert t.turn_value("detected_intent") in {"BUYER_REQUEST", None} or t.intent == "BUYER_REQUEST", t.intent


@pytest.mark.parametrize("role", ROLES)
def test_interrupting_a_creation_draft_with_orders_does_not_create_anything(role):
    with _conv(role) as h:
        h.send("j'ai besoin de 3 chevres chaque semaine", llm=Script(nt=new_task(
            "CREATE_RECURRING_NEED", product="chevre", quantity=3.0, unit="UNITE", recurrence_type="WEEKLY")))
        t = h.send("montre mes commandes", llm=Script(nt=new_task("BUYER_LIST_ORDERS", 0.9)))
        assert t.intent == "BUYER_LIST_ORDERS" and h.server.created == [], "aucune écriture sans confirmation"


# ── 10. CONVERSATION LONGUE : la cible reste correcte ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("role", ROLES)
def test_a_ten_turn_conversation_keeps_the_same_draft_then_the_right_target(role):
    with _conv(role) as h:
        h.send("je veux 3 chèvres chaque semaine", llm=Script(nt=new_task(
            "CREATE_RECURRING_NEED", product="chevre", quantity=3.0, unit="UNITE", recurrence_type="WEEKLY")))
        h.send("finalement 5", llm=Script(nt=new_task("CREATE_RECURRING_NEED", 0.9, quantity=5.0)))
        h.send("chaque mois plutôt", llm=Script(nt=new_task("CREATE_RECURRING_NEED", 0.9, recurrence_type="MONTHLY")))
        done = h.send("oui", llm=UNKNOWN)
        assert "C'est noté" in done.response and len(h.server.created) == 1
        assert h.server.created[0]["quantity"] == 5 and h.server.created[0]["recurrence_type"] == "MONTHLY"
        _list_with_twins(h)
        h.send("montre mes besoins", llm=Script(nt=new_task("GET_MY_NEEDS", 0.9)))
        sel = {"event": "SELECTION", "selection_index": 3, "selected_value": None, "confidence": 0.9}
        opened = h.send("le troisième", llm=Script(sel=sel))
        assert _opened(opened) == ["N-BOEUF"]
        t = h.send("pause-le", llm=Script(sel=INTERRUPTION, nt=new_task("UPDATE_RECURRING_NEED", 0.9, update_action="PAUSE")))
        assert "update_recurring_need" not in t.mcp_tools(), "la pause attend sa confirmation fermée"
        assert "Boeuf" in t.response, "la cible est le besoin affiché (Bœuf), pas un ancien besoin"
        ok = h.send("1", llm=UNKNOWN)
        assert [(c["recurring_need_id"], c["action"]) for c in _calls(ok, "update_recurring_need")] == [("N-BOEUF", "PAUSE")]


@pytest.mark.parametrize("role", ROLES)
def test_an_interrupted_conversation_does_not_contaminate_the_old_target(role):
    with _conv(role) as h:
        _list_with_twins(h)
        h.send("mes besoins", llm=UNKNOWN)
        h.send("3", llm=UNKNOWN)  # écran Bœuf
        h.send("montre mes commandes", llm=Script(nt=new_task("BUYER_LIST_ORDERS", 0.9)))
        h.runtime.responses["get_buyer_orders_dashboard"] = {"status": "success", "formatted_menu": "📦 SUIVI DE VOS COMMANDES", "mapping": {}}
        h.send("mes besoins", llm=UNKNOWN)
        t = h.send("change mes chèvres à 5", llm=Script(nt=new_task("UPDATE_RECURRING_NEED", 0.9, product="chevres", quantity=5.0)))
        assert "update_recurring_need" not in t.mcp_tools(), "deux Chèvre : clarification, jamais l'ancienne cible Bœuf"
        assert "2 besoins récurrents de Chevre" in t.response, t.response


# ── 11. SÉCURITÉ : le langage naturel ne donne aucune autorité ───────────────────────────────────────────────────────
@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("text", ["accepte quand même", "force la commande", "annule celui de l'autre producteur"])
def test_security_phrases_grant_no_authority(role, text):
    with _conv(role) as h:
        _to_detail(h, allocations=ALLOC)
        t = h.send(text, llm=Script(sel={**SEL_1, "confidence": 0.99}, nt=NT_UNKNOWN))
        assert not {"accept_match_proposal", "update_recurring_need", "refresh_recurring_need_matching"} & set(t.mcp_tools())


# ── 12. FAIL-SAFE : un message incompréhensible garde le repli générique ─────────────────────────────────────────────
@pytest.mark.parametrize("role", ROLES)
def test_gibberish_keeps_the_generic_fallback_and_changes_nothing(role):
    with _conv(role) as h:
        t = h.send("blabla xyz", llm=Script(nt=NT_UNKNOWN))
        assert t.error is None and t.response
        assert not {"accept_match_proposal", "update_recurring_need", "refresh_recurring_need_matching"} & set(t.mcp_tools())


# ── 13. MÉTRIQUES SANS PII ───────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("role", ROLES)
def test_arbitration_log_exposes_paths_and_clarification_without_pii(role, caplog):
    with _conv(role) as h:
        _to_detail(h, allocations=ALLOC)
        with caplog.at_level(logging.INFO):
            h.send("1", llm=UNKNOWN)
    closed = [x for x in _arbitration(caplog) if "closed_menu_answer" in x]
    assert closed and "deterministic_path=true" in closed[-1] and "semantic_path=false" in closed[-1], _arbitration(caplog)
    with _conv(role) as h:
        _to_detail(h)
        caplog.clear()
        with caplog.at_level(logging.INFO):
            h.send("mets-en 3", llm=Script(nt=new_task("UPDATE_RECURRING_NEED", 0.9, quantity=3.0)))
    line = next(x for x in _arbitration(caplog) if "semantic_intent=UPDATE_RECURRING_NEED" in x)
    assert "semantic_path=true" in line and "clarification_reason=none" in line and "mets" not in line.lower(), line
