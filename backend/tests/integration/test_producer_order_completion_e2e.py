"""B11 (2026-10-02) — clôture producteur d'une commande (livrée + payée en espèces).

Le service DB `confirm_delivery_and_payment` (verrou, propriété, idempotence, historique, outbox,
évènement) existait déjà ; ces replays verrouillent la couche conversationnelle :
récap NON vide, référence explicite résolue (« la commande #11DE2D1B est livrée »), commande
annulée / déjà clôturée / inconnue refusées sans mutation, « non » sans mutation, menu multi-commandes.

Replays sur le VRAI graphe compilé ; seuls le LLM et le MCP sont doublés.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List

import pytest
from langgraph.checkpoint.memory import MemorySaver

from ladini.graphs.agents.market_coach.core.graph_builder import build_graph
from tests.conftest import StubRuntime, run
from tests.integration.test_buyer_deterministic_product_switch_e2e import _Comp

PHONE = "+22670000001"
ORD1 = "11de2d1b-0000-4000-8000-000000000001"
ORD2 = "22aa2d1b-0000-4000-8000-000000000002"
ORD3 = "33bb2d1b-0000-4000-8000-000000000003"
ORD4 = "44cc2d1b-0000-4000-8000-000000000004"
_NOISE = ("get_account_status", "get_prohibited_terms", "get_user_by_phone", "identify_or_create_user")


def _order(oid: str, ref: str, buyer: str = "Awa", **kw: Any) -> Dict[str, Any]:
    base = {"order_id": oid, "reference": ref, "status": "CONFIRMED", "payment_status": "PENDING",
            "delivery_status": "PENDING", "order_type": "STANDARD", "total_amount": 17500,
            "currency": "XOF", "buyer_name": buyer,
            "items": [{"product_name": "oignons", "quantity": 5.0, "unit": "KG", "tier_label": None}]}
    base.update(kw)
    return base


class LLM:
    """Classifie comme le vrai prompt : « livrée/payé/terminée » -> PRODUCER_CONFIRM_DELIVERY_PAYMENT."""

    def __init__(self, mode: str = "ok") -> None:
        self.mode, self.n = mode, 0
        self.chat = self
        self.completions = self

    def create(self, **kw: Any):
        self.n += 1
        if self.mode == "exception":
            raise TimeoutError("llm timeout")
        if self.mode == "invalid_json":
            return _Comp("{not json")
        if self.mode == "unknown":
            return _Comp(json.dumps({"disposition": "UNKNOWN", "confidence": 0.2}))
        msgs = kw.get("messages") or []
        sysm = " ".join(m["content"] for m in msgs if m["role"] == "system")
        user = " ".join(m["content"] for m in msgs if m["role"] == "user")
        found = re.findall(r'Message utilisateur :\s*"{1,3}(.*?)"{1,3}', user, re.S)
        text = (found[-1].strip() if found else "").lower()
        if "CATALOGUE OFFICIEL" in sysm:
            ents: Dict[str, Any] = {}  # NewTaskEntities interdit tout identifiant technique (extra=forbid)
            if "commandes re" in text:
                return _Comp(json.dumps({"disposition": "NEW_TASK", "intent": "SALES_LIST_ORDERS",
                                         "confidence": 0.9, "entities": {}}))
            return _Comp(json.dumps({"disposition": "NEW_TASK", "intent": "PRODUCER_CONFIRM_DELIVERY_PAYMENT",
                                     "confidence": 0.9, "entities": ents}))
        return _Comp(json.dumps({"disposition": "UNKNOWN", "confidence": 0.1}))


class Rt(StubRuntime):
    def __init__(self, llm: Any, orders: List[Dict[str, Any]]) -> None:
        super().__init__(llm=llm)
        self.orders = orders
        self.log: List[tuple] = []
        self.closures: List[str] = []

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)

        async def _n(*a: Any, **k: Any):
            return None

        return _n

    def _find(self, oid: str):
        return next((o for o in self.orders if o["order_id"] == oid), None)

    async def call_db(self, tool: str, **kw: Any) -> Any:
        self.log.append((tool, dict(kw)))
        if tool == "get_user_by_phone":
            return {"status": "success", "data": {"id": "u1", "phone": kw.get("phone"), "role": "PRODUCER"}}
        if tool == "get_producer_orders":
            st = (kw.get("status") or "").upper()
            data = [o for o in self.orders if not st or o["status"] == st]
            return {"status": "success", "data": [dict(o) for o in data], "count": len(data)}
        if tool == "confirm_delivery_and_payment":
            o = self._find(kw.get("order_id"))
            if o is None:
                return {"status": "error", "message": "Commande introuvable."}
            if o["status"] == "COMPLETED":
                return {"status": "success", "outcome": "ALREADY_COMPLETED", "order_id": o["order_id"],
                        "message": "Cette commande est déjà clôturée"}
            self.closures.append(o["order_id"])
            o.update(status="COMPLETED", payment_status="PAID", delivery_status="DELIVERED")
            return {"status": "success", "outcome": "COMPLETED", "order_id": o["order_id"],
                    "message": f"✅ Commande #{o['reference']} clôturée."}
        return await super().call_db(tool, **kw)


class Conv:
    def __init__(self, orders: List[Dict[str, Any]], llm: Any = None) -> None:
        self.llm = llm or LLM()
        self.rt = Rt(self.llm, orders)
        self.graph = build_graph("PRODUCER", mc_runtime=self.rt, checkpointer=MemorySaver())
        self.cfg = {"configurable": {"thread_id": "t"}}
        self.i = 0

    def say(self, text: str) -> Dict[str, Any]:
        self.i += 1
        self.rt.log.clear()
        run(self.graph.ainvoke({"user_query": text, "normalized_text": text, "user_phone": PHONE,
                                "user_role": "PRODUCER", "message_sid": f"m{self.i}"}, self.cfg))
        return self.graph.get_state(self.cfg).values

    def tools(self) -> List[str]:
        return [t for t, _ in self.rt.log if t not in _NOISE]


def _resp(st: Dict[str, Any]) -> str:
    return str(st.get("final_response") or "")


def test_single_order_recap_is_informative_then_close_once():
    c = Conv([_order(ORD1, "11DE2D1B")])
    st = c.say("commande livrée")
    assert c.rt.closures == []  # aucune mutation avant « oui »
    r = _resp(st)
    assert "11DE2D1B" in r and "17" in r and "oignons" in r and "Awa" in r
    assert "reçu la commande" in r and "paiement" in r
    assert "Confirmez-vous cette opération" not in r
    st = c.say("oui")
    assert c.rt.closures == [ORD1]
    assert "11DE2D1B" in _resp(st) and "clôturée" in _resp(st)
    o = c.rt.orders[0]
    assert (o["status"], o["payment_status"], o["delivery_status"]) == ("COMPLETED", "PAID", "DELIVERED")


def test_no_cancels_without_mutation():
    c = Conv([_order(ORD1, "11DE2D1B")])
    c.say("commande livrée")
    c.say("non")
    assert c.rt.closures == []
    assert c.rt.orders[0]["status"] == "CONFIRMED"


def test_multi_order_menu_then_index_selects_that_order_only():
    c = Conv([_order(ORD1, "11DE2D1B"), _order(ORD2, "22AA2D1B", "Moussa")])
    st = c.say("commande livrée")
    r = _resp(st)
    assert "1." in r and "2." in r and "22AA2D1B" in r
    assert c.rt.closures == []
    st = c.say("2")
    assert c.rt.closures == []
    assert "22AA2D1B" in _resp(st) and "Moussa" in _resp(st)
    c.say("oui")
    assert c.rt.closures == [ORD2]
    assert c.rt._find(ORD1)["status"] == "CONFIRMED"


def test_wrong_index_in_menu_never_mutates():
    c = Conv([_order(ORD1, "11DE2D1B"), _order(ORD2, "22AA2D1B")])
    c.say("commande livrée")
    c.say("9")
    assert c.rt.closures == []


def test_explicit_short_reference_resolves_full_id():
    c = Conv([_order(ORD1, "11DE2D1B"), _order(ORD2, "22AA2D1B")])
    st = c.say("la commande #22AA2D1B est livrée")
    assert "22AA2D1B" in _resp(st)
    c.say("oui")
    assert c.rt.closures == [ORD2]  # identifiant COMPLET transmis au service


def test_cancelled_order_refused_without_mutation():
    c = Conv([_order(ORD3, "33BB2D1B", status="CANCELLED", payment_status="CANCELLED")])
    st = c.say("la commande #33BB2D1B est livrée")
    assert "annulée" in _resp(st)
    assert c.rt.closures == [] and "confirm_delivery_and_payment" not in c.tools()


def test_already_completed_order_reports_closed_without_mutation():
    c = Conv([_order(ORD4, "44CC2D1B", status="COMPLETED", payment_status="PAID", delivery_status="DELIVERED")])
    st = c.say("la commande #44CC2D1B est livrée")
    assert "déjà clôturée" in _resp(st)
    assert "confirm_delivery_and_payment" not in c.tools()


def test_unknown_or_foreign_reference_leaks_nothing():
    c = Conv([_order(ORD1, "11DE2D1B")])
    st = c.say("la commande #99ABCDEF est livrée")
    assert "Je ne trouve pas cette commande parmi vos commandes actives." in _resp(st)
    assert "confirm_delivery_and_payment" not in c.tools()
    assert "99ABCDEF" not in _resp(st)


def test_duplicate_yes_does_not_close_twice():
    c = Conv([_order(ORD1, "11DE2D1B")])
    c.say("commande livrée")
    c.say("oui")
    c.say("oui")
    assert c.rt.closures == [ORD1]


def test_repeat_request_after_completion_is_idempotent():
    c = Conv([_order(ORD1, "11DE2D1B")])
    c.say("commande livrée")
    c.say("oui")
    st = c.say("la commande #11DE2D1B est livrée")
    assert "déjà clôturée" in _resp(st)
    assert c.rt.closures == [ORD1]


def test_history_reflects_final_status():
    c = Conv([_order(ORD1, "11DE2D1B")])
    c.say("commande livrée")
    c.say("oui")
    assert c.rt.orders[0]["status"] == "COMPLETED"
    c.say("commande livrée")
    assert c.rt.closures == [ORD1]  # plus rien d'éligible : aucune seconde clôture


@pytest.mark.parametrize("mode", ["unknown", "invalid_json", "exception"])
def test_confirmation_turns_survive_hostile_llm(mode: str):
    """Après le récap, « oui » est du vocabulaire fermé : le LLM n'a aucun rôle."""
    c = Conv([_order(ORD1, "11DE2D1B")])
    c.say("commande livrée")
    c.llm.mode = mode
    c.say("oui")
    assert c.rt.closures == [ORD1]


@pytest.mark.parametrize("mode", ["unknown", "invalid_json", "exception"])
def test_hostile_llm_on_entry_never_mutates(mode: str):
    c = Conv([_order(ORD1, "11DE2D1B")], llm=LLM(mode))
    c.say("commande livrée")
    assert c.rt.closures == []


def test_stale_pending_recap_replaced_by_new_task():
    c = Conv([_order(ORD1, "11DE2D1B")])
    c.say("commande livrée")
    c.say("commandes reçues")
    assert c.rt.closures == []
