"""Contrat de persistance de la sémantique commerciale (Phase B2a) sur un VRAI PostgreSQL.

La base est reconstruite UNIQUEMENT depuis les migrations officielles (fixture `pg_dsn`) : ces tests
prouvent que le schéma migré (colonnes, CHECK, triggers d'immuabilité) tient ses promesses — anciennes
lignes lisibles, nouvelles lignes écrivables, sens commercial gelé, combinaisons impossibles refusées.
Ignorés hors CI sans `SCHEMA_TEST_DSN` (en CI `REQUIRE_SCHEMA_DB=1` transforme l'absence en échec).
"""
from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace

import pytest
from factories import Graph, insert
from psycopg2 import errors
from psycopg2.extras import Json, RealDictCursor

from ladini.domain.commercial_offer import PriceBasis
from ladini.domain.commercial_pricing_snapshot import (
    CommercialPricingSnapshot,
    PricingReliability,
    bid_pricing_view,
    build_bid_pricing_snapshot,
    build_order_item_pricing_snapshot,
    build_total_lot_order_item_snapshot,
    order_item_pricing_view,
)

D = Decimal
TIERS = [{"tier_id": "t1", "quantity": 0.5, "unit": "LITRE", "price": 500, "packaging": "sachet"}]


@pytest.fixture
def g(db):
    with db.cursor() as cur:
        yield Graph(cur)


@contextmanager
def violation(cur, *kinds):
    """Le bloc DOIT lever une des erreurs `kinds` ; la transaction est sauvée par un SAVEPOINT."""
    cur.execute("SAVEPOINT expect_violation")
    try:
        yield
    except kinds:
        cur.execute("ROLLBACK TO SAVEPOINT expect_violation")
        return
    else:
        pytest.fail(f"aucune violation levée ({', '.join(k.__name__ for k in kinds)} attendue)")


def _row(db, table, row_id):
    with db.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(f"SELECT * FROM marketplace.{table} WHERE id = %s", (row_id,))
        return SimpleNamespace(**cur.fetchone())


def _item(cur, g, order, **cols):
    return insert(cur, "marketplace.order_items", order_id=order, product_id=g.product,
                  quantity=cols.pop("quantity", 4), price_at_sale=cols.pop("price_at_sale", 500), **cols)


def _sachet_columns():
    return build_order_item_pricing_snapshot(
        product_price=1000, product_unit="LITRE", quantity=4, product_pricing_tiers=TIERS,
        tier_id="t1", base_unit_quantity=2, price_at_sale=500,
    ).to_order_item_columns()


# =====================================================================
# Migration : anciennes lignes lisibles, nouvelles lignes écrivables
# =====================================================================


class TestMigration:
    def test_legacy_rows_without_a_snapshot_stay_valid_and_readable(self, db, g):
        with db.cursor() as cur:
            order = g.order()
            item = _item(cur, g, order)
            auction = g.auction()
            bid = g.bid(auction, offered_price=450000)
        legacy_item, legacy_bid = _row(db, "order_items", item), _row(db, "bids", bid)
        assert legacy_item.pricing_snapshot_version is None and legacy_item.price_basis is None
        assert legacy_bid.offered_price_basis is None and legacy_bid.pricing_snapshot_version is None
        assert order_item_pricing_view(legacy_item).reliability == PricingReliability.LEGACY_PARTIAL
        view = bid_pricing_view(legacy_bid)
        assert view.amount == D("450000") and view.basis is None  # JAMAIS « par unité de l'enchère »
        assert view.reliability == PricingReliability.UNKNOWN_BASIS

    def test_the_new_columns_are_nullable_and_have_no_default(self, db):
        with db.cursor() as cur:
            cur.execute(
                """SELECT table_name, column_name, is_nullable, column_default
                   FROM information_schema.columns
                   WHERE table_schema = 'marketplace'
                     AND ((table_name = 'order_items' AND column_name IN ('quantity_unit','commercial_price_amount','price_basis',
                            'price_unit','package_type','package_content_amount','package_content_unit','normalized_unit_price',
                            'normalized_unit','currency','pricing_snapshot_version'))
                       OR (table_name = 'bids' AND column_name IN ('offered_price_basis','offered_price_unit','offered_price_currency',
                            'package_type','package_content_amount','package_content_unit','normalized_unit_price','normalized_unit',
                            'pricing_snapshot_version'))
                       OR (table_name = 'products' AND column_name = 'commercial_pricing')
                       OR (table_name = 'market_offers' AND column_name = 'pricing_snapshot')
                       OR (table_name = 'orders' AND column_name = 'award_pricing_snapshot'))"""
            )
            rows = cur.fetchall()
        assert len(rows) == 23
        assert all(r[2] == "YES" and r[3] is None for r in rows), rows


