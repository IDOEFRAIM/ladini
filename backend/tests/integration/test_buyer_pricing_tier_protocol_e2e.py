"""B8 (2026-10-02) — protocole conditionnement (palier) / nombre de paquets + sûreté des dimensions.

Incident prod : après « producteur 4 » (2 conditionnements : bidon 5 L / 400, bidon 10 L / 700), le texte
disait « Lequel voulez-vous ? (le nombre que vous avez donné comptera comme le nombre de paquets… »
(deux étapes mélangées) et « 5 » / « 50 » ré-affichaient le même menu sans dire que seuls 1 ou 2 sont
valides. Protocole strict :

  SELECT_PRODUCER    + « 2 »  -> producteur n°2
  ENTER_QUANTITY     + « 2 »  -> quantité 2          (« producteur 4 » -> producteur n°4)
  SELECT_PRICING_TIER+ « 2 »  -> conditionnement n°2 (hors plage = INVALIDE, aucune mutation)
  ENTER_PACKAGE_COUNT+ « 2 »  -> 2 paquets

Replays sur le VRAI graphe compilé ; seuls le LLM et le MCP sont doublés (harnais de B4).
"""
from __future__ import annotations

import pytest

from ladini.domain.quantity_unit import are_units_compatible, convert_quantity
from tests.integration.test_buyer_deterministic_product_switch_e2e import (
    _HOSTILE,
    _POULETS,
    _Conv,
    _offer,
    _pending,
    _Rt,
)

_AMBIGUOUS = "comptera comme le nombre de paquets"


def _tiers(suffix=""):
    return [
        {"tier_id": f"t5{suffix}", "quantity": 5.0, "unit": "L", "price": 400.0, "packaging": "bidon",
         "base_unit_quantity": 5.0, "min_order_quantity": 1},
        {"tier_id": f"t10{suffix}", "quantity": 10.0, "unit": "L", "price": 700.0, "packaging": "bidon",
         "base_unit_quantity": 10.0, "min_order_quantity": 1},
    ]


def _lait7(tiered=(3,)):
    offers = [_offer("lait", "LITRE", f"Producteur{i}", f"L{i}") for i in range(7)]
    for i in tiered:
        offers[i]["pricing_tiers"] = _tiers(f"_{i}")
    return offers


class _NoLLM(_Conv):
    """Même graphe, mais SANS LLM : la fast-path numérique historique est seule aux commandes."""

    def __init__(self, offers):
        super().__init__(offers)
        from langgraph.checkpoint.memory import MemorySaver

        from ladini.graphs.agents.market_coach.core.graph_builder import build_graph

        self.rt = _Rt(None, offers)
        self.graph = build_graph("BUYER", mc_runtime=self.rt, checkpointer=MemorySaver())


_PARSERS = {
    "unknown": lambda offers: _Conv(offers, _HOSTILE["unknown"]),
    "invalid_json": lambda offers: _Conv(offers, _HOSTILE["invalid_json"]),
    "exception": lambda offers: _Conv(offers, _HOSTILE["exception"]),
    "no_llm": lambda offers: _NoLLM(offers),
}


def _vendor(st):
    return ((st.get("vendor_selection_context") or {}).get("chosen_vendor") or {}).get("vendor_name")


def _cart(st):
    return [(x["quantity"], x.get("base_unit_quantity"), x["line_total"], x["vendor_name"]) for x in st["active_cart"]]


def _at_tier_menu(factory, offers=None):
    c = factory(offers or _lait7())
    st = c.say("je veux acheter du lait")
    assert len(st["expected_candidates"]) == 7
    st = c.say("2")  # SELECT_PRODUCER + « 2 » -> producteur n°2
    assert _vendor(st) == "Producteur1" and "ENTER_QUANTITY" in _pending(st)
    st = c.say("producteur 4")  # ENTER_QUANTITY + commande explicite -> producteur n°4 (B7, inchangé)
    assert _vendor(st) == "Producteur3"
    return c, st


@pytest.fixture(params=list(_PARSERS), ids=list(_PARSERS))
def factory(request):
    return _PARSERS[request.param]


