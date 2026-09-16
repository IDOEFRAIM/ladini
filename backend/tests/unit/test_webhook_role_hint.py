"""`api/routes/twilio_webhook.py`/`whatsapp_webhook.py` — indice d'ÉTAT de
rôle attendu, lu AVANT de figer `force_role` (2026-09-13, incident WhatsApp
#3, utilisateur double-rôle, 2e couche du correctif).

## Le gap que ce fichier ferme

Le premier correctif (`orchestrator.py::_run_market`, voir
`test_orchestrator_dual_role_hint.py`) lit `pending_role_hint:{phone}` avant
de faire confiance à `ws.workspace_type` — MAIS seulement quand
`force_role` n'est pas déjà `True`. Ce webhook (le point d'entrée RÉEL de
tout message WhatsApp) fige `force_role=True` pour TOUT numéro ayant déjà un
workspace — ce qui court-circuite TOTALEMENT le correctif de
l'orchestrateur : il ne s'exécute jamais en pratique tant que ce webhook n'a
pas, lui aussi, été corrigé. Un producteur qui répond "confirmer" à une
notification de commande reçue, alors que son workspace est resté en mode
BUYER (session buyer antérieure), continuait donc à être classifié contre
le catalogue BUYER — malgré le premier correctif, entièrement mort côté
webhook.

Ce fichier verrouille que le webhook lit et consomme
`pending_role_hint:{phone}` lui-même, AVANT de calculer
`ws_type`/`resolved_role`/`force_role`, pour les DEUX webhooks (Twilio et
WhatsApp Cloud — même gabarit, même bug, même correctif)."""
from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock, Mock

import pytest

from tests.conftest import run

pytest.importorskip("celery", reason="webhook module imports api.tasks -> celery.signals")

PHONE = "+22670000001"


class _FakeWorkspace:
    def __init__(self, workspace_type: str = "buyer"):
        self.metadata: Dict[str, Any] = {}
        self.workspace_type = workspace_type


class _FakeRequest:
    def __init__(self, form: Dict[str, str]):
        self._form = form

        class _State:
            trace_id = None

        self.state = _State()

    async def form(self):
        return self._form


def _text_form(text: str = "confirmer") -> Dict[str, str]:
    return {
        "From": f"whatsapp:{PHONE}",
        "Body": text,
        "MessageSid": "SMxxxROLEHINTxxx",
        "NumMedia": "0",
    }


class TestTwilioWebhookConsumesThePendingRoleHint:
    async def _run(self, monkeypatch, *, hint: str | None, ws_type: str = "buyer"):
        import ladini.api.routes.twilio_webhook as webhook_mod

        monkeypatch.setattr(webhook_mod.redis_client, "set", lambda *a, **k: True)
        monkeypatch.setattr(
            webhook_mod.WorkspaceStore,
            "get",
            AsyncMock(return_value=_FakeWorkspace(workspace_type=ws_type)),
        )
        released: list[str] = []
        monkeypatch.setattr(webhook_mod, "_get_role_hint", lambda key: hint)
        monkeypatch.setattr(
            webhook_mod, "_release_role_hint", lambda key: released.append(key)
        )
        delay_mock = Mock()
        monkeypatch.setattr(webhook_mod.process_agent_task, "delay", delay_mock)

        request = _FakeRequest(_text_form())
        await webhook_mod._handle_twilio_webhook(
            request, None, f"whatsapp:{PHONE}", "confirmer", "SMxxxROLEHINTxxx"
        )
        return delay_mock, released

    def test_a_producer_hint_overrides_the_stale_buyer_workspace(self, monkeypatch):
        delay_mock, released = run(
            self._run(monkeypatch, hint="PRODUCER", ws_type="buyer")
        )
        kwargs = delay_mock.call_args.kwargs
        assert kwargs["workspace_type"] == "producer"
        assert kwargs["role"] == "PRODUCER"
        assert kwargs["force_role"] is True
        assert released == [f"pending_role_hint:{PHONE}"]

    def test_no_hint_keeps_the_sticky_workspace_type(self, monkeypatch):
        """Non-régression : le cas normal (immense majorité des tours) —
        pas d'indice en attente — garde le comportement historique."""
        delay_mock, released = run(
            self._run(monkeypatch, hint=None, ws_type="buyer")
        )
        kwargs = delay_mock.call_args.kwargs
        assert kwargs["workspace_type"] == "buyer"
        assert kwargs["role"] == "BUYER"
        assert kwargs["force_role"] is True
        assert released == []


class TestWhatsappCloudWebhookConsumesThePendingRoleHint:
    async def _run(self, monkeypatch, *, hint: str | None, ws_type: str = "buyer"):
        import ladini.api.routes.whatsapp_webhook as webhook_mod

        monkeypatch.setattr(
            webhook_mod.WorkspaceStore,
            "get",
            AsyncMock(return_value=_FakeWorkspace(workspace_type=ws_type)),
        )
        released: list[str] = []
        monkeypatch.setattr(webhook_mod, "_get_role_hint", lambda key: hint)
        monkeypatch.setattr(
            webhook_mod, "_release_role_hint", lambda key: released.append(key)
        )
        delay_mock = Mock()
        monkeypatch.setattr(webhook_mod.process_agent_task, "delay", delay_mock)

        message = {
            "id": "wamid.xxxROLEHINTxxx",
            "from": PHONE.lstrip("+"),
            "type": "text",
            "text": {"body": "confirmer"},
        }
        await webhook_mod._process_single_message(message, None)
        return delay_mock, released

    def test_a_producer_hint_overrides_the_stale_buyer_workspace(self, monkeypatch):
        delay_mock, released = run(
            self._run(monkeypatch, hint="PRODUCER", ws_type="buyer")
        )
        kwargs = delay_mock.call_args.kwargs
        assert kwargs["workspace_type"] == "producer"
        assert kwargs["role"] == "PRODUCER"
        assert kwargs["force_role"] is True
        assert released == [f"pending_role_hint:{PHONE.lstrip('+')}"]

    def test_no_hint_keeps_the_sticky_workspace_type(self, monkeypatch):
        delay_mock, released = run(
            self._run(monkeypatch, hint=None, ws_type="buyer")
        )
        kwargs = delay_mock.call_args.kwargs
        assert kwargs["workspace_type"] == "buyer"
        assert kwargs["role"] == "BUYER"
        assert kwargs["force_role"] is True
        assert released == []
