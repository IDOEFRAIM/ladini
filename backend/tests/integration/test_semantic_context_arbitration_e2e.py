"""B24 — arbitrage sémantique contexte/attente/cible (suite de la B23).

    EXPECTATION guides interpretation · SEMANTIC INTENT identifies the task · RELATION TO CONTEXT explains how the message
    relates to the current task · CONTEXT RESOLUTION identifies the target · VALIDATION protects invariants · DOMAIN SERVICES
    execute.

Rejoué sur le vrai orchestrateur + graphe compilé (seuls LLM, Redis, dispatcher, MCP doublés), avec le rôle RÉEL de
l'acteur (ADMIN `can_buy` -> graphe PRODUCER ; BUYER -> graphe BUYER). Deux familles de LLM :

* LLM CAPABLE   : juge la relation au menu (INTERRUPTION) puis NEW_TASK renvoie la bonne intention/les bonnes entités ;
* LLM IMPARFAIT : « SELECTION 1 » pour n'importe quel texte libre (hallucination de sélection).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

import pytest

from tests.harness import new_task
from tests.integration.test_recurring_intent_integration_e2e import (
    CREATE_CHEVRE,
    FREE_TEXT,
    ROLES,
    _conv,
    _detail,
    _system,
    _to_detail,
    _user,
)
from tests.integration.test_recurring_self_service_entrypoint_e2e import UNKNOWN

pytestmark = pytest.mark.integration

UPDATE_QTY_3 = new_task("UPDATE_RECURRING_NEED", 0.93, quantity=3.0)
UPDATE_MONTHLY = new_task("UPDATE_RECURRING_NEED", 0.9, recurrence_type="MONTHLY")
AMBIGUOUS_CHANGE = {"disposition": "AMBIGUOUS", "confidence": 0.5, "entities": {},
                    "candidate_goals": ["UPDATE_RECURRING_NEED", "CREATE_RECURRING_NEED"]}


class ScriptLLM:
    """SELECTION : `selection` = « interrupt » (capable) | « fake_1 » (hallucine SELECTION 1) | « unknown ».
    NEW_TASK : `payload`."""

    def __init__(self, payload: Any = None, *, selection: str = "interrupt") -> None:
        self.payload, self.selection = payload, selection
        self.selection_calls = 0

    def __call__(self, kw: Dict[str, Any]) -> Any:
        sysm = _system(kw)
        if "liste de choix" in sysm:
            self.selection_calls += 1
            if self.selection == "fake_1":
                return {"event": "SELECTION", "selection_index": 1, "selected_value": None}
            if self.selection == "unknown":
                return {"event": "UNKNOWN", "selection_index": None, "selected_value": None}
            return {"event": "INTERRUPTION", "selection_index": None, "selected_value": None}
        if "CATALOGUE OFFICIEL" in sysm:
            return self.payload or {"disposition": "UNKNOWN", "confidence": 0.0}
        return {"disposition": "UNKNOWN", "confidence": 0.0}


def _call_kwargs(turn, tool: str) -> List[Dict[str, Any]]:
    return [kw for name, kw in turn.mcp_calls if name == tool]


def _arbitration_lines(caplog) -> List[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("INTENT_ARBITRATION")]


def _second_need(h, product="chevre", qty=3.0, unit="UNITE", freq="WEEKLY"):
    h.send(f"{product} {qty:g} {unit.lower()} chaque semaine", llm=ScriptLLM(new_task(
        "CREATE_RECURRING_NEED", 0.95, product=product, quantity=qty, unit=unit, recurrence_type=freq)))
    h.send("oui")
    assert len(h.server.created) >= 2, "le second besoin doit réellement exister"


def _detail_of(h, index: str):
    """Ouvre l'écran du besoin n° `index` de la liste (detail doublé pour le produit visé)."""
    h.send("mes besoins", llm=UNKNOWN)
    return h.send(index, llm=UNKNOWN)


