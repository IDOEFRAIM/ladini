"""Persistance transactionnelle réelle de `PreorderDraft` (2026-09-03,
migration PREORDER). Gabarit DIRECT de
`test_procurement_draft_persistence.py` — même faux moteur SQL, même
portée honnête (voir docstring de ce fichier jumeau pour le détail exact
de ce que ces tests prouvent / ne prouvent pas : logique Python face à la
sémantique CAS, jamais un vrai Postgres).

`_FakeDraftTable`/`_install_fake_db` sont exportés pour être réutilisés par
d'autres fichiers de test PREORDER (mêmes garanties de fidélité SQL, une
seule source de vérité pour la simulation — voir
`tests/nodes/test_preorder_confirm_ux_and_gps.py`)."""
from __future__ import annotations

import threading
import time
import types
from typing import Any, Dict, Optional

from ladini.graphs.agents.market_coach.domain.preorder_draft import (
    PreorderDraft,
    PreorderDraftStatus,
)
from ladini.services.database import preorder_draft_store as store_mod
from tests.conftest import run


class _FakeDraftTable:
    def __init__(self) -> None:
        self._rows: Dict[str, Dict[str, Any]] = {}
        self.lock = threading.Lock()

    def select(self, draft_id: str) -> Optional[types.SimpleNamespace]:
        with self.lock:
            row = self._rows.get(draft_id)
            return types.SimpleNamespace(**{k: v for k, v in row.items() if k != "updated_at"}) if row else None

    def select_by_order_id(self, order_id: str) -> Optional[types.SimpleNamespace]:
        with self.lock:
            for row in self._rows.values():
                if row.get("order_id") == order_id:
                    return types.SimpleNamespace(**{k: v for k, v in row.items() if k != "updated_at"})
            return None

    def insert(self, *, draft_id, conversation_id, version, status, order_id, payload) -> int:
        with self.lock:
            if draft_id in self._rows:
                return 0
            self._rows[draft_id] = {
                "draft_id": draft_id,
                "conversation_id": conversation_id,
                "version": version,
                "status": status,
                "order_id": order_id,
                "payload": payload,
                "updated_at": time.time(),
            }
            return 1

    def cas_update(self, *, draft_id, expected_version, new_version, status, order_id, payload) -> int:
        with self.lock:
            row = self._rows.get(draft_id)
            if row is None or row["version"] != expected_version:
                return 0
            row["version"] = new_version
            row["status"] = status
            row["order_id"] = order_id
            row["payload"] = payload
            row["updated_at"] = time.time()
            return 1

    def force_updated_at(self, draft_id: str, updated_at: float) -> None:
        with self.lock:
            if draft_id in self._rows:
                self._rows[draft_id]["updated_at"] = updated_at

    def select_stale_by_status(self, status: str, older_than_seconds: float) -> list:
        with self.lock:
            now = time.time()
            return [
                types.SimpleNamespace(**{k: v for k, v in row.items() if k != "updated_at"})
                for row in self._rows.values()
                if row["status"] == status and (now - row["updated_at"]) > older_than_seconds
            ]


