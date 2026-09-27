"""Phase C — RECURRING call-sites, sans PostgreSQL (les mêmes scénarios contre un vrai PG vivent dans
`tests/schema/test_analytics_recurring_events.py`, exécuté en CI).

On remplace UNIQUEMENT `analytics_outbox_repo.enqueue` par une doublure qui dédoublonne sur
`dedupe_key` comme le vrai `ON CONFLICT DO NOTHING` : on prouve ainsi (1) QUEL fait déclenche QUEL
event, (2) que la clé identifie le fait métier (un rejeu retombe sur la même clé)."""
from __future__ import annotations

import uuid
from datetime import date, datetime
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


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


def _svc(session):
    from ladini.services.database.recurring_supply import RecurringSupplyMixin

    class _S(RecurringSupplyMixin):
        pass

    s = _S()
    s.__dict__["session"] = None
    type(s).session = property(lambda self: session)
    return s


def _need(**over):
    base = dict(
        id=uuid.uuid4(), buyer_id=uuid.uuid4(), sub_category_id=uuid.uuid4(), quantity=40, unit="KG",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _rule():
    from ladini.domain.recurring_supply.recurrence import RecurrenceRule

    return RecurrenceRule(recurrence_type="DAILY", weekly_days=(), excluded_weekdays=(), starts_at=date(2026, 9, 16), ends_at=None)


def _names(outbox):
    return [v["event_name"] for v in outbox.values()]


class TestOccurrenceCreated:
    def test_one_event_per_row_actually_inserted(self, outbox):
        inserted = [(uuid.uuid4(), datetime(2026, 9, 16)), (uuid.uuid4(), datetime(2026, 9, 17))]
        session = SimpleNamespace(execute=AsyncMock(return_value=_Result(inserted)))
        n = run(_svc(session)._materialize_occurrences(_need(), _rule(), from_date=date(2026, 9, 15), to_date=date(2026, 9, 22)))
        assert n == 2
        assert _names(outbox) == ["RECURRING_OCCURRENCE_CREATED"] * 2
        assert {f"RECURRING_OCCURRENCE_CREATED:{i}" for i, _ in inserted} == set(outbox)

    def test_conflicting_replay_returns_no_rows_and_emits_nothing(self, outbox):
        session = SimpleNamespace(execute=AsyncMock(return_value=_Result([])))
        n = run(_svc(session)._materialize_occurrences(_need(), _rule(), from_date=date(2026, 9, 15), to_date=date(2026, 9, 22)))
        assert n == 0 and outbox == {}

    def test_system_actor_for_cron_and_buyer_actor_for_creation_share_one_emission_point(self, outbox):
        occ_id = uuid.uuid4()
        session = SimpleNamespace(execute=AsyncMock(return_value=_Result([(occ_id, datetime(2026, 9, 16))])))
        svc = _svc(session)
        need = _need()
        run(svc._materialize_occurrences(need, _rule(), from_date=date(2026, 9, 15), to_date=date(2026, 9, 22), actor_type="BUYER"))
        assert outbox[f"RECURRING_OCCURRENCE_CREATED:{occ_id}"]["payload"]["actor_type"] == "BUYER"
        # Même occurrence re-vue par le cron (cas théorique) : même clé => pas de 2e event.
        run(svc._materialize_occurrences(need, _rule(), from_date=date(2026, 9, 15), to_date=date(2026, 9, 22)))
        assert len(outbox) == 1


class TestNeedCreated:
    def test_event_emitted_for_the_real_need_with_stable_key_and_canonical_unit(self, outbox):
        sub_cat = SimpleNamespace(id=uuid.uuid4(), name="tomate", priority_unit="KG")
        session = SimpleNamespace(add=lambda x: None, flush=AsyncMock(), execute=AsyncMock(return_value=_Result([])))
        svc = _svc(session)
        svc._resolve_sub_category = AsyncMock(return_value=sub_cat)
        result = run(
            svc._insert_one_recurring_need(
                buyer_id=uuid.uuid4(), product_query="tomate", quantity=1500, unit="G", recurrence_type="DAILY",
                weekly_days=None, excluded_weekdays=None, start_date=date(2026, 9, 16), end_date=None,
                max_price_per_unit=None, rule=_rule(),
            )
        )
        key = f"RECURRING_NEED_CREATED:{result['recurring_need_id']}"
        payload = outbox[key]["payload"]
        assert payload["entity_type"] == "RECURRING_NEED"
        assert payload["quantity"] == 1500 and payload["unit"] == "G"
        assert payload["canonical_quantity"] == pytest.approx(1.5) and payload["canonical_unit"] == "KG"


class TestSkip:
    def test_skip_emits_after_persisted_status_and_replay_dedupes(self, outbox):
        occ = SimpleNamespace(id=uuid.uuid4(), status="OPEN", version=1, requested_quantity=40, unit="KG")
        session = SimpleNamespace(flush=AsyncMock())
        svc = _svc(session)
        svc._get_mutable_occurrence = AsyncMock(return_value=occ)
        need = _need()
        run(svc._apply_occurrence_skip(need, occurrence_date=date(2026, 9, 17)))
        assert occ.status == "SKIPPED"
        run(svc._apply_occurrence_skip(need, occurrence_date=date(2026, 9, 17)))
        assert list(outbox) == [f"RECURRING_OCCURRENCE_SKIPPED:{occ.id}"]

    def test_invalid_skip_emits_nothing(self, outbox):
        from ladini.services.database.errors import BusinessRuleException

        svc = _svc(SimpleNamespace(flush=AsyncMock()))
        svc._get_mutable_occurrence = AsyncMock(side_effect=BusinessRuleException("non modifiable"))
        with pytest.raises(BusinessRuleException):
            run(svc._apply_occurrence_skip(_need(), occurrence_date=date(2026, 9, 17)))
        assert outbox == {}


class TestDigestAccepted:
    def _accept(self, outbox, *, allocations):
        need = _need()
        occ = SimpleNamespace(
            id=uuid.uuid4(), status="MATCHED", version=1, requested_quantity=40, unit="KG",
            quantity_confirmed=0, order_group_id=None, accepted_at=None, occurrence_date=datetime(2026, 9, 16),
        )
        product = SimpleNamespace(id=uuid.uuid4(), producer_id=uuid.uuid4(), name="tomate", quantity_for_sale=100)
        scalars = iter([need, occ])
        session = SimpleNamespace(
            scalar=AsyncMock(side_effect=lambda *_a, **_k: next(scalars)),
            execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: allocations))),
            get=AsyncMock(return_value=product),
            add=lambda x: None,
            flush=AsyncMock(),
        )
        svc = _svc(session)
        user = SimpleNamespace(id=uuid.uuid4(), zone_id=None, name="x")
        profile = SimpleNamespace(id=need.buyer_id, establishment_name="resto")
        svc.get_buyer_profile = AsyncMock(return_value=(user, profile))
        result = run(svc.accept_match_proposal("+22670000000", str(need.id), "ACCEPT"))
        return occ, result

    def _alloc(self):
        return SimpleNamespace(
            producer_id=uuid.uuid4(), product_id=uuid.uuid4(), quantity=40, unit_price=500, status="PROPOSED", order_item_id=None,
        )

    def test_event_emitted_after_acceptance_is_persisted(self, outbox):
        occ, result = self._accept(outbox, allocations=[self._alloc()])
        assert occ.status == "ACCEPTED"
        payload = outbox[f"RECURRING_DIGEST_ACCEPTED:{occ.id}"]["payload"]
        assert payload["quantity"] == 40 and payload["amount"] == 20000

    def test_reject_or_failed_accept_emits_nothing(self, outbox):
        from ladini.services.database.errors import BusinessRuleException

        with pytest.raises(BusinessRuleException):
            self._accept(outbox, allocations=[])  # rejeu : plus aucune allocation PROPOSED
        assert outbox == {}


