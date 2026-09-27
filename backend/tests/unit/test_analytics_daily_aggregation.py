"""Phase D — pure aggregation rules (no database). Covers weighted rates, unit segmentation,
cohort/state rules, determinism, and the honest RECURRING receipt rule."""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from ladini.domain.analytics.daily_aggregation import (
    NIL_UUID,
    DigestFact,
    DirectOrderFact,
    RecurringFact,
    SearchFact,
    TenderFact,
    aggregate_buyer,
    aggregate_direct,
    aggregate_recurring,
    aggregate_tender,
    resolve_canonical,
)

DAY = date(2026, 9, 20)
ZONE, CAT, SUB = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())


def _rec(**over):
    base = dict(
        occurrence_id=uuid.uuid4(), need_id=uuid.uuid4(), buyer_id=uuid.uuid4(), zone_id=ZONE, category_id=CAT,
        sub_category_id=SUB, priority_unit="KG", unit="KG", requested_quantity=100, quantity_matched=0,
        quantity_confirmed=0, status="OPEN", notified=False, potential_value=0, confirmed_value=0,
        orders_count=0, received_orders_count=0, received_value=0,
    )
    base.update(over)
    return RecurringFact(**base)


class TestRecurringUnits:
    def test_grams_are_converted_into_the_subcategory_priority_unit(self):
        rows = aggregate_recurring(DAY, [_rec(unit="G", requested_quantity=1500, quantity_matched=500)])
        assert len(rows) == 1
        assert rows[0]["canonical_unit"] == "KG" and rows[0]["requested_quantity"] == Decimal("1.5")
        assert rows[0]["matched_quantity"] == Decimal("0.5") and rows[0]["measurement_family"] == "MASS"

    def test_kg_and_litres_are_never_summed_into_one_row(self):
        rows = aggregate_recurring(DAY, [
            _rec(requested_quantity=100), _rec(unit="L", priority_unit="L", requested_quantity=500, sub_category_id=str(uuid.uuid4())),
        ])
        assert {r["canonical_unit"] for r in rows} == {"KG", "L"}
        assert {r["measurement_family"] for r in rows} == {"MASS", "VOLUME"}

    def test_incompatible_unit_keeps_its_own_unit_and_is_not_converted(self):
        assert resolve_canonical("TETE", "KG") == "TETE"
        assert resolve_canonical("SAC", "KG") == "SAC"  # no invented bag size

    def test_two_masses_of_the_same_subcategory_share_a_row(self):
        rows = aggregate_recurring(DAY, [_rec(requested_quantity=100), _rec(unit="G", requested_quantity=2000)])
        assert len(rows) == 1 and rows[0]["requested_quantity"] == Decimal("102.000") or rows[0]["requested_quantity"] == Decimal("102")


class TestRecurringRules:
    def test_unmatched_is_requested_minus_matched_per_occurrence_never_negative(self):
        rows = aggregate_recurring(DAY, [
            _rec(requested_quantity=100, quantity_matched=40),
            _rec(requested_quantity=50, quantity_matched=60),  # over-matched must not offset the first
        ])
        assert rows[0]["unmatched_quantity"] == Decimal("60")

    def test_skipped_occurrences_leave_the_demand_but_count_in_total(self):
        rows = aggregate_recurring(DAY, [_rec(status="SKIPPED", requested_quantity=100), _rec(requested_quantity=50)])
        r = rows[0]
        assert r["occurrences_total"] == 2 and r["occurrences_active"] == 1 and r["occurrences_skipped"] == 1
        assert r["requested_quantity"] == Decimal("50")

    def test_full_coverage_and_acceptance_counts(self):
        rows = aggregate_recurring(DAY, [
            _rec(requested_quantity=100, quantity_matched=100, notified=True, status="ACCEPTED"),
            _rec(requested_quantity=100, quantity_matched=20, notified=True),
        ])
        r = rows[0]
        assert (r["occurrences_fully_covered"], r["occurrences_notified"], r["occurrences_accepted"]) == (1, 2, 1)

    def test_received_requires_every_order_of_the_occurrence_to_be_received(self):
        rows = aggregate_recurring(DAY, [
            _rec(orders_count=2, received_orders_count=2, status="ACCEPTED"),
            _rec(orders_count=2, received_orders_count=1, status="ACCEPTED"),  # one order still pending / with issue
            _rec(orders_count=0, received_orders_count=0, status="ACCEPTED"),
        ])
        assert rows[0]["occurrences_all_received"] == 1 and rows[0]["occurrences_with_orders"] == 2

    def test_needs_with_occurrence_counts_distinct_needs(self):
        need = uuid.uuid4()
        rows = aggregate_recurring(DAY, [_rec(need_id=need), _rec(need_id=need), _rec()])
        assert rows[0]["needs_with_occurrence"] == 2

    def test_missing_zone_uses_the_nil_uuid_not_null(self):
        assert aggregate_recurring(DAY, [_rec(zone_id=None)])[0]["zone_id"] == NIL_UUID

    def test_replay_of_the_same_facts_is_byte_identical(self):
        facts = [_rec(requested_quantity=10 * i, quantity_matched=i) for i in range(1, 6)]
        assert aggregate_recurring(DAY, facts) == aggregate_recurring(DAY, list(reversed(facts)))


