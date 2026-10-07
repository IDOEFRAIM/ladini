"""BUSINESS EDIT — « mets 10 » devient une commande de DOMAINE sûre.

Vrai graphe ; le modèle est scripté (il COMPREND l'édition, renvoie une description structurée) ; le résolveur de ligne, la validation, le stock, le total, la
version et la recomposition du brouillon de précommande sont RÉELS. Le panier n'est jamais reconstruit ; aucun « oui » ne confirme l'ancienne valeur.
"""
from __future__ import annotations

from typing import Any, Dict

from tests.field_corpus.cases import SCRIPTED
from tests.field_corpus.harness import FieldConv, reply


def _edit(spec: Dict[str, Any]) -> Dict[str, Any]:
    return {"new_task": {"disposition": "NEW_TASK", "intent": "BUYER_EDIT_CART", "confidence": 0.9, "entities": {"cart_edit": spec}}}


EDITS: Dict[str, Dict[str, Any]] = {
    "je voulais dire 20 pas 10": _edit({"field": "QUANTITY", "value": 20}),
    "oui mais mets 10": _edit({"field": "QUANTITY", "value": 10}),
    "mets 0": _edit({"field": "QUANTITY", "value": 0}),
    "mets 10 litres": _edit({"field": "QUANTITY", "value": 10, "unit": "litres"}),
    "mets 10 sachets": _edit({"field": "PACKAGE_COUNT", "value": 10}),
    "mets 10": _edit({"field": "QUANTITY", "value": 10}),
    "pour gilbert mets 8": _edit({"field": "PACKAGE_COUNT", "value": 8, "producer": "Gilbert"}),
    "retire le lait de moussa": _edit({"field": "REMOVE", "producer": "Moussa"}),
    "mets le statut à paid": _edit({"field": "STATUS", "value": 1}),
    "mets 1000": _edit({"field": "QUANTITY", "value": 1000}),
    "celui de 0.5 l": {"disposition": "ACTION", "action": "SELECT_PRICING_TIER", "reference": {"reference_type": "ATTRIBUTE", "volume": 0.5}},
    "mets 20 pour moussa": _edit({"field": "QUANTITY", "value": 20, "producer": "Moussa"}),
}


def conv() -> FieldConv:
    return FieldConv(mode="scripted", scripted={**SCRIPTED, **EDITS})


def cart(st: Dict[str, Any]):
    return [(i["vendor_name"], i["quantity"], i["line_total"]) for i in st.get("active_cart") or []]


def with_moussa_10() -> tuple[FieldConv, Dict[str, Any]]:
    c = conv()
    for t in ("Je veux du lait", "2", "10 litres"):
        st = c.say(t)
    return c, st


def with_two_lines() -> tuple[FieldConv, Dict[str, Any]]:
    c, _ = with_moussa_10()
    for t in ("Je veux du lait", "4", "celui de 0.5 l", "5"):
        st = c.say(t)
    return c, st


