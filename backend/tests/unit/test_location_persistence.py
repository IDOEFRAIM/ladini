"""`core/location.py::persist_shared_location` — refonte GPS 2026-09-02.

Remplace `test_webhook_location_geofencing.py` (l'ancienne fonction cible,
`twilio_webhook.py::_persist_location_background`, a été retirée — la
persistance est désormais SYNCHRONE, dans `core/location.py`, appelée AVANT
l'enqueue Celery plutôt qu'en `BackgroundTasks` après la réponse HTTP. Voir
la docstring du module pour le détail de l'incident corrigé (course
webhook/tâche + repli silencieux sur une position périmée)."""

from __future__ import annotations

from unittest.mock import AsyncMock

from tests.conftest import run

from agriconnect.core.location import LocationOutcome, persist_shared_location

PHONE = "+22670000001"


class _FakeSession:
    async def commit(self):
        pass

    async def rollback(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _patch_update_geo_location(monkeypatch, result: dict):
    from agriconnect.services.database.auth import AuthMixin

    monkeypatch.setattr(
        AuthMixin, "update_geo_location", AsyncMock(return_value=result)
    )
    monkeypatch.setattr(
        "agriconnect.core.database.get_sessionmaker",
        lambda: (lambda: _FakeSession()),
    )


class TestPersistSharedLocation:
    def test_a_point_outside_burkina_faso_is_rejected_with_a_message(
        self, monkeypatch
    ):
        _patch_update_geo_location(
            monkeypatch,
            {"status": "error", "message": "hors zone", "reason": "out_of_country"},
        )

        outcome, message = run(persist_shared_location(PHONE, 48.85, 2.35))

        assert outcome == LocationOutcome.LOCATION_OUT_OF_ZONE
        assert message and "Burkina Faso" in message

    def test_a_point_inside_burkina_faso_is_accepted_without_a_message(
        self, monkeypatch
    ):
        _patch_update_geo_location(
            monkeypatch, {"status": "success", "message": "ok"}
        )

        outcome, message = run(persist_shared_location(PHONE, 12.37, -1.52))

        assert outcome == LocationOutcome.NEW_LOCATION_ACCEPTED
        assert message is None

    def test_a_non_geofencing_error_is_a_distinct_persistence_error(
        self, monkeypatch
    ):
        """Non-régression du principe (pas du comportement littéral, qui a
        changé avec cette refonte) : une erreur non géographique (compte
        introuvable, etc.) ne doit JAMAIS être confondue avec un rejet
        géographique — outcome distinct, jamais LOCATION_OUT_OF_ZONE."""
        _patch_update_geo_location(
            monkeypatch,
            {"status": "error", "message": "Utilisateur introuvable."},
        )

        outcome, message = run(persist_shared_location(PHONE, 12.37, -1.52))

        assert outcome == LocationOutcome.LOCATION_PERSISTENCE_ERROR
        assert outcome != LocationOutcome.LOCATION_OUT_OF_ZONE

    def test_a_missing_sessionmaker_never_raises(self, monkeypatch):
        monkeypatch.setattr(
            "agriconnect.core.database.get_sessionmaker", lambda: None
        )

        outcome, message = run(persist_shared_location(PHONE, 12.37, -1.52))

        assert outcome == LocationOutcome.LOCATION_PERSISTENCE_ERROR

    def test_an_unexpected_exception_never_propagates(self, monkeypatch):
        from agriconnect.services.database.auth import AuthMixin

        async def _boom(*args, **kwargs):
            raise RuntimeError("infra down")

        monkeypatch.setattr(AuthMixin, "update_geo_location", _boom)
        monkeypatch.setattr(
            "agriconnect.core.database.get_sessionmaker",
            lambda: (lambda: _FakeSession()),
        )

        outcome, message = run(persist_shared_location(PHONE, 12.37, -1.52))

        assert outcome == LocationOutcome.LOCATION_PERSISTENCE_ERROR