# =====================================================================
# OrderItem : instantané immuable
# =====================================================================


class TestOrderItemSnapshot:
    def test_package_snapshot_persists_and_reloads_exactly(self, db, g):
        with db.cursor() as cur:
            item = _item(cur, g, g.order(), base_unit_quantity=2, tier_id="t1", **_sachet_columns())
        row = _row(db, "order_items", item)
        assert (row.price_basis, row.package_type, row.quantity_unit) == ("PER_PACKAGE", "SACHET", "SACHET")
        assert (row.commercial_price_amount, row.package_content_amount, row.package_content_unit) == (D("500.00"), D("0.500"), "LITRE")
        assert (row.normalized_unit_price, row.normalized_unit, row.currency) == (D("1000.0000"), "LITRE", "XOF")
        back = CommercialPricingSnapshot.from_order_item(row)
        assert back.price_basis == PriceBasis.PER_PACKAGE and back.commercial_price_amount == D("500")

    def test_mutating_the_product_never_changes_a_historical_order_item(self, db, g):
        with db.cursor() as cur:
            item = _item(cur, g, g.order(), base_unit_quantity=2, tier_id="t1", **_sachet_columns())
            before = _row(db, "order_items", item)
            cur.execute(
                "UPDATE marketplace.products SET price = 9999, unit = 'KG', pricing_tiers = %s, commercial_pricing = %s WHERE id = %s",
                (Json([{"tier_id": "t1", "quantity": 1, "unit": "KG", "price": 9999, "packaging": "sac"}]),
                 Json({"schema_version": 1}), g.product),
            )
        after = _row(db, "order_items", item)
        assert vars(after) == vars(before)
        assert order_item_pricing_view(after).snapshot.package_content_amount == D("0.5")

    def test_the_snapshot_cannot_be_rewritten(self, db, g):
        with db.cursor() as cur:
            item = _item(cur, g, g.order(), base_unit_quantity=2, tier_id="t1", **_sachet_columns())
            for col, val in (("commercial_price_amount", 999), ("price_basis", "TOTAL_LOT"), ("package_content_amount", 1),
                             ("normalized_unit_price", 1), ("currency", "EUR"), ("pricing_snapshot_version", 2)):
                with violation(cur, errors.CheckViolation):
                    cur.execute(f"UPDATE marketplace.order_items SET {col} = %s WHERE id = %s", (val, item))

    def test_the_economic_fields_are_frozen_with_the_snapshot(self, db, g):
        """B2b (étape 2) : `quantity`, `base_unit_quantity`, `price_at_sale`… ne divergent JAMAIS du snapshot."""
        with db.cursor() as cur:
            item = _item(cur, g, g.order(), base_unit_quantity=2, tier_id="t1", **_sachet_columns())
            for col, val in (("quantity", 5), ("base_unit_quantity", 3), ("price_at_sale", 1), ("tier_id", "t2")):
                with violation(cur, errors.CheckViolation):
                    cur.execute(f"UPDATE marketplace.order_items SET {col} = %s WHERE id = %s", (val, item))
            with violation(cur, errors.CheckViolation):
                cur.execute("UPDATE marketplace.order_items SET order_id = %s WHERE id = %s", (g.order(), item))
        row = _row(db, "order_items", item)
        assert (row.quantity, row.base_unit_quantity, row.price_at_sale) == (D("4.000"), D("2.000"), D("500.00"))

    def test_a_row_without_a_snapshot_stays_editable(self, db, g):
        with db.cursor() as cur:
            item = _item(cur, g, g.order())
            cur.execute("UPDATE marketplace.order_items SET quantity = 5, price_at_sale = 400 WHERE id = %s", (item,))

    def test_an_identical_rewrite_is_not_a_violation(self, db, g):
        with db.cursor() as cur:
            item = _item(cur, g, g.order(), base_unit_quantity=2, tier_id="t1", **_sachet_columns())
            cur.execute("UPDATE marketplace.order_items SET quantity = quantity WHERE id = %s", (item,))

    def test_a_legacy_row_can_be_backfilled_once_but_not_rewritten(self, db, g):
        cols = _sachet_columns()
        with db.cursor() as cur:
            item = _item(cur, g, g.order())
            sets = ", ".join(f"{k} = %s" for k in cols)
            cur.execute(f"UPDATE marketplace.order_items SET {sets} WHERE id = %s", (*cols.values(), item))
            with violation(cur, errors.CheckViolation):
                cur.execute("UPDATE marketplace.order_items SET commercial_price_amount = 1 WHERE id = %s", (item,))

    def test_total_lot_keeps_the_exact_total_and_a_rounded_derivative(self, db, g):
        cols = build_total_lot_order_item_snapshot(
            total_amount=1_000_000, quantity=3, unit="KG", price_at_sale=D("333333.33")
        ).to_order_item_columns()
        with db.cursor() as cur:
            item = _item(cur, g, g.order(), quantity=3, price_at_sale=D("333333.33"), **cols)
        row = _row(db, "order_items", item)
        assert row.price_basis == "TOTAL_LOT" and row.commercial_price_amount == D("1000000.00")
        assert row.normalized_unit_price == D("333333.3333")  # dérivé arrondi, le total ne change JAMAIS
        assert order_item_pricing_view(row).snapshot.total_for(3, "KG") == D("1000000.00")


