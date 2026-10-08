"""PRODUCTION CONVERSATION HARDENING — parcours ACHETEUR (menu producteurs -> panier -> édition -> précommande) sous redémarrage, rejeu et concurrence.

Même moteur réel que `test_production_conversation_hardening.py` (orchestrateur, graphe compilé, checkpointer, store JSON, Celery eager, FakeRedis perdable) ;
le LLM est scripté (il COMPRÉHENSION), le catalogue est celui du corpus terrain.
"""
from __future__ import annotations

import json
from contextlib import ExitStack
from typing import Any, Callable, Dict, List, Tuple

import pytest

from tests.field_corpus.business_edits._builders import edit
from tests.field_corpus.cases import SCRIPTED
from tests.field_corpus.harness import ScriptedLLM, catalog
from tests.harness import ConversationHarness
from tests.harness.conversation import FakeRedis

pytestmark = pytest.mark.integration

_SCRIPT: Dict[str, Dict[str, Any]] = {
    **SCRIPTED,
    "mets 20": edit({"field": "QUANTITY", "value": 20}),
    "mets 10": edit({"field": "QUANTITY", "value": 10}),
    "mets 15": edit({"field": "QUANTITY", "value": 15}),
    "le quatrième": {"disposition": "ACTION", "action": "SELECT_PRODUCER", "reference": {"reference_type": "ORDINAL", "ordinal": 4}},
}

_BUYER = {
    "status": "success",
    "data": {"id": "u1", "phone": "+22670000001", "name": "Zouba", "role": "BUYER", "declared_location": "Kadiogo",
             "permissions": {"can_buy": True, "can_sell": False, "is_admin": False}, "zone": {"id": "z", "name": "Kadiogo"}},
}


def _wire(conv: ConversationHarness) -> Callable[[Dict[str, Any]], Any]:
    offers = catalog()
    llm = ScriptedLLM(_SCRIPT)

    def search(**kw: Any) -> Dict[str, Any]:
        found = [o for o in offers if str(kw.get("product", "")).lower() in o["name"].lower()]
        return {"status": "success", "results": found, "data": {"results": found}}

    conv.runtime.responses["search_products"] = search
    conv.runtime.responses["validate_stock_availability_atomic"] = {"status": "success", "available_quantity": 500}
    conv.runtime.responses["get_user_by_phone"] = _BUYER

    def script(kwargs: Dict[str, Any]) -> Any:
        return json.loads(llm.create(**kwargs).choices[0].message.content)

    return script


def _restart(old: ConversationHarness, stack: ExitStack) -> Tuple[ConversationHarness, Callable[[Dict[str, Any]], Any]]:
    """Processus relancé : RAM et Redis perdus ; workspace (état LangGraph) conservé."""
    new = ConversationHarness(role=old.role, phone=old.phone, channel=old.channel, store=old.store, redis=FakeRedis())
    stack.enter_context(new)
    return new, _wire(new)


def _cart(state: Dict[str, Any]) -> List[Tuple[str, float]]:
    return [(i["vendor_name"], i["quantity"]) for i in state.get("active_cart") or []]


@pytest.fixture()
def shop():
    with ExitStack() as stack:
        conv = stack.enter_context(ConversationHarness(role="BUYER"))
        script = _wire(conv)
        for text in ("Je veux du lait", "2", "10 litres"):
            conv.send(text, llm=script)
        yield stack, conv, script


class TestCartSurvivesRestart:
    def test_the_cart_is_identical_after_a_process_restart(self, shop):
        stack, conv, script = shop
        before = conv.state()
        restarted, _ = _restart(conv, stack)
        after = restarted.state()
        assert _cart(after) == _cart(before) == [("Moussa", 10.0)]
        assert [i["line_id"] for i in after["active_cart"]] == [i["line_id"] for i in before["active_cart"]]
        assert after["cart_meta"]["version"] == before["cart_meta"]["version"]

    def test_the_producer_menu_is_restored_with_its_creation_time(self):
        with ExitStack() as stack:
            conv = stack.enter_context(ConversationHarness(role="BUYER"))
            script = _wire(conv)
            conv.send("Je veux du lait", llm=script)
            created_before = conv.state()["vendor_selection_context"]["created_at"]
            restarted, _ = _restart(conv, stack)
            created_after = restarted.state()["vendor_selection_context"]["created_at"]
            assert created_before and created_after == created_before, "la péremption du menu survit au redémarrage"


