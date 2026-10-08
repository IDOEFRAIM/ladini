"""BUSINESS EDIT STABILIZATION — le modèle n'est jamais l'autorité (vrai graphe, modèle scripté — y compris FAUX).

Chaque test fait dire au « modèle » quelque chose d'incohérent avec le texte ou l'état ; le validateur / le domaine doit refuser ou réparer, JAMAIS muter de travers.
"""
from __future__ import annotations

from typing import Any, Dict

from tests.field_corpus.cases import SCRIPTED
from tests.field_corpus.harness import FieldConv, SellerConv, cart, reply

_EXEC_TOOLS = ("create_preorder", "confirm_preorder", "init_preorder", "publish", "create_product")


def _edit(spec: Dict[str, Any]) -> Dict[str, Any]:
    return {"new_task": {"disposition": "NEW_TASK", "intent": "BUYER_EDIT_CART", "confidence": 0.9, "entities": {"cart_edit": spec}}}


def _buyer(extra: Dict[str, Dict[str, Any]]) -> FieldConv:
    return FieldConv(mode="scripted", scripted={**SCRIPTED, **extra})


def _with_cart(c: FieldConv):
    st: Dict[str, Any] = {}
    for t in ("Je veux du lait", "2", "10 litres"):
        st = c.say(t)
    return st


def _executed(c) -> bool:
    return any(any(t in name for t in _EXEC_TOOLS) for name, _ in c.rt.tool_log)


class TestConfirmationNeverCarriesAnEdit:
    def test_a_model_that_says_confirm_for_a_message_with_a_value_never_executes_the_old_cart(self):
        c = _buyer({"vas-y mais plutôt 20": {"new_task": {"disposition": "CONFIRM", "confidence": 0.95, "entities": {}}}})
        _with_cart(c)
        st = c.say("vas-y mais plutôt 20")
        assert not _executed(c), "l'ancienne confirmation ne s'exécute JAMAIS"
        assert cart(st) == [("Moussa", 10.0)], "aucune mutation quand le modèle est incohérent"

    def test_a_remove_labelled_confirm_by_the_model_is_read_as_the_edit_its_structure_describes(self):
        body = {"new_task": {"disposition": "CONFIRM", "confidence": 0.95, "entities": {"cart_edit": {"field": "REMOVE", "product": "lait"}}}}
        c = _buyer({"retire le lait": body})
        _with_cart(c)
        st = c.say("retire le lait")
        assert not _executed(c) and st.get("active_cart") == []


class TestQuantityAndPriceNeverCrossIncompatibleUnits:
    def test_a_money_value_is_never_applied_as_a_cart_line_quantity(self):
        c = _buyer({"mets 450 francs": _edit({"field": "QUANTITY", "value": 450})})
        _with_cart(c)
        st = c.say("mets 450 francs")
        assert cart(st) == [("Moussa", 10.0)] and "prix" in reply(st).lower()

    def test_seller_kg_value_read_as_price_by_the_model_is_repaired_to_the_quantity(self):
        draft_say = {"j'ai 300 kg de tomates à vendre à 250 fcfa/kg": {"new_task": {
            "disposition": "NEW_TASK", "intent": "SALES_PUBLISH_PRODUCT", "confidence": 0.9,
            "entities": {"product": "tomates", "quantity": 300, "unit": "kg", "price": 250, "price_unit": "kg"}}},
            "non 250 kilos": {"new_task": {"disposition": "NEW_TASK", "intent": "SALES_PUBLISH_PRODUCT", "confidence": 0.9,
                                           "entities": {"price": 250, "price_unit": "kg", "is_correction": True}}}}
        c = SellerConv(mode="scripted", scripted=draft_say)
        c.say("J'ai 300 kg de tomates à vendre à 250 FCFA/kg")
        st = c.say("non 250 kilos")
        d = st["sales_publish_draft"]
        assert d["quantity"] == 250.0 and d["price"] == 250.0 and not _executed(c)

    def test_a_bare_number_with_two_candidate_fields_asks_instead_of_choosing(self):
        say = {"j'ai 300 kg de tomates à vendre à 250 fcfa/kg": {"new_task": {
            "disposition": "NEW_TASK", "intent": "SALES_PUBLISH_PRODUCT", "confidence": 0.9,
            "entities": {"product": "tomates", "quantity": 300, "unit": "kg", "price": 250, "price_unit": "kg"}}},
            "non 280": {"new_task": {"disposition": "NEW_TASK", "intent": "SALES_PUBLISH_PRODUCT", "confidence": 0.9,
                                     "entities": {"price": 280, "is_correction": True}}}}
        c = SellerConv(mode="scripted", scripted=say)
        before = c.say("J'ai 300 kg de tomates à vendre à 250 FCFA/kg")["sales_publish_draft"]
        st = c.say("non 280")
        d = st["sales_publish_draft"]
        assert (d["quantity"], d["price"], d["version"]) == (before["quantity"], before["price"], before["version"])
        assert "quantité" in reply(st).lower() and "prix" in reply(st).lower()