class TestOrderItemConstraints:
    def _bad(self, db, g, **cols):
        with db.cursor() as cur:
            order = g.order()
            with violation(cur, errors.CheckViolation):
                _item(cur, g, order, **cols)

    def test_versioned_row_with_a_null_amount_or_basis_is_rejected(self, db, g):
        ok = _sachet_columns()
        self._bad(db, g, **{**ok, "commercial_price_amount": None})
        self._bad(db, g, **{**ok, "price_basis": None})
        self._bad(db, g, **{**ok, "package_content_amount": None})

    def test_half_written_snapshot_without_a_version_is_rejected(self, db, g):
        self._bad(db, g, price_basis="PER_BASE_UNIT", price_unit="KG")

    def test_impossible_combinations_are_rejected(self, db, g):
        ok = _sachet_columns()
        self._bad(db, g, **{**ok, "package_type": None})                       # PER_PACKAGE sans conditionnement
        self._bad(db, g, **{**ok, "package_content_amount": 0})
        self._bad(db, g, **{**ok, "price_basis": "PER_KG"})                    # base inconnue
        self._bad(db, g, **{**ok, "commercial_price_amount": 0})
        per_unit = build_order_item_pricing_snapshot(product_price=300, product_unit="KG", quantity=10, price_at_sale=300).to_order_item_columns()
        self._bad(db, g, **{**per_unit, "price_unit": None})                   # PER_BASE_UNIT sans unité
        self._bad(db, g, **{**per_unit, "package_type": "SAC"})               # PER_BASE_UNIT avec conditionnement
        lot = build_total_lot_order_item_snapshot(total_amount=1000, quantity=2, unit="KG").to_order_item_columns()
        self._bad(db, g, **{**lot, "price_unit": "KG"})                        # TOTAL_LOT avec unité de prix


# =====================================================================
# Bid : la base du prix est persistée, jamais déduite
# =====================================================================


