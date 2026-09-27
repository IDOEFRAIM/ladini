"""`AnalyticsEventDispatcher` sans PostgreSQL : forme du INSERT, conversion du payload, et machine à
états de l'outbox (succès -> SENT ; échec d'INSERT -> item récupérable, batch non interrompu).

Régression : l'ancienne version passait `metadata=` à `pg_insert(BusinessEventRecord).values(...)`,
qui résout vers l'attribut Declarative `MetaData` et faisait planter CHAQUE event (tous DEAD après
5 essais). Le vrai atterrissage en base est couvert en CI par `tests/schema/test_analytics_dispatcher.py`."""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import insert as pg_insert

from tests.conftest import run


def _payload(**over):
    p = {
        "event_name": "RECURRING_NEED_CREATED", "journey": "RECURRING", "actor_type": "BUYER",
        "actor_id": str(uuid.uuid4()), "buyer_id": str(uuid.uuid4()), "producer_id": None,
        "entity_type": "RECURRING_NEED", "entity_id": str(uuid.uuid4()), "category_id": None,
        "sub_category_id": str(uuid.uuid4()), "zone_id": None, "quantity": 40.0, "unit": "KG",
        "canonical_quantity": 40.0, "canonical_unit": "KG", "measurement_family": "MASS", "amount": None,
        "currency": "XOF", "metadata": {"recurrence_type": "DAILY"}, "occurred_at": "2026-09-16T10:00:00+00:00",
        "idempotency_key": "RECURRING_NEED_CREATED:x",
    }
    p.update(over)
    return p


def test_payload_maps_metadata_to_orm_attribute_and_uuid_strings_to_uuid():
    from ladini.workers.outbox.analytics_dispatcher import _payload_to_record_kwargs

    kwargs = _payload_to_record_kwargs(_payload())
    assert "metadata" not in kwargs and kwargs["metadata_"] == {"recurrence_type": "DAILY"}
    assert isinstance(kwargs["entity_id"], uuid.UUID) and isinstance(kwargs["buyer_id"], uuid.UUID)
    assert kwargs["producer_id"] is None
    assert kwargs["occurred_at"].tzinfo is not None


def test_insert_statement_compiles_and_is_idempotent_on_idempotency_key():
    from ladini.domain.analytics.models import BusinessEventRecord
    from ladini.workers.outbox.analytics_dispatcher import _payload_to_record_kwargs

    stmt = (
        pg_insert(BusinessEventRecord)
        .values(**_payload_to_record_kwargs(_payload()))
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
    )
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT (idempotency_key) DO NOTHING" in sql and "metadata" in sql


def test_claim_uses_skip_locked_so_two_workers_never_take_the_same_row():
    from ladini.workers.repositories import analytics_outbox_repo

    captured = {}

    class _Session:
        async def execute(self, stmt):
            captured["sql"] = str(stmt.compile(dialect=postgresql.dialect()))
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

        async def flush(self):
            pass

    run(analytics_outbox_repo.claim_due(_Session(), limit=5))
    assert "FOR UPDATE SKIP LOCKED" in captured["sql"]


class _Harness:
    def __init__(self, monkeypatch, *, jobs, fail_ids=()):
        from ladini.workers.outbox import analytics_dispatcher as mod

        self.sent, self.failed, self.inserted = [], [], []
        rows = [SimpleNamespace(id=i, payload=_payload(idempotency_key=f"k{i}")) for i in jobs]

        @asynccontextmanager
        async def _worker_session():
            yield SimpleNamespace(execute=self._execute)

        async def _claim_due(session, limit):
            return rows

        async def _mark_sent(session, outbox_id):
            self.sent.append(outbox_id)

        async def _mark_failed(session, outbox_id, *, error):
            self.failed.append(outbox_id)

        self.fail_ids = set(fail_ids)
        monkeypatch.setattr(mod, "worker_session", _worker_session)
        monkeypatch.setattr(mod.analytics_outbox_repo, "claim_due", _claim_due)
        monkeypatch.setattr(mod.analytics_outbox_repo, "mark_sent", _mark_sent)
        monkeypatch.setattr(mod.analytics_outbox_repo, "mark_failed", _mark_failed)
        self.mod = mod

    async def _execute(self, stmt):
        key = stmt.compile().params.get("idempotency_key")
        if key in {f"k{i}" for i in self.fail_ids}:
            raise RuntimeError("insert boom")
        self.inserted.append(key)


def test_pending_items_land_and_are_marked_sent(monkeypatch):
    h = _Harness(monkeypatch, jobs=[1, 2])
    report = run(h.mod.AnalyticsEventDispatcher().run())
    assert (report.claimed, report.landed, report.failed) == (2, 2, 0)
    assert h.sent == [1, 2] and h.failed == [] and h.inserted == ["k1", "k2"]


def test_failed_insert_is_marked_for_retry_and_does_not_stop_the_batch(monkeypatch):
    h = _Harness(monkeypatch, jobs=[1, 2, 3], fail_ids=[2])
    report = run(h.mod.AnalyticsEventDispatcher().run())
    assert (report.landed, report.failed) == (2, 1)
    assert h.sent == [1, 3] and h.failed == [2]


def test_mark_failed_backs_off_then_goes_dead_after_five_attempts():
    from ladini.workers.repositories import analytics_outbox_repo as repo

    row = SimpleNamespace(attempts=0, status="SENDING", last_error=None, next_attempt_at=None)

    class _S:
        async def get(self, model, pk):
            return row

    for expected in ("PENDING", "PENDING", "PENDING", "PENDING", "DEAD"):
        run(repo.mark_failed(_S(), 1, error="x"))
        assert row.status == expected
