"""B23 — le contexte d'un menu récurrent est une ATTENTE, pas une prison.

Rejoué sur le vrai orchestrateur + le vrai graphe compilé (seuls LLM, Redis, dispatcher et MCP sont doublés), avec le
rôle RÉEL de l'acteur (ADMIN `can_buy` -> graphe PRODUCER ; BUYER -> graphe BUYER). Le LLM est scripté PAR PROMPT :

* ``capable``      : lit les options affichées dans le micro-prompt SELECTION ; classe une demande étrangère en
                     INTERRUPTION, puis le micro-prompt NEW_TASK la classe correctement ;
* ``prompt_blind`` : le comportement observé en prod — quand le micro-prompt ne lui montre AUCUNE option, il « choisit »
                     l'option 1 (aucune base pour décider) ;
* ``imperfect``    : ne sait rien classer (UNKNOWN partout) ;
* ``always_interrupt`` : classe toujours INTERRUPTION puis laisse le NEW_TASK scripté décider (isole la validation métier).

Règle centrale : EXPECTATION guides interpretation, SEMANTIC INTENT decides the task, DOMAIN SERVICES execute.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

import pytest

from tests.harness import new_task
from tests.integration.test_recurring_self_service_entrypoint_e2e import UNKNOWN, _Conv

pytestmark = pytest.mark.integration

BOEUF = "N-BOEUF"
CREATE_CHEVRE = new_task("CREATE_RECURRING_NEED", product="chevre", quantity=2.0, unit="UNITE", recurrence_type="WEEKLY")
BUY_TOMATES = new_task("BUYER_REQUEST", 0.95, product="tomates", quantity=100.0, unit="KG")
SELL_OIGNONS = new_task("SALES_PUBLISH_PRODUCT", 0.95, product="oignons", quantity=100.0, unit="KG")
FREE_TEXT = "j ai besoin de 2 chevres chaque semaine"


def _system(kw: Dict[str, Any]) -> str:
    return " ".join(m["content"] for m in kw.get("messages") or [] if m["role"] == "system")


def _user(kw: Dict[str, Any]) -> str:
    return " ".join(m["content"] for m in kw.get("messages") or [] if m["role"] == "user")


class PromptLLM:
    """LLM scripté par prompt. `seen_selection_prompts` : ce que le micro-prompt SELECTION a RÉELLEMENT montré au modèle."""

    def __init__(self, new_task_payload: Any = None, *, mode: str = "capable") -> None:
        self.new_task_payload, self.mode = new_task_payload, mode
        self.seen_selection_prompts: List[str] = []

    def __call__(self, kw: Dict[str, Any]) -> Any:
        sysm = _system(kw)
        if "liste de choix" in sysm:
            user = _user(kw)
            self.seen_selection_prompts.append(user)
            if self.mode == "imperfect":
                return {"event": "UNKNOWN", "selection_index": None, "selected_value": None}
            if self.mode == "always_interrupt":  # LLM qui reconnaît l'interruption même sans voir les options
                return {"event": "INTERRUPTION", "selection_index": None, "selected_value": None}
            if "(aucune option listée)" in user:  # prompt aveugle : le modèle n'a aucune base, il « choisit » 1
                return {"event": "SELECTION", "selection_index": None, "selected_value": "1"}
            return {"event": "INTERRUPTION", "selection_index": None, "selected_value": None}
        if "CATALOGUE OFFICIEL" in sysm:
            return self.new_task_payload or {"disposition": "UNKNOWN", "confidence": 0.0}
        return {"disposition": "UNKNOWN", "confidence": 0.0}


def _detail(*, allocations: List[Dict[str, Any]] | None = None, need_id: str = BOEUF, product: str = "boeuf"):
    def _fn(**_: Any) -> Dict[str, Any]:
        base = {"status": "success", "recurring_need_id": need_id, "recurrence_type": "WEEKLY", "need_status": "ACTIVE",
                "product": product, "requested_quantity": 2, "unit": "TETE", "occurrence_id": "OCC-1",
                "need_version": 1000, "occurrence_version": 1, "occurrence_date": "2026-10-05", "occurrence_status": "OPEN"}
        return {**base, "allocations": allocations or [], "quantity_matched": sum(a["quantity"] for a in allocations or [])}
    return _fn


def _conv(role: str = "ADMIN", **kw: Any) -> _Conv:
    return _Conv(role, **kw)


def _to_detail(h, *, allocations: List[Dict[str, Any]] | None = None):
    """Besoin Bœuf créé pour de vrai, puis « mes besoins » -> « 1 » : l'écran du prod."""
    h.send("boeuf 2 tete chaque semaine",
           llm=new_task("CREATE_RECURRING_NEED", product="boeuf", quantity=2.0, unit="TETE", recurrence_type="WEEKLY"))
    h.send("oui")
    h.runtime.responses["get_recurring_need_detail"] = _detail(allocations=allocations)
    h.runtime.responses["refresh_recurring_need_matching"] = {"status": "success", "outcome": "NO_AVAILABILITY", "changed": False,
                                                              "occurrence_version": 1}
    h.runtime.responses["accept_match_proposal"] = {"status": "success", "occurrence_id": "OCC-1", "action": "ACCEPT",
                                                    "order_ids": ["O1"], "quantity_confirmed": 2.0}
    h.send("mes besoins", llm=UNKNOWN)
    return h.send("1", llm=UNKNOWN)


