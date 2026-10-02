"""B7 (2026-10-02) — collision « numéro de quantité » / « numéro de producteur ».

Incident : après « 2 » (Gilbert-prod choisi, « Quelle quantité ? »), l'UI disait « Pour changer de
producteur, répondez avec le numéro correspondant (1 à 7) » alors que le nombre nu suivant doit être
une QUANTITÉ. Protocole final : le sens d'un nombre dépend du pending ACTIF.

  SELECT_PRODUCER + « 2 »              -> producteur n°2
  ENTER_QUANTITY  + « 2 », « 5 L »     -> quantité
  ENTER_QUANTITY  + « producteur 2 », « changer producteur » -> commande producteur explicite

Replays sur le VRAI graphe compilé ; seuls le LLM et le MCP sont doublés (harnais de B4).
"""
from __future__ import annotations

import pytest

from tests.integration.test_buyer_deterministic_product_switch_e2e import (
    _HOSTILE,
    _POULETS,
    _Conv,
    _pending,
)
from tests.integration.test_buyer_same_product_continuation_e2e import _LAIT7, _chosen

_QTY2 = {"disposition": "ACTION", "action": "SET_QUANTITY", "quantity": 2, "unit": None, "confidence": 0.95}
_DEVIATION = {"disposition": "DEVIATION", "confidence": 0.9}
_PARSERS = {"sane": _QTY2, "unknown": _HOSTILE["unknown"], "invalid_json": _HOSTILE["invalid_json"]}
_AMBIGUOUS_UI = "répondez avec le numéro"


def _cart(st):
    return [(x["quantity"], x["unit"], x["vendor_name"]) for x in st["active_cart"]]


def _at_quantity(parser=_DEVIATION, offers=None):
    c = _Conv(offers or _LAIT7, parser)
    st = c.say("je veux acheter du lait")
    assert len(st["expected_candidates"]) == 7 and "SELECTION_MENU" in _pending(st)
    st = c.say("2")  # SELECT_PRODUCER + « 2 » -> producteur n°2
    assert _chosen(st) == "Producteur1" and "ENTER_QUANTITY" in _pending(st)
    return c, st


class TestProdReplay:
    def test_full_prod_scenario(self):
        c, st = _at_quantity()
        assert _AMBIGUOUS_UI not in st["final_response"].lower()
        assert st["expected_candidates"] == [], "menu producteur résolu : candidats périmés neutralisés"

        c.rt.tool_log.clear()
        st = c.say("je veux acheter du lait")  # B6 : même produit
        assert c.rt.of("search_products") == [] and _chosen(st) == "Producteur1"
        assert "Quelle quantité" in st["final_response"]
        assert _AMBIGUOUS_UI not in st["final_response"].lower(), "l'UI promet encore « nombre = producteur »"
        assert "changer producteur" in st["final_response"] and "producteur 3" in st["final_response"]

        st = c.say("2")  # ENTER_QUANTITY + « 2 » -> quantité 2, PAS producteur n°2
        assert _cart(st) == [(2.0, "LITRE", "Producteur1")]
        assert [(k["product_id"], k["quantity"]) for k in c.rt.of("validate_stock_availability_atomic")] == [("L1", 2.0)]
        assert c.rt.of("search_products") == []


@pytest.mark.parametrize("parser", list(_PARSERS), ids=list(_PARSERS))
class TestBareNumberIsAlwaysAQuantity:
    @pytest.mark.parametrize("digit,qty", [("1", 1.0), ("2", 2.0), ("7", 7.0), ("10", 10.0), ("2,5", 2.5)])
    def test_in_and_out_of_the_candidate_range(self, parser, digit, qty):
        # 1, 2, 7 sont dans la plage des 7 producteurs historiques ; 10 est au-dessus.
        c, _ = _at_quantity(_PARSERS[parser])
        c.rt.tool_log.clear()
        st = c.say(digit)
        if parser == "sane" and digit != "2":
            pytest.skip("parser scripté fixe (toujours 2) : le déterminisme est prouvé par les 2 autres parsers")
        assert _cart(st) == [(qty, "LITRE", "Producteur1")], st["final_response"]
        assert _chosen(st) is None or _chosen(st) == "Producteur1"
        assert c.rt.of("search_products") == []

    @pytest.mark.parametrize("text,qty", [("2 L", 2.0), ("5 litres", 5.0)])
    def test_quantities_with_units(self, parser, text, qty):
        c, _ = _at_quantity(_PARSERS[parser])
        st = c.say(text)
        assert _cart(st) == [(qty, "LITRE", "Producteur1")]


