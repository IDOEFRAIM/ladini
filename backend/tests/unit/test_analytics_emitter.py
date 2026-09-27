"""`domain/analytics/emitter.py` + `workers/repositories/analytics_outbox_repo.py`
— no real Postgres here (see `tests/schema/test_analytics_event_outbox.py` for
that), only the emitter's own logic: canonical-unit computation, validation-
before-any-DB-call, and idempotency-key pass-through (mission section 14:
"same transition replay -> no duplicate event" — proven at the repo/DB layer
in `tests/schema/`; this file proves the EMITTER constructs a deterministic,
non-random key every time, which is the precondition for that DB-layer
dedup to actually work)."""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.conftest import run


class _FakeExecResult:
    def __init__(self, *, first_row=None):
        self._first_row = first_row

    def first(self):
        return self._first_row


class TestAnalyticsOutboxRepoEnqueue:
    def test_enqueue_returns_true_when_a_row_is_actually_inserted(self):
        from ladini.workers.repositories.analytics_outbox_repo import enqueue

        session = SimpleNamespace(execute=AsyncMock(return_value=_FakeExecResult(first_row=(uuid.uuid4(),))))
        inserted = run(
            enqueue(session, event_name="TENDER_CREATED", journey="TENDER", payload={}, dedupe_key="k1")
        )
        assert inserted is True
        session.execute.assert_awaited_once()

    def test_enqueue_returns_false_on_conflict_never_raises(self):
        from ladini.workers.repositories.analytics_outbox_repo import enqueue

        session = SimpleNamespace(execute=AsyncMock(return_value=_FakeExecResult(first_row=None)))
        inserted = run(
            enqueue(session, event_name="TENDER_CREATED", journey="TENDER", payload={}, dedupe_key="k1")
        )
        assert inserted is False

    def test_claim_due_marks_claimed_rows_as_sending(self):
        from ladini.workers.repositories.analytics_outbox_repo import claim_due

        rows = [SimpleNamespace(id=1, status="PENDING"), SimpleNamespace(id=2, status="PENDING")]

        class _Scalars:
            def scalars(self):
                return SimpleNamespace(all=lambda: rows)

        session = SimpleNamespace(execute=AsyncMock(return_value=_Scalars()), flush=AsyncMock())
        claimed = run(claim_due(session, limit=10))
        assert [r.status for r in claimed] == ["SENDING", "SENDING"]
        session.flush.assert_awaited_once()