def _order(**over):
    base = dict(buyer_id=uuid.uuid4(), zone_id=ZONE, category_id=CAT, sub_category_id=SUB, total_amount=1000,
                status="PENDING", delivery_status="PENDING")
    base.update(over)
    return DirectOrderFact(**base)


class TestDirect:
    def test_search_conversion_parts_are_stored_separately(self):
        rows = aggregate_direct(DAY, [_order()], [SearchFact(ZONE, False)] * 4 + [SearchFact(ZONE, True)] * 3)
        zone_row = next(r for r in rows if r["sub_category_id"] == NIL_UUID)
        assert (zone_row["searches"], zone_row["successful_searches"]) == (4, 3)
        order_row = next(r for r in rows if r["sub_category_id"] == SUB)
        assert order_row["orders_created"] == 1

    def test_delivered_uses_the_delivered_or_fulfilled_rule_and_drafts_are_not_needs(self):
        rows = aggregate_direct(DAY, [
            _order(delivery_status="DELIVERED"), _order(delivery_status="FULFILLED"), _order(),
            _order(status="DRAFT"), _order(status="SUPERSEDED"),
        ], [])
        r = rows[0]
        assert (r["orders_created"], r["orders_delivered"]) == (3, 2)
        assert r["delivered_value"] == Decimal("2000")

    def test_cancelled_orders_are_needs_but_carry_no_potential_value(self):
        r = aggregate_direct(DAY, [_order(status="CANCELLED"), _order()], [])[0]
        assert r["orders_created"] == 2 and r["created_value"] == Decimal("1000")


def _tender(**over):
    base = dict(buyer_id=uuid.uuid4(), zone_id=ZONE, category_id=CAT, sub_category_id=SUB, quantity=10, max_price_per_unit=100,
                bids_count=0, first_bid_latency_seconds=None, has_winner=False, winner_price=None, orders_count=0,
                delivered_orders_count=0)
    base.update(over)
    return TenderFact(**base)


class TestTender:
    def test_response_winner_and_latency_parts(self):
        r = aggregate_tender(DAY, [
            _tender(),
            _tender(bids_count=3, first_bid_latency_seconds=600.0),
            _tender(bids_count=1, first_bid_latency_seconds=200.0, has_winner=True, winner_price=90, orders_count=1, delivered_orders_count=1),
        ])[0]
        assert (r["tenders_created"], r["tenders_with_bid"], r["bids_received"], r["tenders_with_winner"]) == (3, 2, 4, 1)
        assert (r["first_bid_latency_seconds_sum"], r["first_bid_latency_count"]) == (Decimal("800.0"), 2)
        assert (r["committed_value"], r["delivered_value"], r["tender_orders_delivered"]) == (Decimal("900"), Decimal("900"), 1)


class TestBuyer:
    def test_one_row_per_buyer_and_walk_in_orders_are_ignored(self):
        b1, b2 = uuid.uuid4(), uuid.uuid4()
        rows = aggregate_buyer(
            DAY,
            [_order(buyer_id=b1, delivery_status="DELIVERED"), _order(buyer_id=b1), _order(buyer_id=None)],
            [_tender(buyer_id=b2, has_winner=True, winner_price=50, delivered_orders_count=1, orders_count=1)],
            [_rec(buyer_id=b1, orders_count=1, received_orders_count=1, status="ACCEPTED"), _rec(buyer_id=b1, status="SKIPPED")],
            [DigestFact(b1, ZONE, False), DigestFact(b1, ZONE, True)],
        )
        assert len(rows) == 2
        r1 = next(r for r in rows if r["buyer_id"] == str(b1))
        assert (r1["needs_direct"], r1["satisfied_direct"], r1["needs_recurring"], r1["satisfied_recurring"]) == (2, 1, 1, 1)
        assert (r1["digests_queued"], r1["digests_accepted"]) == (1, 1)
        r2 = next(r for r in rows if r["buyer_id"] == str(b2))
        assert (r2["needs_tender"], r2["satisfied_tender"], r2["confirmed_gmv_tender"]) == (1, 1, Decimal("500"))

    def test_no_confirmed_direct_column_exists(self):
        row = aggregate_buyer(DAY, [_order()], [], [], [])[0]
        assert not any(k.startswith("confirmed_gmv_direct") for k in row)