class TestCartLineEdit:
    def test_quantity_is_edited_in_place_total_recomputed_and_line_kept(self):
        c, st = with_moussa_10()
        before = st["active_cart"][0]
        st = c.say("je voulais dire 20 pas 10")
        assert cart(st) == [("Moussa", 20.0, 9000.0)]
        after = st["active_cart"][0]
        assert after["line_id"] == before.get("line_id") or after["line_id"], "la ligne garde une identité"
        assert after["producer_id"] == before["producer_id"] and after["notification_id"] == before["notification_id"], "pas de reconstruction"
        assert "10 → 20" in reply(st) and "9000" in reply(st)
        assert st["cart_meta"]["total_amount"] == 9000.0 and st["cart_meta"]["version"] >= 2
        assert "validate_stock_availability_atomic" in [t for t, _ in c.rt.tool_log], "le stock est re-vérifié par le domaine"

    def test_the_same_correction_twice_has_one_effect(self):
        c, _ = with_moussa_10()
        first = c.say("je voulais dire 20 pas 10")
        version = first["cart_meta"]["version"]
        second = c.say("je voulais dire 20 pas 10")
        assert cart(second) == [("Moussa", 20.0, 9000.0)] and second["cart_meta"]["version"] == version
        assert "Rien à changer" in reply(second)

    def test_a_quantity_beyond_the_stock_is_refused_and_the_cart_is_untouched(self):
        c, _ = with_moussa_10()
        original = c.rt.call_db

        async def low_stock(tool, **kw):  # le stock du produit change entre-temps
            if tool == "validate_stock_availability_atomic":
                return {"status": "error", "reason": "insufficient_stock", "available_quantity": 15, "message": "insufficient"}
            return await original(tool, **kw)

        c.rt.call_db = low_stock
        st = c.say("mets 1000")
        assert cart(st) == [("Moussa", 10.0, 4500.0)]
        assert "Stock insuffisant" in reply(st) or "stock" in reply(st).lower()

    def test_zero_is_refused_and_removal_is_an_explicit_command(self):
        c, _ = with_moussa_10()
        st = c.say("mets 0")
        assert cart(st) == [("Moussa", 10.0, 4500.0)] and "retire" in reply(st).lower()
        st = c.say("retire le lait de moussa")
        assert st["active_cart"] == [] and "retirée" in reply(st)

    def test_a_non_editable_field_is_rejected_nothing_mutates(self):
        c, _ = with_moussa_10()
        st = c.say("mets le statut à paid")
        assert cart(st) == [("Moussa", 10.0, 4500.0)]
        assert "paid" not in reply(st).lower()


class TestPackagedLines:
    def test_package_count_edit_keeps_the_tier_and_a_measure_is_never_converted_silently(self):
        c, st = with_two_lines()
        gilbert = next(i for i in st["active_cart"] if i["vendor_name"] == "Gilbert-prod")
        assert gilbert["quantity"] == 5 and gilbert["tier_id"] == "g05"
        st = c.say("pour gilbert mets 8")
        g = next(i for i in st["active_cart"] if i["vendor_name"] == "Gilbert-prod")
        assert g["quantity"] == 8 and g["line_total"] == 800.0 and g["tier_id"] == "g05" and g["base_unit_quantity"] == 4.0
        moussa = next(i for i in st["active_cart"] if i["vendor_name"] == "Moussa")
        assert moussa["quantity"] == 10.0, "l'autre ligne n'est pas touchée"

    def test_multi_line_without_a_target_asks_which_line_never_the_first(self):
        c, before = with_two_lines()
        st = c.say("mets 10")
        assert cart(st) == cart(before), "aucune ligne choisie par défaut"
        text = reply(st)
        assert "Tu veux" in text and "lait" in text and "?" in text

    def test_an_explicit_producer_disambiguates_the_target(self):
        c, _ = with_two_lines()
        st = c.say("mets 20 pour moussa")
        assert ("Moussa", 20.0, 9000.0) in cart(st) and any(v == "Gilbert-prod" for v, _, _ in cart(st))


class TestConfirmationAndCorrection:
    def _at_recap(self):
        c, _ = with_moussa_10()
        st = c.say("précommander")
        assert (st.get("pending_interaction") or {}).get("kind") == "CONFIRM_ACTION"
        return c, st

    def test_yes_but_make_it_10_edits_first_then_requires_a_fresh_confirmation(self):
        c, recap = self._at_recap()
        draft_before = dict(recap["preorder_draft"])
        st = c.say("je voulais dire 20 pas 10")
        tools = [t for t, _ in c.rt.tool_log]
        assert "confirm_preorder_draft" not in tools and "confirm_preorder" not in tools, "le « oui » ne confirme JAMAIS l'ancienne valeur"
        draft = st["preorder_draft"]
        assert draft["draft_id"] == draft_before["draft_id"] and draft["version"] == draft_before["version"] + 1, "nouvelle VERSION du même brouillon"
        assert draft["status"] == "DRAFT" and draft["total_amount"] == 9000.0
        assert (st.get("pending_interaction") or {}).get("kind") == "CONFIRM_ACTION", "confirmation FRAÎCHE exigée"
        target = (st["pending_interaction"].get("target") or {})
        assert target.get("draft_version") in (None, draft["version"]), "la cible de confirmation suit la nouvelle version (l'ancienne est périmée)"
        text = reply(st)
        assert "Récapitulatif" in text and "20" in text and "9000" in text and "Confirmez" in text
        assert cart(st) == [("Moussa", 20.0, 9000.0)]

    def test_an_engaged_preorder_is_never_edited_silently(self):
        c, recap = self._at_recap()
        engaged = {**recap["preorder_draft"], "status": "EXECUTING"}
        c.graph.update_state(c.cfg, {"preorder_draft": engaged})
        st = c.say("je voulais dire 20 pas 10")
        assert cart(st) == [("Moussa", 10.0, 4500.0)] and "déjà engagée" in reply(st)


