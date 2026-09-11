"""`api/routes/whatsapp_webhook.py` — callbacks de statut sortant (2026-09-11,
diagnostic « message marqué SENT dans l'Outbox mais jamais reçu par
l'utilisateur »).

Un `POST /messages` qui répond 200 + un `wamid` (voir
`services/whatsapp/cloud_api_client.py::_post`) signifie seulement « Meta a
accepté d'essayer », JAMAIS « livré ». La livraison réelle (et son éventuel
échec, avec le motif) arrive de façon asynchrone via CE webhook, dans le
tableau `statuses`. Avant ce correctif, `failed` était traité exactement
comme `delivered`/`read` — un `logger.info` du mot "failed", sans jamais lire
`errors` (où Meta met le VRAI motif, ex: code 131047 "Re-engagement message"
= fenêtre de conversation de 24h dépassée, le cas typique d'une notification
proactive de l'Outbox — confirmation de commande, résultat d'enchère — que
rien n'a déclenchée en réponse immédiate à un message entrant).

Étape 1 du diagnostic seulement : ce module journalise le motif réel, il ne
corrige pas encore l'échec (pas de retry, pas de repli template)."""
from __future__ import annotations

import logging

import pytest

pytest.importorskip("celery", reason="whatsapp_webhook imports api.tasks -> celery.signals")

from ladini.api.routes.whatsapp_webhook import (  # noqa: E402
    _STATUS_EVENTS_TO_IGNORE,
    _log_failed_status,
)


class TestFailedStatusIsNoLongerDiscarded:
    def test_failed_is_not_in_the_ignore_set(self):
        """Le coeur du correctif : `failed` ne doit PLUS être traité comme
        `sent`/`delivered`/`read` — sinon la branche de journalisation
        détaillée n'est jamais atteinte."""
        assert "failed" not in _STATUS_EVENTS_TO_IGNORE
        assert {"sent", "delivered", "read"} <= _STATUS_EVENTS_TO_IGNORE

    def test_the_real_meta_error_code_is_logged(self, caplog):
        """Le cas réel visé : fenêtre de 24h dépassée (notification
        proactive de l'Outbox — confirmation de commande, enchère)."""
        status = {
            "id": "wamid.HBgLMjI2NzAwMDAwMDAV",
            "status": "failed",
            "recipient_id": "22670000000",
            "errors": [
                {
                    "code": 131047,
                    "title": "Re-engagement message",
                    "message": "Re-engagement message",
                    "error_data": {
                        "details": (
                            "Message failed to send because more than 24 "
                            "hours have passed since the customer last "
                            "replied to this number."
                        )
                    },
                }
            ],
        }
        with caplog.at_level(logging.WARNING, logger="Ladini.WhatsAppWebhook"):
            _log_failed_status(status)

        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert record.levelno == logging.WARNING
        msg = record.getMessage()
        assert "wamid.HBgLMjI2NzAwMDAwMDAV" in msg
        assert "22670000000" in msg
        assert "131047" in msg
        assert "Re-engagement message" in msg
        assert "24 hours" in msg

    def test_multiple_errors_are_all_logged(self, caplog):
        status = {
            "id": "wamid.X",
            "recipient_id": "22670000001",
            "errors": [
                {"code": 131026, "title": "Message undeliverable"},
                {"code": 131047, "title": "Re-engagement message"},
            ],
        }
        with caplog.at_level(logging.WARNING, logger="Ladini.WhatsAppWebhook"):
            _log_failed_status(status)

        assert len(caplog.records) == 2
        codes = {r.getMessage() for r in caplog.records}
        assert any("131026" in m for m in codes)
        assert any("131047" in m for m in codes)

    def test_a_failed_status_with_no_error_detail_is_still_logged(self, caplog):
        """Ne doit JAMAIS redevenir silencieux, même si Meta omet `errors` —
        c'est exactement le trou que ce correctif comble."""
        status = {"id": "wamid.Y", "recipient_id": "22670000002", "status": "failed"}
        with caplog.at_level(logging.WARNING, logger="Ladini.WhatsAppWebhook"):
            _log_failed_status(status)

        assert len(caplog.records) == 1
        assert caplog.records[0].levelno == logging.WARNING
        assert "wamid.Y" in caplog.records[0].getMessage()

    def test_a_malformed_error_entry_does_not_crash(self, caplog):
        status = {
            "id": "wamid.Z",
            "recipient_id": "22670000003",
            "errors": ["not-a-dict", {"code": 999, "title": "ok one"}],
        }
        with caplog.at_level(logging.WARNING, logger="Ladini.WhatsAppWebhook"):
            _log_failed_status(status)  # ne doit pas lever

        assert any("999" in r.getMessage() for r in caplog.records)


class TestDeliveredSentReadStillIgnored:
    """Non-régression : le comportement pour les statuts NON-`failed` ne
    change pas (le dispatch principal dans `_handle_whatsapp_webhook` route
    toujours ces 3-là vers le simple log INFO existant, pas vers
    `_log_failed_status`)."""

    def test_ignore_set_contains_exactly_the_three_non_failure_statuses(self):
        assert _STATUS_EVENTS_TO_IGNORE == frozenset({"sent", "delivered", "read"})


class TestWebhookDispatchRoutesFailedToTheNewHandler:
    """Bout-en-bout à travers `_handle_whatsapp_webhook` (pas seulement la
    fonction pure) : un vrai payload Graph API avec un statut `failed` doit
    atteindre `_log_failed_status` ; `delivered` ne doit pas."""

    def _payload(self, *, status: str, extra: dict | None = None) -> dict:
        entry = {
            "id": "waba-id",
            "changes": [
                {
                    "value": {
                        "messaging_product": "whatsapp",
                        "statuses": [
                            {
                                "id": "wamid.ABC",
                                "status": status,
                                "recipient_id": "22670000009",
                                **(extra or {}),
                            }
                        ],
                    }
                }
            ],
        }
        return {"entry": [entry]}

    def _fake_request(self, payload: dict):
        import json

        class _Req:
            headers: dict = {}

            async def body(self) -> bytes:
                return json.dumps(payload).encode()

            async def json(self):
                return payload

        return _Req()

    def _run(self, monkeypatch, payload):
        import asyncio

        from ladini.api.routes import whatsapp_webhook as mod
        from ladini.core.settings import settings

        monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", "", raising=False)
        monkeypatch.setenv("ENV", "development")  # bypass signature (pas de secret déclaré)

        calls: list[dict] = []
        monkeypatch.setattr(
            mod, "_log_failed_status", lambda st: calls.append(st), raising=False
        )
        response = asyncio.run(mod._handle_whatsapp_webhook(self._fake_request(payload), None))
        return response, calls

    def test_a_failed_status_reaches_log_failed_status(self, monkeypatch):
        payload = self._payload(
            status="failed", extra={"errors": [{"code": 131047, "title": "x"}]}
        )
        response, calls = self._run(monkeypatch, payload)

        assert response.status_code == 200
        assert len(calls) == 1
        assert calls[0]["id"] == "wamid.ABC"

    def test_a_delivered_status_does_not_reach_log_failed_status(self, monkeypatch):
        payload = self._payload(status="delivered")
        response, calls = self._run(monkeypatch, payload)

        assert response.status_code == 200
        assert calls == []
