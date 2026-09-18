"""`ladini.api.celery_app` — configuration TLS du broker/backend Redis
(2026-09-18, incident réel production, release sha-891cb2f).

Le worker Celery crashait au démarrage :
    ValueError: A rediss:// URL must have parameter ssl_cert_reqs and this
    must be set to CERT_REQUIRED, CERT_OPTIONAL, or CERT_NONE
levée depuis `celery/backends/redis.py::RedisBackend.__init__` — confirmé en
lisant le code source installé : `rediss://` sans `redis_backend_use_ssl`
explicite lève une exception bloquante côté RESULT BACKEND (aucun repli),
alors que côté BROKER (kombu), le même cas ne fait qu'un warning et retombe
silencieusement sur `ssl_cert_reqs=CERT_NONE` (aucune vérification de
certificat) — voir `kombu/connection.py::Connection._init_params`.

Ces tests exercent `ladini.api.celery_app._redis_ssl_options`, la fonction
RÉELLE de production (pas une copie), qui calcule ces options pour le broker
ET le backend, séparément, à partir de LEUR PROPRE schéma d'URL."""
from __future__ import annotations

import ssl

import pytest

from ladini.api.celery_app import _redis_ssl_options
from ladini.core.settings import settings as _settings_singleton


class TestRedissUrlRequiresExplicitSslCertReqs:
    def test_a_broker_use_ssl_on_rediss_defaults_to_cert_required(self, monkeypatch):
        """Cas A."""
        monkeypatch.setattr(_settings_singleton, "REDIS_TLS_CERT_REQS", "required", raising=False)
        opts = _redis_ssl_options("rediss://user:pass@redis.example.internal:6379/0")
        assert opts == {"ssl_cert_reqs": ssl.CERT_REQUIRED}

    def test_b_redis_backend_use_ssl_on_rediss_defaults_to_cert_required(self, monkeypatch):
        """Cas B — même fonction, même résultat pour le backend (le schéma
        est ce qui compte, pas le rôle broker/backend)."""
        monkeypatch.setattr(_settings_singleton, "REDIS_TLS_CERT_REQS", "required", raising=False)
        opts = _redis_ssl_options("rediss://user:pass@redis.example.internal:6379/1")
        assert opts == {"ssl_cert_reqs": ssl.CERT_REQUIRED}


class TestPlainRedisNeverForcesSslConfig:
    def test_c_redis_scheme_returns_none_no_ssl_forced(self):
        """Cas C — `redis://` (dev local, pas de TLS) : Celery lève lui-même
        une erreur si des paramètres SSL traînent sur ce schéma (voir
        celery/backends/redis.py::_params_from_url) — `_redis_ssl_options`
        doit donc renvoyer `None`, jamais un dict, même vide."""
        assert _redis_ssl_options("redis://localhost:6379/0") is None

    def test_unknown_scheme_also_returns_none(self):
        assert _redis_ssl_options("") is None


class TestBrokerAndBackendCanDifferIndependently:
    def test_d_each_url_configured_according_to_its_own_scheme(self, monkeypatch):
        """Cas D — CELERY_BROKER_URL/CELERY_RESULT_BACKEND peuvent diverger
        de REDIS_URL (settings.py::celery_broker/celery_backend) : rien ne
        garantit qu'ils partagent le même schéma. Chacun doit être résolu
        SÉPARÉMENT, sans qu'un `rediss://` sur l'un affecte l'autre."""
        monkeypatch.setattr(_settings_singleton, "REDIS_TLS_CERT_REQS", "required", raising=False)
        broker_opts = _redis_ssl_options("rediss://user:pass@broker.example:6379/0")
        backend_opts = _redis_ssl_options("redis://backend.example:6379/1")
        assert broker_opts == {"ssl_cert_reqs": ssl.CERT_REQUIRED}
        assert backend_opts is None