class TestBidSnapshot:
    def test_450000_per_tonne_persists_and_reloads_exactly(self, db, g):
        cols = build_bid_pricing_snapshot(
            amount=450000, basis="PER_BASE_UNIT", price_unit="TONNE", auction_quantity=10, auction_unit="TONNE"
        ).to_bid_columns()
        with db.cursor() as cur:
            bid = insert(cur, "marketplace.bids", auction_id=g.auction(), producer_id=g.producer, **cols)
        row = _row(db, "bids", bid)
        assert (row.offered_price, row.offered_price_basis, row.offered_price_unit) == (D("450000.00"), "PER_BASE_UNIT", "TONNE")
        assert row.offered_price_currency == "XOF" and row.pricing_snapshot_version == 1
        view = bid_pricing_view(row)
        assert view.reliability == PricingReliability.CERTIFIED and view.snapshot.price_unit == "TONNE"

    def test_total_lot_and_package_bids_are_accepted(self, db, g):
        lot = build_bid_pricing_snapshot(
            amount=4_500_000, basis="TOTAL_LOT", price_unit=None, auction_quantity=10, auction_unit="TONNE"
        ).to_bid_columns()
        pkg = build_bid_pricing_snapshot(
            amount=500, basis="PER_PACKAGE", price_unit=None, auction_quantity=50, auction_unit="LITRE",
            package_type="sachet", package_content_amount=0.5, package_content_unit="LITRE",
        ).to_bid_columns()
        with db.cursor() as cur:
            insert(cur, "marketplace.bids", auction_id=g.auction(), producer_id=g.producer, **lot)
            insert(cur, "marketplace.bids", auction_id=g.auction(), producer_id=g.producer, **pkg)

    def test_the_legacy_marker_is_accepted_without_a_version_and_reads_as_unknown(self, db, g):
        with db.cursor():
            bid = g.bid(g.auction(), offered_price=450000, offered_price_basis="LEGACY_UNSPECIFIED")
        assert bid_pricing_view(_row(db, "bids", bid)).reliability == PricingReliability.UNKNOWN_BASIS

    def test_invalid_bids_are_rejected(self, db, g):
        ok = build_bid_pricing_snapshot(
            amount=450000, basis="PER_BASE_UNIT", price_unit="TONNE", auction_quantity=10, auction_unit="TONNE"
        ).to_bid_columns()
        with db.cursor() as cur:
            auction = g.auction()
            for bad in ({**ok, "offered_price_basis": "PER_KG"},               # base inventée
                        {**ok, "offered_price_basis": None},                   # versionné sans base
                        {**ok, "offered_price_unit": None},                    # PER_BASE_UNIT sans unité
                        {**ok, "package_type": "SAC"},                         # conditionnement hors PER_PACKAGE
                        {**ok, "offered_price_basis": "LEGACY_UNSPECIFIED"}):  # marqueur legacy + version
                with violation(cur, errors.CheckViolation):
                    insert(cur, "marketplace.bids", auction_id=auction, producer_id=g.producer, **bad)


# =====================================================================
# Colonnes JSONB : produit, offre de marché, attribution d'appel d'offres
# =====================================================================


class TestJsonbSnapshots:
    def test_product_commercial_pricing_requires_a_versioned_object(self, db, g):
        with db.cursor() as cur:
            cur.execute("UPDATE marketplace.products SET commercial_pricing = %s WHERE id = %s",
                        (Json({"schema_version": 1, "price_basis": "TOTAL_LOT"}), g.product))
            for bad in ([1, 2], {"price_basis": "TOTAL_LOT"}, "text"):
                with violation(cur, errors.CheckViolation):
                    cur.execute("UPDATE marketplace.products SET commercial_pricing = %s WHERE id = %s", (Json(bad), g.product))

    def test_market_offer_pricing_snapshot_requires_a_versioned_object(self, db, g):
        with db.cursor() as cur:
            offer = insert(cur, "marketplace.market_offers", producer_id=g.producer, product_label="lait",
                           pricing_snapshot={"schema_version": 1})
            with violation(cur, errors.CheckViolation):
                cur.execute("UPDATE marketplace.market_offers SET pricing_snapshot = %s WHERE id = %s", (Json({"x": 1}), offer))

    def test_the_award_snapshot_is_written_once_and_frozen(self, db, g):
        bid_columns = build_bid_pricing_snapshot(
            amount=450000, basis="PER_BASE_UNIT", price_unit="TONNE", auction_quantity=10, auction_unit="TONNE"
        )
        frozen = {**bid_columns.to_dict(), "award": {"auction_quantity": "10", "auction_unit": "TONNE", "total_amount": "4500000.00"}}
        with db.cursor() as cur:
            auction = g.auction()
            bid = g.bid(auction)
            order = g.order(auction_id=auction, winning_bid_id=bid, award_pricing_snapshot=frozen)
            cur.execute("SELECT award_pricing_snapshot FROM marketplace.orders WHERE id = %s", (order,))
            stored = cur.fetchone()[0]
            assert stored["price_basis"] == "PER_BASE_UNIT" and stored["award"]["total_amount"] == "4500000.00"
            assert CommercialPricingSnapshot.from_dict(stored).price_unit == "TONNE"
            with violation(cur, errors.CheckViolation):
                cur.execute("UPDATE marketplace.orders SET award_pricing_snapshot = %s WHERE id = %s", (Json({**frozen, "price_unit": "KG"}), order))
            cur.execute("UPDATE marketplace.orders SET total_amount = total_amount + 1 WHERE id = %s", (order,))  # le reste reste éditable

    def test_a_legacy_award_has_no_snapshot(self, db, g):
        with db.cursor() as cur:
            order = g.order(auction_id=g.auction())
            cur.execute("SELECT award_pricing_snapshot FROM marketplace.orders WHERE id = %s", (order,))
            assert cur.fetchone()[0] is None