class TestBusinessEventEmitter:
    def test_emit_enqueues_with_the_exact_idempotency_key_as_dedupe_key(self, monkeypatch):
        from ladini.domain.analytics.business_events import BusinessEventName
        from ladini.domain.analytics.emitter import BusinessEventEmitter
        from ladini.domain.analytics.metric_dictionary import Journey

        captured = {}

        async def _fake_enqueue(session, *, event_name, journey, payload, dedupe_key):
            captured["dedupe_key"] = dedupe_key
            captured["event_name"] = event_name
            captured["payload"] = payload
            return True

        monkeypatch.setattr(
            "ladini.domain.analytics.emitter.analytics_outbox_repo.enqueue", _fake_enqueue
        )

        entity_id = uuid.uuid4()
        emitter = BusinessEventEmitter(session=object())
        key = f"TENDER_CREATED:{entity_id}"
        result = run(
            emitter.emit(
                event_name=BusinessEventName.TENDER_CREATED,
                journey=Journey.TENDER,
                actor_type="BUYER",
                entity_type="AUCTION",
                entity_id=entity_id,
                idempotency_key=key,
                quantity=100,
                unit="KG",
            )
        )
        assert result is True
        assert captured["dedupe_key"] == key
        assert captured["event_name"] == "TENDER_CREATED"
        # entity_id must be JSON-safe (a str), never a raw UUID object leaking into the payload.
        assert isinstance(captured["payload"]["entity_id"], str)

    def test_emit_computes_canonical_quantity_when_unit_is_convertible(self, monkeypatch):
        from ladini.domain.analytics.business_events import BusinessEventName
        from ladini.domain.analytics.emitter import BusinessEventEmitter
        from ladini.domain.analytics.metric_dictionary import Journey

        captured = {}

        async def _fake_enqueue(session, *, event_name, journey, payload, dedupe_key):
            captured.update(payload)
            return True

        monkeypatch.setattr(
            "ladini.domain.analytics.emitter.analytics_outbox_repo.enqueue", _fake_enqueue
        )

        entity_id = uuid.uuid4()
        emitter = BusinessEventEmitter(session=object())
        # Mission worked example: 1500 G -> canonical KG, 1.5.
        run(
            emitter.emit(
                event_name=BusinessEventName.RECURRING_MATCH_FOUND,
                journey=Journey.RECURRING,
                actor_type="SYSTEM",
                entity_type="RECURRING_NEED_OCCURRENCE",
                entity_id=entity_id,
                idempotency_key=f"RECURRING_MATCH_FOUND:{entity_id}:1",
                quantity=1500,
                unit="G",
                canonical_unit_override="KG",
            )
        )
        assert captured["canonical_quantity"] == pytest.approx(1.5)
        assert captured["canonical_unit"] == "KG"
        assert captured["measurement_family"] == "MASS"

    def test_emit_never_fabricates_canonical_quantity_for_incompatible_units(self, monkeypatch):
        from ladini.domain.analytics.business_events import BusinessEventName
        from ladini.domain.analytics.emitter import BusinessEventEmitter
        from ladini.domain.analytics.metric_dictionary import Journey

        captured = {}

        async def _fake_enqueue(session, *, event_name, journey, payload, dedupe_key):
            captured.update(payload)
            return True

        monkeypatch.setattr(
            "ladini.domain.analytics.emitter.analytics_outbox_repo.enqueue", _fake_enqueue
        )

        entity_id = uuid.uuid4()
        emitter = BusinessEventEmitter(session=object())
        # Mission example: "20 chèvres" -> TETE, no conversion invented.
        run(
            emitter.emit(
                event_name=BusinessEventName.RECURRING_MATCH_FOUND,
                journey=Journey.RECURRING,
                actor_type="SYSTEM",
                entity_type="RECURRING_NEED_OCCURRENCE",
                entity_id=entity_id,
                idempotency_key=f"RECURRING_MATCH_FOUND:{entity_id}:1",
                quantity=20,
                unit="TETE",
            )
        )
        assert captured["canonical_quantity"] == pytest.approx(20)
        assert captured["canonical_unit"] == "TETE"
        assert captured["measurement_family"] == "COUNT"

    def test_emit_rejects_a_journey_mismatch_before_touching_the_session(self, monkeypatch):
        from ladini.domain.analytics.business_events import BusinessEventName
        from ladini.domain.analytics.emitter import BusinessEventEmitter
        from ladini.domain.analytics.metric_dictionary import Journey

        called = AsyncMock()
        monkeypatch.setattr("ladini.domain.analytics.emitter.analytics_outbox_repo.enqueue", called)

        emitter = BusinessEventEmitter(session=object())
        with pytest.raises(ValueError):
            run(
                emitter.emit(
                    event_name=BusinessEventName.TENDER_CREATED,  # belongs to TENDER
                    journey=Journey.DIRECT,  # mismatch, on purpose
                    actor_type="BUYER",
                    entity_type="AUCTION",
                    entity_id=uuid.uuid4(),
                    idempotency_key="whatever",
                )
            )
        called.assert_not_awaited()