# ── 6/31. CORRECTION PENDANT LA CRÉATION : même goal, même draft, un seul besoin ────────────────────────────────────
@pytest.mark.parametrize("role", ROLES)
def test_correction_during_creation_is_the_same_draft_and_creates_exactly_one_need(role, caplog):
    with _conv(role) as h:
        t1 = h.send("j'ai besoin de 3 chevres chaque semaine",
                    llm=new_task("CREATE_RECURRING_NEED", product="chevre", quantity=3.0, unit="UNITE", recurrence_type="WEEKLY"))
        draft_id = t1.draft()["draft_id"]
        with caplog.at_level(logging.INFO):
            t2 = h.send("finalement 5", llm=ScriptLLM(new_task("CREATE_RECURRING_NEED", 0.9, quantity=5.0)))
            t3 = h.send("plutôt chaque mois", llm=ScriptLLM(new_task("CREATE_RECURRING_NEED", 0.9, recurrence_type="MONTHLY")))
        assert t2.intent == "CREATE_RECURRING_NEED" and t2.draft()["draft_id"] == draft_id and t2.draft()["quantity"] == 5
        assert t3.draft()["draft_id"] == draft_id, "« plutôt chaque mois » corrige le MÊME draft (aucune nouvelle tâche)"
        assert t3.draft()["recurrence_type"] == "MONTHLY" and t3.draft()["quantity"] == 5
        assert t2.turn_value("relation_to_context") == "CORRECTION" and t3.turn_value("relation_to_context") == "CORRECTION"
        assert sum("relation_to_context=CORRECTION" in line and "target_type=RECURRING_NEED_DRAFT" in line
                   for line in _arbitration_lines(caplog)) == 2
        t4 = h.send("oui")
        assert "C'est noté" in t4.response
        assert len(h.server.created) == 1
        assert h.server.created[0]["quantity"] == 5 and h.server.created[0]["recurrence_type"] == "MONTHLY"
        assert h.send("oui").response is not None and len(h.server.created) == 1, "confirmation répétée : toujours un seul besoin"


def test_a_complete_task_with_a_product_during_a_draft_is_still_a_new_task():
    """La fréquence SEULE corrige ; produit + fréquence = tâche isolée (mandat §9 de la phase 2.5, inchangé)."""
    from ladini.graphs.agents.market_coach.core.turn_policy import TurnAction, decide_active_draft_reply as decide

    kw = {"interpreted_event": "NEW_TASK", "detected_intent": "CREATE_RECURRING_NEED", "current_goal": "CREATE_RECURRING_NEED"}
    assert decide(said_entities={"recurrence_type": "MONTHLY"}, **kw) == TurnAction.CORRECT
    assert decide(said_entities={"quantity": 5.0}, **kw) == TurnAction.CORRECT
    assert decide(said_entities={"product": "poulet", "quantity": 30.0, "recurrence_type": "WEEKLY"}, **kw) == TurnAction.NEW_TASK
    assert decide(said_entities={}, **kw) == TurnAction.NEW_TASK


# ── 7/9/10/14. CORRECTION DEPUIS LE DÉTAIL : intention UPDATE + relation CORRECTION + cible exacte ──────────────────
@pytest.mark.parametrize("role", ROLES)
def test_mets_en_3_from_the_detail_updates_the_displayed_need_exactly(role, caplog):
    with _conv(role) as h:
        _to_detail(h)
        need_id = h.server.created[0]["recurring_need_id"]
        with caplog.at_level(logging.INFO):
            t = h.send("mets-en 3", llm=ScriptLLM(UPDATE_QTY_3))
        assert t.intent == "UPDATE_RECURRING_NEED" and t.turn_value("relation_to_context") == "CORRECTION"
        calls = _call_kwargs(t, "update_recurring_need")
        assert len(calls) == 1 and calls[0]["recurring_need_id"] == need_id
        assert calls[0]["action"] == "PERMANENT_QUANTITY" and calls[0]["quantity"] == 3.0
        assert "quantité 2 → 3" in t.response, t.response
        assert "refresh_recurring_need_matching" not in t.mcp_tools() and "accept_match_proposal" not in t.mcp_tools()
        line = next(x for x in _arbitration_lines(caplog) if "semantic_intent=UPDATE_RECURRING_NEED" in x)
        for field in ("current_goal=", "expected_action=", "relation_to_context=CORRECTION", "target_type=RECURRING_NEED",
                      "target_resolution=context", "selected_route=", "decision_reason=contextual_correction"):
            assert field in line, line
        assert need_id not in line and "mets" not in line.lower()