class TestProdReplay:
    def test_state_text_and_the_two_separate_steps(self, factory):
        from ladini.graphs.agents.market_coach.domain.selection_actions import (
            ActionType,
            build_selection_context,
        )

        c, st = _at_tier_menu(factory)
        # état exact après « producteur 4 »
        assert st["current_goal"] == "BUYER_ADD_TO_CART" and "SELECTION_MENU" in _pending(st)
        assert build_selection_context(st).expected_action == ActionType.SELECT_PRICING_TIER
        assert [t["tier_id"] for t in st["tier_selection_context"]["tiers"]] == ["t5_3", "t10_3"]
        assert st["transaction_payload"].get("product") == "lait"
        assert not st["transaction_payload"].get("quantity") and not st["transaction_payload"].get("package_count")
        # ÉTAPE 1 : choisir le conditionnement — aucun mélange avec le nombre de paquets
        text = st["final_response"]
        assert _AMBIGUOUS not in text and "Lequel voulez-vous" not in text
        assert "Quel conditionnement souhaitez-vous ?" in text and "Répondez 1 ou 2." in text

        st = c.say("1")  # SELECT_PRICING_TIER + « 1 » -> conditionnement n°1
        assert "ENTER_QUANTITY" in _pending(st)
        assert build_selection_context(st).expected_action == ActionType.SET_PACKAGE_COUNT
        assert st["tier_selection_context"]["resolved_tier_id"] == "t5_3"
        assert "Vous avez choisi" in st["final_response"] and "Combien de" in st["final_response"]
        assert st["expected_candidates"] == [], "anciens candidats neutralisés"
        assert st["active_cart"] == []

        st = c.say("5")  # ÉTAPE 2 : ENTER_PACKAGE_COUNT + « 5 » -> 5 paquets
        assert _cart(st) == [(5, 25.0, 2000.0, "Producteur3")]  # 5 bidons × 5 L = 25 L ; 5 × 400 FCFA
        assert [(k["product_id"], k["quantity"]) for k in c.rt.of("validate_stock_availability_atomic")] == [("L3", 25.0)]

    def test_second_tier(self, factory):
        c, _ = _at_tier_menu(factory)
        st = c.say("2")
        assert st["tier_selection_context"]["resolved_tier_id"] == "t10_3"
        st = c.say("5")
        assert _cart(st) == [(5, 50.0, 3500.0, "Producteur3")]  # 5 × 10 L = 50 L ; 5 × 700

    @pytest.mark.parametrize("count", ["1", "2", "5"])
    def test_package_count_digits_are_packages_never_a_tier_index(self, factory, count):
        c, _ = _at_tier_menu(factory)
        c.say("1")
        c.rt.tool_log.clear()
        st = c.say(count)  # y compris « 2 » : 2 paquets, jamais le conditionnement n°2
        assert _cart(st) == [(int(count), 5.0 * int(count), 400.0 * int(count), "Producteur3")]
        assert st["active_cart"][0]["tier_id"] == "t5_3"


class TestInvalidTierIndex:
    @pytest.mark.parametrize("digit", ["3", "5", "50", "0"])
    def test_out_of_range_is_invalid_and_never_mutates(self, factory, digit):
        c, before = _at_tier_menu(factory)
        c.rt.tool_log.clear()
        st = c.say(digit)
        text = st["final_response"]
        assert "Je n'ai que 2 conditionnements disponibles" in text and "Répondez 1 ou 2." in text
        assert "*lait* propose plusieurs conditionnements" in text and "5.0 L (bidon)" in text and "10.0 L (bidon)" in text
        # aucune mutation
        assert st["active_cart"] == [] and c.rt.of("validate_stock_availability_atomic") == []
        tp = st["transaction_payload"]
        assert not tp.get("quantity") and not tp.get("package_count") and not tp.get("selection_index")
        assert _vendor(st) == "Producteur3" and "SELECTION_MENU" in _pending(st)
        assert [t["tier_id"] for t in st["tier_selection_context"]["tiers"]] == ["t5_3", "t10_3"]
        assert not st["tier_selection_context"].get("resolved_tier_id")
        assert int(st.get("retry_count") or 0) == 0
        assert not [t for t, _ in c.rt.tool_log if t in ("add_to_cart", "create_agent_action", "send_notification")]
        # …et la saisie valide qui suit fonctionne
        st = c.say("1")
        assert st["tier_selection_context"]["resolved_tier_id"] == "t5_3"
        st = c.say("5")
        assert _cart(st) == [(5, 25.0, 2000.0, "Producteur3")]