class TestEmitProductPublishedForSale:
    def _capture(self, monkeypatch):
        captured = {}

        async def _fake_enqueue(session, *, event_name, journey, payload, dedupe_key):
            captured["event_name"] = event_name
            captured["journey"] = journey
            captured["payload"] = payload
            captured["dedupe_key"] = dedupe_key
            return True

        monkeypatch.setattr(
            "ladini.domain.analytics.emitter.analytics_outbox_repo.enqueue", _fake_enqueue
        )
        return captured

    def test_emits_supply_journey_with_the_producer_id(self, monkeypatch):
        from ladini.domain.analytics.emitter import BusinessEventEmitter

        captured = self._capture(monkeypatch)
        product_id = uuid.uuid4()
        producer_id = uuid.uuid4()
        product = SimpleNamespace(
            id=product_id, producer_id=producer_id, sub_category_id=None,
            quantity_for_sale=50.0, unit="KG",
        )
        result = run(BusinessEventEmitter(session=object()).emit_product_published_for_sale(product))
        assert result is True
        assert captured["event_name"] == "PRODUCT_PUBLISHED_FOR_SALE"
        assert captured["journey"] == "SUPPLY"
        assert captured["dedupe_key"] == f"PRODUCT_PUBLISHED_FOR_SALE:{product_id}"
        assert captured["payload"]["producer_id"] == str(producer_id)
        assert captured["payload"]["quantity"] == 50.0

    def test_idempotency_key_is_product_id_only_no_timestamp(self, monkeypatch):
        """Deliberate design (see the method's own docstring): a re-publish
        after a later pause reuses the SAME key as the first publish and is
        silently deduped — intentional, since Producer Activation only needs
        the first occurrence."""
        from ladini.domain.analytics.emitter import BusinessEventEmitter

        captured = self._capture(monkeypatch)
        product_id = uuid.uuid4()
        product = SimpleNamespace(
            id=product_id, producer_id=uuid.uuid4(), sub_category_id=None,
            quantity_for_sale=1.0, unit="KG",
        )
        run(BusinessEventEmitter(session=object()).emit_product_published_for_sale(product))
        key_first = captured["dedupe_key"]
        run(BusinessEventEmitter(session=object()).emit_product_published_for_sale(product))
        key_second = captured["dedupe_key"]
        assert key_first == key_second == f"PRODUCT_PUBLISHED_FOR_SALE:{product_id}"


class TestEmitProductQuantityChanged:
    def _capture(self, monkeypatch):
        captured = {}

        async def _fake_enqueue(session, *, event_name, journey, payload, dedupe_key):
            captured["event_name"] = event_name
            captured["journey"] = journey
            captured["payload"] = payload
            captured["dedupe_key"] = dedupe_key
            return True

        monkeypatch.setattr(
            "ladini.domain.analytics.emitter.analytics_outbox_repo.enqueue", _fake_enqueue
        )
        return captured

    def test_emits_the_delta_and_source_in_metadata(self, monkeypatch):
        from ladini.domain.analytics.emitter import BusinessEventEmitter

        captured = self._capture(monkeypatch)
        product = SimpleNamespace(
            id=uuid.uuid4(), producer_id=uuid.uuid4(), sub_category_id=None,
            quantity_for_sale=150.0, unit="KG",
        )
        result = run(
            BusinessEventEmitter(session=object()).emit_product_quantity_changed(
                product, previous_quantity=100.0, source="producer_adjustment"
            )
        )
        assert result is True
        assert captured["event_name"] == "PRODUCT_SELLABLE_QUANTITY_CHANGED"
        assert captured["journey"] == "SUPPLY"
        assert captured["payload"]["quantity"] == 150.0
        assert captured["payload"]["metadata"]["previous_quantity"] == 100.0
        assert captured["payload"]["metadata"]["delta"] == 50.0
        assert captured["payload"]["metadata"]["source"] == "producer_adjustment"

    def test_no_op_when_quantity_is_unchanged(self, monkeypatch):
        from ladini.domain.analytics.emitter import BusinessEventEmitter

        called = AsyncMock()
        monkeypatch.setattr(
            "ladini.domain.analytics.emitter.analytics_outbox_repo.enqueue", called
        )
        product = SimpleNamespace(
            id=uuid.uuid4(), producer_id=uuid.uuid4(), sub_category_id=None,
            quantity_for_sale=100.0, unit="KG",
        )
        result = run(
            BusinessEventEmitter(session=object()).emit_product_quantity_changed(
                product, previous_quantity=100.0, source="producer_adjustment"
            )
        )
        assert result is False
        called.assert_not_awaited()

    def test_idempotency_key_includes_the_before_after_pair_and_today(self, monkeypatch):
        from datetime import datetime, timezone

        from ladini.domain.analytics.emitter import BusinessEventEmitter

        captured = self._capture(monkeypatch)
        product = SimpleNamespace(
            id=uuid.uuid4(), producer_id=uuid.uuid4(), sub_category_id=None,
            quantity_for_sale=150.0, unit="KG",
        )
        run(
            BusinessEventEmitter(session=object()).emit_product_quantity_changed(
                product, previous_quantity=100.0, source="producer_adjustment"
            )
        )
        today = datetime.now(timezone.utc).date().isoformat()
        assert captured["dedupe_key"] == (
            f"PRODUCT_SELLABLE_QUANTITY_CHANGED:{product.id}:100.0:150.0:{today}"
        )

    def test_same_transition_replayed_same_day_reuses_the_same_key(self, monkeypatch):
        """A retry of the identical business write (same product, same
        before/after pair, same day) must dedupe — proves the key doesn't
        vary between two calls describing the SAME transition."""
        from ladini.domain.analytics.emitter import BusinessEventEmitter

        captured = self._capture(monkeypatch)
        product = SimpleNamespace(
            id=uuid.uuid4(), producer_id=uuid.uuid4(), sub_category_id=None,
            quantity_for_sale=150.0, unit="KG",
        )
        run(
            BusinessEventEmitter(session=object()).emit_product_quantity_changed(
                product, previous_quantity=100.0, source="producer_adjustment"
            )
        )
        key_first = captured["dedupe_key"]
        run(
            BusinessEventEmitter(session=object()).emit_product_quantity_changed(
                product, previous_quantity=100.0, source="producer_adjustment"
            )
        )
        key_second = captured["dedupe_key"]
        assert key_first == key_second


