"""Contrat de persistance de la sémantique commerciale (Phase B2a) — domaine pur, sans base.

Règle centrale : LA SÉMANTIQUE D'UNE TRANSACTION DOIT SURVIVRE À LA PERSISTANCE. Ces tests verrouillent
la forme, les invariants, la précision `Decimal`, la sérialisation unique (JSON == colonnes) et la
lecture rétro-compatible qui n'invente jamais de certitude.
"""
from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from ladini.domain.commercial_offer import (
    CommercialOffer,
    CommercialQuantity,
    InventoryQuantity,
    PackageDefinition,
    PackageStatus,
    PriceBasis,
    Pricing,
    Provenance,
    derive_normalized,
)
from ladini.domain.commercial_pricing_snapshot import (
    LEGACY_UNSPECIFIED,
    CommercialPricingSnapshot,
    PackageSaleMode,
    PricingReliability,
    PricingSnapshotError,
    assert_legacy_projection_matches,
    assert_order_item_legacy_matches,
    award_total,
    bid_pricing_view,
    build_bid_pricing_snapshot,
    build_order_item_pricing_snapshot,
    build_total_lot_order_item_snapshot,
    comparable_total,
    order_item_pricing_view,
    product_pricing_view,
    render_pricing_label,
    resolve_package_purchase,
    snapshot_from_offer,
)

D = Decimal


def _sachet_offer() -> CommercialOffer:
    """50 L de lait, 500 FCFA / sachet de 0,5 L (l'offre du mandat)."""
    offer = CommercialOffer(
        product="lait",
        commercial_quantity=CommercialQuantity(50.0, "LITRE", Provenance.USER_EXPLICIT),
        inventory_quantity=InventoryQuantity(50.0, "LITRE", Provenance.USER_EXPLICIT),
        pricing=Pricing(500.0, PriceBasis.PER_PACKAGE, "FCFA", Provenance.USER_EXPLICIT, Provenance.USER_EXPLICIT),
        package=PackageDefinition("SACHET", 0.5, "LITRE", PackageStatus.KNOWN, Provenance.QUESTION_CONTEXT_EXPLICIT),
    )
    from dataclasses import replace

    return replace(offer, normalized=derive_normalized(offer))


def _lot_offer(total: float, tonnes: float = 200.0) -> CommercialOffer:
    offer = CommercialOffer(
        product="maïs",
        commercial_quantity=CommercialQuantity(tonnes, "TONNE", Provenance.USER_EXPLICIT),
        inventory_quantity=InventoryQuantity(tonnes * 1000, "KG", Provenance.UNIT_CONVERSION),
        pricing=Pricing(total, PriceBasis.TOTAL_LOT, "FCFA", Provenance.USER_EXPLICIT, Provenance.USER_EXPLICIT),
    )
    from dataclasses import replace

    return replace(offer, normalized=derive_normalized(offer))


# =====================================================================
# Le snapshot d'une offre : « 500 FCFA / sachet de 0,5 L » ne devient jamais « 1000 / L » seul
# =====================================================================


