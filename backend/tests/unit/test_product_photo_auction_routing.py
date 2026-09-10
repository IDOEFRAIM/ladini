"""`workers/media/product_photo_task.py::_process` — priorité "photo pour
une offre/un appel d'offres qu'on vient de créer" (services/pending_photo_target.py)
sur le repli catalogue produit historique.

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


def _patch_download_and_upload(monkeypatch):
    monkeypatch.setattr(
        "ladini.services.whatsapp.twilio_media.download_twilio_media",
        AsyncMock(return_value=(b"binary", "image/jpeg")),
    )
    monkeypatch.setattr(
        "ladini.services.storage.supabase_storage.upload_product_photo",
        AsyncMock(return_value="https://x/uploaded.jpg"),
    )


def _patch_worker_session(monkeypatch):
    """`_process` ouvre son propre `worker_session()` (`workers/runtime.py`)
    avant même de résoudre le produit cible — sans DATABASE_URL configurée
    (le cas en CI/test), ça lève "Sessionmaker indisponible". Même technique
    que `test_workers_runtime_and_repos.py::TestWorkerSession
    ._fake_sessionmaker`."""
    import ladini.workers.runtime as runtime_module

    class _CM:
        async def __aenter__(self_inner):
            return AsyncMock()

        async def __aexit__(self_inner, *exc):
            return False

    monkeypatch.setattr(runtime_module, "get_sessionmaker", lambda: (lambda: _CM()))


class TestPendingBidPhotoTakesPriority:
    def test_a_pending_bid_photo_is_linked_instead_of_the_product_catalog(self, monkeypatch):
        import ladini.workers.media.product_photo_task as mod

        _patch_download_and_upload(monkeypatch)
        monkeypatch.setattr(
            "ladini.services.pending_photo_target.pop_pending_bid_photo",
            lambda phone: "bid-1",
        )
        monkeypatch.setattr(
            "ladini.services.pending_photo_target.pop_pending_auction_photo",
            lambda phone: None,
        )
        resolve_called = {"count": 0}
        monkeypatch.setattr(
            mod, "_resolve_target_product",
            AsyncMock(side_effect=lambda phone: resolve_called.__setitem__("count", 1)),
        )
        link_bid = AsyncMock()
        monkeypatch.setattr(mod, "_link_bid_photo_and_confirm", link_bid)

        run(mod._process(PHONE, "https://twilio/media", "image/jpeg"))

        link_bid.assert_awaited_once_with(
            PHONE, "bid-1", "https://x/uploaded.jpg", message_sid=None
        )
        assert resolve_called["count"] == 0, "le catalogue produit ne doit pas être consulté"


class TestPendingAuctionPhotoTakesPriorityOverProductCatalog:
    def test_a_pending_auction_photo_is_linked_when_no_bid_is_pending(self, monkeypatch):
        import ladini.workers.media.product_photo_task as mod

        _patch_download_and_upload(monkeypatch)
        monkeypatch.setattr(
            "ladini.services.pending_photo_target.pop_pending_bid_photo",
            lambda phone: None,
        )
        monkeypatch.setattr(
            "ladini.services.pending_photo_target.pop_pending_auction_photo",
            lambda phone: "auction-1",
        )
        resolve_called = {"count": 0}
        monkeypatch.setattr(
            mod, "_resolve_target_product",
            AsyncMock(side_effect=lambda phone: resolve_called.__setitem__("count", 1)),
        )
        link_auction = AsyncMock()
        monkeypatch.setattr(mod, "_link_auction_photo_and_confirm", link_auction)

        run(mod._process(PHONE, "https://twilio/media", "image/jpeg"))

        link_auction.assert_awaited_once_with(
            PHONE, "auction-1", "https://x/uploaded.jpg", message_sid=None
        )
        assert resolve_called["count"] == 0


class TestBidTakesPriorityOverAuctionWhenBothPending:
    def test_bid_wins_when_both_markers_are_set(self, monkeypatch):
        import ladini.workers.media.product_photo_task as mod

        _patch_download_and_upload(monkeypatch)
        monkeypatch.setattr(
            "ladini.services.pending_photo_target.pop_pending_bid_photo",
            lambda phone: "bid-1",
        )
        monkeypatch.setattr(
            "ladini.services.pending_photo_target.pop_pending_auction_photo",
            lambda phone: "auction-1",
        )
        link_bid = AsyncMock()
        link_auction = AsyncMock()
        monkeypatch.setattr(mod, "_link_bid_photo_and_confirm", link_bid)
        monkeypatch.setattr(mod, "_link_auction_photo_and_confirm", link_auction)

        run(mod._process(PHONE, "https://twilio/media", "image/jpeg"))

        link_bid.assert_awaited_once()
        link_auction.assert_not_awaited()


class TestNoPendingTargetFallsBackToProductCatalog:
    def test_falls_back_to_the_historical_product_catalog_flow(self, monkeypatch):
        import ladini.workers.media.product_photo_task as mod

        _patch_download_and_upload(monkeypatch)
        monkeypatch.setattr(
            "ladini.services.pending_photo_target.pop_pending_bid_photo",
            lambda phone: None,
        )
        monkeypatch.setattr(
            "ladini.services.pending_photo_target.pop_pending_auction_photo",
            lambda phone: None,
        )
        resolve = AsyncMock(return_value={"none": True})
        monkeypatch.setattr(mod, "_resolve_target_product", resolve)
        sent = AsyncMock()
        monkeypatch.setattr("ladini.api.tasks.send_confirmation_text", sent)
        _patch_worker_session(monkeypatch)

        run(mod._process(PHONE, "https://twilio/media", "image/jpeg"))

        resolve.assert_awaited_once_with(PHONE)
