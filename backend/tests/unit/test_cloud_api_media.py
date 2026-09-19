"""`services/whatsapp/cloud_api_media.py` — validations locales (pas d'appel
réseau réel) du téléchargement de média WhatsApp Cloud API (feature "photo
produit par WhatsApp", portée depuis Twilio le 2026-09-19 — voir
test_whatsapp_webhook_media.py pour le contexte de l'incident)."""
from __future__ import annotations

import pytest

from tests.conftest import run


class TestCloudAPIMediaDownloadConfig:
    def test_download_raises_when_token_missing(self, monkeypatch):
        from ladini.services.whatsapp import cloud_api_media as mod

        monkeypatch.setattr(mod.settings, "WHATSAPP_CLOUD_API_TOKEN", "")
        with pytest.raises(mod.CloudAPIMediaError):
            run(mod.download_cloud_api_media("1234567890"))

    def test_download_raises_on_missing_media_id(self, monkeypatch):
        from ladini.services.whatsapp import cloud_api_media as mod

        monkeypatch.setattr(mod.settings, "WHATSAPP_CLOUD_API_TOKEN", "token")
        with pytest.raises(mod.CloudAPIMediaError):
            run(mod.download_cloud_api_media(""))


class _FakeAsyncClient:
    """Simule les DEUX requêtes séquentielles du flux Cloud API : la
    résolution (`graph.facebook.com/.../{media_id}`) puis le téléchargement
    de l'URL temporaire renvoyée — chacune sur sa PROPRE instance
    `httpx.AsyncClient` (voir l'implémentation, deux `async with` distincts)."""

    _instances = 0

    def __init__(self, **kwargs):
        self.captured_kwargs = kwargs
        _FakeAsyncClient._instances += 1
        self._call_index = _FakeAsyncClient._instances

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        if "graph.facebook.com" in url:
            return _JsonResponse(
                200, {"url": "https://lookaside.fbsbx.com/media/x", "mime_type": "image/jpeg"}
            )
        return _BinaryResponse(200, b"binary", "image/jpeg")


class _JsonResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _BinaryResponse:
    def __init__(self, status_code, content, content_type):
        self.status_code = status_code
        self.content = content
        self.headers = {"Content-Type": content_type}


class TestCloudAPIMediaDownloadHappyPath:
    def test_resolves_media_id_then_downloads_the_temporary_url(self, monkeypatch):
        from ladini.services.whatsapp import cloud_api_media as mod

        monkeypatch.setattr(mod.settings, "WHATSAPP_CLOUD_API_TOKEN", "token")
        monkeypatch.setattr(mod.settings, "WHATSAPP_GRAPH_API_VERSION", "v21.0")
        monkeypatch.setattr(mod.httpx, "AsyncClient", _FakeAsyncClient)

        binary, content_type = run(mod.download_cloud_api_media("1234567890"))

        assert binary == b"binary"
        assert content_type == "image/jpeg"

    def test_the_bearer_token_is_sent_on_both_requests(self, monkeypatch):
        """Contrairement à Twilio (Basic Auth sur la 1ère requête, retiré
        automatiquement par httpx au changement d'origine du redirect), Meta
        exige explicitement le MÊME Bearer token sur les DEUX requêtes."""
        from ladini.services.whatsapp import cloud_api_media as mod

        seen_headers = []

        class _CapturingClient(_FakeAsyncClient):
            async def get(self, url, headers=None):
                seen_headers.append(headers)
                return await super().get(url, headers=headers)

        monkeypatch.setattr(mod.settings, "WHATSAPP_CLOUD_API_TOKEN", "secret-token")
        monkeypatch.setattr(mod.httpx, "AsyncClient", _CapturingClient)

        run(mod.download_cloud_api_media("1234567890"))

        assert len(seen_headers) == 2
        assert all(h == {"Authorization": "Bearer secret-token"} for h in seen_headers)


class TestCloudAPIMediaSSRFGuard:
    def test_a_temporary_url_outside_meta_domains_is_rejected(self, monkeypatch):
        from ladini.services.whatsapp import cloud_api_media as mod

        class _MaliciousClient(_FakeAsyncClient):
            async def get(self, url, headers=None):
                if "graph.facebook.com" in url:
                    return _JsonResponse(
                        200, {"url": "https://169.254.169.254/latest/meta-data", "mime_type": "image/jpeg"}
                    )
                return _BinaryResponse(200, b"leak", "text/plain")

        monkeypatch.setattr(mod.settings, "WHATSAPP_CLOUD_API_TOKEN", "token")
        monkeypatch.setattr(mod.httpx, "AsyncClient", _MaliciousClient)

        with pytest.raises(mod.CloudAPIMediaError):
            run(mod.download_cloud_api_media("1234567890"))

    def test_a_non_https_temporary_url_is_rejected(self, monkeypatch):
        from ladini.services.whatsapp import cloud_api_media as mod

        class _HttpClient(_FakeAsyncClient):
            async def get(self, url, headers=None):
                if "graph.facebook.com" in url:
                    return _JsonResponse(
                        200, {"url": "http://lookaside.fbsbx.com/media/x", "mime_type": "image/jpeg"}
                    )
                return _BinaryResponse(200, b"binary", "image/jpeg")

        monkeypatch.setattr(mod.settings, "WHATSAPP_CLOUD_API_TOKEN", "token")
        monkeypatch.setattr(mod.httpx, "AsyncClient", _HttpClient)

        with pytest.raises(mod.CloudAPIMediaError):
            run(mod.download_cloud_api_media("1234567890"))


class TestCloudAPIMediaOversizedRejected:
    def test_download_rejects_oversized_binary(self, monkeypatch):
        from ladini.services.whatsapp import cloud_api_media as mod

        class _OversizedClient(_FakeAsyncClient):
            async def get(self, url, headers=None):
                if "graph.facebook.com" in url:
                    return _JsonResponse(
                        200, {"url": "https://lookaside.fbsbx.com/media/x", "mime_type": "image/jpeg"}
                    )
                return _BinaryResponse(200, b"x" * (mod._MAX_BYTES + 1), "image/jpeg")

        monkeypatch.setattr(mod.settings, "WHATSAPP_CLOUD_API_TOKEN", "token")
        monkeypatch.setattr(mod.httpx, "AsyncClient", _OversizedClient)

        with pytest.raises(mod.CloudAPIMediaError):
            run(mod.download_cloud_api_media("1234567890"))