@pytest.mark.parametrize("role", ROLES)
def test_with_two_needs_the_displayed_one_is_the_target_not_a_question(role):
    with _conv(role) as h:
        _to_detail(h)
        _second_need(h)
        boeuf_id = h.server.created[0]["recurring_need_id"]
        h.runtime.responses["get_recurring_need_detail"] = _detail(need_id=boeuf_id)
        _detail_of(h, "1")  # Boeuf (première ligne de la liste)
        t = h.send("mets-en 3", llm=ScriptLLM(UPDATE_QTY_3))
        calls = _call_kwargs(t, "update_recurring_need")
        assert len(calls) == 1 and calls[0]["recurring_need_id"] == boeuf_id, (calls, t.response)


@pytest.mark.parametrize("role", ROLES)
def test_frequency_correction_from_the_detail(role):
    with _conv(role) as h:
        _to_detail(h)
        t = h.send("plutôt chaque mois", llm=ScriptLLM(UPDATE_MONTHLY))
        calls = _call_kwargs(t, "update_recurring_need")
        assert len(calls) == 1 and calls[0]["action"] == "PERMANENT_FREQUENCY" and calls[0]["recurrence_type"] == "MONTHLY"
        assert calls[0]["recurring_need_id"] == h.server.created[0]["recurring_need_id"]
        assert t.turn_value("relation_to_context") == "CORRECTION"
        assert "chaque semaine → chaque mois" in t.response


@pytest.mark.parametrize("role", ROLES)
def test_stale_quantity_is_never_reapplied_to_a_frequency_change(role):
    """`transaction_payload` accumule les tours : seule la parole du tour courant modifie le besoin."""
    with _conv(role) as h:
        _to_detail(h)
        h.send("mets-en 3", llm=ScriptLLM(UPDATE_QTY_3))
        _detail_of(h, "1")
        t = h.send("plutôt chaque mois", llm=ScriptLLM(UPDATE_MONTHLY))
        calls = _call_kwargs(t, "update_recurring_need")
        assert [c["action"] for c in calls] == ["PERMANENT_FREQUENCY"], calls


# ── 17/28. LLM IMPARFAIT : « SELECTION 1 » sur du langage libre ne devient plus une action ──────────────────────────
@pytest.mark.parametrize("role", ROLES)
def test_imperfect_llm_selection_1_on_free_text_never_runs_the_menu_action(role, caplog):
    with _conv(role) as h:
        _to_detail(h)
        with caplog.at_level(logging.INFO):
            t = h.send("mets-en 3", llm=ScriptLLM(None, selection="fake_1"))
        assert "refresh_recurring_need_matching" not in t.mcp_tools(), (t.mcp_tools(), t.response)
        assert "update_recurring_need" not in t.mcp_tools()
        assert "Je n'ai pas bien compris" in t.response and "Boeuf" in t.response, t.response
        assert any("decision_reason=free_text_selection_rejected" in x for x in _arbitration_lines(caplog))