class TestDirectProducerIdResolution:
    """Mission Phase B step 4/5/6: DIRECT_ORDER_CREATED/CONFIRMED/DELIVERED
    must carry producer_id, resolved from the persisted Order->OrderItem->
    Product relationship — never guessed, never N+1 when already loaded."""

    def test_resolves_from_already_loaded_items_without_a_query(self):
        from ladini.domain.analytics.emitter import BusinessEventEmitter

        producer_id = uuid.uuid4()
        product = SimpleNamespace(producer_id=producer_id)
        item = SimpleNamespace(product=product)
        order = SimpleNamespace(id=uuid.uuid4())
        order.__dict__["items"] = [item]

        session = SimpleNamespace(scalar=AsyncMock())
        result = run(BusinessEventEmitter(session)._resolve_direct_producer_id(order))
        assert result == producer_id
        session.scalar.assert_not_awaited()  # zero extra query when already loaded

    def test_falls_back_to_one_bounded_query_when_items_not_loaded(self):
        from ladini.domain.analytics.emitter import BusinessEventEmitter

        producer_id = uuid.uuid4()
        order = SimpleNamespace(id=uuid.uuid4())  # no "items" in __dict__ at all
        session = SimpleNamespace(scalar=AsyncMock(return_value=producer_id))
        result = run(BusinessEventEmitter(session)._resolve_direct_producer_id(order))
        assert result == producer_id
        session.scalar.assert_awaited_once()

    def test_emit_direct_order_created_carries_producer_id(self, monkeypatch):
        from ladini.domain.analytics.emitter import BusinessEventEmitter

        captured = {}

        async def _fake_enqueue(session, *, event_name, journey, payload, dedupe_key):
            captured.update(payload)
            return True

        monkeypatch.setattr(
            "ladini.domain.analytics.emitter.analytics_outbox_repo.enqueue", _fake_enqueue
        )
        producer_id = uuid.uuid4()
        product = SimpleNamespace(producer_id=producer_id, sub_category_id=None, unit="KG")
        item = SimpleNamespace(product=product, quantity=10.0, tier_id=None)
        order = SimpleNamespace(
            id=uuid.uuid4(), buyer_id=uuid.uuid4(), auction_id=None, order_type="PREORDER",
            market_offer_id=None, zone_id=None, total_amount=1000.0,
        )
        order.__dict__["items"] = [item]

        run(BusinessEventEmitter(session=object()).emit_direct_order_created(order))
        assert captured["producer_id"] == str(producer_id)

    def test_emit_direct_order_confirmed_carries_the_same_producer_id(self, monkeypatch):
        from ladini.domain.analytics.emitter import BusinessEventEmitter

        captured = {}

        async def _fake_enqueue(session, *, event_name, journey, payload, dedupe_key):
            captured.update(payload)
            return True

        monkeypatch.setattr(
            "ladini.domain.analytics.emitter.analytics_outbox_repo.enqueue", _fake_enqueue
        )
        producer_id = uuid.uuid4()
        product = SimpleNamespace(producer_id=producer_id, sub_category_id=None, unit="KG")
        item = SimpleNamespace(product=product, quantity=10.0, tier_id=None)
        order = SimpleNamespace(
            id=uuid.uuid4(), buyer_id=uuid.uuid4(), auction_id=None, order_type="PREORDER",
            market_offer_id=None, zone_id=None, total_amount=1000.0,
        )
        order.__dict__["items"] = [item]

        run(BusinessEventEmitter(session=object()).emit_direct_order_confirmed(order, actor_type="PRODUCER"))
        assert captured["producer_id"] == str(producer_id)


