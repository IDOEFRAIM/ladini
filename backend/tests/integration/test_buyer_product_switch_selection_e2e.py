"""B3 (2026-10-02) — changement de produit pendant ENTER_QUANTITY : jamais de menu vide.

Incident réel : « je veux acheter des poulets » → « Quelle quantité ? » → « je veux acheter du
lait » répondait « Je comprends que vous souhaitez acheter du lait. Veuillez choisir une option : »
SANS aucune option, puis « Répondez uniquement par le numéro ».

Cause : (1) le micro-prompt STRUCTURED_ACTION hallucine `ACTION quantity=1` ; la garde de
provenance (B2) le rejetait en UNKNOWN au lieu de reclassifier ; (2) sur ce tour
UNKNOWN/`recover_active_tunnel`, `interpreter/strategy.py` forçait SELECTION_MENU pour tout
pending ENTER_QUANTITY alors que `cart_management` n'avait rien produit →
`render_selection_menu` rendait un menu numéroté vide.

Replays sur le VRAI graphe compilé (`build_graph`) ; seuls le LLM et le MCP sont doublés.
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
_EMPTY_MENU = "Répondez uniquement par le numéro"
_EMPTY_MENU_HEAD = "Veuillez choisir une option"


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


class FakeLLM:
    """Répond selon la FAMILLE de prompt (structured / active_slot / new_task)."""

    def __init__(self, structured: Dict[str, Any]) -> None:
        self.structured = structured

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
        prod = next((p for p in ("lait", "tomates", "poulets") if p[:-1] in text), None)
        if "tunnel d'achat" in sysm:
            out = self.structured
        elif "À L'INTÉRIEUR d'une transaction" in sysm:
            out = {"disposition": "DEVIATION", "extracted_entities": {}, "confidence": 0.9}
        elif "CATALOGUE OFFICIEL" in sysm:
            out = {
                "disposition": "NEW_TASK",
                "intent": "BUYER_REQUEST",
                "confidence": 0.95,
                "entities": {"product": prod} if prod else {},
            }
        else:
            out = {"text": "ok"}
        return _Comp(json.dumps(out), kw.get("model"))


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
        "id": pid or f"P-{name}-{vendor}",
        "product_id": pid or f"P-{name}-{vendor}",
        "name": name,
        "price": 3500.0,
        "unit": unit,
        "vendor": {"name": vendor},
        "vendor_name": vendor,
        "producer_id": f"PR-{vendor}",
        "available_quantity": 50,
        "source_type": "DIRECT",
        "is_auction": False,
    }


_HALLUCINATED_QTY = {
    "disposition": "ACTION",
    "action": "SET_QUANTITY",
    "quantity": 1,
    "unit": "UNITE",
    "confidence": 0.95,
}
_DEVIATION = {"disposition": "DEVIATION", "confidence": 0.9}


class _Conv:
    def __init__(self, offers: List[Dict[str, Any]], structured: Dict[str, Any]) -> None:
        self.rt = _Rt(FakeLLM(structured), offers)
        self.graph = build_graph("BUYER", mc_runtime=self.rt, checkpointer=MemorySaver())
        self.cfg = {"configurable": {"thread_id": "t"}}
        self.n = 0

    def say(self, text: str) -> Dict[str, Any]:
        self.n += 1
        self.rt.tool_log.clear()
        run(
            self.graph.ainvoke(
                {
                    "user_query": text,
                    "normalized_text": text,
                    "user_phone": _PHONE,
                    "user_role": "BUYER",
                    "message_sid": f"m{self.n}",
                },
                self.cfg,
            )
        )
        return self.graph.get_state(self.cfg).values

    def searched(self, term: str) -> bool:
        return any(k.get("product") == term for k in self.rt.of("search_products"))


def _pending_kind(st: Dict[str, Any]) -> str:
    return str((st.get("pending_interaction") or {}).get("kind"))


def _live_vendor_products(st: Dict[str, Any]) -> set:
    vc = st.get("vendor_selection_context") or {}
    if vc.get("__reset__"):
        return set()
    names = {str(v.get("name")) for v in vc.get("vendors", [])}
    if vc.get("chosen_vendor"):
        names.add(str(vc["chosen_vendor"].get("name")))
    return names


def _no_empty_menu(text: str) -> None:
    assert _EMPTY_MENU not in text and _EMPTY_MENU_HEAD not in text, text


@pytest.mark.parametrize("structured", [_HALLUCINATED_QTY, _DEVIATION], ids=["hallucinated_qty", "deviation"])
class TestSwitchPoulets2Lait:
    def test_zero_results_is_no_results_never_a_selection(self, structured):
        c = _Conv([_offer("poulets", "UNITE")], structured)
        st = c.say("je veux acheter des poulets")
        assert "ENTER_QUANTITY" in _pending_kind(st)
        st = c.say("je veux acheter du lait")
        assert c.searched("lait")
        assert "Aucun produit disponible" in st["final_response"]
        _no_empty_menu(st["final_response"])
        assert _live_vendor_products(st) == set()
        assert st["active_cart"] == [] and c.rt.of("validate_stock_availability_atomic") == []

    def test_one_result_goes_straight_to_the_quantity_question(self, structured):
        c = _Conv([_offer("poulets", "UNITE"), _offer("lait", "LITRE", "Ferme0", "L0")], structured)
        c.say("je veux acheter des poulets")
        st = c.say("je veux acheter du lait")
        text = st["final_response"]
        assert "lait" in text and "Quelle quantité" in text
        _no_empty_menu(text)
        assert "ENTER_QUANTITY" in _pending_kind(st)
        assert _live_vendor_products(st) == {"lait"}, "contexte vendeur poulets recyclé"
        assert st["active_cart"] == [] and c.rt.of("validate_stock_availability_atomic") == []

    def test_several_results_show_a_real_menu_and_the_digit_resolves(self, structured):
        offers = [_offer("poulets", "UNITE")] + [_offer("lait", "LITRE", f"Ferme{i}", f"L{i}") for i in range(3)]
        c = _Conv(offers, structured)
        c.say("je veux acheter des poulets")
        st = c.say("je veux acheter du lait")
        for i in (1, 2, 3):
            assert f"*{i}.*" in st["final_response"], st["final_response"]
        assert len(st["expected_candidates"]) == 3
        assert "poulets" not in _live_vendor_products(st)
        st = c.say("2")
        assert "Ferme1" in st["final_response"] and "Quelle quantité" in st["final_response"]


class TestUnknownTurnNeverRendersAnEmptyMenu:
    def test_unclassifiable_reply_during_enter_quantity_reasks_the_slot(self):
        c = _Conv([_offer("poulets", "UNITE")], {"disposition": "UNKNOWN", "confidence": 0.3})
        c.say("je veux acheter des poulets")
        st = c.say("hmm euh")
        _no_empty_menu(st["final_response"])
        assert "ENTER_QUANTITY" in _pending_kind(st), "le slot en attente ne doit pas être détruit"
        assert st["active_cart"] == [] and c.rt.of("validate_stock_availability_atomic") == []


class TestBreakoutMultiple:
    def test_poulets_lait_tomates_never_accumulate_stale_context(self):
        offers = [
            _offer("poulets", "UNITE"),
            _offer("lait", "LITRE", "Ferme0", "L0"),
            _offer("tomates", "KG", "Ferme9", "T0"),
        ]
        c = _Conv(offers, _HALLUCINATED_QTY)
        c.say("je veux acheter des poulets")
        st = c.say("je veux acheter du lait")
        assert _live_vendor_products(st) == {"lait"}
        st = c.say("je veux acheter des tomates")
        assert _live_vendor_products(st) == {"tomates"}
        assert "tomates" in st["final_response"] and "Quelle quantité" in st["final_response"]
        assert st["transaction_payload"].get("product") == "tomates"
        assert st["active_cart"] == [] and c.rt.of("validate_stock_availability_atomic") == []


class TestUnitInvariants:
    def test_selection_menu_renderer_never_prints_an_empty_numbered_menu(self):
        from ladini.graphs.agents.market_coach.core.pending_interaction import (
            InteractionKind,
            set_pending_interaction,
        )
        from ladini.graphs.agents.market_coach.nodes.rendering.common import (
            RenderContext,
        )
        from ladini.graphs.agents.market_coach.nodes.rendering.menus import (
            render_selection_menu,
        )
        from tests.conftest import make_state

        state = make_state(
            current_goal="BUYER_ADD_TO_CART",
            interpreted_event="UNKNOWN",
            expected_candidates=[],
            **set_pending_interaction(InteractionKind.ENTER_QUANTITY, field_name="quantity"),
        )
        ctx = RenderContext(
            state=state, mc_runtime=StubRuntime(), strategy="SELECTION_MENU", status="WAITING_INPUT",
            goal="BUYER_ADD_TO_CART", salutation="", payload={},
        )
        out = run(render_selection_menu(ctx))
        _no_empty_menu(out["final_response"])

    def test_strategy_recover_on_cart_tunnel_without_output_is_recovery(self):
        from ladini.graphs.agents.market_coach.core.pending_interaction import (
            InteractionKind,
            set_pending_interaction,
        )
        from ladini.graphs.agents.market_coach.interpreter.strategy import (
            response_strategy,
        )
        from tests.conftest import make_state

        state = make_state(
            current_goal="BUYER_ADD_TO_CART",
            interpreted_event="UNKNOWN",
            status="WAITING_INPUT",
            cognitive_decision={"action": "recover_active_tunnel"},
            **set_pending_interaction(InteractionKind.ENTER_QUANTITY, field_name="quantity"),
        )
        assert run(response_strategy(state, StubRuntime()))["response_strategy"] == "RECOVERY"
        # Un flow panier qui a produit une réponse garde SELECTION_MENU (garde G-1 inchangée).
        state["final_response"] = "Quelle quantité ?"
        assert run(response_strategy(state, StubRuntime()))["response_strategy"] == "SELECTION_MENU"