# =====================================================================
# Phase B2b — Bid -> Award
# =====================================================================


class TestBidRequalificationAndBasisChange:
    """Un bid reste modifiable (négociation) : requalifier un bid ANCIEN et changer la base d'un bid certifié doivent
    passer les CHECK ; une mise à jour PARTIELLE (base changée, unité oubliée) doit être refusée."""

    def _cols(self, **kw):
        return build_bid_pricing_snapshot(
            auction_quantity=10, auction_unit="TONNE", **kw
        ).to_bid_columns()

    def _update(self, cur, bid, cols):
        sets = ", ".join(f"{k} = %s" for k in cols)
        cur.execute(f"UPDATE marketplace.bids SET {sets} WHERE id = %s", (*cols.values(), bid))

    def test_a_legacy_bid_is_requalified_then_its_basis_changes_to_total_lot(self, db, g):
        with db.cursor() as cur:
            bid = g.bid(g.auction(), offered_price=450000)
            self._update(cur, bid, self._cols(amount=450000, basis="PER_BASE_UNIT", price_unit="TONNE"))
            assert bid_pricing_view(_row(db, "bids", bid)).basis == PriceBasis.PER_BASE_UNIT
            self._update(cur, bid, self._cols(amount=4_000_000, basis="TOTAL_LOT", price_unit=None))
        view = bid_pricing_view(_row(db, "bids", bid))
        assert (view.basis, view.snapshot.price_unit, view.amount) == (PriceBasis.TOTAL_LOT, None, D("4000000.00"))

    def test_a_partial_basis_change_is_rejected(self, db, g):
        with db.cursor() as cur:
            bid = g.bid(g.auction(), offered_price=450000)
            self._update(cur, bid, self._cols(amount=450000, basis="PER_BASE_UNIT", price_unit="TONNE"))
            with violation(cur, errors.CheckViolation):
                cur.execute("UPDATE marketplace.bids SET offered_price_basis = 'TOTAL_LOT' WHERE id = %s", (bid,))


class TestAwardSnapshotFromACertifiedDecision:
    def test_the_frozen_snapshot_of_a_decision_persists_and_cannot_be_rewritten(self, db, g):
        from ladini.domain.bid_award import build_award_decision

        snap = build_bid_pricing_snapshot(
            amount=450000, basis="PER_BASE_UNIT", price_unit="TONNE", auction_quantity=10, auction_unit="TONNE",
        )
        decision = build_award_decision(
            auction_id="a", bid_id="b", producer_id="p", buyer_id="u", producer_name="Gilbert-prod",
            pricing=snap, auction_quantity=10, auction_unit="TONNE",
        )
        frozen = decision.frozen_snapshot()
        with db.cursor() as cur:
            auction = g.auction()
            bid = g.bid(auction)
            order = g.order(auction_id=auction, winning_bid_id=bid, award_pricing_snapshot=frozen, total_amount=decision.award_total)
            cur.execute("SELECT award_pricing_snapshot, total_amount FROM marketplace.orders WHERE id = %s", (order,))
            stored, total = cur.fetchone()
            assert stored["award"]["fingerprint"] == decision.fingerprint and total == D("4500000.00")
            assert stored["award"]["total_amount"] == "4500000.00"
            with violation(cur, errors.CheckViolation):
                cur.execute(
                    "UPDATE marketplace.orders SET award_pricing_snapshot = %s WHERE id = %s",
                    (Json({**frozen, "award": {**frozen["award"], "total_amount": "1"}}), order),
                )

    def test_a_total_lot_award_stores_the_lot_amount_not_a_multiplied_total(self, db, g):
        from ladini.domain.bid_award import build_award_decision

        snap = build_bid_pricing_snapshot(
            amount=4_200_000, basis="TOTAL_LOT", price_unit=None, auction_quantity=10, auction_unit="TONNE",
        )
        decision = build_award_decision(
            auction_id="a", bid_id="b", producer_id="p", buyer_id="u", producer_name="X",
            pricing=snap, auction_quantity=10, auction_unit="TONNE",
        )
        with db.cursor() as cur:
            auction = g.auction()
            order = g.order(auction_id=auction, award_pricing_snapshot=decision.frozen_snapshot(), total_amount=decision.award_total)
            cur.execute("SELECT total_amount, award_pricing_snapshot->>'price_basis' FROM marketplace.orders WHERE id = %s", (order,))
            assert cur.fetchone() == (D("4200000.00"), "TOTAL_LOT")