class TestSnapshotFromOffer:
    def test_package_price_keeps_its_commercial_meaning_and_the_normalized_derivative(self):
        s = snapshot_from_offer(_sachet_offer())
        assert s.commercial_price_amount == D("500")
        assert s.price_basis == PriceBasis.PER_PACKAGE
        assert (s.package_type, s.package_content_amount, s.package_content_unit) == ("SACHET", D("0.5"), "LITRE")
        assert s.normalized_unit_price == D("1000.0000") and s.normalized_unit == "LITRE"
        assert s.currency == "XOF" and s.schema_version == 1
        assert s.price_source == "USER_EXPLICIT"

    def test_total_lot_is_explicit_and_the_unit_price_is_only_derived(self):
        s = snapshot_from_offer(_lot_offer(5_000_000.0))
        assert s.price_basis == PriceBasis.TOTAL_LOT
        assert s.commercial_price_amount == D("5000000")
        assert s.normalized_unit_price == D("25.0000") and s.normalized_unit == "KG"

    def test_an_incomplete_or_inferred_offer_is_never_persisted(self):
        offer = _sachet_offer()
        from dataclasses import replace

        unknown_size = replace(offer, package=PackageDefinition("SACHET", None, "LITRE", PackageStatus.UNKNOWN))
        with pytest.raises(PricingSnapshotError):
            snapshot_from_offer(unknown_size)
        inferred = replace(
            offer,
            pricing=Pricing(500.0, PriceBasis.PER_PACKAGE, "FCFA", Provenance.USER_EXPLICIT, Provenance.LLM_INFERRED),
        )
        with pytest.raises(PricingSnapshotError):
            snapshot_from_offer(inferred)

    def test_fcfa_maps_to_the_iso_currency_already_used_in_the_schema(self):
        assert snapshot_from_offer(_sachet_offer()).currency == "XOF"


# =====================================================================
# Précision : aucun float ne porte un montant
# =====================================================================


class TestDecimalPrecision:
    def test_exact_division_of_the_mandate_example(self):
        s = snapshot_from_offer(_lot_offer(5_000_000.0))
        assert s.compute_normalized() == D("25.0000")

    @pytest.mark.parametrize(
        "total,tonnes,expected",
        [(1_000_000.0, 0.003, D("333333.3333")), (100.0, 0.003, D("33.3333")), (1.0, 0.007, D("0.1429"))],
    )
    def test_non_exact_division_follows_the_rounding_policy(self, total, tonnes, expected):
        s = snapshot_from_offer(_lot_offer(total, tonnes))
        assert s.normalized_unit_price == expected  # ROUND_HALF_UP, 4 décimales

    def test_the_commercial_total_is_never_altered_by_the_normalization_rounding(self):
        s = snapshot_from_offer(_lot_offer(1_000_000.0, 0.003))
        assert s.commercial_price_amount == D("1000000")
        assert s.total_for(3, "KG") == D("1000000.00")  # le lot entier, pas 333333.3333 × 3

    def test_json_carries_decimals_as_strings_never_floats(self):
        payload = snapshot_from_offer(_lot_offer(1_000_000.0, 0.003)).to_dict()
        assert payload["commercial_price_amount"] == "1000000"
        assert isinstance(payload["normalized_unit_price"], str)
        assert json.loads(json.dumps(payload)) == payload

    def test_floats_enter_through_their_shortest_repr(self):
        s = build_bid_pricing_snapshot(
            amount=0.1, basis="PER_BASE_UNIT", price_unit="KG", auction_quantity=10, auction_unit="KG"
        )
        assert s.commercial_price_amount == D("0.1")


# =====================================================================
# Sérialisation UNIQUE : JSON et colonnes disent la même chose
# =====================================================================


