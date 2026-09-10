"""Persistance transactionnelle réelle de `SalesPublishDraft` (2026-09-04,
migration SALES — gabarit DIRECT de
`test_procurement_draft_persistence.py`, même limite honnête documentée
là-bas : faux moteur SQL fidèle, pas un vrai Postgres."""
from __future__ import annotations

import threading
import time
import types
from typing import Any, Dict, Optional

from tests.conftest import run

from ladini.graphs.agents.market_coach.domain.sales_publish_draft import (
    SalesPublishDraft,
    SalesPublishDraftStatus,
)
from ladini.services.database import sales_publish_draft_store as store_mod


class _FakeDraftTable:
    def __init__(self) -> None:
        self._rows: Dict[str, Dict[str, Any]] = {}
        self.lock = threading.Lock()

    def select(self, draft_id: str) -> Optional[types.SimpleNamespace]:
        with self.lock:
            row = self._rows.get(draft_id)
            return types.SimpleNamespace(**{k: v for k, v in row.items() if k != "updated_at"}) if row else None

    def insert(self, *, draft_id, conversation_id, version, status, payload) -> int:
        with self.lock:
            if draft_id in self._rows:
                return 0
            self._rows[draft_id] = {
                "draft_id": draft_id,
                "conversation_id": conversation_id,
                "version": version,
                "status": status,
                "payload": payload,
                "updated_at": time.time(),
            }
            return 1

    def cas_update(self, *, draft_id, expected_version, new_version, status, payload) -> int:
        with self.lock:
            row = self._rows.get(draft_id)
            if row is None or row["version"] != expected_version:
                return 0
            row["version"] = new_version
            row["status"] = status
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
    def __init__(self, rowcount: int = 0, rows: Optional[list] = None) -> None:
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
        if raw.startswith("SELECT"):
            row = self._table.select(params["draft_id"])
            return _FakeResult(rows=[row] if row else [])
        if raw.startswith("INSERT"):
            rc = self._table.insert(
                draft_id=params["draft_id"],
                conversation_id=params["conversation_id"],
                version=params["version"],
                status=params["status"],
                payload=params["payload"],
            )
            return _FakeResult(rowcount=rc)
        if raw.startswith("UPDATE"):
            rc = self._table.cas_update(
                draft_id=params["draft_id"],
                expected_version=params["expected_version"],
                new_version=params["new_version"],
                status=params["status"],
                payload=params["payload"],
            )
            return _FakeResult(rowcount=rc)
        raise AssertionError(f"Requête SQL inattendue dans le faux moteur : {raw!r}")


def _install_fake_db(monkeypatch, table: Optional[_FakeDraftTable] = None) -> _FakeDraftTable:
    table = table or _FakeDraftTable()
    monkeypatch.setattr(store_mod, "get_sessionmaker", lambda: (lambda: _FakeSession(table)))
    return table


def _draft(**fields) -> SalesPublishDraft:
    base = {"product": "Maïs blanc", "quantity": 50.0, "unit": "KG", "price": 250.0}
    base.update(fields)
    return SalesPublishDraft.new(draft_id=fields.get("draft_id", "sd-persist1"), **{
        k: v for k, v in base.items() if k != "draft_id"
    })


class TestLoadInsertCompareAndSwapSemantics:
    def test_load_on_an_absent_draft_id_returns_none(self, monkeypatch):
        _install_fake_db(monkeypatch)
        assert run(store_mod.load("nope")) is None

    def test_insert_then_load_round_trips_the_exact_draft(self, monkeypatch):
        _install_fake_db(monkeypatch)
        draft = _draft(draft_id="sd1")
        assert run(store_mod.insert(draft, conversation_id="+22600000000")) is True

        loaded = run(store_mod.load("sd1"))
        assert loaded is not None
        assert loaded.draft_id == draft.draft_id
        assert loaded.version == draft.version == 1
        assert loaded.status == SalesPublishDraftStatus.DRAFT
        assert loaded.quantity == draft.quantity
        assert loaded.product == "Maïs blanc"

    def test_a_second_insert_on_the_same_draft_id_is_rejected(self, monkeypatch):
        _install_fake_db(monkeypatch)
        draft = _draft(draft_id="sd2")
        assert run(store_mod.insert(draft, conversation_id="c")) is True

        other = _draft(draft_id="sd2", product="Riz")
        assert run(store_mod.insert(other, conversation_id="c")) is False
        reloaded = run(store_mod.load("sd2"))
        assert reloaded.product == "Maïs blanc"

    def test_compare_and_swap_with_the_expected_version_succeeds_and_persists(self, monkeypatch):
        _install_fake_db(monkeypatch)
        draft = _draft(draft_id="sd3")
        run(store_mod.insert(draft, conversation_id="c"))
        updated = draft.with_updates(price=300.0)
        assert run(
            store_mod.compare_and_swap(draft.draft_id, expected_version=draft.version, new_draft=updated)
        ) is True
        reloaded = run(store_mod.load("sd3"))
        assert reloaded.price == 300.0
        assert reloaded.version == 2

    def test_compare_and_swap_with_a_stale_expected_version_fails(self, monkeypatch):
        _install_fake_db(monkeypatch)
        draft = _draft(draft_id="sd4")
        run(store_mod.insert(draft, conversation_id="c"))
        run(store_mod.compare_and_swap(draft.draft_id, expected_version=1, new_draft=draft.with_updates(price=1.0)))

        stale_update = draft.with_updates(price=999.0)  # toujours basé sur v1
        assert run(
            store_mod.compare_and_swap(draft.draft_id, expected_version=1, new_draft=stale_update)
        ) is False
        reloaded = run(store_mod.load("sd4"))
        assert reloaded.price == 1.0, "la version gagnante (v2) ne doit pas être écrasée"


class TestStaleByStatus:
    def test_finds_only_executing_drafts_older_than_threshold(self, monkeypatch):
        table = _install_fake_db(monkeypatch)
        old = _draft(draft_id="sd-old")._confirm_to_executing()
        recent = _draft(draft_id="sd-recent")._confirm_to_executing()
        run(store_mod.insert(_draft(draft_id="sd-old"), conversation_id="c"))
        run(store_mod.compare_and_swap("sd-old", expected_version=1, new_draft=old))
        run(store_mod.insert(_draft(draft_id="sd-recent"), conversation_id="c"))
        run(store_mod.compare_and_swap("sd-recent", expected_version=1, new_draft=recent))

        table.force_updated_at("sd-old", time.time() - 3600)
        stale = run(store_mod.find_stale_by_status("EXECUTING", older_than_seconds=900))
        assert [d.draft_id for d in stale] == ["sd-old"]


class TestRealThreadConcurrencyAgainstCompareAndSwap:
    def test_ten_threads_racing_the_same_expected_version_only_one_wins(self, monkeypatch):
        _install_fake_db(monkeypatch)
        draft = _draft(draft_id="sd-race")
        run(store_mod.insert(draft, conversation_id="c"))

        results = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(10)

        def worker(i):
            barrier.wait()
            ok = run(
                store_mod.compare_and_swap(
                    draft.draft_id, expected_version=1, new_draft=draft.with_updates(price=float(i))
                )
            )
            with results_lock:
                results.append(ok)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(1 for r in results if r) == 1
        final = run(store_mod.load("sd-race"))
        assert final.version == 2
