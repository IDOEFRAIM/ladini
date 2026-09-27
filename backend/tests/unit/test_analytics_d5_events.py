"""Phase D.5 — DIRECT_ORDER_CREATED/CONFIRMED emission rules and the recurring quantity_delivered
writer, without a database (the same scenarios run against PostgreSQL in
tests/schema/test_analytics_d5_pg.py, in CI)."""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.conftest import run


@pytest.fixture
def outbox(monkeypatch):
    rows: dict[str, dict] = {}

    async def _enqueue(session, *, event_name, journey, payload, dedupe_key):
        if dedupe_key in rows:
            return False
        rows[dedupe_key] = {"event_name": event_name, "payload": payload}
        return True

    monkeypatch.setattr("ladini.domain.analytics.emitter.analytics_outbox_repo.enqueue", _enqueue)
    return rows


def _order(**over):
    base = dict(id=uuid.uuid4(), buyer_id=uuid.uuid4(), zone_id=uuid.uuid4(), auction_id=None, order_type="PREORDER",
                market_offer_id=None, total_amount=1500, status="CONFIRMED", items=[])
    base.update(over)
    return SimpleNamespace(**base)


def _emitter():
    from ladini.domain.analytics.emitter import BusinessEventEmitter

    # Producer Analytics Phase B: emit_direct_order_created/confirmed now resolve
    # producer_id, falling back to one bounded query when it can't be read off
    # already-loaded items (e.g. these fixtures' items never carry a producer_id) —
    # a real session answers that query; here it resolves to "unknown" (None),
    # which none of these DIRECT-journey/idempotency/dimension tests assert on.
    session = SimpleNamespace(scalar=AsyncMock(return_value=None))
    return BusinessEventEmitter(session=session)


class TestDirectOrderConfirmed:
    def test_emitted_for_a_direct_order_with_the_order_keyed_idempotency(self, outbox):
        o = _order()
        assert run(_emitter().emit_direct_order_confirmed(o, actor_type="PRODUCER")) is True
        assert list(outbox) == [f"DIRECT_ORDER_CONFIRMED:{o.id}"]
        assert outbox[f"DIRECT_ORDER_CONFIRMED:{o.id}"]["payload"]["journey"] == "DIRECT"

    def test_retry_or_second_writer_never_duplicates(self, outbox):
        o = _order()
        run(_emitter().emit_direct_order_confirmed(o, actor_type="PRODUCER"))
        assert run(_emitter().emit_direct_order_confirmed(o, actor_type="SYSTEM")) is False  # e.g. escrow path after a replay
        assert len(outbox) == 1

    @pytest.mark.parametrize("over", [
        {"auction_id": uuid.uuid4()},          # tender order
        {"order_type": "RECURRING_SUPPLY"},    # recurring supply
        {"market_offer_id": uuid.uuid4()},     # future-production reservation
        {"buyer_id": None},                    # walk-in sale
    ])
    def test_never_emitted_outside_the_direct_journey(self, outbox, over):
        assert run(_emitter().emit_direct_order_confirmed(_order(**over), actor_type="PRODUCER")) is False
        assert outbox == {}


class TestDirectOrderCreated:
    def test_single_untiered_item_carries_subcategory_quantity_and_unit(self, outbox):
        sub = uuid.uuid4()
        item = SimpleNamespace(quantity=20, tier_id=None, product=SimpleNamespace(sub_category_id=sub, unit="KG"))
        o = _order(items=[item])
        run(_emitter().emit_direct_order_created(o))
        p = outbox[f"DIRECT_ORDER_CREATED:{o.id}"]["payload"]
        assert (p["sub_category_id"], p["quantity"], p["unit"]) == (str(sub), 20, "KG")

    def test_mixed_cart_gets_no_guessed_dimensions(self, outbox):
        items = [SimpleNamespace(quantity=1, tier_id=None, product=SimpleNamespace(sub_category_id=uuid.uuid4(), unit="KG")) for _ in range(2)]
        o = _order(items=items)
        run(_emitter().emit_direct_order_created(o))
        p = outbox[f"DIRECT_ORDER_CREATED:{o.id}"]["payload"]
        assert p["sub_category_id"] is None and p["quantity"] is None

    def test_unloaded_items_are_never_lazy_loaded(self, outbox):
        class _Lazy(SimpleNamespace):
            @property
            def items(self):  # would raise MissingGreenlet in real life
                raise AssertionError("lazy load attempted")

        o = _Lazy(**{k: v for k, v in vars(_order()).items() if k != "items"})
        run(_emitter().emit_direct_order_created(o))
        assert len(outbox) == 1

    def test_tender_and_recurring_orders_do_not_emit(self, outbox):
        for over in ({"auction_id": uuid.uuid4()}, {"order_type": "RECURRING_SUPPLY"}):
            assert run(_emitter().emit_direct_order_created(_order(**over))) is False
        assert outbox == {}


class TestDeliveredHelperSkipsFutureProduction:
    def test_reservation_delivery_is_not_a_direct_delivery(self, outbox):
        assert run(_emitter().emit_order_delivered(_order(market_offer_id=uuid.uuid4()))) is False
        assert outbox == {}


class TestQuantityDeliveredWriter:
    def _svc(self, occurrence, delivered):
        from ladini.services.database.recurring_supply import RecurringSupplyMixin

        session = SimpleNamespace(
            scalar=AsyncMock(side_effect=[occurrence, delivered]) if occurrence is not None else AsyncMock(return_value=None),
            flush=AsyncMock(),
        )

        class _S(RecurringSupplyMixin):
            @property
            def session(self):
                return session

        return _S(), session

    def _occ(self, delivered=0):
        return SimpleNamespace(id=uuid.uuid4(), quantity_delivered=delivered, version=3)

    def test_sets_the_value_recomputed_from_received_orders_and_bumps_the_version(self):
        occ = self._occ()
        svc, _ = self._svc(occ, 70)
        out = run(svc._refresh_occurrence_quantity_delivered(_order(checkout_group_id=uuid.uuid4())))
        assert out == 70 and occ.quantity_delivered == 70 and occ.version == 4

    def test_retry_is_a_no_op_same_value_same_version(self):
        occ = self._occ(delivered=70)
        svc, session = self._svc(occ, 70)
        run(svc._refresh_occurrence_quantity_delivered(_order(checkout_group_id=uuid.uuid4())))
        assert occ.quantity_delivered == 70 and occ.version == 3
        session.flush.assert_not_awaited()

    def test_order_without_a_group_or_occurrence_is_ignored(self):
        svc, _ = self._svc(None, 0)
        assert run(svc._refresh_occurrence_quantity_delivered(_order(checkout_group_id=None))) is None
        assert run(svc._refresh_occurrence_quantity_delivered(_order(checkout_group_id=uuid.uuid4()))) is None