class TestSingleSerialization:
    def test_json_round_trip(self):
        s = snapshot_from_offer(_sachet_offer())
        assert CommercialPricingSnapshot.from_dict(s.to_dict()) == s

    def test_order_item_columns_round_trip(self):
        s = build_order_item_pricing_snapshot(
            product_price=1000, product_unit="LITRE", quantity=4,
            product_pricing_tiers=[{"tier_id": "t1", "quantity": 0.5, "unit": "LITRE", "price": 500, "packaging": "sachet"}],
            tier_id="t1", base_unit_quantity=2, price_at_sale=500,
        )
        row = SimpleNamespace(**s.to_order_item_columns(), quantity=D("4"))
        back = CommercialPricingSnapshot.from_order_item(row)
        assert back is not None
        for field in ("commercial_price_amount", "price_basis", "package_type", "package_content_amount",
                      "package_content_unit", "normalized_unit_price", "normalized_unit", "currency"):
            assert getattr(back, field) == getattr(s, field), field

    def test_bid_columns_round_trip(self):
        s = build_bid_pricing_snapshot(
            amount=450000, basis="PER_BASE_UNIT", price_unit="TONNE", auction_quantity=10, auction_unit="TONNE"
        )
        cols = s.to_bid_columns()
        assert cols["offered_price"] == D("450000") and cols["offered_price_basis"] == "PER_BASE_UNIT"
        back = CommercialPricingSnapshot.from_bid(SimpleNamespace(**cols))
        assert (back.commercial_price_amount, back.price_basis, back.price_unit) == (
            s.commercial_price_amount, s.price_basis, s.price_unit
        )

    def test_columns_and_json_project_the_same_semantics(self):
        s = snapshot_from_offer(_sachet_offer())
        cols = s.to_order_item_columns()
        as_json = s.to_dict()
        assert format(cols["commercial_price_amount"].normalize(), "f") == as_json["commercial_price_amount"]
        assert cols["price_basis"] == as_json["price_basis"]
        assert cols["package_type"] == as_json["package_type"]
        assert cols["pricing_snapshot_version"] == as_json["schema_version"]

    def test_an_unknown_schema_version_is_not_silently_understood(self):
        payload = snapshot_from_offer(_sachet_offer()).to_dict()
        payload["schema_version"] = 99
        assert CommercialPricingSnapshot.from_dict(payload) is None

    def test_a_malformed_snapshot_is_rejected_not_repaired(self):
        payload = snapshot_from_offer(_sachet_offer()).to_dict()
        payload["package_content_amount"] = None
        with pytest.raises(PricingSnapshotError):
            CommercialPricingSnapshot.from_dict(payload)


# =====================================================================
# Cohérence : combinaisons impossibles
# =====================================================================


class TestImpossibleCombinations:
    def _snap(self, **over):
        base = dict(commercial_price_amount=D("500"), price_basis=PriceBasis.PER_PACKAGE, package_type="SACHET",
                    package_content_amount=D("0.5"), package_content_unit="LITRE")
        base.update(over)
        return CommercialPricingSnapshot(**base)

    def test_per_package_requires_the_package(self):
        for missing in ({"package_type": None}, {"package_content_amount": None}, {"package_content_unit": None}):
            assert self._snap(**missing).issues(), missing

    def test_per_base_unit_requires_a_price_unit_and_no_package(self):
        assert CommercialPricingSnapshot(D("450000"), PriceBasis.PER_BASE_UNIT).issues()
        assert CommercialPricingSnapshot(D("450000"), PriceBasis.PER_BASE_UNIT, price_unit="TONNE", package_type="SAC").issues()

    def test_total_lot_admits_neither_unit_nor_package(self):
        assert CommercialPricingSnapshot(D("5000000"), PriceBasis.TOTAL_LOT, price_unit="KG").issues()

    def test_price_must_be_positive(self):
        assert self._snap(commercial_price_amount=D("0")).issues()

    def test_price_unit_must_match_the_measurement_family(self):
        s = CommercialPricingSnapshot(
            D("450000"), PriceBasis.PER_BASE_UNIT, price_unit="TONNE",
            inventory_quantity_amount=D("10"), inventory_quantity_unit="LITRE",
        )
        assert any("incompatible" in i for i in s.issues())

    def test_a_valid_snapshot_has_no_issue(self):
        assert self._snap().issues() == []


# =====================================================================
# OrderItem : instantané immuable (le builder unique)
# =====================================================================

TIERS = [{"tier_id": "t1", "quantity": 0.5, "unit": "LITRE", "price": 500, "packaging": "sachet"}]