class TestCeleryAppImportsCleanly:
    def test_e_import_ladini_api_celery_app_succeeds(self):
        """Cas E — l'import ne doit JAMAIS lever (c'est très exactement ce
        qui plantait en production : le crash survenait à l'accès à
        `app.backend`, déclenché tôt dans le boot du worker). Réimport
        explicite (pas seulement "déjà importé par un autre test") pour
        prouver que le module lui-même, tel quel, s'importe sans exception
        avec les settings de test par défaut (REDIS_URL non-TLS)."""
        import importlib

        import ladini.api.celery_app as mod

        importlib.reload(mod)
        assert mod.celery_app is not None
        assert mod.celery_app.main == "ladini_worker"


class TestInvalidSslConfigIsNeverSilent:
    def test_f_unknown_cert_reqs_value_falls_back_to_required_with_warning(self, monkeypatch, caplog):
        """Cas F — une valeur de REDIS_TLS_CERT_REQS invalide ne doit JAMAIS
        être silencieusement ignorée (ce qui reviendrait à retomber sur le
        comportement kombu par défaut, CERT_NONE — exactement le repli
        insécurisé documenté dans kombu/connection.py). Elle doit être
        détectable : repli sur le choix le PLUS strict (CERT_REQUIRED, jamais
        plus faible) ET un warning logué explicitement."""
        monkeypatch.setattr(_settings_singleton, "REDIS_TLS_CERT_REQS", "totally-not-a-real-value", raising=False)
        import logging

        with caplog.at_level(logging.WARNING, logger="ladini.api.celery_app"):
            opts = _redis_ssl_options("rediss://user:pass@redis.example.internal:6379/0")
        assert opts == {"ssl_cert_reqs": ssl.CERT_REQUIRED}
        assert any("inconnu" in rec.message.lower() or "REDIS_TLS_CERT_REQS" in rec.message for rec in caplog.records)

    def test_f_weaker_than_required_still_applies_but_warns_loudly(self, monkeypatch, caplog):
        """Cas F (variante) — un choix EXPLICITE mais plus faible (CERT_NONE)
        est respecté (c'est un choix documenté d'opérateur, pas une valeur
        invalide) — mais ne doit JAMAIS passer inaperçu : un warning est
        logué à chaque résolution."""
        monkeypatch.setattr(_settings_singleton, "REDIS_TLS_CERT_REQS", "none", raising=False)
        import logging

        with caplog.at_level(logging.WARNING, logger="ladini.api.celery_app"):
            opts = _redis_ssl_options("rediss://user:pass@redis.example.internal:6379/0")
        assert opts == {"ssl_cert_reqs": ssl.CERT_NONE}
        assert any("affaiblie" in rec.message.lower() for rec in caplog.records)


class TestRealCeleryBackendInitProvesTheFix:
    """Reproduit le VRAI crash de production avec le VRAI code Celery
    (celery/backends/redis.py), pas une simulation — puis prouve que le fix
    l'évite. `broker=None` : on ne teste QUE l'initialisation du backend
    (RedisBackend), pas une vraie connexion réseau."""

    def test_without_ssl_options_rediss_backend_raises_exactly_the_production_error(self):
        from celery import Celery

        app = Celery("repro-before-fix", backend="rediss://u:p@fake.invalid:6379/0")
        with pytest.raises(ValueError, match="ssl_cert_reqs"):
            _ = app.backend

    def test_with_ssl_options_rediss_backend_initializes_without_raising(self, monkeypatch):
        from celery import Celery

        monkeypatch.setattr(_settings_singleton, "REDIS_TLS_CERT_REQS", "required", raising=False)
        ssl_opts = _redis_ssl_options("rediss://u:p@fake.invalid:6379/0")
        app = Celery("repro-after-fix", backend="rediss://u:p@fake.invalid:6379/0")
        app.conf.update(redis_backend_use_ssl=ssl_opts)
        backend = app.backend  # ne doit PAS lever
        assert backend is not None
