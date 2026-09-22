"""Câblage Celery des crons de matching (Phase 3) — logique déterministe déjà couverte contre un vrai
PostgreSQL dans `tests/schema/test_need_matching_service.py` ; ici on vérifie seulement que chaque
tâche délègue au bon appel de service et remonte les échecs (même idiome que
`TestProximityMatchingCron`, `tests/unit/test_workers_crons_and_payments.py`)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.conftest import run


class _FakeWorkerSessionCM:
    async def __aenter__(self):
        return SimpleNamespace()

    async def __aexit__(self, *exc):
        return False


def _patch_worker_session(monkeypatch, module) -> None:
    monkeypatch.setattr(module, "worker_session", lambda: _FakeWorkerSessionCM())


def _fake_batch(occurrences_examined=1, reports=None):
    reports = reports or [SimpleNamespace(as_dict=lambda: {"occurrence_id": "occ-1"})]
    return SimpleNamespace(occurrences_examined=occurrences_examined, reports=reports)


class TestMatchProductAgainstRecurringNeeds:
    def test_delegates_to_the_service_for_the_given_product(self, monkeypatch):
        import ladini.workers.crons.recurring_need_matching as mod

        _patch_worker_session(monkeypatch, mod)
        fake_service = SimpleNamespace(match_product=AsyncMock(return_value=_fake_batch()))
        monkeypatch.setattr(mod, "NeedMatchingService", lambda session: fake_service)

        result = run(mod._match_product("prod-1"))
        assert result["occurrences_examined"] == 1
        fake_service.match_product.assert_awaited_once_with("prod-1", trigger="publication")

    def test_celery_task_retries_on_failure(self, monkeypatch):
        import ladini.workers.crons.recurring_need_matching as mod

        async def _boom(product_id):
            raise RuntimeError("matching crashed")

        monkeypatch.setattr(mod, "_match_product", _boom)
        with pytest.raises(Exception, match="matching crashed"):
            mod.match_product_against_recurring_needs.run("prod-1")


class TestRecurringMatchRecentProductsCron:
    def test_uses_the_configured_window_by_default(self, monkeypatch):
        import ladini.workers.crons.recurring_need_matching as mod

        _patch_worker_session(monkeypatch, mod)
        fake_service = SimpleNamespace(match_recent_products=AsyncMock(return_value=_fake_batch()))
        monkeypatch.setattr(mod, "NeedMatchingService", lambda session: fake_service)
        monkeypatch.setattr(mod.settings, "RECURRING_MATCH_RECENT_WINDOW_MINUTES", 7.0)

        run(mod._match_recent_products(mod.settings.RECURRING_MATCH_RECENT_WINDOW_MINUTES))
        fake_service.match_recent_products.assert_awaited_once()
        _, kwargs = fake_service.match_recent_products.call_args
        assert kwargs["trigger"] == "scheduled"

    def test_celery_task_retries_on_failure(self, monkeypatch):
        import ladini.workers.crons.recurring_need_matching as mod

        async def _boom(window_minutes):
            raise RuntimeError("recent products scan crashed")

        monkeypatch.setattr(mod, "_match_recent_products", _boom)
        with pytest.raises(Exception, match="recent products scan crashed"):
            mod.run_recurring_match_recent_products_cron.run()


class TestRecurringMatchUpcomingOccurrencesCron:
    def test_uses_the_configured_window_by_default(self, monkeypatch):
        import ladini.workers.crons.recurring_need_matching as mod

        _patch_worker_session(monkeypatch, mod)
        fake_service = SimpleNamespace(match_upcoming_occurrences=AsyncMock(return_value=_fake_batch()))
        monkeypatch.setattr(mod, "NeedMatchingService", lambda session: fake_service)

        run(mod._match_upcoming_occurrences(mod.settings.RECURRING_MATCH_UPCOMING_WINDOW_HOURS))
        fake_service.match_upcoming_occurrences.assert_awaited_once()

    def test_celery_task_retries_on_failure(self, monkeypatch):
        import ladini.workers.crons.recurring_need_matching as mod

        async def _boom(within_hours):
            raise RuntimeError("upcoming scan crashed")

        monkeypatch.setattr(mod, "_match_upcoming_occurrences", _boom)
        with pytest.raises(Exception, match="upcoming scan crashed"):
            mod.run_recurring_match_upcoming_occurrences_cron.run()