class TestOrderItemSnapshot:
    def test_golden_package_purchase_keeps_500_per_sachet_of_half_a_litre(self):
        s = build_order_item_pricing_snapshot(
            product_price=1000, product_unit="LITRE", quantity=4, product_pricing_tiers=TIERS,
            tier_id="t1", base_unit_quantity=2, price_at_sale=500,
        )
        assert s.commercial_price_amount == D("500") and s.price_basis == PriceBasis.PER_PACKAGE
        assert (s.package_type, s.package_content_amount, s.package_content_unit) == ("SACHET", D("0.5"), "LITRE")
        assert (s.commercial_quantity_amount, s.commercial_quantity_unit) == (D("4"), "SACHET")
        assert s.normalized_unit_price == D("1000.0000") and s.normalized_unit == "LITRE"

    def test_the_snapshot_is_independent_from_the_product_afterwards(self):
        s = build_order_item_pricing_snapshot(
            product_price=1000, product_unit="LITRE", quantity=4, product_pricing_tiers=TIERS,
            tier_id="t1", base_unit_quantity=2, price_at_sale=500,
        )
        cols_before = s.to_order_item_columns()
        # le produit change de prix, d'unité et de paliers : le snapshot (déjà figé) ne dépend de rien
        _ = build_order_item_pricing_snapshot(
            product_price=9999, product_unit="KG", quantity=1,
            product_pricing_tiers=[{"tier_id": "t1", "quantity": 1, "unit": "KG", "price": 9999, "packaging": "sac"}],
            tier_id="t1", base_unit_quantity=1, price_at_sale=9999,
        )
        assert s.to_order_item_columns() == cols_before

    def test_plain_product_records_price_per_unit_with_its_source(self):
        s = build_order_item_pricing_snapshot(product_price=300, product_unit="KG", quantity=100, price_at_sale=300)
        assert s.price_basis == PriceBasis.PER_BASE_UNIT and s.price_unit == "KG"
        assert s.price_source == "PRODUCT_LEGACY_FIELDS"

    def test_certified_per_tonne_product_bought_in_kg_keeps_450000_per_tonne(self):
        certified = build_bid_pricing_snapshot(
            amount=450000, basis="PER_BASE_UNIT", price_unit="TONNE", auction_quantity=10, auction_unit="TONNE"
        ).to_dict()
        s = build_order_item_pricing_snapshot(
            product_price=450, product_unit="KG", quantity=10000, product_commercial_pricing=certified,
            price_at_sale=450,
        )
        assert (s.commercial_price_amount, s.price_basis, s.price_unit) == (D("450000"), PriceBasis.PER_BASE_UNIT, "TONNE")
        assert (s.commercial_quantity_amount, s.commercial_quantity_unit) == (D("10000"), "KG")

    def test_certified_package_product_bought_by_base_unit_records_what_was_actually_charged(self):
        certified = snapshot_from_offer(_sachet_offer()).to_dict()
        s = build_order_item_pricing_snapshot(
            product_price=1000, product_unit="LITRE", quantity=3, product_commercial_pricing=certified,
            price_at_sale=1000,
        )
        assert s.price_basis == PriceBasis.PER_BASE_UNIT and s.commercial_price_amount == D("1000")
        assert s.price_source == "NORMALIZED_FROM_CERTIFIED_PACKAGE"

    def test_total_lot_product_sells_whole_or_not_at_all(self):
        certified = snapshot_from_offer(_lot_offer(5_000_000.0)).to_dict()
        whole = build_order_item_pricing_snapshot(
            product_price=25, product_unit="KG", quantity=200000, product_commercial_pricing=certified, price_at_sale=25,
        )
        assert whole.price_basis == PriceBasis.TOTAL_LOT and whole.commercial_price_amount == D("5000000")
        with pytest.raises(PricingSnapshotError):
            build_order_item_pricing_snapshot(
                product_price=25, product_unit="KG", quantity=1000, product_commercial_pricing=certified, price_at_sale=25,
            )

    def test_declared_sale_is_a_total_lot(self):
        s = build_total_lot_order_item_snapshot(total_amount=25000, quantity=50, unit="KG", price_at_sale=500)
        assert s.price_basis == PriceBasis.TOTAL_LOT and s.commercial_price_amount == D("25000")
        assert s.normalized_unit_price == D("500.0000")

    def test_unknown_tier_is_an_error_not_a_guess(self):
        with pytest.raises(PricingSnapshotError):
            build_order_item_pricing_snapshot(
                product_price=1000, product_unit="LITRE", quantity=1, product_pricing_tiers=TIERS, tier_id="nope"
            )