class TestDigestSent:
    def test_only_newly_inserted_digests_emit_and_replay_emits_nothing(self, outbox, monkeypatch):
        from ladini.workers.automation.recurring_supply_digest_service import (
            RecurringSupplyDigestService,
        )

        buyer_id = uuid.uuid4()
        row = {
            "occurrence_id": uuid.uuid4(), "version": 1, "requested_quantity": 40, "quantity_matched": 40,
            "unit": "KG", "buyer_id": buyer_id, "product": "tomate", "buyer_phone": "+22670000000",
        }
        session = SimpleNamespace(
            execute=AsyncMock(return_value=SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: [row]))),
            commit=AsyncMock(),
        )
        svc = RecurringSupplyDigestService(session)
        inserted_sets = [None, set()]  # 1er run : la clé est insérée ; 2e run : dédupliquée

        async def _enqueue(entries):
            first = inserted_sets.pop(0)
            return {e["dedupe_key"] for e in entries} if first is None else first

        monkeypatch.setattr(svc, "_enqueue_and_get_inserted_keys", _enqueue)
        run(svc.run(target_date=date(2026, 9, 16)))
        run(svc.run(target_date=date(2026, 9, 16)))
        assert _names(outbox) == ["RECURRING_DIGEST_SENT"]
        payload = next(iter(outbox.values()))["payload"]
        assert payload["metadata"]["delivery_guarantee"] == "QUEUED_FOR_OUTBOUND_DELIVERY"


