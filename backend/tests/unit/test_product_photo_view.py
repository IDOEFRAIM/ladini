"""Consultation des photos produit par WhatsApp (« photos <nom> ») :
`services/twilio_sender.py::send_whatsapp_media` +
`workers/media/product_photo_task.py::_find_product_by_name`.

NB : `product_photo_task.py` importe `api.celery_app` (`from celery import
Celery`) au niveau module — comme `test_twilio_webhook_media.py`, la classe
`TestFindProductByName` ne peut donc pas se collecter sans le paquet `celery`
installé (pré-existant, sans rapport avec cette feature). `send_whatsapp_media`
(dans `services/twilio_sender.py`) n'a AUCUNE dépendance Celery et reste
testable dans tous les environnements.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from tests.conftest import run


class TestSendWhatsappMedia:
    def test_passes_the_media_url_and_an_explicit_timeout(self, monkeypatch):
        import ladini.services.twilio_sender as ts

        seen = {}

        class _FakeClient:
            def __init__(self, sid, token, http_client=None):
                seen["timeout"] = getattr(http_client, "timeout", None)

                def _create(**kw):
                    seen["kwargs"] = kw
                    return type("R", (), {"sid": "SM1"})()

                self.messages = type("M", (), {"create": staticmethod(_create)})()

        monkeypatch.setattr(ts, "Client", _FakeClient)
        sid = ts.send_whatsapp_media("+22668815299", "https://x/a.jpg", "légende")

        assert sid == "SM1"
        assert seen["kwargs"]["media_url"] == ["https://x/a.jpg"]
        assert seen["kwargs"]["body"] == "légende"
        assert seen["timeout"] == ts._TIMEOUT_S

    def test_returns_none_without_a_media_url(self, monkeypatch):
        import ladini.services.twilio_sender as ts
        assert ts.send_whatsapp_media("+22668815299", "", "légende") is None

    def test_returns_none_and_does_not_raise_on_twilio_failure(self, monkeypatch):
        import ladini.services.twilio_sender as ts

        class _FailingClient:
            def __init__(self, sid, token, http_client=None):
                def _create(**kw):
                    raise RuntimeError("Twilio down")
                self.messages = type("M", (), {"create": staticmethod(_create)})()

        monkeypatch.setattr(ts, "Client", _FailingClient)
        assert ts.send_whatsapp_media("+22668815299", "https://x/a.jpg") is None

    def test_masks_the_recipient_number_in_logs(self, monkeypatch, caplog):
        import logging

        import ladini.services.twilio_sender as ts

        class _FakeClient:
            def __init__(self, sid, token, http_client=None):
                self.messages = type(
                    "M", (), {"create": staticmethod(lambda **kw: type("R", (), {"sid": "SM1"})())},
                )()

        monkeypatch.setattr(ts, "Client", _FakeClient)
        with caplog.at_level(logging.INFO, logger="Ladini.TwilioSender"):
            ts.send_whatsapp_media("+22668815299", "https://x/a.jpg")
        assert "+22668815299" not in caplog.text
        assert "***5299" in caplog.text


class TestFindProductByName:
    @pytest.fixture(autouse=True)
    def _require_celery(self):
        pytest.importorskip("celery", reason="product_photo_task imports api.celery_app -> celery.Celery")

    def _patch_products(self, monkeypatch, products):
        from ladini.services.database.d import AgriDatabaseService
        # `AgriDatabaseService.__getattribute__` (d.py) mémorise le wrapper
        # @transactional dans un cache DE CLASSE (`_DISPATCH_CACHE`, clé
        # "nom:is_write"), rempli une seule fois pour tout le process. Sans
        # ce nettoyage, seul le PREMIER test à toucher `get_my_products`
        # dans toute la session pytest voit son mock réellement utilisé —
        # les suivants héritent silencieusement du mock du premier (bug
        # constaté : 3 tests "faux positifs" avant ce fix).
        AgriDatabaseService._DISPATCH_CACHE.pop("get_my_products:False", None)
        AgriDatabaseService._DISPATCH_CACHE.pop("get_my_products:True", None)
        monkeypatch.setattr(
            AgriDatabaseService, "get_my_products",
            AsyncMock(return_value={"status": "success", "data": products}),
        )
        # `AgriDatabaseService().get_my_products(...)` passe par le wrapper
        # `@transactional` de `base_service.py`, qui appelle SA PROPRE
        # référence importée de `get_sessionmaker` (pas celle de
        # `workers/runtime.py` — deux noms liés séparément, même origine)
        # pour ouvrir la session "racine". Sans DATABASE_URL configurée (le
        # cas en CI/test), ça lève "Database sessionmaker unavailable". Même
        # technique que `test_workers_runtime_and_repos.py::TestWorkerSession
        # ._fake_sessionmaker`, ciblée sur le bon module.
        import ladini.services.database.base_service as base_service_module

        class _CM:
            async def __aenter__(self_inner):
                return AsyncMock()

            async def __aexit__(self_inner, *exc):
                return False

        monkeypatch.setattr(base_service_module, "get_sessionmaker", lambda: (lambda: _CM()))

    def test_no_products_at_all(self, monkeypatch):
        from ladini.workers.media.product_photo_task import _find_product_by_name
        self._patch_products(monkeypatch, [])
        result = run(_find_product_by_name("+22670000001", "maïs"))
        assert result == {"none": True}

    def test_exact_name_match_is_resolved(self, monkeypatch):
        from ladini.workers.media.product_photo_task import _find_product_by_name
        products = [
            {"id": "1", "name": "Maïs", "images": ["https://x/a.jpg"]},
            {"id": "2", "name": "Tomates", "images": []},
        ]
        self._patch_products(monkeypatch, products)
        result = run(_find_product_by_name("+22670000001", "maïs"))
        assert result["resolved"]["id"] == "1"

    def test_a_typo_still_resolves_via_fuzzy_matching(self, monkeypatch):
        from ladini.workers.media.product_photo_task import _find_product_by_name
        products = [{"id": "1", "name": "Tomates", "images": []}]
        self._patch_products(monkeypatch, products)
        result = run(_find_product_by_name("+22670000001", "tomate"))
        assert result["resolved"]["id"] == "1"

    def test_an_unrelated_query_is_reported_as_not_found_with_the_catalog_listed(self, monkeypatch):
        from ladini.workers.media.product_photo_task import _find_product_by_name
        products = [{"id": "1", "name": "Maïs", "images": []}, {"id": "2", "name": "Tomates", "images": []}]
        self._patch_products(monkeypatch, products)
        result = run(_find_product_by_name("+22670000001", "voitures"))
        assert "not_found" in result
        assert set(result["not_found"]) == {"Maïs", "Tomates"}

    def test_several_batches_of_the_same_name_are_reported_as_ambiguous(self, monkeypatch):
        """Rupture prévenue : un producteur publiant "maïs" trois fois avec
        des quantités différentes (300kg, 245kg, 456kg) ne doit jamais voir
        `_find_product_by_name` en choisir un au hasard — les trois doivent
        remonter comme un lot ambigu à trancher."""
        from ladini.workers.media.product_photo_task import _find_product_by_name
        products = [
            {"id": "1", "name": "maïs", "quantity_for_sale": 300, "unit": "KG", "images": []},
            {"id": "2", "name": "maïs", "quantity_for_sale": 245, "unit": "KG", "images": []},
            {"id": "3", "name": "maïs", "quantity_for_sale": 456, "unit": "KG", "images": []},
            {"id": "4", "name": "tomates", "quantity_for_sale": 10, "unit": "KG", "images": []},
        ]
        self._patch_products(monkeypatch, products)
        result = run(_find_product_by_name("+22670000001", "maïs"))
        assert "ambiguous" in result
        assert {p["id"] for p in result["ambiguous"]} == {"1", "2", "3"}

    def test_a_single_batch_among_same_named_ones_is_not_ambiguous(self, monkeypatch):
        from ladini.workers.media.product_photo_task import _find_product_by_name
        products = [{"id": "1", "name": "maïs", "quantity_for_sale": 300, "unit": "KG", "images": []}]
        self._patch_products(monkeypatch, products)
        result = run(_find_product_by_name("+22670000001", "maïs"))
        assert result["resolved"]["id"] == "1"
