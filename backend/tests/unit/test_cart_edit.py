"""Édition d'une ligne du panier : identité, validation, quantité ≠ paquets, version (CAS), idempotence, total recalculé par le domaine."""
from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from ladini.graphs.agents.market_coach.domain.cart_edit import (
    CartEditSpec,
    CartField,
    EditStatus,
    commit_edit,
    line_identity,
    plan_edit,
    resolve_line,
    with_line_ids,
)


def _flat(name="lait", qty=10.0, price=450.0, vendor="Moussa", unit="LITRE", pid="P-lait", prod="PR-moussa"):
    return {"product_id": pid, "name": name, "quantity": qty, "unit": unit, "price": price, "line_total": qty * price,
            "producer_id": prod, "vendor_name": vendor, "status": "VALIDATED", "notification_id": "N1"}


def _tiered(count=5, price=100.0, tier_q=0.5):
    return {"product_id": "P-lait2", "name": "lait", "quantity": count, "unit": "L", "packaging": "sachet", "tier_id": "t05", "tier_quantity": tier_q,
            "base_unit_quantity": count * tier_q, "price": price, "line_total": count * price, "producer_id": "PR-gil", "vendor_name": "Gilbert-prod"}


def spec(**kw):
    return CartEditSpec(**kw)


def test_line_identity_is_canonical_and_stable():
    a, b = _flat(), _flat(name="LAIT frais")
    assert line_identity(a) == line_identity(b), "l'identité ne dépend pas du libellé affiché"
    assert line_identity(a) != line_identity(_flat(prod="PR-autre"))
    assert all(ln["line_id"] for ln in with_line_ids([a, _tiered()]))


def test_quantity_edit_keeps_the_line_and_recomputes_the_total_in_the_domain():
    cart = [_flat(qty=10)]
    plan = plan_edit(spec(field="QUANTITY", value=20, unit="litres"), cart)
    out = commit_edit(cart, plan, current_version=3, expected_version=3)
    assert out.status == EditStatus.APPLIED and out.old_value == 10 and out.new_value == 20
    line = out.cart[0]
    assert line["quantity"] == 20 and line["line_total"] == 9000.0 and line["vendor_name"] == "Moussa" and line["notification_id"] == "N1"
    assert out.meta["total_amount"] == 9000.0 and out.meta["version"] == 4 and len(out.cart) == 1, "pas de reconstruction du panier"


@pytest.mark.parametrize("value", [0, -5, math.inf])
def test_zero_negative_and_infinite_quantities_are_rejected(value):
    plan = plan_edit(spec(field="QUANTITY", value=value), [_flat()])
    assert plan.status == EditStatus.REJECTED


def test_an_incompatible_unit_is_rejected_and_a_convertible_one_converted():
    assert plan_edit(spec(field="QUANTITY", value=5, unit="kg"), [_flat()]).status == EditStatus.REJECTED
    kg_line = _flat(name="mais", unit="KG", price=100.0)
    plan = plan_edit(spec(field="QUANTITY", value=2, unit="tonne"), [kg_line])
    assert plan.status == EditStatus.APPLIED and plan.new_value == 2000.0


def test_a_measure_on_a_packaged_line_is_never_silently_converted_to_a_package_count():
    plan = plan_edit(spec(field="QUANTITY", value=10, unit="litres"), [_tiered()])
    assert plan.status == EditStatus.REJECTED and "Combien de sachet" in plan.message


def test_package_count_edit_on_a_packaged_line_and_on_a_flat_line():
    out = commit_edit([_tiered(5)], plan_edit(spec(field="PACKAGE_COUNT", value=10), [_tiered(5)]), current_version=1)
    assert out.status == EditStatus.APPLIED and out.cart[0]["quantity"] == 10 and out.cart[0]["line_total"] == 1000.0
    assert out.cart[0]["base_unit_quantity"] == 5.0 and out.cart[0]["tier_id"] == "t05", "le palier est conservé"
    assert plan_edit(spec(field="PACKAGE_COUNT", value=3), [_flat()]).status == EditStatus.REJECTED
    assert plan_edit(spec(field="PACKAGE_COUNT", value=2.5), [_tiered()]).status == EditStatus.REJECTED