@pytest.mark.parametrize("role", ROLES)
def test_imperfect_selection_followed_by_a_capable_semantic_pass_still_resolves_the_intent(role):
    """Le micro-prompt SELECTION se trompe, l'interprétation sémantique (NEW_TASK) corrige : la cible est le besoin affiché."""
    with _conv(role) as h:
        _to_detail(h)
        t = h.send("mets-en 3", llm=ScriptLLM(UPDATE_QTY_3, selection="fake_1"))
        assert t.intent == "UPDATE_RECURRING_NEED" and "refresh_recurring_need_matching" not in t.mcp_tools()
        assert [c["quantity"] for c in _call_kwargs(t, "update_recurring_need")] == [3.0]


@pytest.mark.parametrize("role", ROLES)
def test_free_text_selection_never_mutates_a_proposal(role):
    alloc = [{"producer_label": "Producteur A", "quantity": 2, "unit_price": 500, "unit": "TETE"}]
    with _conv(role) as h:
        _to_detail(h, allocations=alloc)
        t = h.send("mets-en 3", llm=ScriptLLM(None, selection="fake_1"))
        assert "accept_match_proposal" not in t.mcp_tools() and "refresh_recurring_need_matching" not in t.mcp_tools()
        assert "update_recurring_need" not in t.mcp_tools()


# ── 8. PRODUIT EXPLICITE DIFFÉRENT : jamais Bœuf -> Chèvre ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("role", ROLES)
def test_naming_another_product_never_rewrites_the_boeuf_need(role):
    with _conv(role) as h:
        _to_detail(h)
        t = h.send("mets plutôt 5 chèvres", llm=ScriptLLM(new_task("UPDATE_RECURRING_NEED", 0.9, product="chevres", quantity=5.0)))
        assert "update_recurring_need" not in t.mcp_tools(), t.response
        assert "je ne trouve pas de besoin récurrent" in t.response.lower() and "boeuf" in t.response.lower()
        assert h.server.created[0]["quantity"] == 2 and len(h.server.created) == 1


@pytest.mark.parametrize("role", ROLES)
def test_the_same_sentence_read_as_a_new_task_creates_a_chevre_draft_and_leaves_the_boeuf(role):
    with _conv(role) as h:
        _to_detail(h)
        t = h.send("mets plutôt 5 chèvres chaque semaine", llm=ScriptLLM(
            new_task("CREATE_RECURRING_NEED", 0.92, product="chevre", quantity=5.0, unit="UNITE", recurrence_type="WEEKLY")))
        assert t.intent == "CREATE_RECURRING_NEED" and t.turn_value("relation_to_context") == "NEW_TASK"
        assert "update_recurring_need" not in t.mcp_tools() and t.draft()["quantity"] == 5
        h.send("oui")
        assert len(h.server.created) == 2 and h.server.created[0]["quantity"] == 2 and h.server.created[1]["quantity"] == 5


# ── 9/19. COMMANDE CONTEXTUELLE : cible exacte, ou choix explicite — jamais de devinette ─────────────────────────────
REFRESH_BOEUFS = new_task("REFRESH_RECURRING_MATCHING", 0.93, product="boeufs")


@pytest.mark.parametrize("role", ROLES)
def test_cherche_pour_mes_boeufs_refreshes_the_unique_owned_target(role, caplog):
    with _conv(role) as h:
        _to_detail(h)
        need_id = h.server.created[0]["recurring_need_id"]
        with caplog.at_level(logging.INFO):
            t = h.send("cherche pour mes boeufs", llm=ScriptLLM(REFRESH_BOEUFS))
        calls = _call_kwargs(t, "refresh_recurring_need_matching")
        assert t.intent == "REFRESH_RECURRING_MATCHING" and len(calls) == 1 and calls[0]["recurring_need_id"] == need_id
        assert t.turn_value("relation_to_context") == "ANSWER"
        assert "Boeuf" in t.response and "Je passe à" not in t.response, t.response
        assert any("decision_reason=contextual_command" in x for x in _arbitration_lines(caplog))


