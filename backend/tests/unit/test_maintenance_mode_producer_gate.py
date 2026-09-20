"""Verrou de non-régression : les 5 points d'entrée qui PRODUISENT une
tâche Celery doivent refuser de publier (503) quand
`core.maintenance.is_celery_producer_paused()` est vrai — c'est le
mécanisme qui garantit qu'il n'existe JAMAIS de fenêtre "producteur sur le
broker A, consommateur sur le broker B" pendant le cutover Upstash →
Valkey (2026-09-20).

`api/routes/paydunya_webhook.py` et `api/routes/market.py` sont testés de
bout en bout via `TestClient` (léger, aucune vérification de signature à
mocker). `twilio_webhook.py`/`whatsapp_webhook.py` sont testés
STRUCTURELLEMENT (le check apparaît bien AVANT le `.delay()`/`to_thread`
dans le fichier source) plutôt que de bout en bout : leurs routes
nécessitent de mocker signature Twilio/HMAC WhatsApp + résolution DB de
workspace, hors de portée d'un test unitaire ciblé sur CE mécanisme précis
— même discipline que `scripts/test/test-node-preflight-lock.sh` Cas G4
(assertion d'ordre sur le code source)."""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("celery", reason="api.main imports api.tasks -> celery.signals")


@pytest.fixture
def client(monkeypatch):
    from ladini.core.settings import settings

    monkeypatch.setattr(settings, "INTERNAL_API_TOKEN", "test-secret", raising=False)
    from ladini.api.main import app

    return TestClient(app)


class TestPaydunyaWebhookMaintenanceGate:
    def test_returns_503_and_never_enqueues_when_paused(self, client):
        with (
            patch(
                "ladini.api.routes.paydunya_webhook.is_celery_producer_paused",
                return_value=True,
            ),
            patch(
                "ladini.workers.payments.paydunya_ipn_task.process_paydunya_ipn.delay"
            ) as mock_delay,
        ):
            response = client.post(
                "/api/webhooks/paydunya-ipn",
                json={"token": "inv_test_123"},
            )
        assert response.status_code == 503
        mock_delay.assert_not_called()

    def test_returns_200_and_enqueues_when_not_paused(self, client):
        with (
            patch(
                "ladini.api.routes.paydunya_webhook.is_celery_producer_paused",
                return_value=False,
            ),
            patch(
                "ladini.workers.payments.paydunya_ipn_task.process_paydunya_ipn.delay"
            ) as mock_delay,
        ):
            response = client.post(
                "/api/webhooks/paydunya-ipn",
                json={"token": "inv_test_123"},
            )
        assert response.status_code == 200
        mock_delay.assert_called_once_with("inv_test_123")


class TestMarketRoutesMaintenanceGate:
    def test_producer_route_returns_503_when_paused(self, client):
        with (
            patch(
                "ladini.api.routes.market.is_celery_producer_paused",
                return_value=True,
            ),
            patch("ladini.api.routes.market.process_agent_task.delay") as mock_delay,
        ):
            response = client.post(
                "/api/market/producer",
                json={"message": "salut", "phone_number": "+22670000001"},
                headers={"X-Internal-Token": "test-secret"},
            )
        assert response.status_code == 503
        mock_delay.assert_not_called()

    def test_buyer_route_returns_503_when_paused(self, client):
        with (
            patch(
                "ladini.api.routes.market.is_celery_producer_paused",
                return_value=True,
            ),
            patch("ladini.api.routes.market.process_agent_task.delay") as mock_delay,
        ):
            response = client.post(
                "/api/market/buyer",
                json={"message": "salut", "phone_number": "+22670000001"},
                headers={"X-Internal-Token": "test-secret"},
            )
        assert response.status_code == 503
        mock_delay.assert_not_called()

    def test_producer_route_enqueues_normally_when_not_paused(self, client):
        with (
            patch(
                "ladini.api.routes.market.is_celery_producer_paused",
                return_value=False,
            ),
            patch("ladini.api.routes.market.process_agent_task.delay") as mock_delay,
        ):
            mock_delay.return_value = MagicMock(id="task-123")
            response = client.post(
                "/api/market/producer",
                json={"message": "salut", "phone_number": "+22670000001"},
                headers={"X-Internal-Token": "test-secret"},
            )
        assert response.status_code == 200
        assert response.json()["task_id"] == "task-123"
        mock_delay.assert_called_once()


class TestWebhookSourceOrderingStructural:
    """`twilio_webhook.py`/`whatsapp_webhook.py` : le check de maintenance
    doit apparaître AVANT le premier `.delay(`/`to_thread(` dans le fichier
    source — assertion structurelle, pas d'exécution complète du webhook
    (voir docstring du module)."""

    def test_twilio_webhook_checks_maintenance_before_delay(self):
        from ladini.api.routes import twilio_webhook

        source = inspect.getsource(twilio_webhook)
        check_pos = source.find("is_celery_producer_paused()")
        delay_pos = source.find("process_agent_task.delay")
        assert check_pos != -1, "is_celery_producer_paused() introuvable dans twilio_webhook.py"
        assert delay_pos != -1, "process_agent_task.delay introuvable dans twilio_webhook.py"
        assert check_pos < delay_pos, (
            "le check de maintenance doit apparaître AVANT process_agent_task.delay "
            "dans twilio_webhook.py — régression possible de la fenêtre "
            "producteur/consommateur sur des brokers différents"
        )

    def test_whatsapp_webhook_checks_maintenance_before_delay(self):
        from ladini.api.routes import whatsapp_webhook

        source = inspect.getsource(whatsapp_webhook)
        check_pos = source.find("is_celery_producer_paused()")
        delay_pos = source.find("process_agent_task.delay")
        assert check_pos != -1, "is_celery_producer_paused() introuvable dans whatsapp_webhook.py"
        assert delay_pos != -1, "process_agent_task.delay introuvable dans whatsapp_webhook.py"
        assert check_pos < delay_pos, (
            "le check de maintenance doit apparaître AVANT process_agent_task.delay "
            "dans whatsapp_webhook.py — régression possible de la fenêtre "
            "producteur/consommateur sur des brokers différents"
        )
