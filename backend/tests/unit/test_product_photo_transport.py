"""`services/storage/supabase_storage.py` + `services/whatsapp/twilio_media.py`
— validations locales (pas d'appel réseau réel) de la feature "photo produit
par WhatsApp"."""
from __future__ import annotations

import pytest

from tests.conftest import run


class TestSupabaseStorageConfig:
    def test_is_configured_false_when_url_or_key_missing(self, monkeypatch):
        from agriconnect.services.storage import supabase_storage as mod
        monkeypatch.setattr(mod.settings, "SUPABASE_URL", "")
        monkeypatch.setattr(mod.settings, "SUPABASE_SERVICE_ROLE_KEY", "key")
        assert mod.is_configured() is False

    def test_is_configured_true_when_both_present(self, monkeypatch):
        from agriconnect.services.storage import supabase_storage as mod
        monkeypatch.setattr(mod.settings, "SUPABASE_URL", "https://x.supabase.co")
        monkeypatch.setattr(mod.settings, "SUPABASE_SERVICE_ROLE_KEY", "key")
        assert mod.is_configured() is True

    def test_upload_raises_a_safe_message_when_not_configured(self, monkeypatch):
        from agriconnect.services.storage import supabase_storage as mod
        monkeypatch.setattr(mod.settings, "SUPABASE_URL", "")
        monkeypatch.setattr(mod.settings, "SUPABASE_SERVICE_ROLE_KEY", "")
        with pytest.raises(mod.SupabaseStorageError):
            run(mod.upload_product_photo(b"binary", "image/jpeg", "+22670000001"))

    def test_upload_rejects_unsupported_content_type(self, monkeypatch):
        from agriconnect.services.storage import supabase_storage as mod
        monkeypatch.setattr(mod.settings, "SUPABASE_URL", "https://x.supabase.co")
        monkeypatch.setattr(mod.settings, "SUPABASE_SERVICE_ROLE_KEY", "key")
        with pytest.raises(mod.SupabaseStorageError):
            run(mod.upload_product_photo(b"binary", "application/pdf", "+22670000001"))

    def test_upload_rejects_oversized_binary(self, monkeypatch):
        from agriconnect.services.storage import supabase_storage as mod
        monkeypatch.setattr(mod.settings, "SUPABASE_URL", "https://x.supabase.co")
        monkeypatch.setattr(mod.settings, "SUPABASE_SERVICE_ROLE_KEY", "key")
        oversized = b"x" * (mod._MAX_BYTES + 1)
        with pytest.raises(mod.SupabaseStorageError):
            run(mod.upload_product_photo(oversized, "image/jpeg", "+22670000001"))

    def test_object_path_is_namespaced_by_phone_and_extension(self):
        from agriconnect.services.storage import supabase_storage as mod
        path = mod._object_path("+226 70 00 00 01", "image/png")
        assert path.startswith("22670000001/")
        assert path.endswith(".png")


class TestTwilioMediaDownloadConfig:
    def test_download_raises_when_twilio_credentials_missing(self, monkeypatch):
        from agriconnect.services.whatsapp import twilio_media as mod
        monkeypatch.setattr(mod.settings, "TWILIO_ACCOUNT_SID", "")
        monkeypatch.setattr(mod.settings, "TWILIO_AUTH_TOKEN", "")
        with pytest.raises(mod.TwilioMediaError):
            run(mod.download_twilio_media("https://api.twilio.com/media/x"))

    def test_download_raises_on_missing_url(self, monkeypatch):
        from agriconnect.services.whatsapp import twilio_media as mod
        monkeypatch.setattr(mod.settings, "TWILIO_ACCOUNT_SID", "sid")
        monkeypatch.setattr(mod.settings, "TWILIO_AUTH_TOKEN", "token")
        with pytest.raises(mod.TwilioMediaError):
            run(mod.download_twilio_media(""))

    def test_the_http_client_follows_redirects(self, monkeypatch):
        """Régression : l'URL de média Twilio répond par un 307 vers son
        emplacement réel — sans `follow_redirects=True`, httpx ne le suit pas
        et toute photo échoue en prod avec 'Échec de récupération...'."""
        from agriconnect.services.whatsapp import twilio_media as mod

        monkeypatch.setattr(mod.settings, "TWILIO_ACCOUNT_SID", "sid")
        monkeypatch.setattr(mod.settings, "TWILIO_AUTH_TOKEN", "token")

        captured_kwargs = {}

        class _FakeResponse:
            status_code = 200
            content = b"binary"
            headers = {"Content-Type": "image/jpeg"}

        class _FakeAsyncClient:
            def __init__(self, **kwargs):
                captured_kwargs.update(kwargs)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url):
                return _FakeResponse()

        monkeypatch.setattr(mod.httpx, "AsyncClient", _FakeAsyncClient)

        binary, content_type = run(mod.download_twilio_media("https://api.twilio.com/media/x"))

        assert captured_kwargs.get("follow_redirects") is True
        assert binary == b"binary"
        assert content_type == "image/jpeg"
