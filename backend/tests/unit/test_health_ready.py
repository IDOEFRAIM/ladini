"""`api/main.py::readiness_check` (`/health/ready`) — ajouté (2026-09-20,
migration Upstash → Valkey auto-hébergé) car aucun test dédié n'existait
pour la branche Redis de cette probe avant la migration.

Contrat verrouillé ici :
  - DB ok + Redis ok  -> 200, status="ready"
  - DB ok + Redis down (connexion refusée / timeout) -> 503, status="degraded"
  - DB ok + Redis auth invalide (mauvais mot de passe) -> 503, status="degraded"
  - Le corps de réponse ne fuite JAMAIS `str(exc)` (pourrait contenir l'URL
    Redis avec mot de passe en clair, ou un message d'erreur du serveur) —
    seul `type(exc).__name__` doit apparaître. C'est le garde-fou sécurité
    explicitement demandé pour la migration : aucun secret ne doit jamais
    transiter dans une réponse HTTP de healthcheck.

Appelle `readiness_check()` directement (pas de `TestClient`) : c'est une
coroutine simple sans dépendance `Depends`/`Request`, donc l'appel direct
suffit et évite de monter toute l'app FastAPI pour ce test.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import MagicMock, patch

from ladini.api.main import readiness_check
from tests.conftest import run


class _FakeConnection:
    async def execute(self, *_args, **_kwargs):
        return None


class _FakeEngine:
    """Simule `AsyncEngine.connect()` en tant que context manager async."""

    @asynccontextmanager
    async def connect(self):
        yield _FakeConnection()


class _FailingEngine:
    def connect(self):
        raise RuntimeError("db connect refused")


def _body(response) -> dict:
    return json.loads(response.body)


class TestReadinessRedisOk:
    def test_db_ok_redis_ok_returns_200_ready(self):
        fake_redis_client = MagicMock()
        fake_redis_client.ping.return_value = True
        with (
            patch(
                "ladini.core.database.get_engine", return_value=_FakeEngine()
            ),
            patch("redis.from_url", return_value=fake_redis_client),
        ):
            response = run(readiness_check())

        assert response.status_code == 200
        payload = _body(response)
        assert payload["status"] == "ready"
        assert payload["components"]["database"] == "ok"
        assert payload["components"]["redis"] == "ok"


class TestReadinessRedisDown:
    def test_redis_connection_refused_returns_503_degraded(self):
        with (
            patch(
                "ladini.core.database.get_engine", return_value=_FakeEngine()
            ),
            patch(
                "redis.from_url",
                side_effect=ConnectionRefusedError("connection refused"),
            ),
        ):
            response = run(readiness_check())

        assert response.status_code == 503
        payload = _body(response)
        assert payload["status"] == "degraded"
        assert payload["components"]["database"] == "ok"
        assert payload["components"]["redis"] == "error: ConnectionRefusedError"

    def test_redis_timeout_returns_503_degraded(self):
        fake_redis_client = MagicMock()
        fake_redis_client.ping.side_effect = TimeoutError("timed out")
        with (
            patch(
                "ladini.core.database.get_engine", return_value=_FakeEngine()
            ),
            patch("redis.from_url", return_value=fake_redis_client),
        ):
            response = run(readiness_check())

        assert response.status_code == 503
        payload = _body(response)
        assert payload["components"]["redis"] == "error: TimeoutError"


class TestReadinessRedisAuthFailure:
    def test_auth_error_returns_503_and_never_leaks_credentials(self):
        import redis as redis_module

        secret_password = "s3cr3t-valkey-password-DO-NOT-LEAK"
        auth_error = redis_module.AuthenticationError(
            f"invalid username-password pair for user {secret_password}"
        )
        fake_redis_client = MagicMock()
        fake_redis_client.ping.side_effect = auth_error
        with (
            patch(
                "ladini.core.database.get_engine", return_value=_FakeEngine()
            ),
            patch("redis.from_url", return_value=fake_redis_client),
        ):
            response = run(readiness_check())

        assert response.status_code == 503
        payload = _body(response)
        assert payload["components"]["redis"] == "error: AuthenticationError"
        raw_body = response.body.decode()
        assert secret_password not in raw_body


class TestReadinessDbDown:
    def test_db_down_returns_503_even_if_redis_ok(self):
        fake_redis_client = MagicMock()
        fake_redis_client.ping.return_value = True
        with (
            patch(
                "ladini.core.database.get_engine", return_value=_FailingEngine()
            ),
            patch("redis.from_url", return_value=fake_redis_client),
        ):
            response = run(readiness_check())

        assert response.status_code == 503
        payload = _body(response)
        assert payload["status"] == "degraded"
        assert payload["components"]["database"] == "error: RuntimeError"
        assert payload["components"]["redis"] == "ok"


class TestReadinessMalformedUrl:
    def test_malformed_redis_url_is_reported_as_error_not_500(self):
        with (
            patch(
                "ladini.core.database.get_engine", return_value=_FakeEngine()
            ),
            patch(
                "redis.from_url",
                side_effect=ValueError("Redis URL must specify one of the following"),
            ),
        ):
            response = run(readiness_check())

        assert response.status_code == 503
        payload = _body(response)
        assert payload["components"]["redis"] == "error: ValueError"
