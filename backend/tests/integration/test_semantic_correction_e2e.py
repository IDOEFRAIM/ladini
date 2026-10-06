"""B24-correction — clôture des limites de #90 : réponse fermée vs texte libre, menus de commande à confirmation, LLM adverse.

    A CLOSED REPLY answers an expectation · A CORRECTION modifies the current task · A NEW TASK supersedes it ·
    A TARGET identifies the exact business object · AMBIGUITY produces a clarification · a free-text LLM classification
    alone never executes a menu action or a business mutation · DOMAIN SERVICES stay the authority.

Rejoué sur le vrai orchestrateur + graphe compilé (seuls LLM, Redis, dispatcher, MCP doublés), rôles ADMIN `can_buy` et BUYER.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

import pytest

from tests.harness import new_task
from tests.integration.test_recurring_intent_integration_e2e import (
    CREATE_CHEVRE,
    FREE_TEXT,
    ROLES,
    _conv,
    _to_detail,
)
from tests.integration.test_recurring_self_service_entrypoint_e2e import UNKNOWN
from tests.integration.test_semantic_context_arbitration_e2e import (
    UPDATE_QTY_3,
    ScriptLLM,
    _arbitration_lines,
    _call_kwargs,
)

pytestmark = pytest.mark.integration

CANCEL = new_task("UPDATE_RECURRING_NEED", 0.9, product="boeufs", update_action="CANCEL")
SKIP = new_task("UPDATE_RECURRING_NEED", 0.9, update_action="SKIP_OCCURRENCE")
PAUSE = new_task("UPDATE_RECURRING_NEED", 0.9, update_action="PAUSE")
OVERRIDE_10 = new_task("UPDATE_RECURRING_NEED", 0.9, update_action="OVERRIDE_OCCURRENCE", quantity=10.0)


class CountingLLM(ScriptLLM):
    """Compte TOUS les appels LLM (un fast-path fermé n'en fait aucun)."""

    def __init__(self, *a: Any, **k: Any) -> None:
        super().__init__(*a, **k)
        self.calls = 0

    def __call__(self, kw: Dict[str, Any]) -> Any:
        self.calls += 1
        return super().__call__(kw)


def _forbidden(t, *, allow=()):
    for tool in ("update_recurring_need", "refresh_recurring_need_matching", "accept_match_proposal"):
        if tool not in allow:
            assert tool not in t.mcp_tools(), (tool, t.mcp_tools(), t.response)


# ── 17/32. DÉTECTEUR DE RÉPONSE FERMÉE ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,closed", [
    ("1", "1"), ("01", "1"), ("1.", "1"), ("1)", "1"), (" 2 ", "2"), ("option 1", "1"), ("Option 2", "2"), ("choix 1", "1"),
    ("numéro 2", "2"),
    ("je veux 1 chèvre", None), ("mets-en 1", None), ("j'en veux 1 de plus", None), ("1 chèvre", None),
    ("mets-en 3", None), ("rechercher maintenant", None), ("", None),
])
def test_closed_reply_detector_requires_the_whole_message_to_be_a_menu_index(text, closed):
    from ladini.graphs.agents.market_coach.interpreter.context_arbitration import (
        closed_menu_index,
        fold,
    )

    assert closed_menu_index(fold(text)) == closed


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("message", ["1", "01", "1.", "option 1", "rechercher maintenant", "actualiser"])
def test_closed_replies_run_the_menu_action_without_any_llm_call(role, message):
    with _conv(role) as h:
        _to_detail(h)
        llm = CountingLLM(None, selection="fake_1")
        t = h.send(message, llm=llm)
        assert "refresh_recurring_need_matching" in t.mcp_tools(), (message, t.response)
        assert llm.calls == 0, "un message fermé est déterministe : zéro appel LLM"


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("message", ["retour", "2", "option 2"])
def test_closed_back_replies_return_to_the_list_with_zero_llm(role, message):
    with _conv(role) as h:
        _to_detail(h)
        llm = CountingLLM(None, selection="fake_1")
        t = h.send(message, llm=llm)
        assert "Mes besoins récurrents" in t.response and llm.calls == 0
        _forbidden(t)


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("message", ["je veux 1 chèvre", "mets-en 1", "j'en veux 1 de plus"])
def test_a_digit_inside_a_sentence_is_not_a_closed_reply(role, message):
    """`mets-en 1` + SELECTION 1 hallucinée : jamais REFRESH — l'interprétation sémantique tranche (ici : rien d'exécuté)."""
    with _conv(role) as h:
        _to_detail(h)
        t = h.send(message, llm=ScriptLLM(None, selection="fake_1"))
        _forbidden(t)
        assert "Boeuf" in t.response, t.response


