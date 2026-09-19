"""Webchat + photo produit — incident 2026-09-19 : une photo envoyée depuis le
chat du site n'avait aucun chemin (pas de champ média) et retombait sur le
LLM (« je ne peux pas voir les photos »). Voir `core/reply_sink.py`."""
from __future__ import annotations

import base64
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from tests.conftest import run

from ladini.api.routes import webchat
from ladini.api.routes.webchat import WebChatRequest, _run
from ladini.core.reply_sink import active_sink, collect_replies

_PNG = base64.b64encode(b"\x89PNG-fake").decode()
_PHOTO = "ladini.workers.media.product_photo_task"


class TestRequestContract:
    def test_a_photo_alone_is_valid_without_text(self):
        req = WebChatRequest(phone_number="+226700", image_base64=_PNG, image_mime="image/png")
        assert req.message == ""

    def test_empty_request_is_rejected(self):
        with pytest.raises(ValidationError):
            WebChatRequest(phone_number="+226700")

    def test_image_without_mime_is_rejected(self):
        with pytest.raises(ValidationError):
            WebChatRequest(phone_number="+226700", image_base64=_PNG)


class TestReplySink:
    def test_send_confirmation_text_is_collected_not_sent_on_whatsapp(self, monkeypatch):
        from ladini.api import tasks

        dispatcher = AsyncMock()
        monkeypatch.setattr(tasks, "get_dispatcher", lambda: dispatcher)
        with collect_replies() as replies:
            out = run(tasks.send_confirmation_text("+226700", "✅ Photo ajoutée"))
        assert replies == ["✅ Photo ajoutée"]
        assert out["status"] == "collected"
        dispatcher.dispatch.assert_not_called()
        assert active_sink() is None  # jamais de fuite hors du bloc


class TestWebchatPhotoRouting:
    def test_photo_goes_to_the_photo_pipeline_never_to_the_llm(self, monkeypatch):
        seen = {}

        async def _fake_photo(phone, binary, content_type):
            seen.update(phone=phone, binary=binary, ct=content_type)
            from ladini.api.tasks import send_confirmation_text
            await send_confirmation_text(phone, "✅ Photo ajoutée à *poivrons*.")

        monkeypatch.setattr(f"{_PHOTO}.handle_inbound_photo_bytes", _fake_photo)
        orchestrator = AsyncMock()
        monkeypatch.setattr(webchat, "_orchestrator", orchestrator)

        resp = run(_run(
            WebChatRequest(phone_number="+226700", image_base64=_PNG, image_mime="image/png"),
            "producer",
        ))

        assert resp.reply == "✅ Photo ajoutée à *poivrons*."
        assert seen["binary"] == b"\x89PNG-fake" and seen["ct"] == "image/png"
        orchestrator.handle.assert_not_called()

    def test_invalid_base64_is_a_422_not_a_crash(self):
        with pytest.raises(HTTPException) as exc:
            run(_run(
                WebChatRequest(phone_number="+226700", image_base64="@@@", image_mime="image/png"),
                "producer",
            ))
        assert exc.value.status_code == 422

    def test_digit_reply_resolves_the_pending_product_menu(self, monkeypatch):
        async def _fake_resolve(phone, text):
            from ladini.api.tasks import send_confirmation_text
            await send_confirmation_text(phone, f"✅ 1 photo ajoutée (choix {text})")

        monkeypatch.setattr(f"{_PHOTO}.has_pending_photo_selection", lambda p: True)
        monkeypatch.setattr(f"{_PHOTO}.handle_pending_photo_selection", _fake_resolve)
        orchestrator = AsyncMock()
        monkeypatch.setattr(webchat, "_orchestrator", orchestrator)

        resp = run(_run(WebChatRequest(phone_number="+226700", message="2"), "producer"))

        assert "choix 2" in resp.reply
        orchestrator.handle.assert_not_called()

    def test_plain_text_still_reaches_the_agent(self, monkeypatch):
        monkeypatch.setattr(f"{_PHOTO}.has_pending_photo_selection", lambda p: False)
        orchestrator = AsyncMock()
        orchestrator.handle.return_value = {"final_response": "Bonjour", "workspace_id": "w"}
        monkeypatch.setattr(webchat, "_orchestrator", orchestrator)

        resp = run(_run(WebChatRequest(phone_number="+226700", message="bonjour"), "producer"))

        assert resp.reply == "Bonjour"
        orchestrator.handle.assert_awaited_once()