class TestUnitsKeepTheirNormalHandling:
    def test_incompatible_unit_is_refused_by_the_cart_not_read_as_a_producer(self):
        c, _ = _at_quantity()
        st = c.say("3 unités")
        assert st["active_cart"] == [] and _chosen(st) == "Producteur1"
        assert "est vendu en" in st["final_response"] and "LITRE" in st["final_response"]

    def test_kg_goes_through_the_existing_unit_conversion_never_a_producer_index(self):
        c, _ = _at_quantity()
        c.rt.tool_log.clear()
        st = c.say("10 kg")
        assert c.rt.of("search_products") == []
        assert all(v == "Producteur1" for _, _, v in _cart(st))  # aucun changement de producteur


class TestExplicitProducerCommands:
    @pytest.mark.parametrize("cmd,vendor", [
        ("producteur 4", "Producteur3"),
        ("changer producteur 3", "Producteur2"),
        ("changer de producteur 6", "Producteur5"),
        ("je veux le producteur 5", "Producteur4"),
    ])
    def test_numbered_command_changes_the_producer_not_the_quantity(self, cmd, vendor):
        c, _ = _at_quantity()
        c.rt.tool_log.clear()
        st = c.say(cmd)
        assert _chosen(st) == vendor and "ENTER_QUANTITY" in _pending(st)
        assert st["transaction_payload"].get("product") == "lait"
        assert st["active_cart"] == [] and not st["transaction_payload"].get("quantity")
        assert c.rt.of("search_products") == [] and c.rt.of("validate_stock_availability_atomic") == []
        assert _AMBIGUOUS_UI not in st["final_response"].lower()
        # …et le nombre nu suivant est la quantité chez le NOUVEAU producteur
        st = c.say("5")
        assert [(q, v) for q, _, v in _cart(st)] == [(5.0, vendor)]

    @pytest.mark.parametrize("cmd", ["changer producteur", "changer de producteur"])
    def test_change_without_number_reshows_the_live_menu_without_a_new_search(self, cmd):
        c, _ = _at_quantity()
        c.rt.tool_log.clear()
        st = c.say(cmd)
        assert c.rt.of("search_products") == []
        assert len(st["expected_candidates"]) == 7 and "*7.*" in st["final_response"]
        assert "SELECTION_MENU" in _pending(st)
        st = c.say("3")  # SELECT_PRODUCER + « 3 » -> producteur n°3 (le protocole menu est intact)
        assert _chosen(st) == "Producteur2" and "ENTER_QUANTITY" in _pending(st)


class TestPriorityDuringEnterQuantity:
    def test_same_product_with_quantity(self):
        c, _ = _at_quantity()
        st = c.say("je veux acheter 5 L de lait")
        assert _cart(st) == [(5.0, "LITRE", "Producteur1")]

    def test_different_product_still_switches(self):
        c, _ = _at_quantity(offers=_LAIT7 + [_POULETS])
        c.rt.tool_log.clear()
        st = c.say("je veux acheter des poulets")
        assert any(k.get("product") == "poulets" for k in c.rt.of("search_products"))
        assert "poulets" in st["final_response"] and _chosen(st) != "Producteur1"

    def test_producer_menu_digit_keeps_selecting_a_producer(self):
        c = _Conv(_LAIT7, _DEVIATION)
        c.say("je veux acheter du lait")
        for digit, vendor in (("4", "Producteur3"), ):
            st = c.say(digit)
            assert _chosen(st) == vendor and st["active_cart"] == []