class TestBusinessEditReplay:
    def test_the_same_edit_delivered_twice_is_applied_once_even_without_redis(self, shop):
        stack, conv, script = shop
        version0 = conv.state()["cart_meta"]["version"]
        first = conv.send("mets 20", llm=script, message_id="wamid.EDIT")
        conv.redis._data.clear()
        conv.redis._expires.clear()
        second = conv.send("mets 20", llm=script, message_id="wamid.EDIT")
        state = conv.state()
        assert _cart(state) == [("Moussa", 20.0)]
        assert state["cart_meta"]["version"] == version0 + 1, "UNE seule édition logique"
        assert second.response == first.response and second.mcp_tools() == [] and second.llm_calls == 0

    def test_the_same_edit_after_a_restart_does_not_apply_twice(self, shop):
        stack, conv, script = shop
        conv.send("mets 20", llm=script, message_id="wamid.EDIT2")
        version = conv.state()["cart_meta"]["version"]
        restarted, rscript = _restart(conv, stack)
        again = restarted.send("mets 20", llm=rscript, message_id="wamid.EDIT2")
        state = restarted.state()
        assert _cart(state) == [("Moussa", 20.0)] and state["cart_meta"]["version"] == version
        assert again.mcp_tools() == [] and again.llm_calls == 0

    def test_a_different_message_with_the_same_text_is_a_new_edit_that_changes_nothing_more(self, shop):
        stack, conv, script = shop
        conv.send("mets 20", llm=script, message_id="wamid.A")
        version = conv.state()["cart_meta"]["version"]
        conv.send("mets 20", llm=script, message_id="wamid.B")
        state = conv.state()
        assert _cart(state) == [("Moussa", 20.0)] and state["cart_meta"]["version"] == version, "idempotence métier : même valeur -> aucun changement"


class TestEditThenRestartThenConfirm:
    def test_the_confirmation_after_a_restart_targets_the_edited_version_never_the_old_quantity(self, shop):
        stack, conv, script = shop
        recap = conv.send("précommander", llm=script)
        assert (recap.after.get("pending_interaction") or {}).get("kind") == "CONFIRM_ACTION"
        draft_v1 = dict(recap.after["preorder_draft"])
        edited = conv.send("mets 20", llm=script, message_id="wamid.E")
        draft_v2 = dict(edited.after["preorder_draft"])
        assert draft_v2["draft_id"] == draft_v1["draft_id"] and draft_v2["version"] == draft_v1["version"] + 1
        restarted, rscript = _restart(conv, stack)
        confirm = restarted.send("oui", llm=rscript, message_id="wamid.OK")
        calls = [(tool, kw) for tool, kw in confirm.mcp_calls if "preorder" in tool or "order" in tool]
        for tool, kwargs in calls:
            blob = json.dumps(kwargs, default=str)
            assert '"quantity": 10' not in blob and "4500" not in blob, (tool, kwargs)
        after = restarted.state()
        assert _cart(after) == [("Moussa", 20.0)]
        assert (after.get("preorder_draft") or {}).get("total_amount") in (9000.0, None) or (after["preorder_draft"]["version"] >= draft_v2["version"])


class TestStaleMenuAfterReload:
    def test_a_natural_reference_on_an_old_menu_is_refused_after_a_restart(self, monkeypatch):
        from ladini.graphs.agents.market_coach.domain import menu_facts

        with ExitStack() as stack:
            conv = stack.enter_context(ConversationHarness(role="BUYER"))
            script = _wire(conv)
            conv.send("Je veux du lait", llm=script)
            restarted, rscript = _restart(conv, stack)
            monkeypatch.setattr(menu_facts, "MENU_FACTS_TTL_SECONDS", -1)  # 10+ minutes plus tard
            late = restarted.send("le quatrième", llm=rscript, message_id="wamid.LATE")
            state = restarted.state()
            assert not (state.get("vendor_selection_context") or {}).get("chosen_vendor"), "aucune sélection sur une liste périmée"
            assert _cart(state) == []
            assert "date un peu" in late.response or "redemande" in late.response.lower() or "redis" in late.response.lower()


class TestConcurrentEdits:
    def test_two_concurrent_edits_are_serialised_and_neither_is_lost(self, shop):
        stack, conv, script = shop
        version0 = conv.state()["cart_meta"]["version"]
        outcome = conv.send_concurrently([("mets 15", script), ("mets 20", script)])
        assert not outcome.interleaved
        assert not [r for r in outcome.responses if isinstance(r, BaseException)]
        state = outcome.after
        assert len(state["active_cart"]) == 1 and _cart(state)[0][1] in (15.0, 20.0)
        assert state["cart_meta"]["version"] == version0 + 2, "chaque édition a vu le résultat de l'autre : aucune mise à jour perdue"