class TestExplicitCommandsAcrossTheTierTunnel:
    def test_enter_quantity_digit_is_still_a_quantity_and_command_still_a_producer(self, factory):
        # B7 préservé : ENTER_QUANTITY + « 4 » = quantité 4 ; « producteur 4 » = producteur n°4.
        c = factory(_lait7(tiered=()))
        c.say("je veux acheter du lait")
        c.say("2")
        st = c.say("4")
        assert [(q, v) for q, _, _, v in _cart(st)] == [(4, "Producteur1")]
        c2 = factory(_lait7(tiered=(3,)))
        c2.say("je veux acheter du lait")
        c2.say("2")
        st = c2.say("producteur 4")
        assert _vendor(st) == "Producteur3" and st["active_cart"] == []

    def test_producer_command_during_the_tier_menu_to_a_flat_producer(self, factory):
        c, _ = _at_tier_menu(factory)
        c.rt.tool_log.clear()
        st = c.say("producteur 3")
        assert _vendor(st) == "Producteur2" and "ENTER_QUANTITY" in _pending(st)
        assert not (st.get("tier_selection_context") or {}).get("tiers"), "paliers périmés du producteur précédent"
        assert "Quelle quantité" in st["final_response"] and "conditionnements" not in st["final_response"]
        assert c.rt.of("search_products") == []
        st = c.say("3")
        assert [(q, v) for q, _, _, v in _cart(st)] == [(3, "Producteur2")]

    def test_producer_command_to_another_tiered_producer_recomputes_its_tiers(self, factory):
        c, _ = _at_tier_menu(factory, _lait7(tiered=(3, 5)))
        st = c.say("producteur 6")
        assert _vendor(st) == "Producteur5"
        assert [t["tier_id"] for t in st["tier_selection_context"]["tiers"]] == ["t5_5", "t10_5"]
        c.say("1")
        st = c.say("2")
        assert _cart(st) == [(2, 10.0, 800.0, "Producteur5")]
        assert st["active_cart"][0]["tier_id"] == "t5_5"

    def test_producer_command_also_works_once_the_tier_is_resolved(self, factory):
        c, _ = _at_tier_menu(factory)
        c.say("1")
        st = c.say("producteur 3")
        assert _vendor(st) == "Producteur2" and st["active_cart"] == []
        assert not (st.get("tier_selection_context") or {}).get("resolved_tier_id")

    def test_change_producer_without_number_reshows_the_live_menu(self, factory):
        c, _ = _at_tier_menu(factory)
        c.rt.tool_log.clear()
        st = c.say("changer producteur")
        assert c.rt.of("search_products") == []
        assert len(st["expected_candidates"]) == 7 and "*7.*" in st["final_response"]
        assert not (st.get("tier_selection_context") or {}).get("tiers")


class TestSameAndDifferentProductDuringTierMenu:
    def test_same_product_keeps_vendor_tiers_and_reminds_1_or_2(self, factory):
        c, _ = _at_tier_menu(factory)
        c.rt.tool_log.clear()
        st = c.say("je veux acheter du lait")
        assert c.rt.of("search_products") == []
        assert _vendor(st) == "Producteur3" and "SELECTION_MENU" in _pending(st)
        assert [t["tier_id"] for t in st["tier_selection_context"]["tiers"]] == ["t5_3", "t10_3"]
        assert "Répondez 1 ou 2." in st["final_response"] and "Producteurs disponibles" not in st["final_response"]
        assert int(st.get("retry_count") or 0) == 0

    def test_different_product_switches_and_clears_the_tier_context(self, factory):
        c, _ = _at_tier_menu(factory, _lait7() + [_POULETS])
        c.rt.tool_log.clear()
        st = c.say("je veux acheter des poulets")
        assert any(k.get("product") == "poulets" for k in c.rt.of("search_products"))
        assert "poulets" in st["final_response"] and _vendor(st) != "Producteur3"
        assert not (st.get("tier_selection_context") or {}).get("tiers")
        assert st["active_cart"] == []


