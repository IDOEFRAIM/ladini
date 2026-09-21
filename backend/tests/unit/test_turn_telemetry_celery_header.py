"""L'horodatage de publication traverse un VRAI worker Celery (broker mémoire) : `queue_duration_ms` est mesurable."""
from __future__ import annotations

import time

from celery import Celery
from celery.contrib.testing.worker import start_worker

from ladini.core import turn_telemetry as tt


def test_publish_timestamp_reaches_the_task_request_through_a_real_worker(monkeypatch):
    # Nom de tâche PROPRE au test : jamais celui de la vraie tâche (résolue via l'app Celery par défaut dans la suite complète).
    monkeypatch.setattr(tt, "TRACKED_TASKS", {*tt.TRACKED_TASKS, "hdr_test.tracked"})
    tt.connect_celery_signals()
    app = Celery("hdr_test", broker="memory://", backend="cache+memory://")
    seen: dict = {}

    @app.task(bind=True, name="hdr_test.tracked")
    def tracked_task(self):
        seen["enqueued_at"] = tt.enqueued_at_from_request(self.request)
        return "ok"

    with start_worker(app, perform_ping_check=False, pool="solo", loglevel="ERROR"):
        before = time.time()
        assert tracked_task.delay().get(timeout=20) == "ok"

    assert seen["enqueued_at"] is not None and before - 1 <= seen["enqueued_at"] <= time.time()
    assert tt.TurnRecorder(phone="+22670000000", enqueued_at=seen["enqueued_at"]).queue_duration_ms is not None
