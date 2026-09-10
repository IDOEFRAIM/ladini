"""Consultation acheteur « photos <numéro> » — retrouve un produit d'une
recherche via le cache `services/search_results_cache.py`, distinct du
catalogue producteur (`photos <nom>`, voir test_product_photo_view.py).

NB : `product_photo_task.py` importe `api.celery_app` (`from celery import
Celery`) au niveau module — ce fichier ne peut donc pas se collecter sans le
paquet `celery` installé (pré-existant, sans rapport avec cette feature).
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from tests.conftest import run

pytest.importorskip("celery", reason="product_photo_task imports api.celery_app -> celery.Celery")

PHONE = "+22670000001"


class TestSendSearchResultPhotos:
    def test_a_cached_entry_with_photos_sends_them(self, monkeypatch):
        import ladini.workers.media.product_photo_task as mod

        monkeypatch.setattr(
            "ladini.services.search_results_cache.load_results",
            lambda phone: {"1": {"id": "p1", "name": "maïs", "images": ["https://x/a.jpg"]}},
        )
        sent_media = []
        monkeypatch.setattr(
            "ladini.services.twilio_sender.send_whatsapp_media",
            lambda phone, url, caption="": sent_media.append((phone, url, caption)) or "SM1",
        )
        sent = AsyncMock()
        monkeypatch.setattr("ladini.api.tasks.send_confirmation_text", sent)

        run(mod._send_search_result_photos(PHONE, "1"))

        assert sent_media == [(PHONE, "https://x/a.jpg", "📸 maïs")]

    def test_viewing_a_photo_reminds_the_buyer_the_selection_number_still_works(self, monkeypatch):
        """Rupture prévenue : consulter une photo ne doit jamais faire perdre
        le fil d'une commande en cours (constaté en usage réel — "on perd le
        fil"). Photo + rappel partent désormais dans UN SEUL ResponsePlan
        multipart (voir docstring de `_send_search_result_photos`)."""
        import ladini.workers.media.product_photo_task as mod

        monkeypatch.setattr(
            "ladini.services.search_results_cache.load_results",
            lambda phone: {"2": {"id": "p1", "name": "maïs", "images": ["https://x/a.jpg"]}},
        )
        monkeypatch.setattr(
            "ladini.services.twilio_sender.send_whatsapp_media",
            lambda phone, url, caption="": "SM1",
        )
        captured = {}

        class _CapturingDispatcher:
            async def dispatch(self, phone_number, plan):
                captured["plan"] = plan
                return []

        monkeypatch.setattr(
            "ladini.api.response_dispatch.get_dispatcher",
            lambda: _CapturingDispatcher(),
        )

        run(mod._send_search_result_photos(PHONE, "2"))

        from ladini.api.response_dispatch import ImageResponse, TextResponse

        plan = captured["plan"]
        assert isinstance(plan.items[0], ImageResponse)
        reminder = plan.items[-1]
        assert isinstance(reminder, TextResponse)
        assert "*2*" in reminder.text
        assert "continuer" in reminder.text

    def test_an_unknown_index_reports_invalid(self, monkeypatch):
        import ladini.workers.media.product_photo_task as mod

        monkeypatch.setattr(
            "ladini.services.search_results_cache.load_results",
            lambda phone: {"1": {"id": "p1", "name": "maïs", "images": []}},
        )
        sent = AsyncMock()
        monkeypatch.setattr("ladini.api.tasks.send_confirmation_text", sent)

        run(mod._send_search_result_photos(PHONE, "9"))

        assert "Numéro invalide" in sent.await_args.args[1]

    def test_no_cached_search_reports_expiry(self, monkeypatch):
        import ladini.workers.media.product_photo_task as mod

        monkeypatch.setattr(
            "ladini.services.search_results_cache.load_results", lambda phone: None,
        )
        sent = AsyncMock()
        monkeypatch.setattr("ladini.api.tasks.send_confirmation_text", sent)

        run(mod._send_search_result_photos(PHONE, "1"))

        assert "recherche" in sent.await_args.args[1]

    def test_a_result_with_no_photo_says_so(self, monkeypatch):
        import ladini.workers.media.product_photo_task as mod

        monkeypatch.setattr(
            "ladini.services.search_results_cache.load_results",
            lambda phone: {"1": {"id": "p1", "name": "tomates", "images": []}},
        )
        captured = {}

        class _CapturingDispatcher:
            async def dispatch(self, phone_number, plan):
                captured["plan"] = plan
                return []

        monkeypatch.setattr(
            "ladini.api.response_dispatch.get_dispatcher",
            lambda: _CapturingDispatcher(),
        )

        run(mod._send_search_result_photos(PHONE, "1"))

        texts = [item.text for item in captured["plan"].items]
        assert any("Aucune photo" in t and "tomates" in t for t in texts)
