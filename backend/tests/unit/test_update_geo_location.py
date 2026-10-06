"""`services/database/auth.py::AuthMixin.update_geo_location` — geofencing
Burkina Faso ajouté en défense en profondeur (le webhook Twilio valide déjà,
mais cette méthode est aussi un outil MCP appelable directement)."""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock

from tests.conftest import run

PHONE = "+22670000001"


class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    def __init__(self, user_id):
        self._user_id = user_id
        self.executed = []

    async def execute(self, stmt):
        self.executed.append(stmt)
        return _FakeScalarResult(self._user_id)


def _service(user_id):
    from ladini.services.database.auth import AuthMixin

    class _Svc(AuthMixin):
        def __init__(self, session):
            self._session = session

        @property
        def session(self):
            return self._session

    return _Svc(_FakeSession(user_id))


class TestUpdateGeoLocationGeofencing:
    def test_a_point_inside_burkina_faso_succeeds(self):
        svc = _service(uuid.uuid4())
        result = run(svc.update_geo_location(phone=PHONE, lat=12.37, lon=-1.52))
        assert result["status"] == "success"

    def test_a_point_outside_burkina_faso_is_rejected(self):
        svc = _service(uuid.uuid4())
        result = run(svc.update_geo_location(phone=PHONE, lat=48.85, lon=2.35))
        assert result["status"] == "error"
        assert result["reason"] == "out_of_country"

    def test_missing_coordinates_is_a_distinct_error(self):
        svc = _service(uuid.uuid4())
        result = run(svc.update_geo_location(phone=PHONE, lat=None, lon=None))
        assert result["status"] == "error"
        assert result.get("reason") != "out_of_country"

    def test_mathematically_invalid_coordinates_are_rejected_before_geofencing(self):
        svc = _service(uuid.uuid4())
        result = run(svc.update_geo_location(phone=PHONE, lat=200.0, lon=0.0))
        assert result["status"] == "error"
        assert result.get("reason") != "out_of_country"
