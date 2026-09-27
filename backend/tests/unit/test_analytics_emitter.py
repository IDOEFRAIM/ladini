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
