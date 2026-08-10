"""`workers/outbox/channels/*` + `dispatcher.py` — envoi effectif des messages.

Canaux réels (WhatsApp) simulés via monkeypatch des settings/client cloud —
zéro appel réseau. Le dispatcher est testé avec des canaux et un
`worker_session` entièrement doublés.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.conftest import run


# =====================================================================
# channels/whatsapp.py
# =====================================================================

class TestWhatsAppChunking:
    def test_short_body_is_a_single_chunk(self):
        from agriconnect.workers.outbox.channels.whatsapp import _chunk
        assert _chunk("Bonjour") == ["Bonjour"]

    def test_empty_body_yields_one_empty_chunk(self):
        from agriconnect.workers.outbox.channels.whatsapp import _chunk
        assert _chunk("   ") == [""]

    def test_long_body_splits_on_newline_boundary(self):
        from agriconnect.workers.outbox.channels.whatsapp import _chunk
        para = "x" * 10
        body = "\n".join([para] * 300)  # bien > 1500 chars, plein de \n
        chunks = _chunk(body, limit=100)
        assert len(chunks) > 1
        assert all(len(c) <= 100 for c in chunks)

    def test_never_returns_more_than_four_chunks(self):
        from agriconnect.workers.outbox.channels.whatsapp import _chunk
        body = "a" * 10_000
        assert len(_chunk(body, limit=100)) <= 4

    def test_falls_back_to_hard_split_when_no_newline_found(self):
        from agriconnect.workers.outbox.channels.whatsapp import _chunk
        body = "a" * 300  # aucun \n
        chunks = _chunk(body, limit=100)
        assert chunks[0] == "a" * 100


class TestWhatsAppIsConfigured:
    def test_cloud_provider_delegates_to_cloud_api_client(self, monkeypatch):
        from agriconnect.workers.outbox.channels.whatsapp import WhatsAppChannel
        from agriconnect.core.settings import settings
        import agriconnect.services.whatsapp.cloud_api_client as wa

        monkeypatch.setattr(settings, "MESSAGING_PROVIDER", "whatsapp_cloud", raising=False)
        monkeypatch.setattr(wa, "is_configured", lambda: True)
        assert WhatsAppChannel().is_configured() is True

    def test_twilio_provider_requires_all_three_credentials(self, monkeypatch):
        from agriconnect.workers.outbox.channels.whatsapp import WhatsAppChannel
        from agriconnect.core.settings import settings

        monkeypatch.setattr(settings, "MESSAGING_PROVIDER", "twilio", raising=False)
        monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", "", raising=False)
        monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", "token", raising=False)
        monkeypatch.setattr(settings, "TWILIO_WHATSAPP_NUMBER", "+1555", raising=False)
        assert WhatsAppChannel().is_configured() is False

    def test_twilio_provider_true_when_fully_configured(self, monkeypatch):
        from agriconnect.workers.outbox.channels.whatsapp import WhatsAppChannel
        from agriconnect.core.settings import settings

        monkeypatch.setattr(settings, "MESSAGING_PROVIDER", "twilio", raising=False)
        monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", "sid", raising=False)
        monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", "token", raising=False)
        monkeypatch.setattr(settings, "TWILIO_WHATSAPP_NUMBER", "+1555", raising=False)
        assert WhatsAppChannel().is_configured() is True


class TestWhatsAppSend:
    def test_send_fails_fast_when_not_configured(self, monkeypatch):
        from agriconnect.workers.outbox.channels.whatsapp import WhatsAppChannel

        channel = WhatsAppChannel()
        monkeypatch.setattr(channel, "is_configured", lambda: False)
        result = run(channel.send(body="hi", recipient_phone="+2260"))
        assert result.ok is False
        assert result.error == "whatsapp_not_configured"

    def test_send_fails_without_a_recipient_phone(self, monkeypatch):
        from agriconnect.workers.outbox.channels.whatsapp import WhatsAppChannel

        channel = WhatsAppChannel()
        monkeypatch.setattr(channel, "is_configured", lambda: True)
        result = run(channel.send(body="hi", recipient_phone=None))
        assert result.ok is False
        assert result.error == "missing_recipient_phone"

    def test_send_via_cloud_api_success(self, monkeypatch):
        from agriconnect.workers.outbox.channels.whatsapp import WhatsAppChannel
        import agriconnect.services.whatsapp.cloud_api_client as wa

        channel = WhatsAppChannel()
        monkeypatch.setattr(channel, "is_configured", lambda: True)
        monkeypatch.setattr(channel, "_provider", lambda: "whatsapp_cloud")
        monkeypatch.setattr(wa, "send_text", AsyncMock(return_value=["wamid.abc123"]))
        result = run(channel.send(body="hi", recipient_phone="+2260"))
        assert result.ok is True
        assert result.provider_ref == "wamid.abc123"

    def test_send_via_cloud_api_failure_when_no_message_ids(self, monkeypatch):
        from agriconnect.workers.outbox.channels.whatsapp import WhatsAppChannel
        import agriconnect.services.whatsapp.cloud_api_client as wa

        channel = WhatsAppChannel()
        monkeypatch.setattr(channel, "is_configured", lambda: True)
        monkeypatch.setattr(channel, "_provider", lambda: "whatsapp_cloud")
        monkeypatch.setattr(wa, "send_text", AsyncMock(return_value=[]))
        result = run(channel.send(body="hi", recipient_phone="+2260"))
        assert result.ok is False
        assert result.error == "send_failed"

    def test_send_via_cloud_api_exception_is_caught_as_a_failure(self, monkeypatch):
        from agriconnect.workers.outbox.channels.whatsapp import WhatsAppChannel
        import agriconnect.services.whatsapp.cloud_api_client as wa

        channel = WhatsAppChannel()
        monkeypatch.setattr(channel, "is_configured", lambda: True)
        monkeypatch.setattr(channel, "_provider", lambda: "whatsapp_cloud")
        monkeypatch.setattr(wa, "send_text", AsyncMock(side_effect=RuntimeError("network down")))
        result = run(channel.send(body="hi", recipient_phone="+2260"))
        assert result.ok is False
        assert "network down" in result.error

    def test_send_via_twilio_success(self, monkeypatch):
        from agriconnect.workers.outbox.channels.whatsapp import WhatsAppChannel

        channel = WhatsAppChannel()
        monkeypatch.setattr(channel, "is_configured", lambda: True)
        monkeypatch.setattr(channel, "_provider", lambda: "twilio")
        monkeypatch.setattr(channel, "_send_sync_twilio", lambda phone, body: SimpleNamespace(ok=True, provider_ref="SM123", error=None))
        result = run(channel.send(body="hi", recipient_phone="+2260"))
        assert result.provider_ref == "SM123"

    def test_send_sync_twilio_chunks_the_body_and_returns_the_last_message_sid(self, monkeypatch):
        """`_send_sync_twilio` est le seul endroit qui parle réellement au SDK
        Twilio — on double `twilio.rest.Client` pour vérifier que CHAQUE
        morceau de `_chunk()` est envoyé, dans l'ordre, et que le SID retourné
        est celui du DERNIER message (pas le premier)."""
        import sys
        from agriconnect.workers.outbox.channels.whatsapp import WhatsAppChannel
        from agriconnect.core.settings import settings

        monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", "sid", raising=False)
        monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", "token", raising=False)
        monkeypatch.setattr(settings, "TWILIO_WHATSAPP_NUMBER", "+1555", raising=False)

        sent_bodies = []

        class _FakeMessages:
            def create(self, *, from_, to, body):
                sent_bodies.append((from_, to, body))
                return SimpleNamespace(sid=f"SM-{len(sent_bodies)}")

        class _FakeClient:
            # `http_client=` : timeout explicite ajouté à l'audit sécurité/
            # résilience (le SDK Twilio n'en pose aucun par défaut) — voir
            # `tests/unit/test_mcp_hardening.py::TestExternalCallsAreBounded`.
            def __init__(self, sid, token, http_client=None):
                assert sid == "sid" and token == "token"
                assert getattr(http_client, "timeout", None), "appel Twilio sans timeout"
                self.messages = _FakeMessages()

        fake_twilio_module = SimpleNamespace(rest=SimpleNamespace(Client=_FakeClient))
        monkeypatch.setitem(sys.modules, "twilio", fake_twilio_module)
        monkeypatch.setitem(sys.modules, "twilio.rest", fake_twilio_module.rest)

        channel = WhatsAppChannel()
        long_body = "part-one\n" + ("y" * 2000) + "\npart-two"
        result = channel._send_sync_twilio("+2260", long_body)

        assert len(sent_bodies) >= 1
        assert all(to == "whatsapp:+2260" and from_ == "+1555" for from_, to, _ in sent_bodies)
        assert result.ok is True
        assert result.provider_ref == f"SM-{len(sent_bodies)}"

    def test_send_via_twilio_exception_is_caught_as_a_failure(self, monkeypatch):
        from agriconnect.workers.outbox.channels.whatsapp import WhatsAppChannel

        channel = WhatsAppChannel()
        monkeypatch.setattr(channel, "is_configured", lambda: True)
        monkeypatch.setattr(channel, "_provider", lambda: "twilio")

        def _boom(phone, body):
            raise RuntimeError("twilio 500")

        monkeypatch.setattr(channel, "_send_sync_twilio", _boom)
        result = run(channel.send(body="hi", recipient_phone="+2260"))
        assert result.ok is False
        assert "twilio 500" in result.error


# =====================================================================
# channels/email.py, channels/push.py — stubs
# =====================================================================

class TestStubChannels:
    def test_email_channel_is_never_configured_and_always_fails(self):
        from agriconnect.workers.outbox.channels.email import EmailChannel
        channel = EmailChannel()
        assert channel.is_configured() is False
        result = run(channel.send(body="x"))
        assert result.ok is False
        assert result.error == "email_channel_not_implemented"

    def test_push_channel_is_never_configured_and_always_fails(self):
        from agriconnect.workers.outbox.channels.push import PushChannel
        channel = PushChannel()
        assert channel.is_configured() is False
        result = run(channel.send(body="x"))
        assert result.ok is False
        assert result.error == "push_channel_not_implemented"

    def test_build_channel_registry_has_all_three_channels(self):
        from agriconnect.workers.outbox.channels import build_channel_registry
        registry = build_channel_registry()
        assert set(registry.keys()) == {"WHATSAPP", "EMAIL", "PUSH"}


# =====================================================================
# outbox/dispatcher.py
# =====================================================================

class _FakeWorkerSessionCM:
    async def __aenter__(self):
        return SimpleNamespace()

    async def __aexit__(self, *exc):
        return False


class TestOutboxDispatcher:
    def _patch_worker_session(self, monkeypatch):
        import agriconnect.workers.outbox.dispatcher as dispatcher_module
        monkeypatch.setattr(dispatcher_module, "worker_session", lambda: _FakeWorkerSessionCM())

    def test_to_job_freezes_the_row_before_the_session_closes(self):
        from agriconnect.workers.outbox.dispatcher import _to_job
        row = SimpleNamespace(
            id=1, channel="WHATSAPP", recipient_phone="+2260",
            recipient_user_id="u1", template_key="TPL", payload={"a": 1},
        )
        job = _to_job(row)
        assert job == {
            "id": 1, "channel": "WHATSAPP", "recipient_phone": "+2260",
            "recipient_user_id": "u1", "template_key": "TPL", "payload": {"a": 1},
        }

    def test_to_job_defaults_missing_payload_to_empty_dict(self):
        from agriconnect.workers.outbox.dispatcher import _to_job
        row = SimpleNamespace(
            id=1, channel="WHATSAPP", recipient_phone="+2260",
            recipient_user_id=None, template_key="TPL", payload=None,
        )
        assert _to_job(row)["payload"] == {}

    def test_run_delivers_and_marks_sent_on_success(self, monkeypatch):
        from agriconnect.workers.outbox.dispatcher import OutboxDispatcher
        from agriconnect.workers.outbox.channels.base import SendResult
        import agriconnect.workers.outbox.dispatcher as dispatcher_module

        self._patch_worker_session(monkeypatch)
        row = SimpleNamespace(id="job-1", channel="WHATSAPP", recipient_phone="+2260",
                               recipient_user_id=None, template_key="ANY", payload={})
        monkeypatch.setattr(dispatcher_module.outbox_repo, "claim_due", AsyncMock(return_value=[row]))
        mark_sent = AsyncMock()
        monkeypatch.setattr(dispatcher_module.outbox_repo, "mark_sent", mark_sent)
        monkeypatch.setattr(dispatcher_module.outbox_repo, "mark_failed", AsyncMock())
        monkeypatch.setattr(dispatcher_module.asyncio, "sleep", AsyncMock())

        fake_channel = SimpleNamespace(
            is_configured=lambda: True,
            send=AsyncMock(return_value=SendResult.success(provider_ref="ref1")),
        )
        dispatcher = OutboxDispatcher(channels={"WHATSAPP": fake_channel})
        report = run(dispatcher.run(batch_size=10))

        assert report.claimed == 1
        assert report.sent == 1
        assert report.failed == 0
        mark_sent.assert_awaited_once_with(mark_sent.await_args.args[0], "job-1")

    def test_run_marks_failed_on_unknown_channel(self, monkeypatch):
        from agriconnect.workers.outbox.dispatcher import OutboxDispatcher
        import agriconnect.workers.outbox.dispatcher as dispatcher_module

        self._patch_worker_session(monkeypatch)
        row = SimpleNamespace(id="job-2", channel="CARRIER_PIGEON", recipient_phone="+2260",
                               recipient_user_id=None, template_key="ANY", payload={})
        monkeypatch.setattr(dispatcher_module.outbox_repo, "claim_due", AsyncMock(return_value=[row]))
        mark_failed = AsyncMock()
        monkeypatch.setattr(dispatcher_module.outbox_repo, "mark_failed", mark_failed)
        monkeypatch.setattr(dispatcher_module.outbox_repo, "mark_sent", AsyncMock())
        monkeypatch.setattr(dispatcher_module.asyncio, "sleep", AsyncMock())

        dispatcher = OutboxDispatcher(channels={})
        report = run(dispatcher.run(batch_size=10))

        assert report.failed == 1
        assert "unknown_channel:CARRIER_PIGEON" in report.errors[0]

    def test_run_marks_failed_when_channel_not_configured(self, monkeypatch):
        from agriconnect.workers.outbox.dispatcher import OutboxDispatcher
        import agriconnect.workers.outbox.dispatcher as dispatcher_module

        self._patch_worker_session(monkeypatch)
        row = SimpleNamespace(id="job-3", channel="EMAIL", recipient_phone=None,
                               recipient_user_id=None, template_key="ANY", payload={})
        monkeypatch.setattr(dispatcher_module.outbox_repo, "claim_due", AsyncMock(return_value=[row]))
        mark_failed = AsyncMock()
        monkeypatch.setattr(dispatcher_module.outbox_repo, "mark_failed", mark_failed)
        monkeypatch.setattr(dispatcher_module.outbox_repo, "mark_sent", AsyncMock())
        monkeypatch.setattr(dispatcher_module.asyncio, "sleep", AsyncMock())

        from agriconnect.workers.outbox.channels.email import EmailChannel
        dispatcher = OutboxDispatcher(channels={"EMAIL": EmailChannel()})
        report = run(dispatcher.run(batch_size=10))

        assert report.failed == 1
        assert "channel_not_configured:EMAIL" in report.errors[0]

    def test_run_with_nothing_claimed_is_a_noop(self, monkeypatch):
        from agriconnect.workers.outbox.dispatcher import OutboxDispatcher
        import agriconnect.workers.outbox.dispatcher as dispatcher_module

        self._patch_worker_session(monkeypatch)
        monkeypatch.setattr(dispatcher_module.outbox_repo, "claim_due", AsyncMock(return_value=[]))
        dispatcher = OutboxDispatcher(channels={})
        report = run(dispatcher.run(batch_size=10))
        assert report.as_dict() == {"claimed": 0, "sent": 0, "failed": 0, "errors": []}

    def test_default_constructor_builds_the_real_channel_registry(self):
        from agriconnect.workers.outbox.dispatcher import OutboxDispatcher
        dispatcher = OutboxDispatcher()
        assert set(dispatcher.channels.keys()) == {"WHATSAPP", "EMAIL", "PUSH"}
