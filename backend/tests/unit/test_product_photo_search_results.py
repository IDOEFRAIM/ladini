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
        import agriconnect.workers.media.product_photo_task as mod

        monkeypatch.setattr(
            "agriconnect.services.search_results_cache.load_results",
            lambda phone: {"1": {"id": "p1", "name": "maïs", "images": ["https://x/a.jpg"]}},
        )
        sent_media = []
        monkeypatch.setattr(
            "agriconnect.services.twilio_sender.send_whatsapp_media",
            lambda phone, url, caption="": sent_media.append((phone, url, caption)) or "SM1",
        )
        sent = AsyncMock()
        monkeypatch.setattr("agriconnect.api.tasks.send_confirmation_text", sent)

        run(mod._send_search_result_photos(PHONE, "1"))

        assert sent_media == [(PHONE, "https://x/a.jpg", "📸 maïs")]

    def test_viewing_a_photo_reminds_the_buyer_the_selection_number_still_works(self, monkeypatch):
        """Rupture prévenue : consulter une photo ne doit jamais faire perdre
        le fil d'une commande en cours (constaté en usage réel — "on perd le
        fil")."""
        import agriconnect.workers.media.product_photo_task as mod

        monkeypatch.setattr(
            "agriconnect.services.search_results_cache.load_results",
            lambda phone: {"2": {"id": "p1", "name": "maïs", "images": ["https://x/a.jpg"]}},
        )
        monkeypatch.setattr(
            "agriconnect.services.twilio_sender.send_whatsapp_media",
            lambda phone, url, caption="": "SM1",
        )
        sent = AsyncMock()
        monkeypatch.setattr("agriconnect.api.tasks.send_confirmation_text", sent)

        run(mod._send_search_result_photos(PHONE, "2"))

        assert "*2*" in sent.await_args.args[1]
        assert "continuer" in sent.await_args.args[1]

    def test_an_unknown_index_reports_invalid(self, monkeypatch):
        import agriconnect.workers.media.product_photo_task as mod

        monkeypatch.setattr(
            "agriconnect.services.search_results_cache.load_results",
            lambda phone: {"1": {"id": "p1", "name": "maïs", "images": []}},
        )
        sent = AsyncMock()
        monkeypatch.setattr("agriconnect.api.tasks.send_confirmation_text", sent)

        run(mod._send_search_result_photos(PHONE, "9"))

        assert "Numéro invalide" in sent.await_args.args[1]

    def test_no_cached_search_reports_expiry(self, monkeypatch):
        import agriconnect.workers.media.product_photo_task as mod

        monkeypatch.setattr(
            "agriconnect.services.search_results_cache.load_results", lambda phone: None,
        )
        sent = AsyncMock()
        monkeypatch.setattr("agriconnect.api.tasks.send_confirmation_text", sent)

        run(mod._send_search_result_photos(PHONE, "1"))

        assert "recherche" in sent.await_args.args[1]

    def test_a_result_with_no_photo_says_so(self, monkeypatch):
        import agriconnect.workers.media.product_photo_task as mod

        monkeypatch.setattr(
            "agriconnect.services.search_results_cache.load_results",
            lambda phone: {"1": {"id": "p1", "name": "tomates", "images": []}},
        )
        sent = AsyncMock()
        monkeypatch.setattr("agriconnect.api.tasks.send_confirmation_text", sent)

        run(mod._send_search_result_photos(PHONE, "1"))

        texts = [call.args[1] for call in sent.await_args_list]
        assert any("Aucune photo" in t and "tomates" in t for t in texts)