class TestMatchFound:
    def _persist(self, outbox, *, before, persisted, allocs):
        from ladini.workers.automation.need_matching_service import NeedMatchingService

        results = iter([_Result(before), _Result(persisted), SimpleNamespace(), SimpleNamespace()])
        session = SimpleNamespace(execute=AsyncMock(side_effect=lambda *a, **k: next(results)))
        occ_row = {
            "buyer_id": uuid.uuid4(), "sub_category_id": uuid.uuid4(), "need_id": uuid.uuid4(), "requested_quantity": 40,
        }
        return run(NeedMatchingService(session)._persist_allocations(uuid.uuid4(), occ_row, allocs))

    def _alloc(self, producer, product):
        return SimpleNamespace(producer_id=producer, product_id=product, quantity=40, unit_price=500, unit="KG")

    def test_one_event_per_allocation_keyed_by_allocation_id(self, outbox):
        producer, product, alloc_id = str(uuid.uuid4()), str(uuid.uuid4()), uuid.uuid4()
        changed = self._persist(
            outbox, before=[], persisted=[(alloc_id, producer, product, 40, 500, "KG")], allocs=[self._alloc(producer, product)]
        )
        assert changed is True
        assert list(outbox) == [f"RECURRING_MATCH_FOUND:{alloc_id}"]

    def test_unchanged_rematch_emits_nothing_new(self, outbox):
        producer, product, alloc_id = str(uuid.uuid4()), str(uuid.uuid4()), uuid.uuid4()
        before = [(producer, product, 40, 500)]
        changed = self._persist(
            outbox, before=before, persisted=[(alloc_id, producer, product, 40, 500, "KG")], allocs=[self._alloc(producer, product)]
        )
        assert changed is False and outbox == {}

    def test_changed_rematch_of_same_allocation_keeps_single_event(self, outbox):
        producer, product, alloc_id = str(uuid.uuid4()), str(uuid.uuid4()), uuid.uuid4()
        for before in ([], [(producer, product, 30, 500)]):
            self._persist(
                outbox, before=before, persisted=[(alloc_id, producer, product, 40, 500, "KG")], allocs=[self._alloc(producer, product)]
            )
        assert list(outbox) == [f"RECURRING_MATCH_FOUND:{alloc_id}"]
