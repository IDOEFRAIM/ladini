"""La suite ne peut JAMAIS joindre la vraie base / le vrai Redis d'un développeur (fail-closed, voir `tests/conftest.py`)."""
from __future__ import annotations

from urllib.parse import urlsplit

from ladini.core.settings import settings

_LOCAL = {"", "localhost", "127.0.0.1", "::1"}


def _host(url: str) -> str:
    try:
        return (urlsplit(str(url).strip()).hostname or "").lower()
    except ValueError:
        return "unparseable"


def test_the_effective_database_url_is_local_or_closed():
    assert _host(settings.DATABASE_URL) in _LOCAL, "les tests ne doivent pas viser une base distante"
    assert _host(settings.DO_DATABASE_URL) in _LOCAL


def test_the_effective_redis_url_is_local_or_closed():
    assert _host(settings.REDIS_URL) in _LOCAL, "les tests ne doivent pas viser un Redis hébergé"