class TestNumericProtocolUnit:
    @staticmethod
    def _state(pending="ENTER_QUANTITY", stale=True, vendors=7):
        from ladini.graphs.agents.market_coach.core.pending_interaction import (
            InteractionKind,
            set_pending_interaction,
        )
        from tests.conftest import make_state

        return make_state(
            current_goal="BUYER_ADD_TO_CART",
            transaction_payload={"product": "lait"},
            expected_candidates=[f"P{i}" for i in range(7)] if stale else [],
            vendor_selection_context={
                "product": "lait",
                "vendors": [{"name": "lait", "vendor_name": f"P{i}", "producer_id": f"p{i}"} for i in range(vendors)],
                "chosen_vendor": {"name": "lait", "vendor_name": "P1"},
            },
            **set_pending_interaction(InteractionKind[pending], field_name="quantity"),
        )

    @pytest.mark.parametrize("text,qty", [("1", 1.0), ("2", 2.0), ("7", 7.0), ("10", 10.0), ("2,5", 2.5)])
    def test_bare_number_is_quantity_even_with_stale_candidates(self, text, qty):
        from ladini.graphs.agents.market_coach.interpreter.numeric_protocol import (
            interpret_buyer_numeric_protocol,
        )

        for stale in (True, False):
            got = interpret_buyer_numeric_protocol(self._state(stale=stale), text)
            assert got is not None and got["interpreted_event"] == "ANSWER", text
            ents = got["extracted_entities"]
            # contrat canonique STRUCTURED_ACTION/SET_QUANTITY (identique au micro-prompt)
            assert ents["agent_action"] == "SET_QUANTITY" and ents["action_quantity"] == qty
            assert "action_unit" not in ents  # aucune unité écrite : celle de l'offre fait foi
            assert "selection_index" not in ents and "action_producer_id" not in ents, "nombre nu ≠ index"

    def test_unit_is_only_forwarded_when_written(self):
        from ladini.graphs.agents.market_coach.interpreter.numeric_protocol import (
            interpret_buyer_numeric_protocol,
        )

        got = interpret_buyer_numeric_protocol(self._state(), "5 litres")
        assert got["extracted_entities"]["action_quantity"] == 5.0
        assert got["extracted_entities"]["action_unit"] == "LITRE"

    @pytest.mark.parametrize("text,idx", [("producteur 2", 2), ("changer producteur 2", 2), ("je veux le producteur 3", 3),
                                          ("producteur n°4", 4), ("vendeur 5", 5)])
    def test_explicit_command_is_a_producer_index(self, text, idx):
        from ladini.graphs.agents.market_coach.interpreter.numeric_protocol import (
            interpret_buyer_numeric_protocol,
        )

        got = interpret_buyer_numeric_protocol(self._state(), text)
        assert got is not None and got["interpreted_event"] == "SELECTION"
        assert got["extracted_entities"] == {"selection_index": idx}

    @pytest.mark.parametrize("text", ["producteur", "le producteur", "500 fcfa", "0", "2 et 5", "je veux acheter des poulets",
                                      "annuler", "oui", "je ne sais pas", "du lait", ""])
    def test_everything_else_is_left_to_the_normal_path(self, text):
        from ladini.graphs.agents.market_coach.interpreter.numeric_protocol import (
            interpret_buyer_numeric_protocol,
        )

        assert interpret_buyer_numeric_protocol(self._state(), text) is None, text

    def test_only_enter_quantity_is_concerned(self):
        # SELECT_PRODUCER (menu) : « 2 » reste un index de producteur (route existante, non touchée).
        from ladini.graphs.agents.market_coach.interpreter.numeric_protocol import (
            interpret_buyer_numeric_protocol,
        )

        menu = self._state(pending="SELECTION_MENU")
        del menu["vendor_selection_context"]["chosen_vendor"]  # menu producteur VIVANT : aucun choix fait
        assert interpret_buyer_numeric_protocol(menu, "2") is None
        # (B8) palier résolu : le nombre nu est un NOMBRE DE PAQUETS (contrat SET_PACKAGE_COUNT).
        pack = self._state(pending="ENTER_PACKAGE_COUNT")
        pack["tier_selection_context"] = {"tiers": [{"tier_id": "t5", "quantity": 5.0, "unit": "L"}], "resolved_tier_id": "t5"}
        got = interpret_buyer_numeric_protocol(pack, "2")
        assert got is not None and got["extracted_entities"]["action_package_count"] == 2.0

    def test_producer_command_needs_a_real_choice(self):
        from ladini.graphs.agents.market_coach.interpreter.numeric_protocol import (
            interpret_buyer_numeric_protocol,
        )

        assert interpret_buyer_numeric_protocol(self._state(vendors=1), "changer producteur") is None

    def test_without_an_llm_the_historical_numeric_fast_path_keeps_the_bare_number(self):
        # Sans LLM, `_interpret_fast_path` résout déjà le nombre nu (héritage d'unité inclus) : la
        # partie « nombre nu » du protocole s'efface ; les commandes producteur restent actives.
        from ladini.graphs.agents.market_coach.interpreter.numeric_protocol import (
            interpret_buyer_numeric_protocol,
        )

        assert interpret_buyer_numeric_protocol(self._state(), "2", llm_available=False) is None
        assert interpret_buyer_numeric_protocol(self._state(), "producteur 2", llm_available=False) is not None
