"""B10 (2026-10-02) — une confirmation après un panier PRÊT ne doit jamais être absorbée par un ancien slot.

Incident prod (après #72) : dernier article ajouté (tier + nombre de paquets), « 🛒 Votre panier actuel …
Répondez *précommander* », « OKAY » -> « Je comprends que vous confirmez, merci. » puis « Quelle quantité
souhaitez-vous ? » / « On continue : Ajout d'un produit au panier ».

Cause : au tour « 3 » (nombre de paquets), `validator` réclamait `payload.quantity` alors que la valeur est
portée par l'action structurée SET_PACKAGE_COUNT -> `ENTER_FIELD(quantity)` + `missing_fields` posés ;
`cart_management` ajoutait bien l'article mais ne les consommait pas. Le raccourci de #72 exigeait
`not locked_goal` : le slot périmé (goal BUYER_ADD_TO_CART, WAITING_INPUT) reprenait la main.

Replays sur le VRAI graphe compilé ; seuls le LLM et le MCP sont doublés.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

import pytest
from langgraph.checkpoint.memory import MemorySaver

from ladini.graphs.agents.market_coach.core.graph_builder import build_graph
from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    set_pending_interaction,
)
from ladini.graphs.agents.market_coach.domain.selection_actions import (
    build_selection_context,
)
from tests.integration.test_buyer_deterministic_product_switch_e2e import (
    _PHONE,
    _offer,
    run,
)
from tests.integration.test_buyer_preorder_confirmation_e2e import (  # noqa: F401  (fixtures autouse)
    CartLLM,
    _pin_cod_checkout,
    _PreorderRt,
    _role_hints,
)

_ALIASES = ["okay", "OKAY", "ok", "oui", "confirmer", "valider", "précommander"]
_NOISE = ("get_account_status", "get_prohibited_terms")
_RECURRING = [{"recurring_need_id": "r1", "product": "oignon", "matched_quantity": 50}]
_BAD = ("Quelle quantité", "On continue", "j'ai juste besoin")


def _tiers():
    return [
        {"tier_id": "t5", "quantity": 5.0, "unit": "L", "price": 400.0, "packaging": "bidon",
         "base_unit_quantity": 5.0, "min_order_quantity": 1},
        {"tier_id": "t10", "quantity": 10.0, "unit": "L", "price": 700.0, "packaging": "bidon",
         "base_unit_quantity": 10.0, "min_order_quantity": 1},
    ]


def _offers():
    offers = [_offer("lait", "LITRE", f"Prod{i}", f"L{i}") for i in range(7)]
    offers[5]["pricing_tiers"] = _tiers()  # producteur n°6
    offers.append(_offer("poulets", "UNITE", "Pou", "P0"))
    return offers


class _Conv:
    def __init__(self, *, structured: Any = None, recurring=None, llm: bool = True) -> None:
        self.llm = CartLLM()
        if structured is not None:
            self.llm.structured = structured
        self.rt = _PreorderRt(self.llm if llm else None, _offers(), recurring if recurring is not None else _RECURRING, "ok")
        self.graph = build_graph("BUYER", mc_runtime=self.rt, checkpointer=MemorySaver())
        self.cfg = {"configurable": {"thread_id": "t"}}
        self.n = 0

    def say(self, text: str) -> Dict[str, Any]:
        self.n += 1
        self.rt.tool_log.clear()
        run(self.graph.ainvoke(
            {"user_query": text, "normalized_text": text, "user_phone": _PHONE, "user_role": "BUYER",
             "message_sid": f"m{self.n}"}, self.cfg))
        return self.state()

    def state(self) -> Dict[str, Any]:
        return self.graph.get_state(self.cfg).values

    def inject(self, values: Dict[str, Any]) -> None:
        self.graph.update_state(self.cfg, values)

    def tools(self) -> List[str]:
        return [t for t, _ in self.rt.tool_log if t not in _NOISE]

    def count(self, tool: str) -> int:
        return self.rt.all_calls.count(tool)

    def build_ready_cart(self) -> Dict[str, Any]:
        """lait -> producteur 6 -> palier 1 -> 3 paquets (le scénario prod)."""
        for t in ("je veux acheter du lait", "6", "1"):
            self.say(t)
        st = self.say("3")
        assert len(st["active_cart"]) == 1 and "Répondez *précommander* pour valider" in st["final_response"]
        return st


_PARSERS = {
    "sane": None,
    "unknown": {"disposition": "UNKNOWN", "confidence": 0.2},
    "invalid_json": "{not json",
    "exception": TimeoutError("llm timeout"),
}


@pytest.fixture(params=list(_PARSERS), ids=list(_PARSERS))
def conv_factory(request):
    return lambda **kw: _Conv(structured=_PARSERS[request.param], **kw)


class TestReadyCartStateAfterTheLastAdd:
    def test_no_slot_survives_the_add_to_cart(self):
        c = _Conv()
        st = c.build_ready_cart()
        # état exact après affichage du panier (plus aucun reliquat du slot « nombre de paquets »)
        assert not st.get("current_goal") and st["status"] == "COMPLETED"
        assert (st.get("preorder_workflow") or {}).get("phase") == "CART"
        assert not (st.get("pending_interaction") or {}).get("kind")
        sel = build_selection_context(st)
        assert sel is None or sel.expected_action is None
        assert not st.get("missing_fields") and not st.get("expected_candidates")
        assert not (st.get("vendor_selection_context") or {}).get("chosen_vendor")
        tctx = st.get("tier_selection_context")
        assert not tctx or tctx.get("__reset__")
        assert not (st["transaction_payload"].get("product") or st["transaction_payload"].get("package_count"))


class TestTheProdScenario:
    @pytest.mark.parametrize("alias", _ALIASES)
    def test_every_alias_enters_the_preorder_flow_never_the_quantity_slot(self, conv_factory, alias):
        c = conv_factory()
        c.build_ready_cart()
        st = c.say(alias)
        assert st["current_goal"] == "BUYER_PREORDER_INIT"
        assert "Récapitulatif de votre précommande" in st["final_response"]
        assert not any(b in st["final_response"] for b in _BAD), st["final_response"]
        assert c.tools() == ["create_preorder_draft"]
        assert "accept_match_proposal" not in c.rt.all_calls  # #72 : jamais le flux récurrent
        assert len(st["active_cart"]) == 1

    def test_all_aliases_produce_the_same_path(self):
        seen = set()
        for alias in _ALIASES:
            c = _Conv()
            c.build_ready_cart()
            st = c.say(alias)
            seen.add((st["current_goal"], (st.get("pending_interaction") or {}).get("kind"),
                      (st.get("preorder_workflow") or {}).get("phase"), tuple(c.tools()), st["final_response"]))
        assert len(seen) == 1, seen

    def test_without_an_llm(self):
        c = _Conv(llm=False)
        c.build_ready_cart()
        st = c.say("okay")
        assert st["current_goal"] == "BUYER_PREORDER_INIT" and "Récapitulatif" in st["final_response"]


class TestStaleSlotIsIgnored:
    @pytest.mark.parametrize("kind,field", [
        (InteractionKind.ENTER_QUANTITY, "quantity"),
        (InteractionKind.ENTER_PACKAGE_COUNT, "package_count"),
        (InteractionKind.ENTER_FIELD, "quantity"),
    ])
    def test_stale_quantity_slot_with_a_ready_cart(self, kind, field, caplog):
        caplog.set_level(logging.INFO)
        c = _Conv()
        c.build_ready_cart()
        c.inject({"current_goal": "BUYER_ADD_TO_CART", "status": "WAITING_INPUT",
                  "missing_fields": ["quantity"], **set_pending_interaction(kind, field_name=field)})
        st = c.say("okay")
        assert st["current_goal"] == "BUYER_PREORDER_INIT" and "Récapitulatif" in st["final_response"]
        assert not any(b in st["final_response"] for b in _BAD)
        assert "accept_match_proposal" not in c.rt.all_calls
        assert "BUYER_CART_READY_CONFIRMATION_ROUTED" in caplog.text
        assert "stale_slot_ignored=True" in caplog.text and "goal_after=BUYER_PREORDER_INIT" in caplog.text

    def test_stale_add_to_cart_goal_alone(self):
        c = _Conv()
        c.build_ready_cart()
        c.inject({"current_goal": "BUYER_ADD_TO_CART", "status": "WAITING_INPUT"})
        st = c.say("okay")
        assert st["current_goal"] == "BUYER_PREORDER_INIT"

    def test_stale_vendor_context_of_the_line_that_is_already_in_the_cart(self):
        c = _Conv()
        st = c.build_ready_cart()
        last = st["active_cart"][-1]
        c.inject({"current_goal": "BUYER_ADD_TO_CART", "status": "WAITING_INPUT",
                  "vendor_selection_context": {"product": "lait", "vendors": [], "chosen_vendor": {
                      "name": "lait", "product_id": last["product_id"], "vendor_name": "Prod5", "unit": "LITRE"}},
                  **set_pending_interaction(InteractionKind.ENTER_QUANTITY, field_name="quantity")})
        assert build_selection_context(c.state()).expected_action is not None  # SET_QUANTITY dérivé
        st = c.say("okay")
        assert st["current_goal"] == "BUYER_PREORDER_INIT" and "Récapitulatif" in st["final_response"]

    def test_stale_slot_plus_a_stale_recurring_digest_never_accepts_recurring(self):
        c = _Conv(recurring=_RECURRING)
        c.build_ready_cart()
        c.inject({"current_goal": "BUYER_ADD_TO_CART", "status": "WAITING_INPUT",
                  **set_pending_interaction(InteractionKind.ENTER_QUANTITY, field_name="quantity")})
        st = c.say("okay")
        assert st["current_goal"] == "BUYER_PREORDER_INIT"
        assert not {"accept_match_proposal", "list_my_recurring_needs"} & set(c.rt.all_calls)


class TestRealSlotsAreNeverSkipped:
    def test_real_package_count_slot_with_okay(self):
        c = _Conv()
        for t in ("je veux acheter du lait", "6", "1"):  # palier choisi -> « Combien de … ? » : slot RÉEL
            st = c.say(t)
        assert "Combien de" in st["final_response"]
        c.rt.all_calls.clear()
        st = c.say("okay")
        assert "create_preorder_draft" not in c.rt.all_calls and "accept_match_proposal" not in c.rt.all_calls
        assert st["current_goal"] != "BUYER_PREORDER_INIT" and st["active_cart"] == []

    def test_real_quantity_slot_with_okay(self):
        c = _Conv()
        st = c.say("je veux acheter des poulets")  # produit trouvé, « Quelle quantité ? » : slot RÉEL
        assert "Quelle quantité" in st["final_response"]
        c.rt.all_calls.clear()
        st = c.say("okay")
        assert "create_preorder_draft" not in c.rt.all_calls
        assert st["current_goal"] != "BUYER_PREORDER_INIT" and st["active_cart"] == []

    def test_real_slot_for_a_SECOND_line_while_the_cart_is_not_empty(self):
        c = _Conv()
        c.build_ready_cart()
        st = c.say("je veux acheter des poulets")  # nouvel article en cours : le panier N'EST PAS prêt
        assert "Quelle quantité" in st["final_response"] and len(st["active_cart"]) == 1
        c.rt.all_calls.clear()
        st = c.say("okay")
        assert "create_preorder_draft" not in c.rt.all_calls and "accept_match_proposal" not in c.rt.all_calls
        assert len(st["active_cart"]) == 1  # l'article incomplet n'est pas sauté, le panier n'est pas touché

    def test_empty_cart_okay_precommits_nothing(self, conv_factory):
        c = conv_factory(recurring=[])
        st = c.say("okay")
        assert not {"create_preorder_draft", "confirm_preorder_draft", "accept_match_proposal"} & set(c.rt.all_calls)
        assert "Précommande confirmée" not in st["final_response"]


class TestIdempotence:
    def test_repeated_okay_never_resets_the_cart_nor_duplicates_the_draft(self):
        c = _Conv()
        c.build_ready_cart()
        for _ in range(3):
            st = c.say("okay")
            assert not any(b in st["final_response"] for b in _BAD), st["final_response"]
        assert c.count("create_preorder_draft") == 1
        assert c.count("confirm_preorder_draft") <= 1
        assert "accept_match_proposal" not in c.rt.all_calls

    def test_precommander_then_okay_then_okay(self):
        c = _Conv()
        c.build_ready_cart()
        for t in ("précommander", "okay", "okay"):
            c.say(t)
        assert c.count("create_preorder_draft") == 1 and c.count("confirm_preorder_draft") <= 1


class TestReadyPredicate:
    @staticmethod
    def _state(**kw):
        from tests.conftest import make_state

        base = dict(preorder_workflow={"phase": "CART"}, active_cart=[{"product_id": "P1", "name": "lait"}])
        base.update(kw)
        return make_state(**base)

    def test_truth_table(self):
        from ladini.graphs.agents.market_coach.interpreter.routing import (
            _cart_ready_for_preorder as ready,
        )

        assert ready(self._state())
        assert not ready(self._state(active_cart=[]))
        assert not ready(self._state(preorder_workflow={"phase": "PREORDER_DRAFTED"}))
        assert not ready(self._state(current_goal="BUYER_LIST_ORDERS"))
        # slot quantité périmé (aucun contexte) : prêt ; slot RÉEL (contexte vendeur d'un autre produit) : non
        stale = self._state(current_goal="BUYER_ADD_TO_CART",
                            **set_pending_interaction(InteractionKind.ENTER_QUANTITY, field_name="quantity"))
        assert ready(stale)
        real = self._state(current_goal="BUYER_ADD_TO_CART",
                           vendor_selection_context={"product": "poulets", "vendors": [],
                                                     "chosen_vendor": {"name": "poulets", "product_id": "P9"}},
                           **set_pending_interaction(InteractionKind.ENTER_QUANTITY, field_name="quantity"))
        assert not ready(real)
        same = self._state(current_goal="BUYER_ADD_TO_CART",
                           vendor_selection_context={"product": "lait", "vendors": [],
                                                     "chosen_vendor": {"name": "lait", "product_id": "P1"}},
                           **set_pending_interaction(InteractionKind.ENTER_QUANTITY, field_name="quantity"))
        assert ready(same)  # le produit en cours EST déjà la dernière ligne : reliquat
        menu = self._state(current_goal="BUYER_ADD_TO_CART",
                           **set_pending_interaction(InteractionKind.SELECTION_MENU, context_ref="ui_menu"))
        assert not ready(menu)
        confirm = self._state(**set_pending_interaction(InteractionKind.CONFIRM_ACTION, context_ref="confirmation"))
        assert not ready(confirm)
