"""Extraction du média entrant WhatsApp Cloud API
(`api/routes/whatsapp_webhook.py::_extract_media_id`) — feature "photo
produit par WhatsApp", portée du webhook Twilio vers le webhook Cloud API
(2026-09-19, incident réel : `MESSAGING_PROVIDER=whatsapp_cloud` est le
défaut en production, et ce webhook n'avait AUCUNE gestion de média —
chaque photo envoyée tombait dans le pipeline texte vide, produisant la
réponse générique de clarification au lieu du pipeline photo produit).

NB : ce module importe `whatsapp_webhook.py`, qui importe `api/tasks.py`
(`from celery.signals import ...`) — même contrainte de collecte que
`test_twilio_webhook_media.py` (voir sa docstring)."""
from __future__ import annotations

import pytest

pytest.importorskip("celery", reason="webhook module imports api.tasks -> celery.signals")

from ladini.api.routes.whatsapp_webhook import (
    _extract_media_id,
    _extract_view_photos_query,
)


class TestExtractMediaId:
    def test_a_text_only_message_extracts_nothing(self):
        message = {"type": "text", "text": {"body": "Bonjour"}}
        assert _extract_media_id(message) is None

    def test_a_real_cloud_api_image_payload_is_extracted(self):
        message = {
            "type": "image",
            "image": {
                "id": "1234567890",
                "mime_type": "image/jpeg",
                "sha256": "abc",
            },
        }
        result = _extract_media_id(message)
        assert result == ("1234567890", "image/jpeg")

    def test_image_type_without_an_image_payload_extracts_nothing(self):
        # Défensif : le type annonce "image" mais le bloc est absent/mal formé —
        # ne doit jamais lever, juste ne rien extraire.
        message = {"type": "image"}
        assert _extract_media_id(message) is None

    def test_an_image_payload_without_an_id_extracts_nothing(self):
        message = {"type": "image", "image": {"mime_type": "image/jpeg"}}
        assert _extract_media_id(message) is None

    def test_a_non_image_type_is_not_treated_as_a_photo(self):
        # Audio (note vocale) — déjà géré par un autre chemin, ne doit jamais
        # partir vers le pipeline photo produit.
        message = {"type": "audio", "audio": {"id": "xyz", "mime_type": "audio/ogg"}}
        assert _extract_media_id(message) is None

    def test_missing_mime_type_still_extracts_the_id(self):
        message = {"type": "image", "image": {"id": "1234567890"}}
        result = _extract_media_id(message)
        assert result == ("1234567890", "")


class TestExtractViewPhotosQuery:
    def test_photos_prefix_extracts_the_product_name(self):
        assert _extract_view_photos_query("photos maïs") == "maïs"

    def test_singular_photo_prefix_is_also_accepted(self):
        assert _extract_view_photos_query("photo tomates") == "tomates"

    def test_is_case_insensitive_on_the_prefix(self):
        assert _extract_view_photos_query("PHOTOS maïs") == "maïs"

    def test_a_bare_prefix_with_no_product_name_extracts_nothing(self):
        assert _extract_view_photos_query("photos") is None
        assert _extract_view_photos_query("photos   ") is None

    def test_unrelated_text_extracts_nothing(self):
        assert _extract_view_photos_query("je veux vendre des tomates") is None
        assert _extract_view_photos_query("Bonjour") is None