@pytest.mark.parametrize("role", ROLES)
def test_cherche_pour_mes_boeufs_with_two_boeuf_needs_asks_which_one(role):
    with _conv(role) as h:
        _to_detail(h)
        _second_need(h, product="boeuf", qty=4.0, unit="TETE")
        assert len(h.server.created) == 2
        t = h.send("cherche pour mes boeufs", llm=ScriptLLM(REFRESH_BOEUFS))
        assert "refresh_recurring_need_matching" not in t.mcp_tools(), t.mcp_tools()
        assert "2 besoins récurrents de Boeuf" in t.response and "Lequel" in t.response, t.response
        assert t.turn_value("relation_to_context") == "AMBIGUOUS"
        # le choix passe par le menu FRAIS (identifiant exact) : « 2 » ouvre le second besoin, rien n'est relancé tout seul
        pick = h.send("2", llm=UNKNOWN)
        assert "refresh_recurring_need_matching" not in pick.mcp_tools() and "get_recurring_need_detail" in pick.mcp_tools()


@pytest.mark.parametrize("role", ROLES)
def test_cherche_pour_mes_boeufs_without_such_a_need_is_not_found(role):
    with _conv(role) as h:
        _to_detail(h)
        t = h.send("cherche pour mes tomates", llm=ScriptLLM(new_task("REFRESH_RECURRING_MATCHING", 0.9, product="tomates")))
        assert "refresh_recurring_need_matching" not in t.mcp_tools() and "ne trouve pas" in t.response


# ── 13/21. AMBIGUÏTÉ DESTRUCTIVE ; BESOIN vs OCCURRENCE ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("role", ROLES)
def test_annule_mes_boeufs_is_a_cancel_or_skip_clarification_never_executed(role, caplog):
    with _conv(role) as h:
        _to_detail(h)
        with caplog.at_level(logging.INFO):
            t = h.send("annule mes boeufs", llm=ScriptLLM(new_task("UPDATE_RECURRING_NEED", 0.9, product="boeufs", update_action="CANCEL")))
        assert "update_recurring_need" not in t.mcp_tools(), t.mcp_tools()
        assert "Ignorer seulement la prochaine livraison" in t.response and "Arrêter complètement" in t.response, t.response
        assert t.turn_value("relation_to_context") == "AMBIGUOUS"
        assert any("decision_reason=destructive_ambiguity" in x for x in _arbitration_lines(caplog))


@pytest.mark.parametrize("action,needle", [("SKIP_OCCURRENCE", "Ignorer seulement la livraison"), ("OVERRIDE_OCCURRENCE", "Changer seulement la livraison")])
def test_occurrence_scoped_requests_are_understood_but_never_touch_the_durable_need(action, needle):
    with _conv("ADMIN") as h:
        _to_detail(h)
        t = h.send("pas cette semaine", llm=ScriptLLM(new_task("UPDATE_RECURRING_NEED", 0.9, update_action=action, quantity=5.0)))
        assert "update_recurring_need" not in t.mcp_tools(), t.mcp_tools()
        assert needle in t.response and "1. Oui" in t.response, t.response  # B24 : confirmation fermée, rien d'exécuté


# ── 15. MESSAGE VAGUE ──────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("payload", [new_task("UPDATE_RECURRING_NEED", 0.9), None, AMBIGUOUS_CHANGE])
def test_vague_message_asks_what_to_change_and_changes_nothing(role, payload):
    with _conv(role) as h:
        _to_detail(h)
        t = h.send("change ça", llm=ScriptLLM(payload))
        assert "update_recurring_need" not in t.mcp_tools() and "refresh_recurring_need_matching" not in t.mcp_tools()
        assert "la quantité, la fréquence ou la prochaine livraison" in t.response, t.response
        assert "Boeuf" in t.response
        assert h.server.created[0]["quantity"] == 2


