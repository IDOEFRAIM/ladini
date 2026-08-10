"""`workers/runtime.py` + `workers/repositories/*` — la tuyauterie DB des crons.

Zéro Postgres réel : chaque test simule `AsyncSession`/`sessionmaker` avec des
doublures minimales (`unittest.mock.AsyncMock`, objets `SimpleNamespace`
mutables pour les "lignes" ORM). Priorité : c'est le socle dont dépendent
TOUS les crons (paiement Paydunya inclus) — un bug ici est silencieux
jusqu'à ce qu'un paiement confirmé ne débite jamais le stock.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.conftest import run


# =====================================================================
# runtime.py — run_async / worker_session
# =====================================================================

class TestRunAsync:
    def test_falls_back_to_asyncio_run_without_a_persistent_loop(self):
        from agriconnect.workers.runtime import run_async

        async def _coro():
            return 42

        assert run_async(_coro()) == 42

    def test_uses_the_persistent_worker_loop_when_available(self, monkeypatch):
        import asyncio
        import agriconnect.workers.runtime as runtime_module

        persistent_loop = asyncio.new_event_loop()
        try:
            calls = []
            original_run_until_complete = persistent_loop.run_until_complete

            def _spy(coro):
                calls.append(coro)
                return original_run_until_complete(coro)

            monkeypatch.setattr(persistent_loop, "run_until_complete", _spy)

            import agriconnect.api.tasks as tasks_module
            monkeypatch.setattr(tasks_module, "_loop", persistent_loop, raising=False)

            async def _coro():
                return "from-persistent-loop"

            result = runtime_module.run_async(_coro())
            assert result == "from-persistent-loop"
            assert len(calls) == 1
        finally:
            persistent_loop.close()


class TestWorkerSession:
    def test_raises_when_sessionmaker_is_unavailable(self, monkeypatch):
        import agriconnect.workers.runtime as runtime_module

        monkeypatch.setattr(runtime_module, "get_sessionmaker", lambda: None)

        async def _use():
            async with runtime_module.worker_session():
                pass

        with pytest.raises(RuntimeError, match="Sessionmaker indisponible"):
            run(_use())

    def _fake_sessionmaker(self, fake_session):
        # `get_sessionmaker()` returns a session FACTORY (callable), which
        # `worker_session` then calls (`session_factory()`) to obtain the
        # async context manager itself — two levels of indirection to mirror.
        class _CM:
            async def __aenter__(self_inner):
                return fake_session

            async def __aexit__(self_inner, *exc):
                return False

        session_factory = lambda: _CM()
        return lambda: session_factory

    def test_commits_on_success_and_publishes_the_context_var(self, monkeypatch):
        import agriconnect.workers.runtime as runtime_module
        from agriconnect.services.database.base_service import db_session_ctx

        fake_session = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
        monkeypatch.setattr(runtime_module, "get_sessionmaker", self._fake_sessionmaker(fake_session))

        seen_in_context = {}

        async def _use():
            async with runtime_module.worker_session() as session:
                seen_in_context["session"] = db_session_ctx.get()
                assert session is fake_session

        run(_use())
        assert seen_in_context["session"] is fake_session
        fake_session.commit.assert_awaited_once()
        fake_session.rollback.assert_not_awaited()
        assert db_session_ctx.get() is None, "le ContextVar doit être remis à None après le bloc"

    def test_rolls_back_and_reraises_on_exception(self, monkeypatch):
        import agriconnect.workers.runtime as runtime_module

        fake_session = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
        monkeypatch.setattr(runtime_module, "get_sessionmaker", self._fake_sessionmaker(fake_session))

        async def _use():
            async with runtime_module.worker_session():
                raise ValueError("boom mid-transaction")

        with pytest.raises(ValueError, match="boom mid-transaction"):
            run(_use())
        fake_session.rollback.assert_awaited_once()
        fake_session.commit.assert_not_awaited()


# =====================================================================
# repositories/outbox_repo.py
# =====================================================================

class _FakeExecResult:
    def __init__(self, *, all_rows=None, scalars_rows=None):
        self._all_rows = all_rows or []
        self._scalars_rows = scalars_rows or []

    def all(self):
        return self._all_rows

    def scalars(self):
        return SimpleNamespace(all=lambda: self._scalars_rows)


class TestOutboxRepo:
    def test_backoff_delay_clamps_at_the_last_tier_for_high_attempt_counts(self):
        from agriconnect.workers.repositories.outbox_repo import backoff_delay, _BACKOFF_MINUTES
        from datetime import timedelta
        assert backoff_delay(999) == timedelta(minutes=_BACKOFF_MINUTES[-1])

    def test_backoff_delay_clamps_at_the_first_tier_for_zero_or_negative(self):
        from agriconnect.workers.repositories.outbox_repo import backoff_delay, _BACKOFF_MINUTES
        from datetime import timedelta
        assert backoff_delay(0) == timedelta(minutes=_BACKOFF_MINUTES[0])
        assert backoff_delay(-5) == timedelta(minutes=_BACKOFF_MINUTES[0])

    def test_enqueue_returns_zero_without_hitting_the_session_on_empty_entries(self):
        from agriconnect.workers.repositories.outbox_repo import enqueue

        session = SimpleNamespace(execute=AsyncMock())
        assert run(enqueue(session, [])) == 0
        session.execute.assert_not_awaited()

    def test_enqueue_returns_the_count_of_rows_actually_inserted(self):
        from agriconnect.workers.repositories.outbox_repo import enqueue

        session = SimpleNamespace(execute=AsyncMock(return_value=_FakeExecResult(all_rows=[(1,), (2,)])))
        count = run(enqueue(session, [{"dedupe_key": "a"}, {"dedupe_key": "b"}]))
        assert count == 2
        session.execute.assert_awaited_once()

    def test_claim_due_marks_claimed_rows_as_sending_and_flushes(self):
        from agriconnect.workers.repositories.outbox_repo import claim_due

        rows = [SimpleNamespace(id=1, status="PENDING"), SimpleNamespace(id=2, status="PENDING")]
        session = SimpleNamespace(
            execute=AsyncMock(return_value=_FakeExecResult(scalars_rows=rows)),
            flush=AsyncMock(),
        )
        claimed = run(claim_due(session, limit=10))
        assert [r.status for r in claimed] == ["SENDING", "SENDING"]
        session.flush.assert_awaited_once()

    def test_mark_sent_noop_when_row_missing(self):
        from agriconnect.workers.repositories.outbox_repo import mark_sent

        session = SimpleNamespace(get=AsyncMock(return_value=None))
        run(mark_sent(session, "ghost-id"))  # ne doit pas lever

    def test_mark_sent_sets_status_and_clears_last_error(self):
        from agriconnect.workers.repositories.outbox_repo import mark_sent

        row = SimpleNamespace(status="SENDING", sent_at=None, attempts=2, last_error="prev fail")
        session = SimpleNamespace(get=AsyncMock(return_value=row))
        run(mark_sent(session, "id-1"))
        assert row.status == "SENT"
        assert row.sent_at is not None
        assert row.attempts == 3
        assert row.last_error is None

    def test_mark_failed_noop_when_row_missing(self):
        from agriconnect.workers.repositories.outbox_repo import mark_failed

        session = SimpleNamespace(get=AsyncMock(return_value=None))
        run(mark_failed(session, "ghost-id", error="x"))  # ne doit pas lever

    def test_mark_failed_reschedules_with_backoff_below_max_attempts(self):
        from agriconnect.workers.repositories.outbox_repo import mark_failed

        row = SimpleNamespace(status="SENDING", attempts=1, max_attempts=5, last_error=None, next_attempt_at=None)
        session = SimpleNamespace(get=AsyncMock(return_value=row))
        run(mark_failed(session, "id-1", error="timeout"))
        assert row.status == "PENDING"
        assert row.attempts == 2
        assert row.last_error == "timeout"
        assert row.next_attempt_at is not None

    def test_mark_failed_declares_dead_at_max_attempts(self):
        from agriconnect.workers.repositories.outbox_repo import mark_failed

        row = SimpleNamespace(status="SENDING", attempts=4, max_attempts=5, last_error=None, next_attempt_at=None)
        session = SimpleNamespace(get=AsyncMock(return_value=row))
        run(mark_failed(session, "id-1", error="fatal"))
        assert row.status == "DEAD"

    def test_mark_failed_truncates_overly_long_error_messages(self):
        from agriconnect.workers.repositories.outbox_repo import mark_failed

        row = SimpleNamespace(status="SENDING", attempts=1, max_attempts=5, last_error=None, next_attempt_at=None)
        session = SimpleNamespace(get=AsyncMock(return_value=row))
        run(mark_failed(session, "id-1", error="x" * 1000))
        assert len(row.last_error) == 500


# =====================================================================
# repositories/solicitation_repo.py
# =====================================================================

class TestSolicitationRepo:
    def test_fetch_auctions_to_solicit_returns_scalars(self):
        from agriconnect.workers.repositories.solicitation_repo import fetch_auctions_to_solicit

        auctions = [SimpleNamespace(id="a1"), SimpleNamespace(id="a2")]
        session = SimpleNamespace(execute=AsyncMock(return_value=_FakeExecResult(scalars_rows=auctions)))
        result = run(fetch_auctions_to_solicit(session, limit=10))
        assert result == auctions

    def test_upsert_auction_solicitations_returns_empty_without_hitting_db_on_no_producers(self):
        from agriconnect.workers.repositories.solicitation_repo import upsert_auction_solicitations

        session = SimpleNamespace(execute=AsyncMock())
        auction = SimpleNamespace(id="a1", sub_category_id="sc1", target_zone_id=None)
        result = run(upsert_auction_solicitations(session, auction=auction, producers=[]))
        assert result == []
        session.execute.assert_not_awaited()

    def test_upsert_auction_solicitations_filters_producers_without_an_id(self):
        """Un producteur sans `producer_id` (donnée de ciblage incomplète) ne
        doit jamais atteindre l'INSERT — sinon une ligne NULL casserait la
        contrainte d'idempotence sur `(auction_id, target_producer_id)`."""
        from agriconnect.workers.repositories.solicitation_repo import upsert_auction_solicitations

        session = SimpleNamespace(execute=AsyncMock())
        auction = SimpleNamespace(id="a1", sub_category_id="sc1", target_zone_id=None)
        producers = [{"producer_id": None}, {}]
        result = run(upsert_auction_solicitations(session, auction=auction, producers=producers))
        assert result == []
        session.execute.assert_not_awaited()

    def test_upsert_auction_solicitations_returns_newly_created_rows(self):
        from agriconnect.workers.repositories.solicitation_repo import upsert_auction_solicitations

        session = SimpleNamespace(execute=AsyncMock(return_value=_FakeExecResult(all_rows=[("sol-1", "prod-1")])))
        auction = SimpleNamespace(id="a1", sub_category_id="sc1", target_zone_id="z1")
        result = run(upsert_auction_solicitations(session, auction=auction, producers=[{"producer_id": "prod-1"}]))
        assert result == [{"solicitation_id": "sol-1", "producer_id": "prod-1"}]

    def test_upsert_offer_solicitations_returns_empty_on_no_buyers(self):
        from agriconnect.workers.repositories.solicitation_repo import upsert_offer_solicitations

        session = SimpleNamespace(execute=AsyncMock())
        result = run(upsert_offer_solicitations(
            session, market_offer_id="o1", sub_category_id="sc1", zone_id="z1", buyers=[],
        ))
        assert result == []
        session.execute.assert_not_awaited()

    def test_upsert_offer_solicitations_filters_buyers_without_an_id(self):
        from agriconnect.workers.repositories.solicitation_repo import upsert_offer_solicitations

        session = SimpleNamespace(execute=AsyncMock())
        result = run(upsert_offer_solicitations(
            session, market_offer_id="o1", sub_category_id="sc1", zone_id="z1",
            buyers=[{"buyer_id": None}],
        ))
        assert result == []
        session.execute.assert_not_awaited()

    def test_upsert_offer_solicitations_returns_newly_created_rows(self):
        from agriconnect.workers.repositories.solicitation_repo import upsert_offer_solicitations

        session = SimpleNamespace(execute=AsyncMock(return_value=_FakeExecResult(all_rows=[("sol-9", "buy-9")])))
        result = run(upsert_offer_solicitations(
            session, market_offer_id="o1", sub_category_id="sc1", zone_id="z1",
            buyers=[{"buyer_id": "buy-9"}],
        ))
        assert result == [{"solicitation_id": "sol-9", "buyer_id": "buy-9"}]

    def test_mark_notified_noop_on_empty_ids(self):
        from agriconnect.workers.repositories.solicitation_repo import mark_notified

        session = SimpleNamespace(execute=AsyncMock())
        run(mark_notified(session, []))
        session.execute.assert_not_awaited()

    def test_mark_notified_executes_the_update(self):
        from agriconnect.workers.repositories.solicitation_repo import mark_notified

        session = SimpleNamespace(execute=AsyncMock())
        run(mark_notified(session, ["sol-1", "sol-2"]))
        session.execute.assert_awaited_once()