def test_a_bare_number_on_a_packaged_line_is_a_package_count_like_the_cart_flow():
    plan = plan_edit(spec(field="QUANTITY", value=10), [_tiered()])
    assert plan.status == EditStatus.APPLIED and plan.new_value == 10


def test_same_value_is_unchanged_and_idempotent():
    cart = [_flat(qty=10)]
    plan = plan_edit(spec(field="QUANTITY", value=10), cart)
    out = commit_edit(cart, plan, current_version=7)
    assert out.status == EditStatus.UNCHANGED and out.meta["version"] == 7, "rejouer la même correction ne change ni la ligne ni la version"


def test_version_conflict_never_overwrites_silently():
    cart = [_flat(qty=10)]
    plan = plan_edit(spec(field="QUANTITY", value=20), cart)
    first = commit_edit(cart, plan, current_version=5, expected_version=5)
    assert first.status == EditStatus.APPLIED
    # deuxième écrivain : il voyait la version 5, le panier est maintenant en 6
    second = commit_edit(first.cart, plan_edit(spec(field="QUANTITY", value=30), first.cart), current_version=6, expected_version=5)
    assert second.status == EditStatus.CONFLICT and second.cart[0]["quantity"] == 20 and second.meta["version"] == 6


def test_remove_is_an_explicit_command():
    cart = [_flat(), _tiered()]
    out = commit_edit(cart, plan_edit(spec(field="REMOVE", product="lait", producer="Gilbert"), cart), current_version=1)
    assert out.status == EditStatus.APPLIED and [ln["vendor_name"] for ln in out.cart] == ["Moussa"]


def test_multi_line_without_a_target_is_ambiguous_never_the_first_line():
    cart = [_flat(), _flat(name="tomates", unit="KG", pid="P-tom", prod="PR-x", vendor="Awa", price=300.0)]
    res = resolve_line(spec(field="QUANTITY", value=10), cart)
    assert res.status == EditStatus.AMBIGUOUS and "lait" in res.message and "tomates" in res.message
    plan = plan_edit(spec(field="QUANTITY", value=10), cart)
    assert plan.status == EditStatus.AMBIGUOUS and plan.new_line is None


def test_explicit_references_pick_exactly_one_line():
    cart = [_flat(), _flat(name="tomates", unit="KG", pid="P-tom", prod="PR-x", vendor="Awa", price=300.0)]
    assert resolve_line(spec(field="QUANTITY", value=10, product="tomates"), cart).status == EditStatus.APPLIED
    assert resolve_line(spec(field="QUANTITY", value=10, producer="Awa"), cart).status == EditStatus.APPLIED
    assert resolve_line(spec(field="QUANTITY", value=10, ordinal=1), cart).status == EditStatus.APPLIED
    assert resolve_line(spec(field="QUANTITY", value=10, product="mil"), cart).status == EditStatus.NOT_FOUND
    assert resolve_line(spec(field="QUANTITY", value=10, ordinal=5), cart).status == EditStatus.NOT_FOUND


def test_only_domain_editable_fields_exist_and_a_hostile_status_edit_is_invalid():
    with pytest.raises(ValidationError):
        CartEditSpec(field="STATUS", value=1)  # « mets le statut à PAID »
    assert {f.value for f in CartField} == {"QUANTITY", "PACKAGE_COUNT", "REMOVE"}
    assert CartEditSpec.model_validate({"field": "quantity", "value": 3, "line_id": "uuid-x"}).value == 3  # id ignoré, casse tolérée


def test_an_empty_cart_has_nothing_to_edit():
    assert plan_edit(spec(field="QUANTITY", value=5), []).status == EditStatus.NOT_FOUND
