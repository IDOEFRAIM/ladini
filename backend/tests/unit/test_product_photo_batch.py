"""Envoi de plusieurs photos en rafale — une seule question "quel produit ?"
au lieu de la reposer à chaque image (bug constaté en usage réel : Twilio
livre chaque photo d'un envoi groupé comme un message webhook séparé).

NB : `product_photo_task.py` importe `api.celery_app` (`from celery import
Celery`) au niveau module — comme les autres tests de ce module, ce fichier
ne peut donc pas se collecter sans le paquet `celery` installé (pré-existant,
sans rapport avec cette feature).
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from tests.conftest import run

pytest.importorskip("celery", reason="product_photo_task imports api.celery_app -> celery.Celery")


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, _ttl, value):
        self.store[key] = value

    def delete(self, key):
        self.store.pop(key, None)

    def exists(self, key):
        return key in self.store


PHONE = "+22670000001"
CANDIDATES = [{"id": "1", "name": "maïs"}, {"id": "2", "name": "tomates"}]


def _patch_worker_session(monkeypatch):
    """`_resolve_pending`/`_resolve_pending_view` ouvrent leur propre
    `worker_session()` (`workers/runtime.py`) avant même de toucher
    `AgriDatabaseService` — sans DATABASE_URL configurée (le cas en CI/test),
    ça lève "Sessionmaker indisponible". Même technique que
    `test_workers_runtime_and_repos.py::TestWorkerSession._fake_sessionmaker`
    : un faux sessionmaker qui publie une session factice dans
    `db_session_ctx`, ce qui fait aussi passer les appels @transactional
    imbriqués (`AgriDatabaseService...`, déjà mockés séparément) par le
    chemin "session existante" sans re-vérifier le sessionmaker."""
    import agriconnect.workers.runtime as runtime_module

    fake_session = AsyncMock()

    class _CM:
        async def __aenter__(self_inner):
            return fake_session

        async def __aexit__(self_inner, *exc):
            return False

    monkeypatch.setattr(runtime_module, "get_sessionmaker", lambda: (lambda: _CM()))
    return fake_session


def _patch_add_product_photo(monkeypatch, **mock_kwargs) -> AsyncMock:
    """`AgriDatabaseService.__getattribute__` (d.py) mémorise le wrapper
    @transactional dans un cache DE CLASSE (`_DISPATCH_CACHE`, clé
    "nom:is_write"), rempli une seule fois pour tout le process. Sans ce
    nettoyage, seul le PREMIER test de ce fichier à toucher
    `add_product_photo` voit son mock réellement utilisé — les suivants
    héritent silencieusement du mock du premier (bug constaté : 2 tests
    "faux positifs" avant ce fix — le code de production est correct, c'est
    le mocking qui était trompé par le cache)."""
    from agriconnect.services.database.d import AgriDatabaseService

    AgriDatabaseService._DISPATCH_CACHE.pop("add_product_photo:False", None)
    AgriDatabaseService._DISPATCH_CACHE.pop("add_product_photo:True", None)
    mock = AsyncMock(**mock_kwargs)
    monkeypatch.setattr(AgriDatabaseService, "add_product_photo", mock)
    return mock


class TestAskOrAccumulate:
    def test_first_ambiguous_photo_creates_the_pending_state_and_sends_the_menu(self, monkeypatch):
        import agriconnect.workers.media.product_photo_task as mod

        fake_redis = _FakeRedis()
        monkeypatch.setattr(mod, "_redis", lambda: fake_redis)
        sent = AsyncMock()
        monkeypatch.setattr("agriconnect.api.tasks.send_confirmation_text", sent)

        run(mod._ask_or_accumulate(PHONE, CANDIDATES, "https://x/a.jpg"))

        assert sent.await_count == 1
        pending = json.loads(fake_redis.store[mod.pending_photo_key(PHONE)])
        assert pending["image_urls"] == ["https://x/a.jpg"]
        assert pending["product_ids"] == ["1", "2"]

    def test_a_second_ambiguous_photo_accumulates_without_resending_the_menu(self, monkeypatch):
        import agriconnect.workers.media.product_photo_task as mod

        fake_redis = _FakeRedis()
        monkeypatch.setattr(mod, "_redis", lambda: fake_redis)
        sent = AsyncMock()
        monkeypatch.setattr("agriconnect.api.tasks.send_confirmation_text", sent)

        run(mod._ask_or_accumulate(PHONE, CANDIDATES, "https://x/a.jpg"))
        run(mod._ask_or_accumulate(PHONE, CANDIDATES, "https://x/b.jpg"))

        assert sent.await_count == 1, "le menu ne doit être envoyé qu'une seule fois par rafale"
        pending = json.loads(fake_redis.store[mod.pending_photo_key(PHONE)])
        assert pending["image_urls"] == ["https://x/a.jpg", "https://x/b.jpg"]

    def test_a_third_photo_keeps_accumulating(self, monkeypatch):
        import agriconnect.workers.media.product_photo_task as mod

        fake_redis = _FakeRedis()
        monkeypatch.setattr(mod, "_redis", lambda: fake_redis)
        monkeypatch.setattr("agriconnect.api.tasks.send_confirmation_text", AsyncMock())

        for url in ("https://x/a.jpg", "https://x/b.jpg", "https://x/c.jpg"):
            run(mod._ask_or_accumulate(PHONE, CANDIDATES, url))

        pending = json.loads(fake_redis.store[mod.pending_photo_key(PHONE)])
        assert len(pending["image_urls"]) == 3


class TestResolvePendingLinksTheWholeBatch:
    def test_resolving_links_every_accumulated_photo_in_one_confirmation(self, monkeypatch):
        import agriconnect.workers.media.product_photo_task as mod

        fake_redis = _FakeRedis()
        key = mod.pending_photo_key(PHONE)
        fake_redis.store[key] = json.dumps({
            "image_urls": ["https://x/a.jpg", "https://x/b.jpg"],
            "product_ids": ["1", "2"],
        })
        monkeypatch.setattr(mod, "_redis", lambda: fake_redis)
        sent = AsyncMock()
        monkeypatch.setattr("agriconnect.api.tasks.send_confirmation_text", sent)
        add_photo = _patch_add_product_photo(
            monkeypatch, return_value={"status": "success", "data": {"name": "maïs"}},
        )
        _patch_worker_session(monkeypatch)

        run(mod._resolve_pending(PHONE, "1"))

        assert add_photo.await_count == 2
        assert add_photo.await_args_list[0].kwargs["image_url"] == "https://x/a.jpg"
        assert add_photo.await_args_list[1].kwargs["image_url"] == "https://x/b.jpg"
        sent.assert_awaited_once()
        assert "2 photos ajoutées" in sent.await_args.args[1]
        assert key not in fake_redis.store

    def test_a_single_photo_batch_uses_the_singular_wording(self, monkeypatch):
        import agriconnect.workers.media.product_photo_task as mod

        fake_redis = _FakeRedis()
        key = mod.pending_photo_key(PHONE)
        fake_redis.store[key] = json.dumps({"image_urls": ["https://x/a.jpg"], "product_ids": ["1", "2"]})
        monkeypatch.setattr(mod, "_redis", lambda: fake_redis)
        sent = AsyncMock()
        monkeypatch.setattr("agriconnect.api.tasks.send_confirmation_text", sent)
        _patch_add_product_photo(
            monkeypatch, return_value={"status": "success", "data": {"name": "maïs"}},
        )
        _patch_worker_session(monkeypatch)

        run(mod._resolve_pending(PHONE, "1"))

        assert "1 photo ajoutée à" in sent.await_args.args[1]
        assert "photos ajoutées" not in sent.await_args.args[1]

    def test_legacy_single_url_pending_entries_are_still_handled(self, monkeypatch):
        """Compat rétro : une entrée Redis encore au format mono-photo
        (créée avant ce fix, pas encore expirée) doit rester résoluble."""
        import agriconnect.workers.media.product_photo_task as mod

        fake_redis = _FakeRedis()
        key = mod.pending_photo_key(PHONE)
        fake_redis.store[key] = json.dumps({"image_url": "https://x/legacy.jpg", "product_ids": ["1", "2"]})
        monkeypatch.setattr(mod, "_redis", lambda: fake_redis)
        sent = AsyncMock()
        monkeypatch.setattr("agriconnect.api.tasks.send_confirmation_text", sent)
        add_photo = _patch_add_product_photo(
            monkeypatch, return_value={"status": "success", "data": {"name": "maïs"}},
        )
        _patch_worker_session(monkeypatch)

        run(mod._resolve_pending(PHONE, "1"))

        add_photo.assert_awaited_once()
        assert add_photo.await_args.kwargs["image_url"] == "https://x/legacy.jpg"

    def test_no_pending_state_reports_expiry(self, monkeypatch):
        import agriconnect.workers.media.product_photo_task as mod

        fake_redis = _FakeRedis()
        monkeypatch.setattr(mod, "_redis", lambda: fake_redis)
        sent = AsyncMock()
        monkeypatch.setattr("agriconnect.api.tasks.send_confirmation_text", sent)

        run(mod._resolve_pending(PHONE, "1"))


class TestFormatCandidateLabel:
    """`_format_candidate_label` — évite le menu illisible "1. maïs / 2. maïs
    / 3. maïs" quand un producteur publie plusieurs lots du même nom."""

    def test_includes_quantity_and_unit_when_present(self):
        import agriconnect.workers.media.product_photo_task as mod
        product = {"name": "maïs", "quantity_for_sale": 300, "unit": "KG"}
        assert mod._format_candidate_label(0, product) == "1. maïs (300 KG)"

    def test_falls_back_to_the_bare_name_without_a_quantity(self):
        import agriconnect.workers.media.product_photo_task as mod
        product = {"name": "maïs"}
        assert mod._format_candidate_label(0, product) == "1. maïs"

    def test_index_drives_the_displayed_number(self):
        import agriconnect.workers.media.product_photo_task as mod
        product = {"name": "tomates", "quantity_for_sale": 10, "unit": "KG"}
        assert mod._format_candidate_label(2, product) == "3. tomates (10 KG)"


