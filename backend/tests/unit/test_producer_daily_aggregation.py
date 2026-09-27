"""Producer Analytics Phase C — pure aggregation rules (no database). Mirrors
`test_analytics_daily_aggregation.py`'s style/scope for the producer side:
activity-only rows, per-journey confirmed/delivered rules, delivered<=confirmed
safety net, GMV attribution, and unit segmentation."""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from ladini.domain.analytics.producer_daily_aggregation import (
    ProducerActivityEventFact,
    ProducerOrderFact,
    ProducerQuantityItemFact,
    aggregate_producer_daily,
    aggregate_producer_quantity_daily,
    order_is_confirmed,
    order_is_delivered,
)

DAY = date(2026, 9, 20)
PRODUCER = str(uuid.uuid4())
ZONE = str(uuid.uuid4())


def _order(**over):
    base = dict(order_id=uuid.uuid4(), producer_id=PRODUCER, zone_id=ZONE, journey="DIRECT",
                total_amount=1000, delivery_status="PENDING", confirmed=False)
    base.update(over)
    return ProducerOrderFact(**base)


def _event(**over):
    base = dict(event_name="PRODUCT_PUBLISHED_FOR_SALE", producer_id=PRODUCER, zone_id=ZONE)
    base.update(over)
    return ProducerActivityEventFact(**base)


def _item(**over):
    base = dict(producer_id=PRODUCER, journey="DIRECT", unit="KG", priority_unit="KG",
                quantity=10, delivery_status="PENDING", confirmed=False)
    base.update(over)
    return ProducerQuantityItemFact(**base)


class TestActiveProducerRowExistence:
    def test_a_producer_with_no_qualifying_fact_gets_no_row(self):
        assert aggregate_producer_daily(DAY, [], []) == []

    def test_an_order_alone_creates_a_row(self):
        rows = aggregate_producer_daily(DAY, [_order(confirmed=True)], [])
        assert len(rows) == 1 and rows[0]["producer_id"] == PRODUCER

    def test_a_pure_activity_event_alone_creates_a_row(self):
        rows = aggregate_producer_daily(DAY, [], [_event()])
        assert len(rows) == 1 and rows[0]["products_published"] == 1

    def test_an_order_without_a_producer_is_ignored(self):
        assert aggregate_producer_daily(DAY, [_order(producer_id=None, confirmed=True)], []) == []


class TestDirectConfirmedDeliveredRule:
    def test_delivered_implies_confirmed_even_if_the_confirmed_flag_was_missed(self):
        """Safety net mirroring the buyer layer's `direct_is_confirmed`: a
        delivered order counts as confirmed even if the SQL-side CONFIRMED
        predicate somehow missed it — fulfillment rate can never exceed 100%."""
        o = _order(confirmed=False, delivery_status="DELIVERED")
        assert order_is_confirmed(o) is True
        rows = aggregate_producer_daily(DAY, [o], [])
        assert rows[0]["orders_confirmed_direct"] == 1 and rows[0]["orders_delivered_direct"] == 1

    def test_confirmed_but_not_delivered_only_increments_confirmed(self):
        rows = aggregate_producer_daily(DAY, [_order(confirmed=True, delivery_status="PENDING")], [])
        assert rows[0]["orders_confirmed_direct"] == 1 and rows[0]["orders_delivered_direct"] == 0

    def test_delivered_gmv_only_accrues_on_delivery_not_on_confirmation(self):
        rows = aggregate_producer_daily(DAY, [_order(confirmed=True, delivery_status="PENDING", total_amount=5000)], [])
        assert rows[0]["delivered_gmv_direct"] == Decimal("0")
        rows = aggregate_producer_daily(DAY, [_order(confirmed=True, delivery_status="DELIVERED", total_amount=5000)], [])
        assert rows[0]["delivered_gmv_direct"] == Decimal("5000")


