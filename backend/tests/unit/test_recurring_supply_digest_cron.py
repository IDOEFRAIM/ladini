"""Câblage Celery du cron de digest (Phase 4) — logique d'agrégation déjà couverte contre un vrai
PostgreSQL dans `tests/schema/test_recurring_supply_digest_service.py` ; ici on vérifie seulement la
délégation et la remontée d'échec (même idiome que `TestProximityMatchingCron`)."""
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


class TestRecurringSupplyDigestCron:
    def test_run_targets_tomorrow_by_default_and_returns_the_report(self, monkeypatch):
        import ladini.workers.crons.recurring_supply_digest as mod

        _patch_worker_session(monkeypatch, mod)
        fake_batch = SimpleNamespace(
            occurrence_date="2026-09-23", buyers_examined=2, duration_ms=5,
            reports=[SimpleNamespace(as_dict=lambda: {"buyer_id": "b1"})],
        )
        fake_service = SimpleNamespace(run=AsyncMock(return_value=fake_batch))
        monkeypatch.setattr(mod, "RecurringSupplyDigestService", lambda session: fake_service)

        result = run(mod._run(1))
        assert result["occurrence_date"] == "2026-09-23"
        assert result["buyers_examined"] == 2
        fake_service.run.assert_awaited_once()
        _, kwargs = fake_service.run.call_args
        assert kwargs["trigger"] == "scheduled"

    def test_celery_task_retries_on_failure(self, monkeypatch):
        import ladini.workers.crons.recurring_supply_digest as mod

        async def _boom(day_offset):
            raise RuntimeError("digest crashed")

        monkeypatch.setattr(mod, "_run", _boom)
        with pytest.raises(Exception, match="digest crashed"):
            mod.run_recurring_supply_digest_cron.run()
