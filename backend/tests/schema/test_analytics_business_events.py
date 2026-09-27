"""Real-PostgreSQL tests for `analytics.business_events` (mission Phase C,
section 13). Self-skips locally without SCHEMA_TEST_DSN, fails outright in
CI (REQUIRE_SCHEMA_DB=1) — same discipline as every other file here."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import psycopg2
import pytest
from factories import Graph, insert, uniq


def _occurred_now():
    return datetime.now(timezone.utc)


class TestBusinessEventsInsert:
    def test_a_valid_event_inserts_cleanly(self, db):
        cur = db.cursor()
        g = Graph(cur)
        event_id = insert(
            cur,
            "analytics.business_events",
            event_name="RECURRING_NEED_CREATED",
            journey="RECURRING",
            actor_type="BUYER",
            actor_id=g.buyer_user,
            buyer_id=g.buyer,
            entity_type="RECURRING_NEED",
            entity_id=uuid.uuid4(),
            sub_category_id=g.sub_category,
            zone_id=g.zone,
            quantity=10,
            unit="KG",
            canonical_quantity=10,
            canonical_unit="KG",
            measurement_family="MASS",
            occurred_at=_occurred_now(),
            idempotency_key=uniq("idem"),
        )
        assert event_id is not None

    def test_duplicate_idempotency_key_is_rejected(self, db):
        cur = db.cursor()
        g = Graph(cur)
        key = uniq("idem")
        insert(
            cur, "analytics.business_events",
            event_name="TENDER_CREATED", journey="TENDER", actor_type="BUYER",
            buyer_id=g.buyer, entity_type="AUCTION", entity_id=uuid.uuid4(),
            occurred_at=_occurred_now(), idempotency_key=key,
        )
        cur.execute("SAVEPOINT dup_check")
        with pytest.raises(psycopg2.errors.UniqueViolation):
            insert(
                cur, "analytics.business_events",
                event_name="TENDER_CREATED", journey="TENDER", actor_type="BUYER",
                buyer_id=g.buyer, entity_type="AUCTION", entity_id=uuid.uuid4(),
                occurred_at=_occurred_now(), idempotency_key=key,
            )
        cur.execute("ROLLBACK TO SAVEPOINT dup_check")

    def test_invalid_journey_is_rejected(self, db):
        cur = db.cursor()
        g = Graph(cur)
        with pytest.raises(psycopg2.errors.CheckViolation):
            insert(
                cur, "analytics.business_events",
                event_name="TENDER_CREATED", journey="NOT_A_JOURNEY", actor_type="BUYER",
                buyer_id=g.buyer, entity_type="AUCTION", entity_id=uuid.uuid4(),
                occurred_at=_occurred_now(), idempotency_key=uniq("idem"),
            )

    def test_invalid_measurement_family_is_rejected(self, db):
        cur = db.cursor()
        with pytest.raises(psycopg2.errors.CheckViolation):
            insert(
                cur, "analytics.business_events",
                event_name="RECURRING_MATCH_FOUND", journey="RECURRING", actor_type="SYSTEM",
                entity_type="RECURRING_NEED_OCCURRENCE", entity_id=uuid.uuid4(),
                quantity=5, unit="TETE", canonical_quantity=5, canonical_unit="TETE",
                measurement_family="NOT_A_FAMILY",
                occurred_at=_occurred_now(), idempotency_key=uniq("idem"),
            )

    def test_invalid_actor_type_is_rejected(self, db):
        cur = db.cursor()
        with pytest.raises(psycopg2.errors.CheckViolation):
            insert(
                cur, "analytics.business_events",
                event_name="TENDER_CREATED", journey="TENDER", actor_type="ROBOT",
                entity_type="AUCTION", entity_id=uuid.uuid4(),
                occurred_at=_occurred_now(), idempotency_key=uniq("idem"),
            )

    def test_invalid_event_name_is_rejected(self, db):
        cur = db.cursor()
        with pytest.raises(psycopg2.errors.CheckViolation):
            insert(
                cur, "analytics.business_events",
                event_name="PROCESS_SOMETHING", journey="TENDER", actor_type="SYSTEM",
                entity_type="AUCTION", entity_id=uuid.uuid4(),
                occurred_at=_occurred_now(), idempotency_key=uniq("idem"),
            )

    def test_negative_quantity_is_rejected(self, db):
        cur = db.cursor()
        with pytest.raises(psycopg2.errors.CheckViolation):
            insert(
                cur, "analytics.business_events",
                event_name="TENDER_CREATED", journey="TENDER", actor_type="SYSTEM",
                entity_type="AUCTION", entity_id=uuid.uuid4(), quantity=-1,
                occurred_at=_occurred_now(), idempotency_key=uniq("idem"),
            )

    def test_negative_amount_is_rejected(self, db):
        cur = db.cursor()
        with pytest.raises(psycopg2.errors.CheckViolation):
            insert(
                cur, "analytics.business_events",
                event_name="DIRECT_ORDER_CREATED", journey="DIRECT", actor_type="SYSTEM",
                entity_type="ORDER", entity_id=uuid.uuid4(), amount=-500,
                occurred_at=_occurred_now(), idempotency_key=uniq("idem"),
            )

    def test_missing_entity_id_is_rejected(self, db):
        cur = db.cursor()
        with pytest.raises(psycopg2.errors.NotNullViolation):
            insert(
                cur, "analytics.business_events",
                event_name="TENDER_CREATED", journey="TENDER", actor_type="SYSTEM",
                entity_type="AUCTION", entity_id=None,
                occurred_at=_occurred_now(), idempotency_key=uniq("idem"),
            )


class TestBusinessEventsIndexesAndConstraints:
    def test_expected_indexes_exist(self, db):
        cur = db.cursor()
        cur.execute(
            "select indexname from pg_indexes where schemaname='analytics' and tablename='business_events'"
        )
        names = {r[0] for r in cur.fetchall()}
        for expected in (
            "business_events_idempotency_key_uq",
            "business_events_event_name_idx",
            "business_events_journey_idx",
            "business_events_occurred_at_idx",
            "business_events_buyer_idx",
            "business_events_entity_idx",
            "business_events_sub_category_idx",
            "business_events_zone_idx",
        ):
            assert expected in names, f"missing index {expected}"

    def test_idempotency_key_index_is_unique(self, db):
        cur = db.cursor()
        cur.execute(
            "select indexdef from pg_indexes where schemaname='analytics' "
            "and tablename='business_events' and indexname='business_events_idempotency_key_uq'"
        )
        (indexdef,) = cur.fetchone()
        assert "UNIQUE" in indexdef.upper()