def _domain_calls(t) -> List[str]:
    quiet = {"get_account_status", "get_prohibited_terms", "get_last_interactive_outbound", "get_user_by_phone"}
    return [m for m in t.mcp_tools() if m not in quiet]


def _chevre_needs(h) -> List[Dict[str, Any]]:
    return [c for c in h.server.created if "chevre" in str(c.get("product_query") or "").lower()]


ROLES = ["ADMIN", "BUYER"]


# ── 14. TEST CRITIQUE 1 — le bug prod ───────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("role", ROLES)
def test_free_text_new_need_leaves_the_boeuf_context(role):
    llm = PromptLLM(CREATE_CHEVRE)
    with _conv(role) as h:
        detail = _to_detail(h)
        assert "1. Rechercher maintenant" in detail.response and "2. Retour" in detail.response
        t = h.send(FREE_TEXT, llm=llm)
        assert t.intent == "CREATE_RECURRING_NEED", (t.intent, t.response)
        assert "refresh_recurring_need_matching" not in t.mcp_tools(), "aucun REFRESH(Boeuf)"
        assert "chevre" in t.response.lower() and "boeuf" not in t.response.lower()
        d = t.draft()
        assert d is not None and d["quantity"] == 2 and "chevre" in str(d["product"]).lower()
        # le menu Bœuf est abandonné : un « 1 » ultérieur ne relance pas la recherche Bœuf
        later = h.send("1", llm=UNKNOWN)
        assert "refresh_recurring_need_matching" not in later.mcp_tools(), later.mcp_tools()


@pytest.mark.parametrize("role", ROLES)
def test_the_selection_micro_prompt_shows_the_live_menu_options(role):
    """Cause de l'attribution aveugle : le micro-prompt SELECTION disait « (aucune option listée) » pour ce menu."""
    llm = PromptLLM(CREATE_CHEVRE)
    with _conv(role) as h:
        _to_detail(h)
        h.send(FREE_TEXT, llm=llm)
    assert llm.seen_selection_prompts, "le micro-prompt SELECTION doit être consulté pour du langage libre"
    shown = llm.seen_selection_prompts[-1]
    assert "(aucune option listée)" not in shown
    assert "Rechercher maintenant" in shown and "Retour" in shown


@pytest.mark.parametrize("role", ROLES)
def test_prod_scenario_down_to_exactly_one_new_chevre_need(role):
    """37. scénario critique jusqu'à la persistance : ouvrir le détail Bœuf, créer Chèvre en texte libre, confirmer."""
    with _conv(role) as h:
        _to_detail(h)
        assert len(h.server.created) == 1  # le Bœuf
        h.send(FREE_TEXT, llm=PromptLLM(CREATE_CHEVRE))
        t = h.send("oui")
        assert "C'est noté" in t.response, t.response
        assert len(h.server.created) == 2 and len(_chevre_needs(h)) == 1
        chevre = _chevre_needs(h)[0]
        assert chevre["quantity"] == 2 and chevre["recurrence_type"] == "WEEKLY"
        boeuf = [c for c in h.server.created if "boeuf" in str(c.get("product_query") or "").lower()]
        assert len(boeuf) == 1 and boeuf[0]["quantity"] == 2


