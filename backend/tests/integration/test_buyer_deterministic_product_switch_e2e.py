"""B4 (2026-10-02) — changement de produit explicite pendant ENTER_QUANTITY : le switch
déterministe gagne sur le micro-parser STRUCTURED_ACTION, quelle que soit sa sortie.

Incident prod : « je veux acheter du lait » pendant la quantité des poulets ->
STRUCTURED_ACTION renvoie un JSON invalide (« Input should be 'SELECT_PRODUCER'… ») -> UNKNOWN
-> recover_active_tunnel -> « On continue : j'ai juste besoin de la quantité » -> retry++ ->
« Max retries reached ».

Replays sur le VRAI graphe compilé ; seuls le LLM et le MCP sont doublés.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

import pytest
from langgraph.checkpoint.memory import MemorySaver

from ladini.graphs.agents.market_coach.core.graph_builder import build_graph
from tests.conftest import StubRuntime, run

_PHONE = "+22601479800"
_STALE_MARKERS = ("On continue", "j'ai juste besoin", "Max retries")


class _Msg:
    def __init__(self, c: str) -> None:
        self.content = c


class _Ch:
    def __init__(self, c: str) -> None:
        self.message = _Msg(c)


class _Comp:
    def __init__(self, c: str, m: Any = None) -> None:
        self.choices = [_Ch(c)]
        self.model = m


class HostileLLM:
    """`structured` : sortie (ou exception) du micro-parser du tunnel — le cas hostile.
    `new_task_ok` : le classifieur NEW_TASK répond normalement (False = lui aussi hostile)."""

    def __init__(self, structured: Any, new_task_ok: bool = True) -> None:
        self.structured = structured
        self.new_task_ok = new_task_ok
        self.families: List[str] = []

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kw: Any):
        msgs = kw.get("messages") or []
        sysm = " ".join(m["content"] for m in msgs if m["role"] == "system")
        user = " ".join(m["content"] for m in msgs if m["role"] == "user")
        m = re.search(r'Message utilisateur :\s*"{1,3}(.*?)"{1,3}\s*$', user, re.S)
        text = m.group(1).strip() if m else user[-120:]
        if "tunnel d'achat" in sysm:
            self.families.append("structured")
            if isinstance(self.structured, Exception):
                raise self.structured
            return _Comp(self.structured if isinstance(self.structured, str) else json.dumps(self.structured))
        if "À L'INTÉRIEUR d'une transaction" in sysm:
            self.families.append("active_slot")
            return _Comp(json.dumps({"disposition": "UNKNOWN", "extracted_entities": {}, "confidence": 0.1}))
        if "CATALOGUE OFFICIEL" in sysm:
            self.families.append("new_task")
            if not self.new_task_ok:
                return _Comp("{not json")
            prod = next((p for p in ("lait", "tomates", "poulets", "produitxyz") if p[:-1] in text), None)
            ents: Dict[str, Any] = {"product": prod} if prod else {}
            if "5 L" in text:
                ents.update({"quantity": 5, "unit": "L"})
            return _Comp(json.dumps({"disposition": "NEW_TASK", "intent": "BUYER_REQUEST",
                                     "confidence": 0.95, "entities": ents}))
        self.families.append("other")
        return _Comp(json.dumps({"text": "ok"}))


class _Rt(StubRuntime):
    def __init__(self, llm: Any, offers: List[Dict[str, Any]]) -> None:
        super().__init__(llm=llm)
        self.offers = offers
        self.tool_log: List[tuple] = []

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)

        async def _noop(*a: Any, **k: Any):
            return None

        return _noop

    async def call_db(self, tool: str, **kw: Any) -> Any:
        self.tool_log.append((tool, dict(kw)))
        if tool == "search_products":
            term = str(kw.get("product", "")).lower()
            res = [o for o in self.offers if term and term in o["name"].lower()]
            return {"status": "success", "results": res, "data": {"results": res}}
        if tool == "get_user_by_phone":
            return {"status": "success", "data": {"id": "u1", "phone": kw.get("phone"), "role": "BUYER"}}
        if tool == "validate_stock_availability_atomic":
            return {"status": "success", "available_quantity": 50}
        return await super().call_db(tool, **kw)

    def of(self, tool: str) -> List[Dict[str, Any]]:
        return [kw for t, kw in self.tool_log if t == tool]


def _offer(name: str, unit: str, vendor: str = "TEST Producteur", pid: Optional[str] = None) -> Dict[str, Any]:
    return {
        "id": pid or f"P-{name}-{vendor}", "product_id": pid or f"P-{name}-{vendor}", "name": name,
        "price": 3500.0, "unit": unit, "vendor": {"name": vendor}, "vendor_name": vendor,
        "producer_id": f"PR-{vendor}", "available_quantity": 50, "source_type": "DIRECT", "is_auction": False,
    }


_POULETS = _offer("poulets", "UNITE")
_LAIT1 = [_offer("lait", "LITRE", "Ferme0", "L0")]
_LAIT3 = [_offer("lait", "LITRE", f"Ferme{i}", f"L{i}") for i in range(3)]
_TOMATES = [_offer("tomates", "KG", "Ferme9", "T0")]

_HOSTILE = {
    "invalid_json": "{not json at all",
    "out_of_schema_action": {"disposition": "ACTION", "action": "SET_QUANTITY_X", "confidence": 0.9},
    "unknown": {"disposition": "UNKNOWN", "confidence": 0.3},
    "hallucinated_qty": {"disposition": "ACTION", "action": "SET_QUANTITY", "quantity": 1, "unit": "UNITE",
                         "confidence": 0.95},
    "exception": TimeoutError("llm timeout"),
}


class _Conv:
    def __init__(self, offers: List[Dict[str, Any]], structured: Any = None, new_task_ok: bool = True) -> None:
        self.llm = HostileLLM(structured if structured is not None else _HOSTILE["invalid_json"], new_task_ok)
        self.rt = _Rt(self.llm, offers)
        self.graph = build_graph("BUYER", mc_runtime=self.rt, checkpointer=MemorySaver())
        self.cfg = {"configurable": {"thread_id": "t"}}
        self.n = 0

    def say(self, text: str) -> Dict[str, Any]:
        self.n += 1
        self.rt.tool_log.clear()
        run(self.graph.ainvoke(
            {"user_query": text, "normalized_text": text, "user_phone": _PHONE, "user_role": "BUYER",
             "message_sid": f"m{self.n}"}, self.cfg))
        return self.graph.get_state(self.cfg).values

    def searched(self, term: str) -> bool:
        return any(k.get("product") == term for k in self.rt.of("search_products"))


def _pending(st: Dict[str, Any]) -> str:
    return str((st.get("pending_interaction") or {}).get("kind"))


def _live_products(st: Dict[str, Any]) -> set:
    vc = st.get("vendor_selection_context") or {}
    if vc.get("__reset__"):
        return set()
    names = {str(v.get("name")) for v in vc.get("vendors", [])}
    if vc.get("chosen_vendor"):
        names.add(str(vc["chosen_vendor"].get("name")))
    return names


def _not_recovery(st: Dict[str, Any]) -> None:
    text = st.get("final_response") or ""
    assert not any(mk in text for mk in _STALE_MARKERS), text
    assert int(st.get("retry_count") or 0) == 0, "retry du tunnel poulets consommé"


@pytest.mark.parametrize("variant", list(_HOSTILE), ids=list(_HOSTILE))
class TestProductSwitchWinsOverAnyStructuredParserOutcome:
    def test_switch_to_lait_one_result(self, variant):
        c = _Conv([_POULETS] + _LAIT1, _HOSTILE[variant])
        st = c.say("je veux acheter des poulets")
        assert "ENTER_QUANTITY" in _pending(st)
        st = c.say("je veux acheter du lait")
        _not_recovery(st)
        assert c.searched("lait")
        assert "lait" in st["final_response"] and "Quelle quantité" in st["final_response"]
        assert _live_products(st) == {"lait"}
        assert st["active_cart"] == [] and c.rt.of("validate_stock_availability_atomic") == []

    def test_switch_with_hostile_new_task_classifier_still_wins(self, variant):
        c = _Conv([_POULETS] + _LAIT1, _HOSTILE[variant])
        c.say("je veux acheter des poulets")
        c.llm.new_task_ok = False  # le classifieur NEW_TASK devient lui aussi hostile
        st = c.say("je veux acheter du lait")
        _not_recovery(st)
        assert c.searched("lait"), c.llm.families
        assert _live_products(st) == {"lait"}


class TestSwitchReplays:
    def test_prod_case_structured_parser_never_consulted(self):
        c = _Conv([_POULETS] + _LAIT1)
        c.say("je veux acheter des poulets")
        c.llm.families.clear()
        c.say("je veux acheter du lait")
        assert "structured" not in c.llm.families and "active_slot" not in c.llm.families

    def test_different_product_with_quantity_keeps_the_quantity_for_lait(self):
        c = _Conv([_POULETS] + _LAIT1)
        c.say("je veux acheter des poulets")
        st = c.say("je veux acheter 5 L de lait")
        _not_recovery(st)
        assert c.searched("lait")
        # la quantité 5 L appartient au LAIT, jamais aux poulets
        assert [(line["name"], line["quantity"], line["unit"]) for line in st["active_cart"]] == [("lait", 5.0, "LITRE")]
        stock = c.rt.of("validate_stock_availability_atomic")
        assert [(k["product_id"], k["quantity"]) for k in stock] == [("L0", 5.0)]

    def test_multiple_switch_poulets_lait_tomates(self):
        c = _Conv([_POULETS] + _LAIT1 + _TOMATES)
        c.say("je veux acheter des poulets")
        st = c.say("je veux acheter du lait")
        assert _live_products(st) == {"lait"}
        st = c.say("je veux acheter des tomates")
        _not_recovery(st)
        assert _live_products(st) == {"tomates"}
        assert st["transaction_payload"].get("product") == "tomates"
        assert st["active_cart"] == []

    def test_unavailable_product_is_no_results_not_poulets_quantity(self):
        c = _Conv([_POULETS])
        c.say("je veux acheter des poulets")
        st = c.say("je veux acheter produitxyz")
        _not_recovery(st)
        assert c.searched("produitxyz")
        assert "Aucun produit disponible" in st["final_response"]
        assert _live_products(st) == set()

    def test_several_results_menu_and_digit_selects(self):
        c = _Conv([_POULETS] + _LAIT3)
        c.say("je veux acheter des poulets")
        st = c.say("je veux acheter du lait")
        for i in (1, 2, 3):
            assert f"*{i}.*" in st["final_response"]
        assert "poulets" not in _live_products(st)
        st = c.say("2")
        assert "Ferme1" in st["final_response"]


class TestNoSwitch:
    _QTY2 = {"disposition": "ACTION", "action": "SET_QUANTITY", "quantity": 2, "unit": None, "confidence": 0.95}

    def test_same_product_with_quantity_stays_in_tunnel(self):
        c = _Conv([_POULETS] + _LAIT1, self._QTY2)
        c.say("je veux acheter des poulets")
        c.llm.families.clear()
        st = c.say("je veux acheter 2 poulets")
        # pas de bypass : le parser du tunnel est consulté, aucun autre produit cherché
        assert "structured" in c.llm.families
        assert not c.searched("lait")
        assert [(line["name"], line["quantity"]) for line in st["active_cart"]] == [("poulets", 2.0)]

    def test_pure_quantity_is_an_answer(self):
        c = _Conv([_POULETS], self._QTY2)
        c.say("je veux acheter des poulets")
        st = c.say("2")
        assert [x["quantity"] for x in c.rt.of("validate_stock_availability_atomic")] == [2.0]
        assert st["active_cart"][-1]["quantity"] == 2.0

    def test_dont_know_is_not_a_switch(self):
        c = _Conv([_POULETS] + _LAIT1, {"disposition": "UNKNOWN", "confidence": 0.2})
        c.say("je veux acheter des poulets")
        c.llm.families.clear()
        st = c.say("je ne sais pas")
        assert not c.searched("lait") and "new_task" not in c.llm.families
        assert "ENTER_QUANTITY" in _pending(st)
        assert st["active_cart"] == []

    def test_cancel_is_not_a_switch(self):
        c = _Conv([_POULETS] + _LAIT1, {"disposition": "REJECT", "confidence": 0.9})
        c.say("je veux acheter des poulets")
        c.llm.families.clear()
        st = c.say("annuler")
        assert not c.searched("lait") and "new_task" not in c.llm.families
        assert st["active_cart"] == []


class TestDetectorUnit:
    """Positifs / négatifs de la primitive (produit courant = poulets, pending ENTER_QUANTITY)."""

    @staticmethod
    def _state():
        from ladini.graphs.agents.market_coach.core.pending_interaction import (
            InteractionKind,
            set_pending_interaction,
        )
        from tests.conftest import make_state

        return make_state(
            current_goal="BUYER_ADD_TO_CART",
            transaction_payload={"product": "poulets"},
            **set_pending_interaction(InteractionKind.ENTER_QUANTITY, field_name="quantity"),
        )

    @pytest.mark.parametrize("text,new", [
        ("je veux acheter du lait", "lait"),
        ("je cherche du lait", "lait"),
        ("je voudrais acheter des tomates", "tomates"),
        ("je veux plutôt du lait", "lait"),
        ("finalement je veux du lait", "lait"),
        ("achète-moi du lait", "lait"),
        ("je veux prendre du lait", "lait"),
        ("je veux acheter 5 L de lait", "lait"),
        ("je veux acheter produitxyz", "produitxyz"),
    ])
    def test_positive(self, text, new):
        from ladini.graphs.agents.market_coach.interpreter.product_switch import (
            detect_buyer_product_switch,
        )

        sw = detect_buyer_product_switch(self._state(), text)
        assert sw is not None and sw.new_product == new

    @pytest.mark.parametrize("text", [
        "2", "2 unités", "5 kg", "deux poulets", "je veux 2 poulets", "je veux acheter 2 poulets",
        "je veux acheter des poulets", "je veux acheter du poulet", "je veux acheter des Poulets",
        "je ne sais pas", "un peu", "annuler", "oui", "non", "je veux 10 kg", "je veux deux",
    ])
    def test_negative(self, text):
        from ladini.graphs.agents.market_coach.interpreter.product_switch import (
            detect_buyer_product_switch,
        )

        assert detect_buyer_product_switch(self._state(), text) is None, text

    def test_needs_a_comparable_current_product_and_an_active_quantity_tunnel(self):
        from ladini.graphs.agents.market_coach.interpreter.product_switch import (
            detect_buyer_product_switch,
        )
        from tests.conftest import make_state

        no_pending = make_state(current_goal="BUYER_ADD_TO_CART", transaction_payload={"product": "poulets"})
        assert detect_buyer_product_switch(no_pending, "je veux acheter du lait") is None
        st = self._state()
        st["transaction_payload"] = {}
        assert detect_buyer_product_switch(st, "je veux acheter du lait") is None
        st = self._state()
        st["current_goal"] = "SALES_PUBLISH_PRODUCT"
        assert detect_buyer_product_switch(st, "je veux acheter du lait") is None