# ── 31. LLM ADVERSE : sorties incorrectes forcées sur chaque texte libre ───────────────────────────────────────────
class HostileLLM:
    """Répond TOUJOURS par une sélection/confirmation hallucinée, quel que soit le prompt."""

    def __init__(self, event: str, index: int | None = 1) -> None:
        self.event, self.index = event, index

    def __call__(self, kw: Dict[str, Any]) -> Any:
        from tests.integration.test_recurring_intent_integration_e2e import _system

        if "liste de choix" in _system(kw):
            return {"event": self.event, "selection_index": self.index, "selected_value": None}
        return {"disposition": "UNKNOWN", "confidence": 0.0}


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("event,index", [("SELECTION", 1), ("SELECTION", 2), ("CONFIRM", None), ("REJECT", None)])
@pytest.mark.parametrize("message", ["mets-en 3", "finalement 3", "change ça", "annule mes tomates", FREE_TEXT])
def test_a_hostile_llm_never_executes_a_menu_action_or_a_mutation_on_free_text(role, event, index, message):
    alloc = [{"producer_label": "Producteur A", "quantity": 2, "unit_price": 500, "unit": "TETE"}]
    with _conv(role) as h:
        _to_detail(h, allocations=alloc)  # écran avec proposition : accepter/refuser existent
        before = dict(h.server.created[0])
        t = h.send(message, llm=HostileLLM(event, index))
        _forbidden(t)
        assert h.server.created[0] == before and len(h.server.created) == 1


# ── 18/19/20. MENU ANNULER / IGNORER : cancel ≠ skip, jamais décidé par le modèle ───────────────────────────────────
@pytest.mark.parametrize("role", ROLES)
def test_cancel_is_a_targeted_menu_and_nothing_runs_until_a_closed_reply(role, caplog):
    with _conv(role) as h:
        _to_detail(h)
        need_id = h.server.created[0]["recurring_need_id"]
        with caplog.at_level(logging.INFO):
            t = h.send("annule mes boeufs", llm=ScriptLLM(CANCEL))
        assert t.response.splitlines()[0] == "Pour votre besoin de Boeuf, que voulez-vous faire ?", t.response
        assert "1. Ignorer seulement la prochaine livraison (5 octobre) — le besoin continue" in t.response
        assert "2. Arrêter complètement le besoin récurrent" in t.response and "3. Ne rien changer" in t.response
        _forbidden(t)
        assert any("decision_reason=destructive_ambiguity" in x and "relation_to_context=AMBIGUOUS" in x
                   for x in _arbitration_lines(caplog))
        # 1 = skip UNE occurrence (le besoin continue)
        done = h.send("1", llm=CountingLLM(None, selection="fake_1"))
        calls = _call_kwargs(done, "update_recurring_need")
        assert [(c["action"], c["recurring_need_id"], c["occurrence_date"]) for c in calls] == [
            ("OCCURRENCE_SKIP", need_id, "2026-10-05")], done.mcp_tools()
        assert "ignorée" in done.response and "continue" in done.response


@pytest.mark.parametrize("role", ROLES)
def test_cancel_option_two_needs_a_second_explicit_confirmation(role):
    with _conv(role) as h:
        _to_detail(h)
        need_id = h.server.created[0]["recurring_need_id"]
        h.send("annule mes boeufs", llm=ScriptLLM(CANCEL))
        ask = h.send("2", llm=UNKNOWN)
        assert "définitive" in ask.response and "1. Oui, arrêter définitivement" in ask.response
        assert "update_recurring_need" not in ask.mcp_tools(), "choisir « arrêter » ne l'exécute pas encore"
        done = h.send("1", llm=UNKNOWN)
        assert [(c["action"], c["recurring_need_id"]) for c in _call_kwargs(done, "update_recurring_need")] == [("CANCEL", need_id)]
        assert "arrêté" in done.response


@pytest.mark.parametrize("role", ROLES)
def test_cancel_option_three_keeps_everything_and_returns_to_the_need_screen(role):
    with _conv(role) as h:
        _to_detail(h)
        h.send("annule mes boeufs", llm=ScriptLLM(CANCEL))
        t = h.send("3", llm=UNKNOWN)
        assert "update_recurring_need" not in t.mcp_tools() and "Boeuf" in t.response and "Besoin :" in t.response