# =====================================================================
# Dual-write : legacy == projection du snapshot, sinon échec AVANT commit
# =====================================================================


class TestDualWrite:
    def test_order_item_legacy_price_must_match(self):
        with pytest.raises(PricingSnapshotError):
            build_order_item_pricing_snapshot(
                product_price=1000, product_unit="LITRE", quantity=4, product_pricing_tiers=TIERS,
                tier_id="t1", base_unit_quantity=2, price_at_sale=1000,  # 1000/L au lieu de 500/sachet
            )

    def test_total_lot_legacy_unit_price_within_half_a_cent_per_unit(self):
        s = snapshot_from_offer(_lot_offer(1_000_000.0, 0.003))
        assert_order_item_legacy_matches(s, quantity=3, price_at_sale=D("333333.33"))
        with pytest.raises(PricingSnapshotError):
            assert_order_item_legacy_matches(s, quantity=3, price_at_sale=D("333000"))

    def test_product_legacy_projection(self):
        s = snapshot_from_offer(_sachet_offer())
        assert_legacy_projection_matches(s, legacy_price=1000.0, legacy_unit="LITRE")
        with pytest.raises(PricingSnapshotError):
            assert_legacy_projection_matches(s, legacy_price=500.0, legacy_unit="LITRE")
        with pytest.raises(PricingSnapshotError):
            assert_legacy_projection_matches(s, legacy_price=1000.0, legacy_unit="KG")


# =====================================================================
# Bid : base obligatoire, jamais déduite de l'unité de l'enchère
# =====================================================================


class TestBidSnapshot:
    def test_450000_per_tonne_on_a_tonne_auction(self):
        s = build_bid_pricing_snapshot(
            amount=450000, basis="PER_BASE_UNIT", price_unit="TONNE", auction_quantity=10, auction_unit="TONNE"
        )
        assert award_total(s, 10, "TONNE") == D("4500000.00")
        assert s.normalized_unit_price == D("450.0000") and s.normalized_unit == "KG"

    def test_same_amount_per_kg_is_a_different_offer(self):
        per_kg = build_bid_pricing_snapshot(
            amount=450000, basis="PER_BASE_UNIT", price_unit="KG", auction_quantity=10, auction_unit="TONNE"
        )
        assert award_total(per_kg, 10, "TONNE") == D("4500000000.00")

    def test_total_lot_bid_is_the_amount_whatever_the_quantity(self):
        s = build_bid_pricing_snapshot(
            amount=4_500_000, basis="TOTAL_LOT", price_unit=None, auction_quantity=10, auction_unit="TONNE"
        )
        assert award_total(s, 10, "TONNE") == D("4500000.00")
        assert s.normalized_unit_price == D("450.0000")

    def test_package_bid_needs_the_package(self):
        with pytest.raises(PricingSnapshotError):
            build_bid_pricing_snapshot(
                amount=500, basis="PER_PACKAGE", price_unit=None, auction_quantity=50, auction_unit="LITRE"
            )
        ok = build_bid_pricing_snapshot(
            amount=500, basis="PER_PACKAGE", price_unit=None, auction_quantity=50, auction_unit="LITRE",
            package_type="sachet", package_content_amount=0.5, package_content_unit="LITRE",
        )
        assert award_total(ok, 50, "LITRE") == D("50000.00")  # 100 sachets × 500

    def test_a_bid_without_a_basis_cannot_be_built(self):
        for basis in (None, "", "PER_KG", LEGACY_UNSPECIFIED):
            with pytest.raises(PricingSnapshotError):
                build_bid_pricing_snapshot(
                    amount=450000, basis=basis, price_unit=None, auction_quantity=10, auction_unit="TONNE"
                )

    def test_a_price_unit_of_another_family_than_the_auction_is_rejected(self):
        with pytest.raises(PricingSnapshotError):
            build_bid_pricing_snapshot(
                amount=450, basis="PER_BASE_UNIT", price_unit="LITRE", auction_quantity=10, auction_unit="TONNE"
            )