class TestTenderAndRecurringCommitmentRule:
    def test_tender_order_is_always_confirmed_its_creation_is_the_commitment(self):
        o = _order(journey="TENDER", confirmed=True, delivery_status="PENDING")
        rows = aggregate_producer_daily(DAY, [o], [])
        assert rows[0]["orders_confirmed_tender"] == 1 and rows[0]["orders_delivered_tender"] == 0

    def test_tender_delivered_requires_delivered_or_fulfilled_status(self):
        o = _order(journey="TENDER", confirmed=True, delivery_status="FULFILLED")
        assert order_is_delivered(o) is True

    def test_recurring_order_is_always_confirmed_its_creation_is_the_commitment(self):
        o = _order(journey="RECURRING", confirmed=True, delivery_status="PENDING")
        rows = aggregate_producer_daily(DAY, [o], [])
        assert rows[0]["orders_confirmed_recurring"] == 1

    def test_recurring_delivered_requires_received_not_delivered(self):
        """RECURRING uses the buyer-confirmed RECEIVED state, not the producer's
        own DELIVERED claim — same rule the buyer layer already applies."""
        o = _order(journey="RECURRING", confirmed=True, delivery_status="DELIVERED")
        assert order_is_delivered(o) is False
        o2 = _order(journey="RECURRING", confirmed=True, delivery_status="RECEIVED")
        assert order_is_delivered(o2) is True


class TestActivitySignalCounts:
    def test_quantity_change_and_bid_received_are_counted_separately(self):
        rows = aggregate_producer_daily(DAY, [], [
            _event(event_name="PRODUCT_SELLABLE_QUANTITY_CHANGED"),
            _event(event_name="PRODUCT_SELLABLE_QUANTITY_CHANGED"),
            _event(event_name="TENDER_BID_RECEIVED"),
        ])
        assert rows[0]["quantity_changes"] == 2 and rows[0]["bids_received"] == 1 and rows[0]["products_published"] == 0

    def test_an_order_and_an_activity_event_merge_into_one_row(self):
        rows = aggregate_producer_daily(DAY, [_order(confirmed=True)], [_event()])
        assert len(rows) == 1
        assert rows[0]["orders_confirmed_direct"] == 1 and rows[0]["products_published"] == 1


class TestQuantityUnitSafety:
    def test_grams_are_converted_into_the_priority_unit(self):
        rows = aggregate_producer_quantity_daily(DAY, [
            _item(journey="DIRECT", unit="G", priority_unit="KG", quantity=1500, confirmed=True, delivery_status="DELIVERED"),
        ])
        assert len(rows) == 1
        assert rows[0]["canonical_unit"] == "KG" and rows[0]["measurement_family"] == "MASS"
        assert rows[0]["confirmed_quantity_direct"] == Decimal("1.5") and rows[0]["delivered_quantity_direct"] == Decimal("1.5")

    def test_kg_and_tete_are_never_summed_into_one_row(self):
        rows = aggregate_producer_quantity_daily(DAY, [
            _item(unit="KG", priority_unit="KG", quantity=100, confirmed=True),
            _item(unit="TETE", priority_unit="KG", quantity=5, confirmed=True),
        ])
        assert {r["canonical_unit"] for r in rows} == {"KG", "TETE"}
        assert {r["measurement_family"] for r in rows} == {"MASS", "COUNT"}

    def test_direct_and_recurring_quantities_accumulate_in_their_own_columns(self):
        rows = aggregate_producer_quantity_daily(DAY, [
            _item(journey="DIRECT", quantity=10, confirmed=True, delivery_status="DELIVERED"),
            _item(journey="RECURRING", quantity=20, confirmed=True, delivery_status="RECEIVED"),
        ])
        assert len(rows) == 1
        assert rows[0]["delivered_quantity_direct"] == Decimal("10") and rows[0]["delivered_quantity_recurring"] == Decimal("20")

    def test_an_item_without_a_producer_is_ignored(self):
        assert aggregate_producer_quantity_daily(DAY, [_item(producer_id=None, confirmed=True)]) == []


class TestDeterminism:
    def test_output_is_sorted_and_a_replay_is_identical(self):
        p1, p2 = str(uuid.uuid4()), str(uuid.uuid4())
        orders = [_order(producer_id=p2, confirmed=True), _order(producer_id=p1, confirmed=True)]
        first = aggregate_producer_daily(DAY, orders, [])
        second = aggregate_producer_daily(DAY, orders, [])
        assert first == second
        assert [r["producer_id"] for r in first] == sorted(r["producer_id"] for r in first)