@pytest.mark.parametrize("role", ROLES)
def test_after_the_cancel_menu_an_ambiguous_llm_gets_a_targeted_question_not_the_generic_fallback(role):
    """Limite #90 (cas résiduel) : après la clarification d'annulation, un message vague gardait le message générique."""
    with _conv(role) as h:
        _to_detail(h)
        h.send("annule mes boeufs", llm=ScriptLLM(CANCEL))
        t = h.send("change ça", llm=ScriptLLM(new_task("UPDATE_RECURRING_NEED", 0.9)))
        _forbidden(t)
        assert "Boeuf" in t.response.lower().capitalize() or "boeuf" in t.response.lower(), t.response
        assert "j'ai juste besoin" not in t.response.lower() and "pas bien saisi" not in t.response.lower(), t.response


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("event,index", [("SELECTION", 1), ("SELECTION", 2), ("CONFIRM", None)])
def test_free_text_never_picks_an_entry_of_the_cancel_menu(role, event, index):
    with _conv(role) as h:
        _to_detail(h)
        h.send("annule mes boeufs", llm=ScriptLLM(CANCEL))
        t = h.send("arrête tout", llm=HostileLLM(event, index))
        assert "update_recurring_need" not in t.mcp_tools(), (t.mcp_tools(), t.response)


@pytest.mark.parametrize("role", ROLES)
def test_an_expired_confirmation_menu_executes_nothing(role, monkeypatch):
    import time as _time

    with _conv(role) as h:
        _to_detail(h)
        h.send("annule mes boeufs", llm=ScriptLLM(CANCEL))
        real = _time.time
        monkeypatch.setattr(_time, "time", lambda: real() + 3600)
        t = h.send("1", llm=UNKNOWN)
        assert "update_recurring_need" not in t.mcp_tools(), t.mcp_tools()


# ── 20/21. BESOIN vs OCCURRENCE : trois portées distinctes ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("role", ROLES)
def test_need_and_occurrence_scopes_are_never_confused(role):
    with _conv(role) as h:
        _to_detail(h)
        need_id = h.server.created[0]["recurring_need_id"]
        # « je veux désormais 5 chaque semaine » -> UPDATE DU BESOIN (permanent)
        t = h.send("je veux désormais 5 chaque semaine", llm=ScriptLLM(new_task(
            "UPDATE_RECURRING_NEED", 0.93, quantity=5.0, recurrence_type="WEEKLY")))
        assert [c["action"] for c in _call_kwargs(t, "update_recurring_need")] == ["PERMANENT_QUANTITY", "PERMANENT_FREQUENCY"]
        # « pas cette semaine » -> SKIP D'UNE OCCURRENCE (confirmation, puis OCCURRENCE_SKIP daté — jamais le besoin)
        s = h.send("pas cette semaine", llm=ScriptLLM(SKIP))
        assert "Ignorer seulement la livraison" in s.response and "update_recurring_need" not in s.mcp_tools()
        s2 = h.send("1", llm=UNKNOWN)
        assert [(c["action"], c["occurrence_date"], c["recurring_need_id"]) for c in _call_kwargs(s2, "update_recurring_need")] == [
            ("OCCURRENCE_SKIP", "2026-10-05", need_id)]
        # « pour cette fois mets 10 kg » -> OVERRIDE D'UNE OCCURRENCE (quantité exacte, jamais PERMANENT_QUANTITY)
        h.send("mes besoins", llm=UNKNOWN)
        h.send("1", llm=UNKNOWN)
        o = h.send("pour cette fois mets 10", llm=ScriptLLM(OVERRIDE_10))
        assert "Changer seulement la livraison" in o.response and "à 10" in o.response and "update_recurring_need" not in o.mcp_tools()
        o2 = h.send("oui", llm=UNKNOWN)
        calls = _call_kwargs(o2, "update_recurring_need")
        assert [(c["action"], c["quantity"], c["occurrence_date"]) for c in calls] == [("OCCURRENCE_OVERRIDE", 10.0, "2026-10-05")]
        # « arrête complètement les tomates » -> CANCEL DU BESOIN, jamais sans double confirmation
        c = h.send("arrête complètement les boeufs", llm=ScriptLLM(CANCEL))
        assert "update_recurring_need" not in c.mcp_tools()


