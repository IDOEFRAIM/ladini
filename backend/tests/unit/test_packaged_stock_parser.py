"""HOTFIX 2026-10-03 — parseur DÉTERMINISTE du stock conditionné + sémantique du prix par conditionnement.

`100 sachets de 500 ml` = 100 packages x 0,5 L = 50 L, prix PAR SACHET. Jamais 100 litres, jamais un prix par ml.
"""
from __future__ import annotations

import pytest

from ladini.domain.commercial_offer_flow import (
    evaluate_sales_offer,
    parse_package_price_reply,
    parse_packaged_stock_message,
    price_for_quantity_in_text,
)
from ladini.domain.packaging_tiers_flow import clean_tiers, render_tiers_summary
from ladini.domain.pricing_tiers import tiers_to_dicts, validate_pricing_tiers
from ladini.graphs.agents.market_coach.interpreter.routing import (
    _apply_packaged_stock_entities,
)


def _groups(text):
    stock = parse_packaged_stock_message(text)
    return None if stock is None else [(g.count, g.label, g.size, g.unit) for g in stock.groups]


@pytest.mark.parametrize("text, expected, total", [
    ("100 sachets de 500 ml", [(100, "SACHET", 0.5, "LITRE")], 50.0),
    ("J aimerais mettre en vente 100 sachet de lait frais pasteurisé de 500ml", [(100, "SACHET", 0.5, "LITRE")], 50.0),
    ("50 bidons de 2 L", [(50, "BIDON", 2.0, "LITRE")], 100.0),
    ("12 bouteilles de 33 cl", [(12, "BOUTEILLE", 0.33, "LITRE")], 3.96),
    ("6 sacs de 25 kg", [(6, "SAC", 25.0, "KG")], 150.0),
    ("20 sacs de riz de 25 kg", [(20, "SAC", 25.0, "KG")], 500.0),
    ("1 sachet de 500 ml", [(1, "SACHET", 0.5, "LITRE")], 0.5),  # singulier
    ("50 bidons de gapal de 500ml et 100 bidons de 330 mL disponible",
     [(50, "BIDON", 0.5, "LITRE"), (100, "BIDON", 0.33, "LITRE")], 58.0),  # 25 + 33 = 58, jamais 250
    ("50 sachets de 500 ml et 20 bidons de 2 L",
     [(50, "SACHET", 0.5, "LITRE"), (20, "BIDON", 2.0, "LITRE")], 65.0),  # deux TYPES jamais fusionnés
])
def test_package_count_times_size_in_base_unit(text, expected, total):
    stock = parse_packaged_stock_message(text)
    assert [(g.count, g.label, g.size, g.unit) for g in stock.groups] == expected
    assert stock.total_base_quantity == pytest.approx(total)


@pytest.mark.parametrize(
    "size_text", ["500 ml", "0,5 L", "0.5 litre", "50 cl", "5 dl", "500ml", "500 mililitres", "500 millilitres"]
)
def test_the_same_physical_size_in_every_notation_is_half_a_litre(size_text):
    assert _groups(f"100 sachets de lait de {size_text}") == [(100, "SACHET", 0.5, "LITRE")]


@pytest.mark.parametrize("text", [
    "20 cartons de 10 unités",  # contenu dénombrable : jamais multiplié
    "30 poulets de 2 kg",  # pas un conditionnement
    "Je veux vendre 50 litres de lait frais pasteurise",
    "100 sachets de 500 ml et 5 sacs de 25 kg",  # VOLUME + MASSE : jamais additionnés
    "50 bidons de 5 L et 50 bidons de 5 L",  # même conditionnement dit deux fois
    "100 sachets de 500 ml 2000",  # nombre inexpliqué
    "60 bidons de 5 litres et 30 bidons de 20 litres. Prix : 3000 FCFA le litre, et 1 bidon de 5 L coûte 10000 FCFA",
    "100 sachets de 500 ml à 500 FCFA et 50 bidons de 2 L",  # prix pour un groupe seulement
])
def test_the_parser_abstains_whenever_the_structure_is_not_certain(text):
    assert parse_packaged_stock_message(text) is None


