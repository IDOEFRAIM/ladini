"""`services/pending_photo_target.py` — marqueur "la prochaine photo va à
CETTE offre/CET appel d'offres", posé par le rendu LangGraph juste après un
`place_bid`/`create_auction` réussi, lu par le pipeline Celery des photos.
Sans dépendance `celery` (comme `services/search_results_cache.py`)."""
from __future__ import annotations


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, _ttl, value):
        self.store[key] = value

    def delete(self, key):
        self.store.pop(key, None)


class TestBidPhotoTarget:
    def test_set_then_pop_round_trip(self, monkeypatch):
        import ladini.services.pending_photo_target as mod
        fake = _FakeRedis()
        monkeypatch.setattr(mod, "_redis", lambda: fake)

        mod.set_pending_bid_photo("+22670000001", "bid-1")
        assert mod.pop_pending_bid_photo("+22670000001") == "bid-1"

    def test_pop_is_single_use(self, monkeypatch):
        import ladini.services.pending_photo_target as mod
        fake = _FakeRedis()
        monkeypatch.setattr(mod, "_redis", lambda: fake)

        mod.set_pending_bid_photo("+22670000001", "bid-1")
        mod.pop_pending_bid_photo("+22670000001")
        assert mod.pop_pending_bid_photo("+22670000001") is None

    def test_pop_with_nothing_set_returns_none(self, monkeypatch):
        import ladini.services.pending_photo_target as mod
        fake = _FakeRedis()
        monkeypatch.setattr(mod, "_redis", lambda: fake)

        assert mod.pop_pending_bid_photo("+22670000001") is None

    def test_set_with_no_phone_or_id_is_a_no_op(self, monkeypatch):
        import ladini.services.pending_photo_target as mod
        fake = _FakeRedis()
        monkeypatch.setattr(mod, "_redis", lambda: fake)

        mod.set_pending_bid_photo("", "bid-1")
        mod.set_pending_bid_photo("+22670000001", "")
        assert fake.store == {}


class TestAuctionPhotoTarget:
    def test_set_then_pop_round_trip(self, monkeypatch):
        import ladini.services.pending_photo_target as mod
        fake = _FakeRedis()
        monkeypatch.setattr(mod, "_redis", lambda: fake)

        mod.set_pending_auction_photo("+22670000001", "auction-1")
        assert mod.pop_pending_auction_photo("+22670000001") == "auction-1"

    def test_bid_and_auction_markers_are_independent(self, monkeypatch):
        import ladini.services.pending_photo_target as mod
        fake = _FakeRedis()
        monkeypatch.setattr(mod, "_redis", lambda: fake)

        mod.set_pending_bid_photo("+22670000001", "bid-1")
        mod.set_pending_auction_photo("+22670000001", "auction-1")

        assert mod.pop_pending_bid_photo("+22670000001") == "bid-1"
        assert mod.pop_pending_auction_photo("+22670000001") == "auction-1"


class TestProductPhotoTarget:
    """Même mécanisme que Bid/Auction, posé après un `create_product` réussi
    (2026-09-15) — voir `nodes/rendering/success.py`."""

    def test_set_then_pop_round_trip(self, monkeypatch):
        import ladini.services.pending_photo_target as mod
        fake = _FakeRedis()
        monkeypatch.setattr(mod, "_redis", lambda: fake)

        mod.set_pending_product_photo("+22670000001", "product-1")
        assert mod.pop_pending_product_photo("+22670000001") == "product-1"

    def test_pop_is_single_use(self, monkeypatch):
        import ladini.services.pending_photo_target as mod
        fake = _FakeRedis()
        monkeypatch.setattr(mod, "_redis", lambda: fake)

        mod.set_pending_product_photo("+22670000001", "product-1")
        mod.pop_pending_product_photo("+22670000001")
        assert mod.pop_pending_product_photo("+22670000001") is None

    def test_bid_auction_and_product_markers_are_independent(self, monkeypatch):
        import ladini.services.pending_photo_target as mod
        fake = _FakeRedis()
        monkeypatch.setattr(mod, "_redis", lambda: fake)

        mod.set_pending_bid_photo("+22670000001", "bid-1")
        mod.set_pending_auction_photo("+22670000001", "auction-1")
        mod.set_pending_product_photo("+22670000001", "product-1")

        assert mod.pop_pending_bid_photo("+22670000001") == "bid-1"
        assert mod.pop_pending_auction_photo("+22670000001") == "auction-1"
        assert mod.pop_pending_product_photo("+22670000001") == "product-1"

    def test_set_with_no_phone_or_id_is_a_no_op(self, monkeypatch):
        import ladini.services.pending_photo_target as mod
        fake = _FakeRedis()
        monkeypatch.setattr(mod, "_redis", lambda: fake)

        mod.set_pending_product_photo("", "product-1")
        mod.set_pending_product_photo("+22670000001", "")
        assert fake.store == {}


class TestResilience:
    def test_a_write_failure_never_raises(self, monkeypatch):
        import ladini.services.pending_photo_target as mod

        class _Broken:
            def setex(self, *a, **kw):
                raise ConnectionError("redis down")

        monkeypatch.setattr(mod, "_redis", lambda: _Broken())
        mod.set_pending_bid_photo("+22670000001", "bid-1")  # ne doit pas lever

    def test_a_read_failure_returns_none(self, monkeypatch):
        import ladini.services.pending_photo_target as mod

        class _Broken:
            def get(self, *a, **kw):
                raise ConnectionError("redis down")

        monkeypatch.setattr(mod, "_redis", lambda: _Broken())
        assert mod.pop_pending_bid_photo("+22670000001") is None