@pytest.mark.parametrize("role", ROLES)
def test_pause_and_resume_are_confirmed_snapshots(role):
    with _conv(role) as h:
        _to_detail(h)
        need_id = h.server.created[0]["recurring_need_id"]
        p = h.send("suspends mes boeufs", llm=ScriptLLM(PAUSE))
        assert "Suspendre votre besoin de Boeuf" in p.response and "update_recurring_need" not in p.mcp_tools()
        done = h.send("confirmer", llm=UNKNOWN)  # alias fermé
        assert [(c["action"], c["recurring_need_id"]) for c in _call_kwargs(done, "update_recurring_need")] == [("PAUSE", need_id)]
        no = h.send("reprends mes boeufs", llm=ScriptLLM(new_task("UPDATE_RECURRING_NEED", 0.9, update_action="RESUME")))
        assert "n'est pas suspendu" in no.response and "update_recurring_need" not in no.mcp_tools()


@pytest.mark.parametrize("role", ROLES)
def test_override_without_a_quantity_asks_for_it_and_runs_nothing(role):
    with _conv(role) as h:
        _to_detail(h)
        t = h.send("pour cette fois change", llm=ScriptLLM(new_task("UPDATE_RECURRING_NEED", 0.9, update_action="OVERRIDE_OCCURRENCE")))
        assert "Quelle quantité" in t.response and "update_recurring_need" not in t.mcp_tools()


@pytest.mark.parametrize("role", ROLES)
def test_a_service_refusal_on_a_confirmed_command_is_reported_and_nothing_else_runs(role):
    with _conv(role) as h:
        _to_detail(h)
        h.send("pas cette semaine", llm=ScriptLLM(SKIP))

        def _refuse(**_: Any) -> Dict[str, Any]:
            return {"status": "error", "message": "Cette date n'est plus modifiable"}

        h.runtime.responses["update_recurring_need"] = _refuse
        t = h.send("1", llm=UNKNOWN)
        assert "ignorée" not in t.response and "Rien n'a été modifié" in t.response, t.response


# ── 15/9. CORRECTION vs NOUVELLE TÂCHE : inchangé ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("role", ROLES)
def test_correction_still_updates_the_same_need_and_a_new_task_still_creates(role):
    with _conv(role) as h:
        _to_detail(h)
        need_id = h.server.created[0]["recurring_need_id"]
        t = h.send("finalement 3", llm=ScriptLLM(UPDATE_QTY_3))
        assert [c["recurring_need_id"] for c in _call_kwargs(t, "update_recurring_need")] == [need_id]
        n = h.send(FREE_TEXT, llm=ScriptLLM(CREATE_CHEVRE))
        assert n.intent == "CREATE_RECURRING_NEED" and "update_recurring_need" not in n.mcp_tools()


# ── 37. OBSERVABILITÉ : les raisons de décision nommées par le contrat ───────────────────────────────────────────────
def test_arbitration_log_reasons_cover_the_contract_vocabulary(caplog):
    from tests.integration.test_semantic_context_arbitration_e2e import _second_need

    with _conv("ADMIN") as h, caplog.at_level(logging.INFO):
        _to_detail(h)
        h.send("1", llm=UNKNOWN)                                            # closed_menu_answer
        h.send("mets-en 3", llm=ScriptLLM(None, selection="fake_1"))        # free_text_selection_rejected
        h.send("mets-en 3", llm=ScriptLLM(UPDATE_QTY_3))                    # contextual_correction
        h.send("pas cette semaine", llm=ScriptLLM(SKIP))                    # confirmation_required
        h.send("annule mes boeufs", llm=ScriptLLM(CANCEL))                  # destructive_ambiguity
        h.send(FREE_TEXT, llm=ScriptLLM(CREATE_CHEVRE))                     # explicit_new_task
        h.send("oui")
        _second_need(h, product="boeuf", qty=4.0, unit="TETE")
        h.send("mes besoins", llm=UNKNOWN)
        h.send("1", llm=UNKNOWN)
        h.send("mets-en 5", llm=ScriptLLM(new_task("UPDATE_RECURRING_NEED", 0.9, product="boeufs", quantity=5.0)))  # target_not_unique
    reasons = {x.split("decision_reason=")[1].split()[0] for x in _arbitration_lines(caplog)}
    for expected in ("closed_menu_answer", "contextual_correction", "free_text_selection_rejected", "destructive_ambiguity",
                     "confirmation_required", "explicit_new_task", "target_not_unique"):
        assert expected in reasons, (expected, sorted(reasons))
    assert all("mets" not in x.lower() and "boeuf" not in x.lower() for x in _arbitration_lines(caplog)), "aucun texte utilisateur"