def test_a_low_confidence_vague_update_keeps_the_screen_and_asks():
    with _conv("ADMIN") as h:
        _to_detail(h)
        t = h.send("change ça", llm=ScriptLLM(new_task("UPDATE_RECURRING_NEED", 0.6)))
        assert "update_recurring_need" not in t.mcp_tools()
        assert "la quantité, la fréquence ou la prochaine livraison" in t.response, t.response
        later = h.send("2", llm=UNKNOWN)  # l'écran est resté actif : « 2 » = Retour
        assert "Mes besoins récurrents" in later.response


# ── 16. ENTRÉES FERMÉES : toujours déterministes ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("answer", ["9", "0"])
def test_closed_index_out_of_bounds_runs_nothing(role, answer):
    with _conv(role) as h:
        _to_detail(h)
        t = h.send(answer, llm=UNKNOWN)
        assert "refresh_recurring_need_matching" not in t.mcp_tools() and "update_recurring_need" not in t.mcp_tools()
        assert "n'ai pas" in t.response, t.response


@pytest.mark.parametrize("role", ROLES)
def test_an_expired_menu_does_not_execute_a_closed_reply(role, monkeypatch):
    import time as _time

    with _conv(role) as h:
        _to_detail(h)
        real = _time.time
        monkeypatch.setattr(_time, "time", lambda: real() + 3600)
        t = h.send("1", llm=UNKNOWN)
        assert "refresh_recurring_need_matching" not in t.mcp_tools(), t.mcp_tools()


@pytest.mark.parametrize("role", ROLES)
def test_a_closed_menu_reply_after_a_new_task_does_not_revive_the_old_screen(role):
    with _conv(role) as h:
        _to_detail(h)
        h.send(FREE_TEXT, llm=ScriptLLM(CREATE_CHEVRE))
        later = h.send("1", llm=UNKNOWN)
        assert "refresh_recurring_need_matching" not in later.mcp_tools() and "update_recurring_need" not in later.mcp_tools()


# ── 27. MATRICE D'ARBITRAGE ────────────────────────────────────────────────────────────────────────────────────────
#  (message, LLM, attendu) depuis l'écran de détail Bœuf — chaque ligne vérifie l'outil de domaine réellement appelé.
MATRIX = [
    ("1", UNKNOWN, "refresh_recurring_need_matching"),
    ("rechercher maintenant", UNKNOWN, "refresh_recurring_need_matching"),
    ("retour", UNKNOWN, "list"),
    ("mets-en 3", ScriptLLM(UPDATE_QTY_3), "update_recurring_need"),
    ("plutôt chaque mois", ScriptLLM(UPDATE_MONTHLY), "update_recurring_need"),
    (FREE_TEXT, ScriptLLM(CREATE_CHEVRE), "draft"),
    ("mes besoins", UNKNOWN, "list"),
    ("mes commandes", ScriptLLM(new_task("BUYER_LIST_ORDERS", 0.9)), "get_buyer_orders_dashboard"),
    ("je veux acheter 100 kg de tomates", ScriptLLM(new_task("BUYER_REQUEST", 0.95, product="tomates", quantity=100.0, unit="KG")), "search_products"),
    ("cherche pour mes boeufs", ScriptLLM(REFRESH_BOEUFS), "refresh_recurring_need_matching"),
    ("change ça", ScriptLLM(new_task("UPDATE_RECURRING_NEED", 0.9)), "clarify"),
]


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("message,llm,expected", MATRIX, ids=[m[0] for m in MATRIX])
def test_arbitration_matrix_from_the_boeuf_detail(role, message, llm, expected):
    with _conv(role) as h:
        _to_detail(h)
        t = h.send(message, llm=llm)
        tools = t.mcp_tools()
        if expected == "list":
            assert "Mes besoins récurrents" in t.response
        elif expected == "draft":
            assert t.draft() is not None and t.intent == "CREATE_RECURRING_NEED"
        elif expected == "clarify":
            assert "la quantité, la fréquence ou la prochaine livraison" in t.response
            assert "update_recurring_need" not in tools and "refresh_recurring_need_matching" not in tools
        else:
            assert expected in tools, (tools, t.response)
        if expected != "refresh_recurring_need_matching":
            assert "refresh_recurring_need_matching" not in tools
        if expected != "update_recurring_need":
            assert "update_recurring_need" not in tools
        assert "accept_match_proposal" not in tools


