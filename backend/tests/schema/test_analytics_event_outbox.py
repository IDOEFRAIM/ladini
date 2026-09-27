"""Real-PostgreSQL tests for `analytics.event_outbox` — proves the exact
`ON CONFLICT (dedupe_key) DO NOTHING` mechanism `analytics_outbox_repo.
enqueue()` relies on (mission section 14: "same event twice -> no duplicate
analytics row", graceful dedup here vs. a hard rejection at the
business_events layer, tested separately)."""
from __future__ import annotations

import psycopg2
import psycopg2.extras
import pytest
from factories import uniq


def _insert_or_ignore(cur, *, event_name: str, journey: str, dedupe_key: str) -> bool:
    """Mirrors `analytics_outbox_repo.enqueue`'s exact SQL shape."""
    cur.execute(
        """
        INSERT INTO analytics.event_outbox (event_name, journey, payload, dedupe_key)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (dedupe_key) DO NOTHING
        RETURNING id
        """,
        (event_name, journey, psycopg2.extras.Json({}), dedupe_key),
    )
    return cur.fetchone() is not None


class TestEventOutboxIdempotentEnqueue:
    def test_first_enqueue_inserts_a_row(self, db):
        cur = db.cursor()
        inserted = _insert_or_ignore(
            cur, event_name="TENDER_CREATED", journey="TENDER", dedupe_key=uniq("dedupe")
        )
        assert inserted is True

    def test_same_dedupe_key_twice_yields_exactly_one_row(self, db):
        cur = db.cursor()
        key = uniq("dedupe")
        first = _insert_or_ignore(cur, event_name="TENDER_CREATED", journey="TENDER", dedupe_key=key)
        second = _insert_or_ignore(cur, event_name="TENDER_CREATED", journey="TENDER", dedupe_key=key)
        assert first is True
        assert second is False  # deduped, not an error
        cur.execute("select count(*) from analytics.event_outbox where dedupe_key = %s", (key,))
        assert cur.fetchone()[0] == 1

    def test_default_status_is_pending(self, db):
        cur = db.cursor()
        key = uniq("dedupe")
        _insert_or_ignore(cur, event_name="TENDER_CREATED", journey="TENDER", dedupe_key=key)
        cur.execute("select status, attempts from analytics.event_outbox where dedupe_key = %s", (key,))
        status, attempts = cur.fetchone()
        assert status == "PENDING"
        assert attempts == 0

    def test_invalid_journey_is_rejected(self, db):
        cur = db.cursor()
        with pytest.raises(psycopg2.errors.CheckViolation):
            _insert_or_ignore(cur, event_name="TENDER_CREATED", journey="NOT_A_JOURNEY", dedupe_key=uniq("dedupe"))

    def test_supply_journey_with_new_events_is_accepted(self, db):
        """Producer Analytics Phase B: migration 0009 added SUPPLY plus the two
        new event names to this table's CHECK constraints too (not just
        business_events) — the outbox is the FIRST insert in the write path,
        so if this constraint were stale every SUPPLY emit would fail here
        before ever reaching business_events."""
        cur = db.cursor()
        for event_name in ("PRODUCT_PUBLISHED_FOR_SALE", "PRODUCT_SELLABLE_QUANTITY_CHANGED"):
            inserted = _insert_or_ignore(
                cur, event_name=event_name, journey="SUPPLY", dedupe_key=uniq("dedupe")
            )
            assert inserted is True

    def test_claim_index_exists_for_the_drain_worker(self, db):
        cur = db.cursor()
        cur.execute(
            "select indexname from pg_indexes where schemaname='analytics' and tablename='event_outbox'"
        )
        names = {r[0] for r in cur.fetchall()}
        assert "event_outbox_claim_idx" in names
        assert "event_outbox_dedupe_key_uq" in names
