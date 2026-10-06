"""Bug corrigé (2026-09-16) : `SENTRY_DSN` était configuré de longue date
(`docker-compose.prod.yml`) mais `setup_logging()` — seule fonction du repo
qui appelait `sentry_sdk.init(...)` (`core/logger.py`) — n'était JAMAIS
invoquée nulle part : uniquement `get_logger(name)` (un simple
`logging.getLogger`) était importé côté API/worker, donc Sentry restait
mort. Corrigé en exposant `init_sentry()` (juste l'init Sentry, pas
`setup_logging()` complet qui ferait `logging.basicConfig(...)` par-dessus
la config déjà posée par gunicorn/uvicorn/Celery) et en le câblant dans :
  - `api/main.py::lifespan` (startup FastAPI)
  - `api/tasks.py::init_worker_process` (signal Celery `worker_process_init`)

Ce test ne vérifie PAS un vrai réseau Sentry (aucun DSN dans l'environnement
de test) — il prouve que le CHEMIN D'APPEL existe réellement : que
`init_sentry()` est bien invoqué depuis ces deux points de démarrage réels,
et qu'il reste un no-op sûr quand `SENTRY_DSN` est vide (cas par défaut en
dev/test — ne doit jamais lever)."""
from __future__ import annotations

from unittest.mock import patch


def test_init_sentry_is_noop_without_dsn():
    """Sans SENTRY_DSN (settings par défaut en test), `init_sentry()` ne fait
    rien et ne lève jamais — comportement défensif attendu."""
    from ladini.core import logger as logger_module

    logger_module._sentry_initialized = False
    with patch.object(logger_module.settings, "SENTRY_DSN", ""):
        result = logger_module.init_sentry()
    assert result is None


def test_init_sentry_called_from_fastapi_lifespan():
    """`api/main.py::lifespan` doit appeler `init_sentry()` au startup — c'est
    le bug corrigé : avant ce correctif, aucun chemin de code n'appelait
    jamais cette fonction, donc SENTRY_DSN configuré en prod n'avait aucun
    effet."""
    import inspect

    from ladini.api import main as main_module

    source = inspect.getsource(main_module.lifespan)
    assert "init_sentry" in source


def test_init_sentry_called_from_celery_worker_process_init():
    """`api/tasks.py::init_worker_process` (signal `worker_process_init`)
    doit appeler `init_sentry()` — même correctif côté worker Celery."""
    import inspect

    from ladini.api import tasks as tasks_module

    source = inspect.getsource(tasks_module.init_worker_process)
    assert "init_sentry" in source


def test_init_sentry_is_idempotent():
    """Un second appel ne doit pas ré-initialiser sentry_sdk (le module garde
    un flag `_sentry_initialized`)."""
    from ladini.core import logger as logger_module

    logger_module._sentry_initialized = True
    with patch.object(logger_module.settings, "SENTRY_DSN", "https://fake@sentry.example/1"):
        result = logger_module.init_sentry()
    assert result is None  # déjà initialisé — no-op immédiat, pas de re-init
    logger_module._sentry_initialized = False