class TestTenderProducerIdResolution:
    def test_resolves_via_winning_bid(self):
        from ladini.domain.analytics.emitter import BusinessEventEmitter

        producer_id = uuid.uuid4()
        order = SimpleNamespace(winning_bid_id=uuid.uuid4())
        session = SimpleNamespace(scalar=AsyncMock(return_value=producer_id))
        result = run(BusinessEventEmitter(session)._resolve_tender_producer_id(order))
        assert result == producer_id
        session.scalar.assert_awaited_once()

    def test_none_when_no_winning_bid(self):
        from ladini.domain.analytics.emitter import BusinessEventEmitter

        order = SimpleNamespace(winning_bid_id=None)
        session = SimpleNamespace(scalar=AsyncMock())
        result = run(BusinessEventEmitter(session)._resolve_tender_producer_id(order))
        assert result is None
        session.scalar.assert_not_awaited()

    def test_emit_order_delivered_resolves_tender_producer_id(self, monkeypatch):
        from ladini.domain.analytics.emitter import BusinessEventEmitter

        captured = {}

        async def _fake_enqueue(session, *, event_name, journey, payload, dedupe_key):
            captured.update(payload)
            return True

        monkeypatch.setattr(
            "ladini.domain.analytics.emitter.analytics_outbox_repo.enqueue", _fake_enqueue
        )
        producer_id = uuid.uuid4()
        order = SimpleNamespace(
            id=uuid.uuid4(), order_type="STANDARD", market_offer_id=None,
            auction_id=uuid.uuid4(), winning_bid_id=uuid.uuid4(),
            buyer_id=uuid.uuid4(), zone_id=None, total_amount=500.0,
        )
        session = SimpleNamespace(scalar=AsyncMock(return_value=producer_id))
        run(BusinessEventEmitter(session).emit_order_delivered(order))
        assert captured["event_name"] == "TENDER_DELIVERED"
        assert captured["producer_id"] == str(producer_id)

    def test_emit_order_delivered_resolves_direct_producer_id(self, monkeypatch):
        from ladini.domain.analytics.emitter import BusinessEventEmitter

        captured = {}

        async def _fake_enqueue(session, *, event_name, journey, payload, dedupe_key):
            captured.update(payload)
            return True

        monkeypatch.setattr(
            "ladini.domain.analytics.emitter.analytics_outbox_repo.enqueue", _fake_enqueue
        )
        producer_id = uuid.uuid4()
        product = SimpleNamespace(producer_id=producer_id)
        item = SimpleNamespace(product=product)
        order = SimpleNamespace(
            id=uuid.uuid4(), order_type="PREORDER", market_offer_id=None,
            auction_id=None, buyer_id=uuid.uuid4(), zone_id=None, total_amount=500.0,
        )
        order.__dict__["items"] = [item]
        run(BusinessEventEmitter(session=object()).emit_order_delivered(order))
        assert captured["event_name"] == "DIRECT_ORDER_DELIVERED"
        assert captured["producer_id"] == str(producer_id)
