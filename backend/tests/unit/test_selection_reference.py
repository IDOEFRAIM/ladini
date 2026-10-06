"""Résolveur DÉTERMINISTE de références naturelles (« le quatrième », « Gilbert », « celui à 500 », « le moins cher »…)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from ladini.graphs.agents.market_coach.domain.selection_reference import (
    Criterion,
    ReferenceType,
    SelectionReference,
    Status,
    options_from_tiers,
    options_from_vendors,
    resolve_reference,
)


def _vendors():
    def v(i, name, zone, price, qty, unit="LITRE", label=None, tiers=None):
        return {"display_index": i, "offer_id": f"o{i}", "vendor_name": name, "zone": zone, "price": price, "unit": unit,
                "available_qty": qty, "pricing_label": label or f"{price:g} FCFA/{unit}", "pricing_tiers": tiers}
    return [
        v(1, "Ferme Sawadogo", "Bobo-Dioulasso", 700, 300),
        v(2, "TEST Producteur", "Ouagadougou", 500, 50, tiers=[{"packaging": "sachet", "base_unit_quantity": 2.0, "price": 500}]),
        v(3, "GARIKO Leila", "Kadiogo", 1200, 900),
        v(4, "Gilbert-prod", "Ouagadougou", 500, 1200, tiers=[{"packaging": "sachet", "base_unit_quantity": 0.5, "price": 100}]),
        v(5, "Gilbert Ouedraogo", "Koudougou", 650, 20),
    ]


OPTS = options_from_vendors(_vendors())


def ordinal(n=None, position=None):
    return SelectionReference(reference_type=ReferenceType.ORDINAL, ordinal=n, position=position)


def attr(**kw):
    return SelectionReference(reference_type=ReferenceType.ATTRIBUTE, **kw)


def pref(c):
    return SelectionReference(reference_type=ReferenceType.PREFERENCE, criterion=c)


@pytest.mark.parametrize("ref,expected", [
    (ordinal(1), 1), (ordinal(4), 4), (ordinal(position="LAST"), 5), (ordinal(position="PENULTIMATE"), 4),
])
def test_ordinals_resolve_against_the_displayed_snapshot(ref, expected):
    r = resolve_reference(ref, OPTS)
    assert r.status == Status.EXACT and r.index == expected


def test_an_ordinal_beyond_the_list_is_not_found_never_guessed():
    assert resolve_reference(ordinal(9), OPTS).status == Status.NOT_FOUND


def test_a_hidden_option_is_not_resolved_silently_when_only_three_were_shown():
    r = resolve_reference(ordinal(4), OPTS, displayed_count=3)
    assert r.status == Status.NOT_VISIBLE and "montre les autres" in r.message
    assert resolve_reference(ordinal(position="LAST"), OPTS, displayed_count=3).index == 3


def test_a_unique_name_selects_and_a_shared_first_name_asks_a_targeted_question():
    assert resolve_reference(attr(producer_name="Sawadogo"), OPTS).index == 1
    assert resolve_reference(attr(producer_name="Gilbert-prod"), OPTS).index == 4
    r = resolve_reference(attr(producer_name="Gilbert"), OPTS)
    assert r.status == Status.AMBIGUOUS and set(r.indices) == {4, 5}
    assert "Gilbert-prod" in r.message and "Gilbert Ouedraogo" in r.message and "ou de" in r.message


def test_price_selection_is_resolved_among_the_visible_offers_only():
    r = resolve_reference(attr(price=1200), OPTS)
    assert r.status == Status.EXACT and r.index == 3
    r = resolve_reference(attr(price=500), OPTS)
    assert r.status == Status.AMBIGUOUS and set(r.indices) == {2, 4}
    assert resolve_reference(attr(price=999), OPTS).status == Status.NOT_FOUND
    assert resolve_reference(attr(price=1200), OPTS, displayed_count=2).status == Status.NOT_VISIBLE


def test_price_and_name_combine_to_disambiguate():
    assert resolve_reference(attr(producer_name="Gilbert", price=500), OPTS).index == 4


def test_availability_and_region_selection():
    assert resolve_reference(attr(availability=1200), OPTS).index == 4
    assert resolve_reference(attr(region="Bobo"), OPTS).index == 1
    r = resolve_reference(attr(region="Ouaga"), OPTS)  # chef-lieu -> Kadiogo : Ouagadougou ET Kadiogo
    assert r.status == Status.AMBIGUOUS and set(r.indices) == {2, 3, 4}
    assert resolve_reference(attr(region="Ouaga", producer_name="Gilbert"), OPTS).index == 4


def test_objective_superlatives_are_decided_in_python_and_ties_stay_ambiguous():
    cheap = [o for o in OPTS if o.index in (1, 3, 5)]
    assert resolve_reference(pref(Criterion.CHEAPEST), cheap).index == 5
    assert resolve_reference(pref(Criterion.HIGHEST_AVAILABILITY), OPTS).index == 4
    tie = resolve_reference(pref(Criterion.CHEAPEST), OPTS)  # 500 et 500
    assert tie.status == Status.AMBIGUOUS and set(tie.indices) == {2, 4}


def test_a_subjective_criterion_is_never_decided():
    r = resolve_reference(pref(Criterion.SUBJECTIVE), OPTS)
    assert r.status == Status.AMBIGUOUS and "moins cher" in r.message


def test_incomparable_units_are_not_ranked():
    mixed = options_from_vendors([
        {"display_index": 1, "offer_id": "a", "vendor_name": "A", "price": 500, "unit": "LITRE", "available_qty": 1},
        {"display_index": 2, "offer_id": "b", "vendor_name": "B", "price": 400, "unit": "KG", "available_qty": 1},
    ])
    r = resolve_reference(pref(Criterion.CHEAPEST), mixed)
    assert r.status == Status.AMBIGUOUS and r.reason.endswith("incomparable_units")


def test_tier_references_by_packaging_volume_and_price():
    tiers = options_from_tiers([
        {"tier_id": "t05", "packaging": "sachet", "base_unit_quantity": 0.5, "price": 100, "unit": "L"},
        {"tier_id": "t1", "packaging": "sachet", "base_unit_quantity": 1.0, "price": 190, "unit": "L"},
        {"tier_id": "t5", "packaging": "bidon", "base_unit_quantity": 5.0, "price": 800, "unit": "L"},
    ])
    assert resolve_reference(attr(volume=0.5), tiers).index == 1
    assert resolve_reference(attr(packaging="bidon"), tiers).index == 3
    assert resolve_reference(attr(price=190), tiers).index == 2
    assert resolve_reference(attr(packaging="sachet"), tiers).status == Status.AMBIGUOUS


def test_the_reference_contract_rejects_malformed_references_and_ignores_technical_ids():
    with pytest.raises(ValidationError):
        SelectionReference(reference_type=ReferenceType.ORDINAL)
    with pytest.raises(ValidationError):
        SelectionReference(reference_type=ReferenceType.ATTRIBUTE)
    ref = SelectionReference.model_validate({"reference_type": "ATTRIBUTE", "producer_name": "Gilbert", "producer_id": "uuid-x"})
    assert not hasattr(ref, "producer_id")