@pytest.mark.parametrize("message,llm,expected", [
    ("finalement 5", ScriptLLM(new_task("CREATE_RECURRING_NEED", 0.9, quantity=5.0)), ("quantity", 5)),
    ("plutôt chaque mois", ScriptLLM(new_task("CREATE_RECURRING_NEED", 0.9, recurrence_type="MONTHLY")), ("recurrence_type", "MONTHLY")),
])
def test_arbitration_matrix_during_creation(message, llm, expected):
    with _conv("ADMIN") as h:
        t1 = h.send("j'ai besoin de 3 chevres chaque semaine",
                    llm=new_task("CREATE_RECURRING_NEED", product="chevre", quantity=3.0, unit="UNITE", recurrence_type="WEEKLY"))
        t2 = h.send(message, llm=llm)
        assert t2.draft()["draft_id"] == t1.draft()["draft_id"] and t2.draft()[expected[0]] == expected[1]


# ── 20. OWNERSHIP : un identifiant du contexte/forgé n'est jamais une cible tant qu'il n'est pas chez l'acheteur ──────
def test_pick_need_only_resolves_ids_owned_by_the_current_buyer():
    from ladini.graphs.agents.market_coach.flows.buyer.recurring_need import _pick_need

    mine = [{"recurring_need_id": "A", "product": "Boeuf", "status": "ACTIVE"},
            {"recurring_need_id": "B", "product": "Chevre", "status": "ACTIVE"}]
    forged = {"type": "RECURRING_NEED", "id": "OTHER-BUYER-NEED"}
    assert _pick_need(mine, product_hint="", context_target={"id": "B"}) == ("context", mine[1])
    assert _pick_need(mine, product_hint="", context_target=forged)[0] == "not_unique", "id étranger : jamais une cible"
    assert _pick_need(mine[:1], product_hint="", context_target=forged)[0] == "single"
    assert _pick_need(mine, product_hint="chevres", context_target={"id": "A"}) == ("name", mine[1]), "le produit nommé prime"
    assert _pick_need(mine, product_hint="tomate", context_target={"id": "A"})[0] == "not_found"
    assert _pick_need([], product_hint="", context_target=None)[0] == "not_unique"


@pytest.mark.parametrize("role", ROLES)
def test_a_forged_context_id_never_reaches_the_service(role):
    with _conv(role) as h:
        _to_detail(h)
        _second_need(h)
        # un écran périmé d'un AUTRE acheteur (id étranger) : jamais utilisé, deux besoins -> choix explicite
        state = h.state()
        menu = (state.get("working_memory") or {}).get("recurring_need_menu") or {}
        assert (menu.get("target") or {}).get("id") != "FORGED"
        t = h.send("mets-en 3", llm=ScriptLLM(UPDATE_QTY_3))
        ids = {c["recurring_need_id"] for c in _call_kwargs(t, "update_recurring_need")}
        assert ids <= {c["recurring_need_id"] for c in h.server.created}


# ── 31/32. HYGIÈNE D'ÉTAT ──────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("role", ROLES)
def test_context_target_is_ephemeral(role):
    with _conv(role) as h:
        _to_detail(h)
        h.send("mets-en 3", llm=ScriptLLM(UPDATE_QTY_3))
        after = h.state()
        assert not after.get("context_target") and not after.get("relation_to_context")