# ── 15. TEST CRITIQUE 2 — vraies réponses de menu : déterministes, sans LLM ─────────────────────────────────────────
@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("answer", ["1", "Rechercher maintenant", "rechercher maintenant !", "actualiser"])
def test_closed_menu_replies_refresh_without_llm(role, answer):
    with _conv(role) as h:
        _to_detail(h)
        t = h.send(answer, llm=UNKNOWN)
        assert "refresh_recurring_need_matching" in t.mcp_tools() and t.llm_calls == 0, (t.mcp_tools(), t.response)


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("answer", ["2", "retour", "Retour"])
def test_back_returns_to_the_list_without_llm(role, answer):
    with _conv(role) as h:
        _to_detail(h)
        t = h.send(answer, llm=UNKNOWN)
        assert "Mes besoins récurrents" in t.response and t.llm_calls == 0
        assert "refresh_recurring_need_matching" not in t.mcp_tools()


# ── 16/17. navigation globale et nouvel achat direct depuis le détail ───────────────────────────────────────────────
@pytest.mark.parametrize("role", ROLES)
def test_global_navigation_my_needs_from_detail(role):
    with _conv(role) as h:
        _to_detail(h)
        t = h.send("mes besoins", llm=UNKNOWN)
        assert t.intent == "GET_MY_NEEDS" and "Mes besoins récurrents" in t.response and t.llm_calls == 0
        assert "refresh_recurring_need_matching" not in t.mcp_tools()


@pytest.mark.parametrize("role", ROLES)
def test_global_navigation_my_orders_from_detail(role):
    with _conv(role) as h:
        _to_detail(h)
        t = h.send("mes commandes", llm=PromptLLM(new_task("BUYER_LIST_ORDERS", 0.9)))
        assert t.intent == "BUYER_LIST_ORDERS", (t.intent, t.response)
        assert "refresh_recurring_need_matching" not in t.mcp_tools()


@pytest.mark.parametrize("role", ROLES)
def test_direct_purchase_from_detail_is_a_new_buyer_intent(role):
    with _conv(role) as h:
        _to_detail(h)
        t = h.send("je veux acheter 100 kg de tomates", llm=PromptLLM(BUY_TOMATES))
        assert t.intent == "BUYER_REQUEST", (t.intent, t.response)
        assert "refresh_recurring_need_matching" not in t.mcp_tools() and "search_products" in t.mcp_tools()


def test_producer_sell_request_is_not_hijacked_by_the_recurring_menu():
    with _conv("ADMIN") as h:
        _to_detail(h)
        t = h.send("je veux vendre 100 kg d'oignons", llm=PromptLLM(SELL_OIGNONS))
        assert t.intent == "SALES_PUBLISH_PRODUCT", (t.intent, t.response)
        assert "refresh_recurring_need_matching" not in t.mcp_tools()


# ── 19. correction pendant la création : même goal, même draft ──────────────────────────────────────────────────────
@pytest.mark.parametrize("role", ROLES)
def test_correction_during_creation_stays_in_the_goal(role):
    with _conv(role) as h:
        t1 = h.send("j'ai besoin de 3 chevres chaque semaine",
                    llm=new_task("CREATE_RECURRING_NEED", product="chevre", quantity=3.0, unit="UNITE", recurrence_type="WEEKLY"))
        draft_id = t1.draft()["draft_id"]
        t2 = h.send("finalement 5 chevres", llm=new_task("CREATE_RECURRING_NEED", product="chevre", quantity=5.0, unit="UNITE"))
        d = t2.draft()
        assert d is not None and d["draft_id"] == draft_id and d["quantity"] == 5
        t3 = h.send("oui")
        assert "C'est noté" in t3.response and len(h.server.created) == 1 and h.server.created[0]["quantity"] == 5


# ── 20. message ambigu : jamais un refresh automatique ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("mode", ["capable", "imperfect"])
def test_ambiguous_message_never_triggers_a_refresh(role, mode):
    with _conv(role) as h:
        _to_detail(h)
        t = h.send("mets-en 3", llm=PromptLLM(None, mode=mode))
        assert "refresh_recurring_need_matching" not in t.mcp_tools(), (t.mcp_tools(), t.response)
        assert "accept_match_proposal" not in t.mcp_tools()


