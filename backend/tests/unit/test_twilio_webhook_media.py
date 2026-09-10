"""Extraction du média entrant Twilio (`api/routes/twilio_webhook.py::_extract_media`)
et non-régression du chemin texte standard (feature "photo produit par WhatsApp").

NB : ce module importe `twilio_webhook.py`, qui importe `api/tasks.py`
(`from celery.signals import ...`) — comme le reste de la suite
`tests/unit/test_workers_crons_and_payments.py` / `tests/integration/`, il ne
peut pas se collecter dans un environnement sans le paquet `celery` installé
(pré-existant, sans rapport avec cette feature — voir la mémoire projet
"test-suite-ladini").
"""
from __future__ import annotations

import pytest

pytest.importorskip("celery", reason="webhook module imports api.tasks -> celery.signals")

from ladini.api.routes.twilio_webhook import _extract_media


class TestExtractMedia:
    def test_a_text_only_message_extracts_nothing(self):
        form = {"Body": "Bonjour", "From": "whatsapp:+22670000001"}
        assert _extract_media(form) is None

    def test_num_media_absent_extracts_nothing_even_with_a_stray_media_url(self):
        # Défense en profondeur : NumMedia doit être positif, pas seulement
        # MediaUrl0 présent.
        form = {"MediaUrl0": "https://api.twilio.com/media/x", "MediaContentType0": "image/jpeg"}
        assert _extract_media(form) is None

    def test_a_real_twilio_image_payload_is_extracted(self):
        # Régression : le VRAI champ Twilio est `MediaContentType0`, pas
        # `MimeType0` (convention Meta Cloud API confondue en prod — une
        # photo envoyée par un producteur tombait silencieusement dans le
        # pipeline texte / fallback générique au lieu du pipeline photo).
        form = {
            "NumMedia": "1",
            "MediaUrl0": "https://api.twilio.com/2010-04-01/Accounts/x/Messages/y/Media/z",
            "MediaContentType0": "image/jpeg",
        }
        result = _extract_media(form)
        assert result == ("https://api.twilio.com/2010-04-01/Accounts/x/Messages/y/Media/z", "image/jpeg")

    def test_mime_type0_is_accepted_as_a_defensive_fallback(self):
        form = {"NumMedia": "1", "MediaUrl0": "https://x/a.jpg", "MimeType0": "image/jpeg"}
        result = _extract_media(form)
        assert result == ("https://x/a.jpg", "image/jpeg")

    def test_media_content_type0_wins_over_mime_type0_when_both_present(self):
        form = {
            "NumMedia": "1",
            "MediaUrl0": "https://x/a.jpg",
            "MediaContentType0": "image/png",
            "MimeType0": "image/jpeg",
        }
        result = _extract_media(form)
        assert result[1] == "image/png"

    def test_a_non_image_media_is_not_treated_as_a_photo(self):
        # Audio (note vocale) — déjà géré par un autre chemin (transcription),
        # ne doit jamais partir vers le pipeline photo produit.
        form = {"NumMedia": "1", "MediaUrl0": "https://x/audio.ogg", "MediaContentType0": "audio/ogg"}
        assert _extract_media(form) is None

    def test_content_type_matching_is_case_insensitive(self):
        form = {"NumMedia": "1", "MediaUrl0": "https://x/a.jpg", "MediaContentType0": "IMAGE/JPEG"}
        result = _extract_media(form)
        assert result is not None
        assert result[1] == "image/jpeg"

    def test_num_media_zero_extracts_nothing(self):
        form = {"NumMedia": "0", "MediaUrl0": "https://x/a.jpg", "MediaContentType0": "image/jpeg"}
        assert _extract_media(form) is None


from ladini.api.routes.twilio_webhook import _extract_view_photos_query


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
