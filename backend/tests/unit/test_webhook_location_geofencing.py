"""`api/routes/twilio_webhook.py::_persist_location_background` — notifie
l'utilisateur (au lieu de silencieusement ignorer) quand le point GPS
partagé tombe hors du Burkina Faso.

NB : `twilio_webhook.py` importe `api.tasks` (`from celery.signals import
...`) au niveau module — ce fichier ne peut donc pas se collecter sans le
paquet `celery` installé (pré-existant, sans rapport avec cette feature).
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from tests.conftest import run

pytest.importorskip("celery", reason="twilio_webhook imports api.tasks -> celery.signals")

PHONE = "+22670000001"


class TestPersistLocationBackgroundGeofencing:
    def test_a_point_outside_burkina_faso_is_not_saved_and_the_user_is_notified(self, monkeypatch):
        import agriconnect.api.routes.twilio_webhook as webhook_mod

        class _FakeSessionmaker:
            def __call__(self):
                raise AssertionError("aucune session ne doit être ouverte pour un point rejeté par le geofencing")

        # `update_geo_location` est appelé via un `_GeoOnlyService` construit
        # ad hoc dans la fonction — le plus simple et le plus fidèle est de
        # patcher `AuthMixin.update_geo_location` lui-même pour renvoyer le
        # rejet geofencing sans toucher à une vraie session.
        from agriconnect.services.database.auth import AuthMixin
        monkeypatch.setattr(
            AuthMixin, "update_geo_location",
            AsyncMock(return_value={
                "status": "error", "message": "hors zone", "reason": "out_of_country",
            }),
        )

        class _FakeSession:
            async def commit(self):
                pass

            async def rollback(self):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr("agriconnect.core.database.get_sessionmaker", lambda: (lambda: _FakeSession()))

        sent = AsyncMock()
        monkeypatch.setattr("agriconnect.api.tasks.send_confirmation_text", sent)

        run(webhook_mod._persist_location_background(PHONE, 48.85, 2.35))

        sent.assert_awaited_once()
        assert sent.await_args.args[0] == PHONE
        assert "Burkina Faso" in sent.await_args.args[1]

    def test_a_point_inside_burkina_faso_is_saved_without_any_notice(self, monkeypatch):
        import agriconnect.api.routes.twilio_webhook as webhook_mod
        from agriconnect.services.database.auth import AuthMixin

        monkeypatch.setattr(
            AuthMixin, "update_geo_location",
            AsyncMock(return_value={"status": "success", "message": "ok"}),
        )

        class _FakeSession:
            async def commit(self):
                pass

            async def rollback(self):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr("agriconnect.core.database.get_sessionmaker", lambda: (lambda: _FakeSession()))

        sent = AsyncMock()
        monkeypatch.setattr("agriconnect.api.tasks.send_confirmation_text", sent)

        run(webhook_mod._persist_location_background(PHONE, 12.37, -1.52))

        sent.assert_not_awaited()

    def test_a_non_geofencing_error_stays_silent_as_before(self, monkeypatch):
        """Non-régression : les autres erreurs (compte introuvable, etc.)
        restent best-effort silencieuses, comme avant cette feature."""
        import agriconnect.api.routes.twilio_webhook as webhook_mod
        from agriconnect.services.database.auth import AuthMixin

        monkeypatch.setattr(
            AuthMixin, "update_geo_location",
            AsyncMock(return_value={"status": "error", "message": "Utilisateur introuvable."}),
        )

        class _FakeSession:
            async def commit(self):
                pass

            async def rollback(self):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr("agriconnect.core.database.get_sessionmaker", lambda: (lambda: _FakeSession()))

        sent = AsyncMock()
        monkeypatch.setattr("agriconnect.api.tasks.send_confirmation_text", sent)

        run(webhook_mod._persist_location_background(PHONE, 12.37, -1.52))

        sent.assert_not_awaited()
