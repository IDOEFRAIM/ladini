"""B17 — les WRITERS de `pricing_tiers` ne perdent/ne réécrivent jamais l'inventaire par conditionnement ;
le recurring ne peut pas désynchroniser compte et stock physique ; pas de repli `Product.price` pour un produit
vendu par conditionnement."""
from __future__ import annotations

import types
import uuid

import pytest

from ladini.domain.package_inventory import (
    PackageInventoryError,
    assert_package_inventory_consistency,
    merge_tiers_preserving_inventory,
    sells_by_package,
)
from ladini.domain.pricing_tiers import tiers_to_dicts, validate_pricing_tiers
from tests.conftest import run


def _t(tid, size, count, price, packaging="bidon", unit="LITRE"):
    return {"tier_id": tid, "quantity": size, "unit": unit, "price": price, "packaging": packaging,
            "base_unit_quantity": size, "min_order_quantity": 1, "available_count": count}


def _product(qty=58.0, tiers=None):
    return types.SimpleNamespace(
        id=uuid.uuid4(), name="gapal", price=500.0, unit="LITRE", quantity_for_sale=qty, is_available=True,
        pricing_tiers=tiers if tiers is not None else [_t("t500", 0.5, 50, 500), _t("t330", 0.33, 100, 350)],
        commercial_pricing=None, to_dict=lambda: {},
    )


def _update(monkeypatch, product, **fields):
    from ladini.services.database import product as product_mod
    from ladini.services.database.product import ProductMixin

    class _Emitter:
        def __init__(self, *_a, **_k):
            pass

        async def emit_product_quantity_changed(self, *a, **k):
            pass

        async def emit_product_published_for_sale(self, *a, **k):
            pass

    monkeypatch.setattr(product_mod, "BusinessEventEmitter", _Emitter)

    class _Res:
        def scalar_one_or_none(self):
            return product

    class _Sess:
        async def execute(self, stmt):
            return _Res()

        async def flush(self):
            pass

        async def refresh(self, obj):
            pass

    class _Svc(ProductMixin):
        @property
        def session(self):
            return _Sess()

    svc = _Svc()

    async def _profile(phone):
        return None, types.SimpleNamespace(id=uuid.uuid4())

    svc.get_producer_profile = _profile
    return run(svc.update_product_price_and_qty("+22670000001", str(uuid.uuid4()), **fields))


def _counts(p):
    return {t["tier_id"]: t["available_count"] for t in p.pricing_tiers}


def _incoming_without_counts(new_price_500=600):
    return [
        {"quantity": 0.5, "unit": "LITRE", "price": new_price_500, "packaging": "bidon"},
        {"quantity": 0.33, "unit": "LITRE", "price": 350, "packaging": "bidon"},
    ]


# ── 37.1 : une modification de PRIX seule garde le compte ─────────────────────────────────────────
def test_price_only_tier_update_preserves_available_count(monkeypatch):
    p = _product()
    res = _update(monkeypatch, p, pricing_tiers=_incoming_without_counts(600))
    assert res["status"] == "success", res
    assert _counts(p) == {"t500": 50, "t330": 100}
    assert {t["tier_id"]: t["price"] for t in p.pricing_tiers} == {"t500": 600.0, "t330": 350.0}
    assert float(p.quantity_for_sale) == 58.0
    assert_package_inventory_consistency(p)


def test_stale_conversation_cache_cannot_overwrite_the_live_count(monkeypatch):
    """Le tunnel de mise à jour met les paliers EN CACHE avant des ventes : renvoyer ce cache (compte 50) ne doit
    pas écraser le compte réel (40) ni le stock."""
    p = _product(qty=53.0, tiers=[_t("t500", 0.5, 40, 500), _t("t330", 0.33, 100, 350)])
    stale = [_t("t500", 0.5, 50, 650), _t("t330", 0.33, 100, 350)]  # compte périmé 50
    res = _update(monkeypatch, p, pricing_tiers=stale)
    assert res["status"] == "success", res
    assert _counts(p) == {"t500": 40, "t330": 100}
    assert_package_inventory_consistency(p)