def test_prices_attached_to_each_group_are_read_per_package():
    stock = parse_packaged_stock_message("50 bidons de gapal de 500 ml à 500 FCFA et 100 bidons de 330 ml à 350 FCFA")
    assert [(g.count, g.size, g.price) for g in stock.groups] == [(50, 0.5, 500.0), (100, 0.33, 350.0)]
    assert stock.all_priced and stock.total_base_quantity == pytest.approx(58.0)


@pytest.mark.parametrize("text, amount, expected", [
    ("500f pour 500mililitre", 500.0, (0.5, "LITRE")),
    ("500 FCFA pour 0,5 litre", 500.0, (0.5, "LITRE")),
    ("500 francs pour 50cl", 500.0, (0.5, "LITRE")),
    ("500 FCFA le sachet", 500.0, None),
    ("1200 francs par litre", 1200.0, None),
])
def test_price_for_a_stated_quantity(text, amount, expected):
    assert price_for_quantity_in_text(text, amount) == expected


_ONE = {"package_count": 100, "package_label": "SACHET", "package_size": 0.5, "package_unit": "LITRE"}
_TWO = {"package_groups": [
    {"count": 50, "label": "BIDON", "size": 0.5, "unit": "LITRE", "size_literal": 500.0, "unit_literal": "MILLILITRE"},
    {"count": 100, "label": "BIDON", "size": 0.33, "unit": "LITRE", "size_literal": 330.0, "unit_literal": "MILLILITRE"}]}


@pytest.mark.parametrize(
    "reply", ["500", "500f", "500 francs", "500 FCFA", "500f pour 500mililitre", "500 FCFA le sachet"]
)
def test_single_package_price_reply(reply):
    assert parse_package_price_reply(reply, _ONE) == {"price": 500.0, "price_unit": "sachet"}


@pytest.mark.parametrize("reply", ["1000 francs le litre", "1 FCFA par ml", "500 et 600", "bonjour"])
def test_single_package_price_reply_abstains_on_an_explicit_per_unit_price_or_ambiguity(reply):
    assert parse_package_price_reply(reply, _ONE) is None


@pytest.mark.parametrize(
    "reply", ["500 et 350", "500 pour 500ml et 350 pour 330ml", "350 pour 330 ml et 500 pour 500 ml"]
)
def test_multi_package_price_reply_maps_prices_to_the_right_packaging(reply):
    out = parse_package_price_reply(reply, _TWO)
    assert [(t["quantity"], t["unit"], t["price"], t["count"]) for t in out["pricing_tiers"]] == [
        (500.0, "ml", 500.0, 50), (330.0, "ml", 350.0, 100)]


def test_multi_package_price_reply_abstains_when_the_count_of_prices_is_wrong():
    assert parse_package_price_reply("500", _TWO) is None
    assert parse_package_price_reply("500 et 350 et 200", _TWO) is None


# ── offre commerciale : la base du prix n'est JAMAIS convertie en silence ──────────────────────────────

def _payload(**over):
    base = {"product": "lait frais pasteurisé", "quantity": 50.0, "unit": "LITRE", **_ONE}
    base.update(over)
    return base


def test_per_package_stays_per_package_and_carries_count_and_content():
    res = evaluate_sales_offer(
        _payload(price=500.0, price_unit="sachet"), said={"price": 500.0}, text="500f pour 500mililitre"
    )
    offer = res.offer
    assert offer.pricing.basis.value == "PER_PACKAGE" and offer.pricing.amount == 500.0
    assert offer.package.count == 100 and offer.package.content_amount == 0.5
    assert offer.inventory_quantity.amount == 50.0 and res.is_valid


def test_per_base_unit_stays_per_base_unit_when_said():
    res = evaluate_sales_offer(
        _payload(price=1000.0, price_unit="litre"), said={"price": 1000.0}, text="1000 francs le litre"
    )
    assert res.offer.pricing.basis.value == "PER_BASE_UNIT"


def test_without_a_price_the_question_is_the_PACKAGE_price_not_per_litre():
    res = evaluate_sales_offer(_payload(), said={"quantity": 50.0}, text="100 sachets de lait de 500 ml")
    assert res.question.expected_basis == "PER_PACKAGE"
    assert "prix d'un *sachet de 500 ml*" in res.question_text
    assert "litre" not in res.question_text.lower().replace("500 ml", "")