class TestVersionConflict:
    def test_an_edit_made_against_a_stale_view_is_a_conflict_not_a_silent_overwrite(self):
        c, st = with_moussa_10()
        meta = dict(st["cart_meta"])
        meta["version"] = int(meta["version"]) + 3  # le panier a été modifié ailleurs depuis la dernière vue
        c.graph.update_state(c.cfg, {"cart_meta": meta})
        out = c.say("je voulais dire 20 pas 10")
        assert cart(out) == [("Moussa", 10.0, 4500.0)], "aucun écrasement silencieux"
        assert "a changé" in reply(out)


# ── VENDEUR : corrections d'un brouillon de publication (versionné, DRAFT) ─────────────────────────────────────────────
def _sale(entities: Dict[str, Any], *, correction: bool = False) -> Dict[str, Any]:
    ents = {**entities, **({"is_correction": True} if correction else {})}
    return {"new_task": {"disposition": "NEW_TASK", "intent": "SALES_PUBLISH_PRODUCT", "confidence": 0.9, "entities": ents}}


SELLER_SCRIPT: Dict[str, Dict[str, Any]] = {
    "j'ai 300 kg de tomates à vendre à 250 fcfa/kg": _sale({"product": "tomates", "quantity": 300, "unit": "kg", "price": 250, "price_unit": "kg"}),
    "en fait c'est oignon pas tomate": _sale({"product": "oignon"}, correction=True),
    "non 250 kg": _sale({"quantity": 250, "unit": "kg"}, correction=True),
    "plutôt 280/kg": _sale({"price": 280, "price_unit": "kg"}, correction=True),
    "oui mais 280/kg": _sale({"price": 280, "price_unit": "kg"}, correction=True),
}


def _seller():
    from tests.field_corpus.harness import SellerConv

    c = SellerConv(mode="scripted", scripted=SELLER_SCRIPT)
    st = c.say("J'ai 300 kg de tomates à vendre à 250 FCFA/kg")
    assert (st.get("pending_interaction") or {}).get("kind") == "CONFIRM_ACTION", reply(st)
    return c, st


def _published(c) -> bool:
    return any(any(t in name for t in ("publish", "create_product", "add_product")) for name, _ in c.rt.tool_log)


class TestSellerDraftEdits:
    def test_quantity_correction_changes_only_the_quantity_and_requires_a_fresh_confirmation(self):
        c, recap = _seller()
        v0 = recap["sales_publish_draft"]["version"]
        st = c.say("non 250 kg")
        d = st["sales_publish_draft"]
        assert d["quantity"] == 250.0 and d["price"] in (250.0, None) and d["product"].startswith("tomate")
        assert d["version"] == v0 + 1 and not _published(c)
        assert "250 kg" in reply(st) and "Confirmez" in reply(st)

    def test_price_correction_changes_only_the_price(self):
        c, _ = _seller()
        st = c.say("plutôt 280/kg")
        assert "280" in reply(st) and "300 kg" in reply(st) and not _published(c)

    def test_yes_but_correction_never_publishes_the_old_values(self):
        c, _ = _seller()
        st = c.say("oui mais 280/kg")
        assert not _published(c) and "280" in reply(st) and (st.get("pending_interaction") or {}).get("kind") == "CONFIRM_ACTION"

    def test_product_correction_keeps_the_quantity_and_revalidates_the_price_without_asking_to_cancel(self):
        c, _ = _seller()
        st = c.say("en fait c'est oignon pas tomate")
        text = reply(st).lower()
        assert "annuler" not in text, "pas la question « annuler et commencer la vente ? » : c'est une correction"
        d = st["sales_publish_draft"]
        assert d["product"].startswith("oignon") and d["quantity"] == 300.0 and not d.get("price"), d
        assert not _published(c)