# ── 18. fonctions pures ────────────────────────────────────────────────────────────────────────────────────────────
def test_pure_relation_and_guard_helpers():
    from ladini.graphs.agents.market_coach.interpreter import context_arbitration as ca

    state = {"pending_interaction": None, "working_memory": {}}
    rel = lambda **r: ca.derive_relation(state, r)  # noqa: E731
    assert rel(interpreted_event="SELECTION", detected_intent="UNKNOWN") == ca.RelationToContext.ANSWER
    assert rel(interpreted_event="UPDATE", detected_intent="X") == ca.RelationToContext.CORRECTION
    assert rel(interpreted_event="NEW_TASK", detected_intent="CREATE_RECURRING_NEED") == ca.RelationToContext.NEW_TASK
    assert rel(interpreted_event="NEW_TASK", detected_intent="GET_MY_NEEDS") == ca.RelationToContext.INTERRUPTION
    assert rel(interpreted_event="OUT_OF_SCOPE", detected_intent="UNKNOWN") == ca.RelationToContext.UNRELATED
    assert rel(interpreted_event="UNKNOWN", detected_intent="UNKNOWN") == ca.RelationToContext.AMBIGUOUS
    assert rel(interpreted_event="NEW_TASK", detected_intent="UPDATE_RECURRING_NEED") == ca.RelationToContext.NEW_TASK, \
        "sans écran vivant, un UPDATE n'est pas une correction de contexte"
    assert ca.guard_free_text_selection(state, {"interpreted_event": "SELECTION", "extracted_entities": {"selection_index": 1}}) == \
        {"interpreted_event": "SELECTION", "extracted_entities": {"selection_index": 1}}, "hors écran récurrent : inchangé"


def test_update_plan_validates_outside_the_llm():
    from ladini.graphs.agents.market_coach.flows.buyer.recurring_need import _plan_permanent_changes as plan

    assert plan({"quantity": 3.0})[0] == [("PERMANENT_QUANTITY", {"quantity": 3.0})]
    for bad in (0, -2, float("nan"), float("inf"), "beaucoup"):
        steps, problem = plan({"quantity": bad})
        assert steps is None and "supérieur à 0" in problem
    assert plan({"recurrence_type": "YEARLY"})[0] is None
    assert plan({"recurrence_type": "WEEKLY_DAYS"})[0] is None
    assert plan({})[0] is None
    both, _ = plan({"quantity": 4.0, "recurrence_type": "MONTHLY"})
    assert [a for a, _ in both] == ["PERMANENT_QUANTITY", "PERMANENT_FREQUENCY"]
    assert plan({"quantity": 3.0, "update_action": "CANCEL"})[0] is not None  # l'action est décidée AVANT (flow), pas ici


# ── 43. ADMIN `can_buy` = capacité, jamais un `if role == ADMIN` ──────────────────────────────────────────────────────
def test_an_admin_without_the_buyer_capability_gets_no_contextual_update():
    with _conv("ADMIN", can_buy=False) as h:
        t = h.send("mets-en 3", llm=ScriptLLM(UPDATE_QTY_3))
        assert "update_recurring_need" not in t.mcp_tools(), t.mcp_tools()


# ── 38. observabilité sans PII ─────────────────────────────────────────────────────────────────────────────────────
def test_every_arbitration_line_is_free_of_user_text_and_identifiers(caplog):
    with _conv("ADMIN") as h:
        _to_detail(h)
        need_id = h.server.created[0]["recurring_need_id"]
        with caplog.at_level(logging.INFO):
            h.send("mets-en 3", llm=ScriptLLM(UPDATE_QTY_3))
            h.send("cherche pour mes boeufs", llm=ScriptLLM(REFRESH_BOEUFS))
            h.send("annule mes boeufs", llm=ScriptLLM(new_task("UPDATE_RECURRING_NEED", 0.9, product="boeufs", update_action="CANCEL")))
    lines = _arbitration_lines(caplog)
    assert len(lines) >= 3
    for line in lines:
        assert need_id not in line and "mets" not in line.lower() and "boeufs" not in line.lower()
        for field in ("current_goal=", "expected_action=", "semantic_intent=", "relation_to_context=", "target_type=",
                      "target_resolution=", "selected_route=", "decision_reason="):
            assert field in line, line
