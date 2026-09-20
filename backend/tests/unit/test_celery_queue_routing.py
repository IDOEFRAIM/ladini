"""`api/celery_app.py::TASK_ROUTES` — verrou de non-régression ajouté
(2026-09-20, migration Upstash → Valkey) : la migration du broker ne doit
JAMAIS changer silencieusement le nom des files (`interactive`/`background`/
`scheduled`/`celery`, la file par défaut) ni leur routage — un worker
`docker-compose.prod.yml` écoute une liste FIGÉE de files (`-Q`
`${CELERY_WORKER_QUEUES:-interactive,background,scheduled,celery}`) ; si une
tâche est routée vers une file que plus aucun worker n'écoute, elle
s'accumule silencieusement dans Redis/Valkey sans jamais s'exécuter.

Importe `celery_app` (donc exécute le module top-level, y compris
`Celery(broker=..., backend=...)`) — REDIS_URL par défaut
("redis://localhost:6379/0", voir core/settings.py) suffit : Celery ne se
connecte pas au broker à l'import, seulement à l'exécution d'une tâche."""

from __future__ import annotations

from ladini.api.celery_app import (
    _BACKGROUND_QUEUE,
    _INTERACTIVE_QUEUE,
    _SCHEDULED_QUEUE,
    TASK_ROUTES,
    celery_app,
)

_DEFAULT_QUEUE = "celery"  # défaut Celery, jamais renommé ici (task_create_missing_queues=True)

# Doit rester strictement identique à la valeur par défaut de
# `CELERY_WORKER_QUEUES` dans docker-compose.prod.yml (`-Q` du service worker).
_EXPECTED_QUEUES = {_INTERACTIVE_QUEUE, _BACKGROUND_QUEUE, _SCHEDULED_QUEUE, _DEFAULT_QUEUE}


class TestQueueNamesUnchanged:
    def test_queue_name_constants_match_docker_compose_default(self):
        assert _INTERACTIVE_QUEUE == "interactive"
        assert _BACKGROUND_QUEUE == "background"
        assert _SCHEDULED_QUEUE == "scheduled"

    def test_all_routed_queues_are_in_the_expected_set(self):
        routed_queues = {route["queue"] for route in TASK_ROUTES.values()}
        assert routed_queues <= _EXPECTED_QUEUES, (
            f"TASK_ROUTES route vers une file inattendue : "
            f"{routed_queues - _EXPECTED_QUEUES} — un worker de prod ne "
            f"l'écoute peut-être pas (voir CELERY_WORKER_QUEUES dans "
            f"docker-compose.prod.yml)"
        )


class TestCriticalTaskRouting:
    """Les tâches interactives (latence utilisateur WhatsApp perçue) ne
    doivent JAMAIS glisser dans la même file que les tâches de fond lentes —
    c'est exactement le risque de tête-de-ligne documenté dans
    celery_app.py (2026-09-16)."""

    def test_process_agent_task_stays_on_interactive_queue(self):
        assert (
            TASK_ROUTES["ladini.api.tasks.process_agent_task"]["queue"]
            == _INTERACTIVE_QUEUE
        )

    def test_media_and_payment_tasks_stay_on_background_queue(self):
        assert (
            TASK_ROUTES["ladini.workers.media.product_photo_task.*"]["queue"]
            == _BACKGROUND_QUEUE
        )
        assert (
            TASK_ROUTES["ladini.workers.payments.paydunya_ipn_task.*"]["queue"]
            == _BACKGROUND_QUEUE
        )

    def test_crons_stay_on_scheduled_queue(self):
        assert TASK_ROUTES["ladini.workers.crons.*"]["queue"] == _SCHEDULED_QUEUE
        assert TASK_ROUTES["workers.*"]["queue"] == _SCHEDULED_QUEUE


class TestCeleryAppConfConsistentWithMigration:
    """La migration Upstash -> Valkey ne doit JAMAIS changer le
    comportement de reconnexion/acquittement des tâches — seul le broker
    change d'hôte, pas la sémantique de livraison."""

    def test_task_acks_late_unchanged(self):
        assert celery_app.conf.task_acks_late is True

    def test_broker_connection_retry_unchanged(self):
        assert celery_app.conf.broker_connection_retry is True
        assert celery_app.conf.broker_connection_max_retries == 2

    def test_visibility_timeout_unchanged(self):
        assert celery_app.conf.broker_transport_options["visibility_timeout"] == 660

    def test_task_create_missing_queues_enabled(self):
        assert celery_app.conf.task_create_missing_queues is True
