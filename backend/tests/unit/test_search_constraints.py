"""Contraintes de première recherche : prix max comparé après normalisation d'unité, conditionnement, jamais de relaxation silencieuse."""
from __future__ import annotations

import pytest

from ladini.domain.quantity_unit import parse_quantity_unit_from_text
from ladini.graphs.agents.market_coach.domain.search_constraints import (
    SearchConstraints,
    apply_constraints,
    constraints_from_payload,
    no_result_message,
)


def _tier(tid, qty, price, packaging="sachet", unit="L"):
    return {"tier_id": tid, "quantity": qty, "unit": unit, "price": price, "packaging": packaging, "base_unit_quantity": qty}


def _v(name, price=None, unit="LITRE", tiers=None, basis=None):
    return {"vendor_name": name, "price": price, "unit": unit, "pricing_tiers": tiers, "price_basis": basis}


def test_payload_constraints_are_read_with_unit_normalisation():
    c = constraints_from_payload({"unit": "LITRE", "max_price_per_unit": 600, "package_type": "Sachet", "package_content_amount": 500,
                                  "package_content_unit": "ml"})
    assert c.max_price_per_unit == 600 and c.package_type == "sachet" and c.package_size == pytest.approx(0.5)


def test_no_constraints_means_no_filtering():
    vendors = [_v("A", 900), _v("B", 100)]
    assert [v["vendor_name"] for v in apply_constraints(vendors, constraints_from_payload({})).kept] == ["A", "B"]


def test_500_fcfa_for_a_2_litre_sachet_is_250_per_litre_not_500():
    c = SearchConstraints(max_price_per_unit=400, price_unit="LITRE")
    report = apply_constraints([_v("Sachet2L", 250, tiers=[_tier("a", 2.0, 500.0)]), _v("Flat500", 500)], c)
    assert [v["vendor_name"] for v in report.kept] == ["Sachet2L"]
    assert report.excluded == [("Flat500", "price")]


def test_package_keeps_only_matching_tiers_and_drops_offers_without_packaging():
    c = constraints_from_payload({"unit": "LITRE", "package_type": "sachet", "package_content_amount": 500, "package_content_unit": "ml"})
    mixed = _v("Mixed", tiers=[_tier("s05", 0.5, 100.0), _tier("s1", 1.0, 190.0), _tier("b5", 5.0, 800.0, "bidon")])
    report = apply_constraints([_v("Flat", 300), mixed], c)
    assert [v["vendor_name"] for v in report.kept] == ["Mixed"]
    assert [t["tier_id"] for t in report.kept[0]["pricing_tiers"]] == ["s05"]
    assert report.excluded == [("Flat", "package")]


def test_a_lot_price_is_not_comparable_so_it_is_not_excluded():
    c = SearchConstraints(max_price_per_unit=100, price_unit="LITRE")
    assert [v["vendor_name"] for v in apply_constraints([_v("Lot", 5000, basis="TOTAL_LOT")], c).kept] == ["Lot"]


def test_an_unconvertible_unit_is_not_compared():
    c = SearchConstraints(max_price_per_unit=100, price_unit="LITRE")
    assert apply_constraints([_v("Kg", 5000, unit="KG")], c).kept, "KG n'est pas convertible en LITRE : rien de prouvé, on n'écarte pas"


def test_no_result_message_names_the_criteria_and_never_relaxes_them():
    c = SearchConstraints(max_price_per_unit=100, price_unit="LITRE", package_type="sachet")
    msg = no_result_message(c, "lait", 4)
    assert "Je n'ai rien trouvé" in msg and "100" in msg and "sachet" in msg and "appel d'offres" in msg and "écarté 4 offres" in msg


@pytest.mark.parametrize("text,qty", [
    ("J'ai des oignons à vendre à 250 FCFA/kg", None),
    ("oignons à 250 francs le kg", None),
    ("300 kg à 250 FCFA", 300.0),
])
def test_a_price_is_never_read_as_a_quantity(text, qty):
    assert parse_quantity_unit_from_text(text).quantity == qty
