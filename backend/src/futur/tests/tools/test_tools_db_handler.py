import sys
import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import importlib
import types

from futur.tools import db_handler as dbmod


def test_get_db_returns_none_when_unconfigured(monkeypatch):
    # Ensure singleton reset
    monkeypatch.setattr(dbmod, "_db_instance", None)

    # Force core_db attributes to None
    import agriconnect.core.database as _core_db
    monkeypatch.setattr(_core_db, "_engine", None)
    monkeypatch.setattr(_core_db, "_SessionLocal", None)

    # Ensure tools module uses no DATABASE_URL by replacing its settings object
    import types
    monkeypatch.setattr(dbmod, "settings", types.SimpleNamespace(DATABASE_URL=None), raising=False)

    res = dbmod.get_db()
    assert res is None


def test_proxies_return_none_or_empty_when_no_db(monkeypatch):
    # Ensure get_db returns None
    monkeypatch.setattr(dbmod, "get_db", lambda: None)
    assert dbmod.save_weather("zone1", temp=30) is None
    assert dbmod.get_market_prices("maïs", "zone1") == []
    assert dbmod.create_alert("type", "sev", "msg", "zone") is None
    assert dbmod.register_surplus("maïs", 100) is None
