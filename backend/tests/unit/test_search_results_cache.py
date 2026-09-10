"""`services/search_results_cache.py` — cache Redis reliant un numéro de
résultat de recherche acheteur à son produit/ses photos, sans dépendance
Celery (contrairement à workers/media/product_photo_task.py)."""
from __future__ import annotations


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, _ttl, value):
        self.store[key] = value


class TestStoreAndLoadResults:
    def test_round_trip(self, monkeypatch):
        import ladini.services.search_results_cache as mod
        fake = _FakeRedis()
        monkeypatch.setattr(mod, "_redis", lambda: fake)

        mod.store_results("+22670000001", {"1": {"id": "a", "name": "maïs", "images": ["https://x/a.jpg"]}})
        loaded = mod.load_results("+22670000001")

        assert loaded == {"1": {"id": "a", "name": "maïs", "images": ["https://x/a.jpg"]}}

    def test_load_returns_none_when_nothing_cached(self, monkeypatch):
        import ladini.services.search_results_cache as mod
        fake = _FakeRedis()
        monkeypatch.setattr(mod, "_redis", lambda: fake)

        assert mod.load_results("+22670000001") is None

    def test_store_with_empty_entries_is_a_no_op(self, monkeypatch):
        import ladini.services.search_results_cache as mod
        fake = _FakeRedis()
        monkeypatch.setattr(mod, "_redis", lambda: fake)

        mod.store_results("+22670000001", {})
        assert fake.store == {}

    def test_store_with_no_phone_is_a_no_op(self, monkeypatch):
        import ladini.services.search_results_cache as mod
        fake = _FakeRedis()
        monkeypatch.setattr(mod, "_redis", lambda: fake)

        mod.store_results("", {"1": {"id": "a"}})
        assert fake.store == {}

    def test_a_redis_failure_on_write_never_raises(self, monkeypatch):
        import ladini.services.search_results_cache as mod

        class _BrokenRedis:
            def setex(self, *a, **kw):
                raise ConnectionError("redis down")

        monkeypatch.setattr(mod, "_redis", lambda: _BrokenRedis())
        mod.store_results("+22670000001", {"1": {"id": "a"}})  # ne doit pas lever

    def test_a_redis_failure_on_read_returns_none(self, monkeypatch):
        import ladini.services.search_results_cache as mod

        class _BrokenRedis:
            def get(self, *a, **kw):
                raise ConnectionError("redis down")

        monkeypatch.setattr(mod, "_redis", lambda: _BrokenRedis())
        assert mod.load_results("+22670000001") is None

    def test_corrupt_json_returns_none(self, monkeypatch):
        import ladini.services.search_results_cache as mod
        fake = _FakeRedis()
        fake.store[mod.key_for("+22670000001")] = "not json"
        monkeypatch.setattr(mod, "_redis", lambda: fake)

        assert mod.load_results("+22670000001") is None

    def test_key_is_namespaced_by_phone(self):
        import ladini.services.search_results_cache as mod
        assert mod.key_for("+22670000001") == "last_search_results:+22670000001"