class TestAskWhichBatchToView:
    def test_sends_a_menu_and_stores_the_pending_selection(self, monkeypatch):
        import agriconnect.workers.media.product_photo_task as mod

        fake_redis = _FakeRedis()
        monkeypatch.setattr(mod, "_redis", lambda: fake_redis)
        sent = AsyncMock()
        monkeypatch.setattr("agriconnect.api.tasks.send_confirmation_text", sent)

        batches = [
            {"id": "1", "name": "maïs", "quantity_for_sale": 300, "unit": "KG"},
            {"id": "2", "name": "maïs", "quantity_for_sale": 245, "unit": "KG"},
        ]
        run(mod._ask_which_batch_to_view(PHONE, batches))

        sent.assert_awaited_once()
        assert "maïs (300 KG)" in sent.await_args.args[1]
        assert "maïs (245 KG)" in sent.await_args.args[1]
        pending = json.loads(fake_redis.store[mod.pending_view_key(PHONE)])
        assert pending["product_ids"] == ["1", "2"]


class TestResolvePendingView:
    def test_resolves_the_chosen_batch_and_sends_its_photos(self, monkeypatch):
        import agriconnect.workers.media.product_photo_task as mod
        from agriconnect.services.database.d import AgriDatabaseService

        fake_redis = _FakeRedis()
        key = mod.pending_view_key(PHONE)
        fake_redis.store[key] = json.dumps({"product_ids": ["1", "2"]})
        monkeypatch.setattr(mod, "_redis", lambda: fake_redis)

        products = [
            {"id": "1", "name": "maïs", "images": ["https://x/a.jpg"]},
            {"id": "2", "name": "maïs", "images": []},
        ]
        AgriDatabaseService._DISPATCH_CACHE.pop("get_my_products:False", None)
        AgriDatabaseService._DISPATCH_CACHE.pop("get_my_products:True", None)
        monkeypatch.setattr(
            AgriDatabaseService, "get_my_products",
            AsyncMock(return_value={"status": "success", "data": products}),
        )

        sent_media = []

        def _fake_send_media(phone, url, caption=""):
            sent_media.append((phone, url, caption))
            return "SM1"

        monkeypatch.setattr(
            "agriconnect.services.twilio_sender.send_whatsapp_media", _fake_send_media,
        )
        _patch_worker_session(monkeypatch)

        run(mod._resolve_pending_view(PHONE, "1"))

        assert sent_media == [(PHONE, "https://x/a.jpg", "📸 maïs")]
        assert key not in fake_redis.store

    def test_an_expired_selection_is_reported(self, monkeypatch):
        import agriconnect.workers.media.product_photo_task as mod

        fake_redis = _FakeRedis()
        monkeypatch.setattr(mod, "_redis", lambda: fake_redis)
        sent = AsyncMock()
        monkeypatch.setattr("agriconnect.api.tasks.send_confirmation_text", sent)

        run(mod._resolve_pending_view(PHONE, "1"))

        assert "expiré" in sent.await_args.args[1]

        assert "expiré" in sent.await_args.args[1]