class TestNumericProtocolUnit:
    @staticmethod
    def _state(resolved=False):
        from ladini.graphs.agents.market_coach.core.pending_interaction import (
            InteractionKind,
            set_pending_interaction,
        )
        from tests.conftest import make_state

        chosen = {"name": "lait", "vendor_name": "P3", "producer_id": "p3", "pricing_tiers": _tiers()}
        tctx = {"product_name": "lait", "tiers": _tiers()}
        if resolved:
            tctx["resolved_tier_id"] = "t5"
        return make_state(
            current_goal="BUYER_ADD_TO_CART",
            transaction_payload={"product": "lait"},
            expected_candidates=["P0", "P1", "P2"],  # candidats périmés d'un ancien menu producteur
            vendor_selection_context={
                "product": "lait", "chosen_vendor": chosen,
                "vendors": [chosen, {"name": "lait", "vendor_name": "P1", "producer_id": "p1"}],
                **({"resolved_tier_id": "t5"} if resolved else {}),
            },
            tier_selection_context=tctx,
            **set_pending_interaction(
                InteractionKind.ENTER_QUANTITY if resolved else InteractionKind.SELECTION_MENU,
                **({"field_name": "quantity"} if resolved else {}),
            ),
        )

    @pytest.mark.parametrize("text", ["3", "5", "50", "0"])
    def test_tier_menu_out_of_range_is_invalid(self, text):
        from ladini.graphs.agents.market_coach.interpreter.numeric_protocol import (
            interpret_buyer_numeric_protocol,
        )

        got = interpret_buyer_numeric_protocol(self._state(), text)
        assert got is not None and got["raw_analysis"]["path"] == "deterministic_invalid_tier_index"
        assert got["extracted_entities"] == {}  # jamais une quantité / un nombre de paquets / un palier

    @pytest.mark.parametrize("text", ["1", "2"])
    def test_tier_menu_valid_index_is_left_to_the_deterministic_fast_path(self, text):
        from ladini.graphs.agents.market_coach.interpreter.numeric_protocol import (
            interpret_buyer_numeric_protocol,
        )

        assert interpret_buyer_numeric_protocol(self._state(), text) is None

    @pytest.mark.parametrize("text,n", [("1", 1.0), ("2", 2.0), ("5", 5.0), ("50", 50.0)])
    def test_resolved_tier_digit_is_a_package_count_even_with_stale_candidates(self, text, n):
        from ladini.graphs.agents.market_coach.interpreter.numeric_protocol import (
            interpret_buyer_numeric_protocol,
        )

        ents = interpret_buyer_numeric_protocol(self._state(resolved=True), text)["extracted_entities"]
        assert ents["agent_action"] == "SET_PACKAGE_COUNT" and ents["action_package_count"] == n
        assert "action_pricing_tier_id" not in ents and "selection_index" not in ents

    def test_a_written_unit_is_a_total_quantity_not_a_package_count(self):
        from ladini.graphs.agents.market_coach.interpreter.numeric_protocol import (
            interpret_buyer_numeric_protocol,
        )

        assert interpret_buyer_numeric_protocol(self._state(resolved=True), "30 L") is None

    def test_producer_command_beats_any_bare_number_in_every_tier_state(self):
        from ladini.graphs.agents.market_coach.interpreter.numeric_protocol import (
            interpret_buyer_numeric_protocol,
        )

        for st in (self._state(), self._state(resolved=True)):
            got = interpret_buyer_numeric_protocol(st, "producteur 2")
            assert got["interpreted_event"] == "SELECTION" and got["extracted_entities"] == {"selection_index": 2}


class TestMassVolumeSafety:
    """MASS ↔ VOLUME n'est JAMAIS converti implicitement ; MASS↔MASS et VOLUME↔VOLUME le sont."""

    @pytest.mark.parametrize("src,dst", [("KG", "LITRE"), ("GRAMME", "MILLILITRE"), ("TONNE", "LITRE"),
                                         ("kg", "L"), ("g", "ml"), ("LITRE", "KG"), ("UNITE", "LITRE")])
    def test_no_inter_family_conversion(self, src, dst):
        assert convert_quantity(10, src, dst) is None
        assert not are_units_compatible(src, dst)

    @pytest.mark.parametrize("src,dst,q,expected", [("LITRE", "MILLILITRE", 2, 2000), ("MILLILITRE", "LITRE", 500, 0.5),
                                                    ("KG", "TONNE", 1000, 1), ("GRAMME", "KG", 500, 0.5)])
    def test_same_family_conversion_is_exact(self, src, dst, q, expected):
        assert convert_quantity(q, src, dst) == pytest.approx(expected)

    @pytest.mark.parametrize("text", ["10 kg", "2 g", "3 tonnes", "3 unités"])
    def test_explicit_foreign_unit_is_refused_never_applied(self, factory, text):
        c = factory([_offer("lait", "LITRE", "Solo", "L0")])
        c.say("je veux acheter du lait")
        st = c.say(text)
        assert st["active_cart"] == [], st["final_response"]
        assert "est vendu en" in st["final_response"] and "LITRE" in st["final_response"]
        assert c.rt.of("validate_stock_availability_atomic") == []

    @pytest.mark.parametrize("text,qty", [("10 L", 10.0), ("500 ml", 0.5), ("5 litres", 5.0)])
    def test_same_dimension_units_are_accepted(self, factory, text, qty):
        c = factory([_offer("lait", "LITRE", "Solo", "L0")])
        c.say("je veux acheter du lait")
        st = c.say(text)
        assert [(x["quantity"], x["unit"]) for x in st["active_cart"]] == [(qty, "LITRE")]

    def test_assumed_default_unit_is_not_a_user_unit_but_a_written_one_always_is(self, factory):
        # « 5 » : aucune unité écrite — l'unité de l'offre (LITRE) gouverne, jamais un « KG » supposé.
        c = factory([_offer("lait", "LITRE", "Solo", "L0")])
        c.say("je veux acheter du lait")
        st = c.say("5")
        assert [(x["quantity"], x["unit"]) for x in st["active_cart"]] == [(5.0, "LITRE")]
        # « 5 kg » : KG est écrit (même s'il coïncide avec le défaut supposé) -> explicite -> refusé.
        c2 = factory([_offer("lait", "LITRE", "Solo", "L0")])
        c2.say("je veux acheter du lait")
        st = c2.say("5 kg")
        assert st["active_cart"] == [] and "est vendu en" in st["final_response"]