class _FakeResult:
    def __init__(self, rowcount: int = 0, rows=None) -> None:
        self.rowcount = rowcount
        self._rows = rows or []

    def mappings(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class _FakeSession:
    def __init__(self, table: _FakeDraftTable) -> None:
        self._table = table

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def commit(self):
        pass

    async def execute(self, sql, params):
        raw = str(sql)
        if "older_than_seconds" in params:
            rows = self._table.select_stale_by_status(params["status"], params["older_than_seconds"])
            return _FakeResult(rows=rows)
        if raw.startswith("SELECT") and "order_id" in params:
            row = self._table.select_by_order_id(params["order_id"])
            return _FakeResult(rows=[row] if row else [])
        if raw.startswith("SELECT"):
            row = self._table.select(params["draft_id"])
            return _FakeResult(rows=[row] if row else [])
        if raw.startswith("INSERT"):
            rc = self._table.insert(
                draft_id=params["draft_id"],
                conversation_id=params["conversation_id"],
                version=params["version"],
                status=params["status"],
                order_id=params.get("order_id"),
                payload=params["payload"],
            )
            return _FakeResult(rowcount=rc)
        if raw.startswith("UPDATE"):
            rc = self._table.cas_update(
                draft_id=params["draft_id"],
                expected_version=params["expected_version"],
                new_version=params["new_version"],
                status=params["status"],
                order_id=params.get("order_id"),
                payload=params["payload"],
            )
            return _FakeResult(rowcount=rc)
        raise AssertionError(f"Requête SQL inattendue : {raw!r}")


def _install_fake_db(monkeypatch, table: Optional[_FakeDraftTable] = None) -> _FakeDraftTable:
    table = table or _FakeDraftTable()
    monkeypatch.setattr(store_mod, "get_sessionmaker", lambda: (lambda: _FakeSession(table)))
    return table


def _draft(**fields) -> PreorderDraft:
    base = {
        "order_id": "order-1",
        "items": [{"product_id": "p1", "name": "riz", "quantity": 10, "unit": "kg", "price": 500}],
        "total_amount": 5000.0,
    }
    base.update(fields)
    return PreorderDraft.new(draft_id=fields.get("draft_id", "pre1"), **{
        k: v for k, v in base.items() if k != "draft_id"
    })


class TestLoadInsertCompareAndSwapSemantics:
    def test_insert_then_load_round_trips(self, monkeypatch):
        _install_fake_db(monkeypatch)
        draft = _draft(draft_id="p1")
        assert run(store_mod.insert(draft, conversation_id="+22600000000")) is True
        loaded = run(store_mod.load("p1"))
        assert loaded.order_id == "order-1"
        assert loaded.version == 1
        assert loaded.status == PreorderDraftStatus.DRAFT

    def test_compare_and_swap_with_stale_version_fails(self, monkeypatch):
        _install_fake_db(monkeypatch)
        draft = _draft(draft_id="p2")
        run(store_mod.insert(draft, conversation_id="c"))
        v2 = draft.with_updates(total_amount=6000.0)
        run(store_mod.compare_and_swap("p2", expected_version=1, new_draft=v2))

        stale = draft.with_updates(total_amount=9999.0)
        ok = run(store_mod.compare_and_swap("p2", expected_version=1, new_draft=stale))
        assert ok is False
        reloaded = run(store_mod.load("p2"))
        assert reloaded.total_amount == 6000.0

    def test_a_missing_sessionmaker_never_raises(self, monkeypatch):
        monkeypatch.setattr(store_mod, "get_sessionmaker", lambda: None)
        draft = _draft(draft_id="p3")
        assert run(store_mod.load("p3")) is None
        assert run(store_mod.insert(draft, conversation_id="c")) is False
        assert run(store_mod.compare_and_swap("p3", expected_version=1, new_draft=draft)) is False


class TestRealThreadConcurrency:
    def test_ten_threads_racing_the_same_expected_version_only_one_wins(self, monkeypatch):
        _install_fake_db(monkeypatch)
        draft = _draft(draft_id="race1")
        run(store_mod.insert(draft, conversation_id="c"))

        results = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(10)

        def worker(i):
            barrier.wait()
            candidate = draft.with_updates(total_amount=float(1000 + i))
            ok = run(store_mod.compare_and_swap("race1", expected_version=1, new_draft=candidate))
            with results_lock:
                results.append(ok)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(1 for r in results if r) == 1
        final = run(store_mod.load("race1"))
        assert final.version == 2


class TestFindByOrderId:
    def test_a_draft_is_found_by_its_order_id(self, monkeypatch):
        _install_fake_db(monkeypatch)
        draft = _draft(draft_id="d1", order_id="order-xyz")
        run(store_mod.insert(draft, conversation_id="c"))
        found = run(store_mod.find_by_order_id("order-xyz"))
        assert found is not None
        assert found.draft_id == "d1"

    def test_an_unknown_order_id_returns_none_not_an_error(self, monkeypatch):
        _install_fake_db(monkeypatch)
        assert run(store_mod.find_by_order_id("nope")) is None


class TestStaleByStatus:
    def test_find_stale_by_status_targets_only_the_requested_status(self, monkeypatch):
        table = _install_fake_db(monkeypatch)
        base = _draft(draft_id="await1")
        run(store_mod.insert(base, conversation_id="c"))
        executing = base._confirm_to_executing(delivery_lat=1.0, delivery_lon=1.0)
        run(store_mod.compare_and_swap("await1", expected_version=1, new_draft=executing))
        table.force_updated_at("await1", time.time() - 7200)

        stale_executing = run(store_mod.find_stale_by_status("EXECUTING", older_than_seconds=3600))
        assert "await1" in [d.draft_id for d in stale_executing]
        stale_awaiting = run(store_mod.find_stale_by_status("AWAITING_PAYMENT", older_than_seconds=3600))
        assert "await1" not in [d.draft_id for d in stale_awaiting]


class TestStaleExecutingDetection:
    def test_an_old_executing_draft_is_reported_stale(self, monkeypatch):
        table = _install_fake_db(monkeypatch)
        draft = _draft(draft_id="stale1")
        run(store_mod.insert(draft, conversation_id="c"))
        executing = draft._confirm_to_executing(delivery_lat=1.0, delivery_lon=1.0)
        run(store_mod.compare_and_swap("stale1", expected_version=1, new_draft=executing))
        table.force_updated_at("stale1", time.time() - 7200)

        stale = run(store_mod.find_stale_executing(older_than_seconds=3600))
        assert "stale1" in [d.draft_id for d in stale]

    def test_a_fresh_executing_draft_is_not_reported(self, monkeypatch):
        table = _install_fake_db(monkeypatch)
        draft = _draft(draft_id="fresh1")
        run(store_mod.insert(draft, conversation_id="c"))
        executing = draft._confirm_to_executing(delivery_lat=1.0, delivery_lon=1.0)
        run(store_mod.compare_and_swap("fresh1", expected_version=1, new_draft=executing))

        stale = run(store_mod.find_stale_executing(older_than_seconds=3600))
        assert "fresh1" not in [d.draft_id for d in stale]