def test_summaries_keep_the_commercial_structure():
    res = evaluate_sales_offer(
        _payload(price=500.0, price_unit="sachet"), said={"price": 500.0}, text="500 FCFA le sachet"
    )
    from ladini.domain.commercial_offer import render_offer_summary

    text = render_offer_summary(res.offer)
    assert "100 sachets de lait frais pasteurisé de 500 ml à 500 FCFA le sachet." in text
    assert "Quantité totale : 50 litres." in text
    assert "100 litres" not in text and "millilitre" not in text


def test_tiers_summary_shows_counts_and_short_units():
    tiers = [{"quantity": 500.0, "unit": "ml", "price": 500.0, "packaging": "bidon", "count": 50},
             {"quantity": 330.0, "unit": "ml", "price": 350.0, "packaging": "bidon", "count": 100}]
    text = render_tiers_summary("gapal", 58.0, "LITRE", tiers)
    assert "50 bidons de 500 ml : 500 FCFA le bidon" in text and "100 bidons de 330 ml : 350 FCFA le bidon" in text
    assert "MILLILITRE" not in text


# ── persistance : le COMPTE de stock n'est jamais persisté, le prix reste PAR CONDITIONNEMENT ───────────

def test_package_counts_are_conversation_only_and_never_reach_the_persisted_tiers():
    raw = [{"quantity": 500.0, "unit": "ml", "price": 500.0, "packaging": "bidon", "count": 50},
           {"quantity": 330.0, "unit": "ml", "price": 350.0, "packaging": "bidon", "count": 100}]
    assert clean_tiers(raw)[0]["count"] == 50  # conservé pour le récapitulatif de la conversation
    persisted = tiers_to_dicts(validate_pricing_tiers(clean_tiers(raw), "LITRE"))
    assert all("count" not in t for t in persisted)
    assert [(t["quantity"], t["unit"], t["price"]) for t in persisted] == [(500.0, "ml", 500.0), (330.0, "ml", 350.0)]
    assert [round(t["base_unit_quantity"], 3) for t in persisted] == [0.5, 0.33]


def test_a_tier_unit_of_the_wrong_dimension_is_refused():
    from ladini.domain.pricing_tiers import PricingTierError

    with pytest.raises(PricingTierError):
        validate_pricing_tiers([{"quantity": 500.0, "unit": "ml", "price": 500.0, "packaging": "sachet"}], "KG")


# ── interpréteur : le LLM n'est jamais l'autorité de la structure ──────────────────────────────────────

def _raw(**entities):
    return {"interpreted_event": "NEW_TASK", "detected_intent": "SALES_PUBLISH_PRODUCT",
            "extracted_entities": entities, "interpreter_confidence": 0.9, "raw_analysis": {"path": "x"}}


def test_llm_arithmetic_is_replaced_by_the_deterministic_reading():
    out = _apply_packaged_stock_entities(
        _raw(product="gapal", quantity=250, unit="litre"), {},
        "50 bidons de gapal de 500ml et 100 bidons de 330 mL disponible")
    ents = out["extracted_entities"]
    assert ents["quantity"] == pytest.approx(58.0) and ents["unit"] == "LITRE" and len(ents["package_groups"]) == 2
    assert ents["product"] == "gapal"


def test_other_intents_and_unpackaged_messages_are_untouched():
    other = {**_raw(product="x", quantity=3, unit="kg"), "detected_intent": "BUYER_REQUEST"}
    assert _apply_packaged_stock_entities(other, {}, "100 sachets de 500 ml") is other
    plain = _raw(product="lait", quantity=50, unit="litres")
    assert _apply_packaged_stock_entities(plain, {}, "Je veux vendre 50 litres de lait") is plain


def test_a_new_plain_quantity_resets_a_previous_packaged_stock_but_a_price_reply_never_does():
    state = {"transaction_payload": {"package_count": 100, "package_size": 0.5}}
    out = _apply_packaged_stock_entities(
        _raw(product="lait", quantity=60, unit="litres"), state, "Je veux vendre 60 litres de lait"
    )
    ents = out["extracted_entities"]
    assert ents["package_count"] is None and ents["package_groups"] is None and ents["quantity"] == 60
    price_reply = {**_raw(price=500, quantity=500, unit="ml"), "interpreted_event": "ANSWER"}
    assert _apply_packaged_stock_entities(price_reply, state, "500f pour 500ml") is price_reply