# =====================================================================
# Lecture rétro-compatible : jamais de certitude fabriquée
# =====================================================================


class TestLegacyReads:
    def test_legacy_bid_has_an_unknown_basis_never_per_kg(self):
        legacy = SimpleNamespace(offered_price=D("450000"), offered_price_basis=None, pricing_snapshot_version=None)
        view = bid_pricing_view(legacy)
        assert view.amount == D("450000")
        assert view.basis is None and view.basis_label == "UNKNOWN"
        assert view.reliability == PricingReliability.UNKNOWN_BASIS
        assert view.snapshot is None

    def test_an_explicit_legacy_marker_is_also_unknown(self):
        row = SimpleNamespace(offered_price=D("450000"), offered_price_basis=LEGACY_UNSPECIFIED, pricing_snapshot_version=1)
        assert bid_pricing_view(row).reliability == PricingReliability.UNKNOWN_BASIS

    def test_legacy_order_item_is_partial_not_certified(self):
        row = SimpleNamespace(price_at_sale=D("500"), pricing_snapshot_version=None)
        view = order_item_pricing_view(row)
        assert view.reliability == PricingReliability.LEGACY_PARTIAL and view.basis is None

    def test_legacy_product_is_partial(self):
        view = product_pricing_view(SimpleNamespace(price=D("1000"), commercial_pricing=None))
        assert view.reliability == PricingReliability.LEGACY_PARTIAL and view.basis is None

    def test_certified_rows_read_as_certified(self):
        s = build_bid_pricing_snapshot(
            amount=450000, basis="PER_BASE_UNIT", price_unit="TONNE", auction_quantity=10, auction_unit="TONNE"
        )
        view = bid_pricing_view(SimpleNamespace(**s.to_bid_columns()))
        assert view.reliability == PricingReliability.CERTIFIED and view.basis == PriceBasis.PER_BASE_UNIT
        prod = product_pricing_view(SimpleNamespace(price=D("450"), commercial_pricing=s.to_dict()))
        assert prod.reliability == PricingReliability.CERTIFIED


# =====================================================================
# Affichage/comparaison depuis une `PricingView` — API unique pour tout consommateur buyer
# (mandat B2c.4 §3) : un seul `pricing_label`, jamais reconstruit par node.
# =====================================================================