# =====================================================================
# automation/targeting.py
# =====================================================================

class TestTargeting:
    def test_producers_for_auction_short_circuits_without_a_sub_category(self):
        from agriconnect.workers.automation.targeting import producers_for_auction

        session = SimpleNamespace(execute=AsyncMock())
        result = run(producers_for_auction(session, sub_category_id=None, target_zone_id=None))
        assert result == []
        session.execute.assert_not_awaited()

    def test_producers_for_auction_falls_back_to_user_name_when_no_business_name(self):
        from agriconnect.workers.automation.targeting import producers_for_auction

        class _MappingResult:
            def mappings(self_inner):
                rows = [
                    {"producer_id": "p1", "user_id": "u1", "phone": "+2260", "business_name": None, "user_name": "Awa"},
                    {"producer_id": "p2", "user_id": "u2", "phone": "+2261", "business_name": "Ferme Bio", "user_name": "Ali"},
                ]
                return SimpleNamespace(all=lambda: rows)

        session = SimpleNamespace(execute=AsyncMock(return_value=_MappingResult()))
        result = run(producers_for_auction(session, sub_category_id="sc1", target_zone_id="z1"))
        assert result[0]["display_name"] == "Awa"
        assert result[1]["display_name"] == "Ferme Bio"

    def test_buyers_in_zone_for_category_short_circuits_without_a_zone(self):
        from agriconnect.workers.automation.targeting import buyers_in_zone_for_category

        session = SimpleNamespace(execute=AsyncMock())
        result = run(buyers_in_zone_for_category(session, zone_id=None))
        assert result == []
        session.execute.assert_not_awaited()

    def test_buyers_in_zone_for_category_defaults_display_name_when_unnamed(self):
        from agriconnect.workers.automation.targeting import buyers_in_zone_for_category

        class _MappingResult:
            def mappings(self_inner):
                rows = [{"buyer_id": "b1", "user_id": "u1", "phone": "+2260", "user_name": None}]
                return SimpleNamespace(all=lambda: rows)

        session = SimpleNamespace(execute=AsyncMock(return_value=_MappingResult()))
        result = run(buyers_in_zone_for_category(session, zone_id="z1"))
        assert result[0]["display_name"] == "Client"