# ── 21. hors contexte ───────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("role", ROLES)
def test_out_of_context_message_is_not_captured_by_the_recurring_menu(role):
    with _conv(role) as h:
        _to_detail(h)
        t = h.send("quel temps fera-t-il demain ?", llm=PromptLLM({"disposition": "OUT_OF_SCOPE", "confidence": 0.9}))
        assert t.intent != "GET_MY_NEEDS" and "refresh_recurring_need_matching" not in t.mcp_tools()
        assert "Recherche terminée" not in t.response and "Rechercher maintenant" not in t.response


# ── 37 (LLM). LLM imparfait : clarification, jamais une action de menu fabriquée ────────────────────────────────────
@pytest.mark.parametrize("role", ROLES)
def test_imperfect_llm_clarifies_instead_of_refreshing(role):
    with _conv(role) as h:
        _to_detail(h)
        t = h.send(FREE_TEXT, llm=PromptLLM(mode="imperfect"))
        assert "refresh_recurring_need_matching" not in t.mcp_tools(), (t.mcp_tools(), t.response)
        assert not h.server.created[1:], "aucun besoin créé sans compréhension"


@pytest.mark.parametrize("role", ROLES)
def test_free_text_never_resolves_to_a_mutating_menu_entry(role):
    """Fail-safe : une sélection « libre » du LLM ne peut JAMAIS accepter/refuser une proposition (mutation)."""
    alloc = [{"producer_label": "Producteur A", "quantity": 2, "unit_price": 500, "unit": "TETE"}]
    with _conv(role) as h:
        _to_detail(h, allocations=alloc)
        t = h.send(FREE_TEXT, llm=PromptLLM(mode="prompt_blind"))
        assert "accept_match_proposal" not in t.mcp_tools(), (t.mcp_tools(), t.response)


def test_the_closed_alias_still_accepts_a_proposal():
    alloc = [{"producer_label": "Producteur A", "quantity": 2, "unit_price": 500, "unit": "TETE"}]
    with _conv("ADMIN") as h:
        _to_detail(h, allocations=alloc)
        t = h.send("accepter", llm=UNKNOWN)
        assert "accept_match_proposal" in t.mcp_tools() and t.llm_calls == 0


# ── 11. validate : une modification vise le besoin NOMMÉ, jamais « le seul besoin » par défaut ──────────────────────
def test_update_naming_another_product_does_not_modify_the_only_need():
    with _conv("ADMIN") as h:
        _to_detail(h)
        t = h.send("mets 5 chevres plutot", llm=PromptLLM(new_task("UPDATE_RECURRING_NEED", 0.9, product="chevres", quantity=5.0), mode="always_interrupt"))
        assert "update_recurring_need" not in t.mcp_tools(), (t.mcp_tools(), t.response)
        assert "je ne trouve pas de besoin récurrent" in t.response.lower() and "boeuf" in t.response.lower()


def test_update_without_product_targets_the_context_need():
    with _conv("ADMIN") as h:
        _to_detail(h)
        t = h.send("mets plutot 5", llm=PromptLLM(new_task("UPDATE_RECURRING_NEED", 0.9, quantity=5.0), mode="always_interrupt"))
        assert "update_recurring_need" in t.mcp_tools(), (t.mcp_tools(), t.response)


# ── 27. observabilité : INTENT_ARBITRATION, sans PII ────────────────────────────────────────────────────────────────
def test_intent_arbitration_is_logged_without_the_user_text(caplog):
    with _conv("ADMIN") as h:
        _to_detail(h)
        with caplog.at_level(logging.INFO):
            h.send(FREE_TEXT, llm=PromptLLM(CREATE_CHEVRE))
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("INTENT_ARBITRATION")]
    assert lines, "INTENT_ARBITRATION attendu"
    joined = " ".join(lines)
    for field in ("current_goal=GET_MY_NEEDS", "expected_action=", "semantic_intent=", "relation_to_expectation=NEW_TASK",
                  "selected_route=", "reason="):
        assert field in joined, joined
    assert "chevre" not in joined.lower() and "semaine" not in joined.lower()