class TestPricingViewDisplayAndComparability:
    def test_certified_total_lot_label_says_pour_l_ensemble(self):
        s = CommercialPricingSnapshot(
            commercial_price_amount=D("4000000"), price_basis=PriceBasis.TOTAL_LOT,
            inventory_quantity_amount=D("10000"), inventory_quantity_unit="KG",
        ).with_normalized()
        view = product_pricing_view(SimpleNamespace(price=D("400"), commercial_pricing=s.to_dict()))
        assert view.status == "CERTIFIED"
        assert "pour l'ensemble" in view.pricing_label
        assert view.is_comparable and view.not_comparable_reason is None

    def test_certified_package_label_never_becomes_a_per_base_unit_guess(self):
        s = CommercialPricingSnapshot(
            commercial_price_amount=D("500"), price_basis=PriceBasis.PER_PACKAGE,
            package_type="SACHET", package_content_amount=D("0.5"), package_content_unit="LITRE",
            inventory_quantity_amount=D("50"), inventory_quantity_unit="LITRE",
        ).with_normalized()
        view = product_pricing_view(SimpleNamespace(price=D("1000"), commercial_pricing=s.to_dict()))
        label = view.pricing_label
        assert "sachet" in label and "0,5" in label
        assert "1000" not in label.replace(" ", "")

    def test_legacy_partial_label_is_honest_never_a_fabricated_basis(self):
        view = product_pricing_view(SimpleNamespace(price=D("400000"), commercial_pricing=None))
        assert view.status == "LEGACY_PARTIAL"
        assert "non certifi" in view.pricing_label
        assert not view.is_comparable
        assert view.not_comparable_reason == "base de prix historique inconnue"

    def test_unknown_basis_with_no_amount_has_nothing_to_show(self):
        view = bid_pricing_view(SimpleNamespace(offered_price=None, offered_price_basis=None, pricing_snapshot_version=None))
        assert view.pricing_label == "Prix non disponible"

    def test_total_lot_without_inventory_is_certified_but_not_comparable(self):
        """TOTAL_LOT sans quantité d'inventaire connue : `normalized_unit_price` est None
        (`compute_normalized`) — la vue reste CERTIFIÉE (l'utilisateur a bien dit un montant total)
        mais n'est pas comparable a une autre offre au prix normalisé (mandat B2c.4 §17)."""
        s = CommercialPricingSnapshot(commercial_price_amount=D("500000"), price_basis=PriceBasis.TOTAL_LOT)
        view = product_pricing_view(SimpleNamespace(price=D("500000"), commercial_pricing=s.to_dict()))
        assert view.status == "CERTIFIED"
        assert not view.is_comparable
        assert view.not_comparable_reason == "quantité de référence inconnue pour normaliser ce lot"

    def test_render_pricing_label_and_comparable_total_are_the_same_object_from_both_modules(self):
        """Relocalisées dans `commercial_pricing_snapshot.py` (B2c.4) — `bid_pricing_flow.py` les
        ré-exporte pour ses appelants existants, jamais une 2e implémentation."""
        from ladini.domain import bid_pricing_flow

        assert bid_pricing_flow.render_pricing_label is render_pricing_label
        assert bid_pricing_flow.comparable_total is comparable_total


# =====================================================================
# Achat d'un produit conditionné (Étape 19)
# =====================================================================


class TestPackageSaleSemantics:
    def _pkg(self, content="0.5"):
        return CommercialPricingSnapshot(
            D("500"), PriceBasis.PER_PACKAGE, package_type="SACHET",
            package_content_amount=D(content), package_content_unit="LITRE",
        )

    def test_package_count_is_taken_as_is(self):
        r = resolve_package_purchase(self._pkg(), 4, "sachet")
        assert (r.status, r.package_count, r.base_quantity) == ("OK", 4, D("2.0"))

    def test_package_only_refuses_a_quantity_in_content_units(self):
        r = resolve_package_purchase(self._pkg(), 2, "litre")
        assert r.status == "NEEDS_PACKAGE_COUNT" and r.package_count is None

    def test_base_unit_allowed_accepts_an_exact_multiple(self):
        r = resolve_package_purchase(self._pkg(), 2, "litre", mode=PackageSaleMode.BASE_UNIT_ALLOWED)
        assert (r.status, r.package_count) == ("OK", 4)

    def test_base_unit_allowed_refuses_a_non_divisible_quantity(self):
        r = resolve_package_purchase(self._pkg(), D("1.2"), "litre", mode=PackageSaleMode.BASE_UNIT_ALLOWED)
        assert r.status == "NOT_DIVISIBLE" and r.package_count is None

    def test_a_fractional_package_count_is_not_divisible(self):
        assert resolve_package_purchase(self._pkg(), D("2.5"), "sachet").status == "NOT_DIVISIBLE"

    def test_content_of_one_unit_makes_the_unit_a_pack_count(self):
        r = resolve_package_purchase(self._pkg("1"), 3, "litre")
        assert (r.status, r.package_count) == ("OK", 3)

    def test_non_packaged_price_is_not_a_package_sale(self):
        plain = CommercialPricingSnapshot(D("300"), PriceBasis.PER_BASE_UNIT, price_unit="KG")
        assert resolve_package_purchase(plain, 2, "kg").status == "NOT_PACKAGED"