# ── 37.3/37.4 : identité canonique ─────────────────────────────────────────────────────────────────
def test_canonical_equivalent_size_matches_the_same_variant():
    existing = [_t("s", 0.5, 100, 500, packaging="sachet")]
    for q, u in ((500, "ML"), (0.5, "LITRE"), (50, "CL")):
        incoming = tiers_to_dicts(validate_pricing_tiers(
            [{"quantity": q, "unit": u, "price": 550, "packaging": "sachet"}], "LITRE"))
        out = merge_tiers_preserving_inventory(existing, incoming)
        assert out[0]["available_count"] == 100 and out[0]["tier_id"] == "s" and out[0]["price"] == 550.0, (q, u)


def test_a_different_package_type_does_not_merge():
    existing = [_t("s", 0.5, 100, 500, packaging="sachet")]
    incoming = tiers_to_dicts(validate_pricing_tiers(
        [{"quantity": 0.5, "unit": "LITRE", "price": 700, "packaging": "bidon", "available_count": 3}], "LITRE"))
    with pytest.raises(PackageInventoryError) as e:  # le sachet en stock disparaîtrait -> refus
        merge_tiers_preserving_inventory(existing, incoming)
    assert e.value.reason == "variant_in_stock_cannot_be_removed"


# ── 37.5 : un writer qui REMPLACE les paliers ne peut pas perdre le compte en silence ──────────────
def test_replacing_writer_cannot_silently_drop_a_variant_in_stock(monkeypatch):
    p = _product()
    only_500 = [{"quantity": 0.5, "unit": "LITRE", "price": 500, "packaging": "bidon"}]
    res = _update(monkeypatch, p, pricing_tiers=only_500)
    assert res["status"] == "error" and res["reason"] == "variant_in_stock_cannot_be_removed"
    assert _counts(p) == {"t500": 50, "t330": 100}  # aucune mutation


# ── 37.2 : sérialisation aller-retour ─────────────────────────────────────────────────────────────
def test_serialization_round_trip_keeps_available_count():
    once = tiers_to_dicts(validate_pricing_tiers([_t("a", 0.5, 7, 500), _t("b", 0.33, 3, 350)], "LITRE"))
    twice = tiers_to_dicts(validate_pricing_tiers(once, "LITRE"))
    assert [t["available_count"] for t in twice] == [7, 3] and twice == once


# ── édition brute du stock / de l'unité d'un produit conditionné : refus (pas de désynchronisation) ─
@pytest.mark.parametrize("fields", [{"quantity": 30.0}, {"unit": "KG"}])
def test_raw_stock_or_unit_edit_on_a_package_inventory_product_is_refused(monkeypatch, fields):
    p = _product()
    res = _update(monkeypatch, p, **fields)
    assert res["status"] == "error" and res["reason"] == "package_inventory_requires_counts"
    assert float(p.quantity_for_sale) == 58.0 and _counts(p) == {"t500": 50, "t330": 100}


def test_legacy_tiers_without_counts_keep_the_historical_stock_edit(monkeypatch):
    legacy = [{"tier_id": "t5", "quantity": 5.0, "unit": "LITRE", "price": 3000, "packaging": "bidon",
               "base_unit_quantity": 5.0, "min_order_quantity": 1}]
    p = _product(qty=60.0, tiers=legacy)
    assert _update(monkeypatch, p, quantity=40.0)["status"] == "success"
    assert p.quantity_for_sale == 40.0


# ── invariant central ─────────────────────────────────────────────────────────────────────────────
def test_consistency_assertion_detects_a_desync_and_ignores_unsupported_mixed_models():
    p = _product()
    assert_package_inventory_consistency(p)
    p.quantity_for_sale = 40.0
    with pytest.raises(PackageInventoryError):
        assert_package_inventory_consistency(p)
    simple = _product(qty=50.0, tiers=[])
    assert_package_inventory_consistency(simple)  # produit sans comptes : rien à affirmer
    assert sells_by_package([{"packaging": "sac"}]) and not sells_by_package([{"quantity": 10}])


# ── recurring : un produit à conditionnements n'est jamais sélectionné ni accepté ────────────────
def test_recurring_candidates_sql_excludes_package_products():
    from ladini.workers.automation.need_matching_service import _CANDIDATES_SQL

    sql = str(_CANDIDATES_SQL)
    assert "pt.tier ->> 'packaging'" in sql and "NOT EXISTS" in sql
