"""Déduplication RÉELLE côté serveur MCP (2026-09-03, phase 2 de la clôture
transactionnelle PROCUREMENT) — `services/database/mcp_idempotency_store.py`
+ le câblage dans `infrastructure/mcp/runtime.py::AgriDBMCPServer.call_tool`.

Portée honnête (identique au principe déjà posé dans
`tests/architecture/test_procurement_draft_persistence.py`) : ce dépôt n'a
aucune infrastructure Postgres de test réelle — ces tests utilisent un faux
moteur qui reproduit fidèlement la sémantique `INSERT ... ON CONFLICT DO
NOTHING` / `UPDATE ... WHERE ...` des requêtes réelles, jamais une vraie
base."""
from __future__ import annotations

import threading
import types
from typing import Any, Dict, Optional

import pytest

from ladini.services.database import mcp_idempotency_store as store_mod
from ladini.services.database.mcp_idempotency_store import IdempotencyOutcome
from tests.conftest import run

# =====================================================================
# FAUX MOTEUR SQL
# =====================================================================


class _FakeIdempotencyTable:
    def __init__(self) -> None:
        self._rows: Dict[tuple, Dict[str, Any]] = {}
        self.lock = threading.Lock()

    def insert_pending(self, key: str, tool: str, request_hash: str) -> int:
        with self.lock:
            pk = (key, tool)
            if pk in self._rows:
                return 0
            self._rows[pk] = {
                "idempotency_key": key,
                "tool_name": tool,
                "request_hash": request_hash,
                "status": "PENDING",
                "external_result": None,
            }
            return 1

    def select(self, key: str, tool: str) -> Optional[types.SimpleNamespace]:
        with self.lock:
            row = self._rows.get((key, tool))
            return types.SimpleNamespace(**row) if row else None

    def update_status(self, key: str, tool: str, status: str, external_result) -> int:
        with self.lock:
            pk = (key, tool)
            if pk not in self._rows:
                return 0
            self._rows[pk]["status"] = status
            self._rows[pk]["external_result"] = external_result
            return 1

    def reopen_failed(self, key: str, tool: str, request_hash: str) -> int:
        with self.lock:
            pk = (key, tool)
            row = self._rows.get(pk)
            if row is None or row["status"] != "FAILED" or row["request_hash"] != request_hash:
                return 0
            row["status"] = "PENDING"
            row["external_result"] = None
            return 1


class _FakeResult:
    def __init__(self, rowcount: int = 0, rows=None) -> None:
        self.rowcount = rowcount
        self._rows = rows or []

    def mappings(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    def __init__(self, table: _FakeIdempotencyTable) -> None:
        self._table = table
        self._pending_op = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def commit(self):
        pass

    async def rollback(self):
        pass

    async def execute(self, sql, params):
        raw = str(sql)
        if raw.startswith("INSERT") and "PENDING" in raw:
            rc = self._table.insert_pending(
                params["idempotency_key"], params["tool_name"], params["request_hash"]
            )
            return _FakeResult(rowcount=rc)
        if raw.startswith("SELECT"):
            row = self._table.select(params["idempotency_key"], params["tool_name"])
            return _FakeResult(rows=[row] if row else [])
        if raw.startswith("UPDATE") and "status = 'PENDING'" in raw:
            rc = self._table.reopen_failed(
                params["idempotency_key"], params["tool_name"], params["request_hash"]
            )
            return _FakeResult(rowcount=rc)
        if raw.startswith("UPDATE"):
            rc = self._table.update_status(
                params["idempotency_key"],
                params["tool_name"],
                params["status"],
                params["external_result"],
            )
            return _FakeResult(rowcount=rc)
        raise AssertionError(f"Requête SQL inattendue : {raw!r}")


def _install_fake_db(monkeypatch, table: Optional[_FakeIdempotencyTable] = None):
    table = table or _FakeIdempotencyTable()
    monkeypatch.setattr(store_mod, "get_sessionmaker", lambda: (lambda: _FakeSession(table)))
    return table


# =====================================================================
# SÉMANTIQUE — claim / complete / fail
# =====================================================================


class TestClaimCompleteFailSemantics:
    def test_a_fresh_key_is_claimed(self, monkeypatch):
        _install_fake_db(monkeypatch)
        result = run(store_mod.claim("k1", "create_auction", "hashA"))
        assert result.outcome is IdempotencyOutcome.CLAIMED

    def test_completing_then_replaying_the_same_key_and_payload_returns_the_stored_result(
        self, monkeypatch
    ):
        _install_fake_db(monkeypatch)
        run(store_mod.claim("k2", "create_auction", "hashA"))
        run(store_mod.complete("k2", "create_auction", {"auction_id": "auc-1"}))

        replay = run(store_mod.claim("k2", "create_auction", "hashA"))
        assert replay.outcome is IdempotencyOutcome.REPLAY
        assert replay.stored_result == {"auction_id": "auc-1"}

    def test_same_key_different_payload_is_a_conflict_never_resolved_silently(
        self, monkeypatch
    ):
        _install_fake_db(monkeypatch)
        run(store_mod.claim("k3", "create_auction", "hashA"))
        run(store_mod.complete("k3", "create_auction", {"auction_id": "auc-1"}))

        conflict = run(store_mod.claim("k3", "create_auction", "hashB"))
        assert conflict.outcome is IdempotencyOutcome.CONFLICT
        assert conflict.stored_result is None

    def test_a_still_pending_key_is_reported_in_progress_not_double_claimed(
        self, monkeypatch
    ):
        _install_fake_db(monkeypatch)
        run(store_mod.claim("k4", "create_auction", "hashA"))  # jamais complété/failed

        second = run(store_mod.claim("k4", "create_auction", "hashA"))
        assert second.outcome is IdempotencyOutcome.IN_PROGRESS

    def test_a_confirmed_failure_can_be_retried_under_the_same_key(self, monkeypatch):
        """FAILED = pas d'effet externe (mandat §2.5/§1.1) — un retry est
        légitime, sous la MÊME clé, jamais une nouvelle ligne."""
        _install_fake_db(monkeypatch)
        run(store_mod.claim("k5", "create_auction", "hashA"))
        run(store_mod.fail("k5", "create_auction", "stock insuffisant"))

        retry = run(store_mod.claim("k5", "create_auction", "hashA"))
        assert retry.outcome is IdempotencyOutcome.CLAIMED

        run(store_mod.complete("k5", "create_auction", {"auction_id": "auc-2"}))
        replay = run(store_mod.claim("k5", "create_auction", "hashA"))
        assert replay.outcome is IdempotencyOutcome.REPLAY
        assert replay.stored_result == {"auction_id": "auc-2"}

    def test_a_failed_key_retried_with_a_different_payload_is_still_a_conflict(
        self, monkeypatch
    ):
        _install_fake_db(monkeypatch)
        run(store_mod.claim("k6", "create_auction", "hashA"))
        run(store_mod.fail("k6", "create_auction", "erreur"))

        conflict = run(store_mod.claim("k6", "create_auction", "hashB"))
        assert conflict.outcome is IdempotencyOutcome.CONFLICT

    def test_the_same_key_on_a_different_tool_name_is_independent(self, monkeypatch):
        _install_fake_db(monkeypatch)
        run(store_mod.claim("shared-key", "create_auction", "hashA"))
        other_tool = run(store_mod.claim("shared-key", "create_order", "hashA"))
        assert other_tool.outcome is IdempotencyOutcome.CLAIMED

    def test_a_missing_sessionmaker_reports_unavailable_never_raises(self, monkeypatch):
        monkeypatch.setattr(store_mod, "get_sessionmaker", lambda: None)
        result = run(store_mod.claim("k7", "create_auction", "hashA"))
        assert result.outcome is IdempotencyOutcome.UNAVAILABLE
        assert run(store_mod.complete("k7", "create_auction", {})) is False
        assert run(store_mod.fail("k7", "create_auction", "x")) is False

    def test_request_hash_is_stable_for_equivalent_payloads_and_differs_otherwise(self):
        h1 = store_mod.compute_request_hash({"product": "maïs", "quantity": 50})
        h2 = store_mod.compute_request_hash({"quantity": 50, "product": "maïs"})
        h3 = store_mod.compute_request_hash({"product": "maïs", "quantity": 51})
        assert h1 == h2
        assert h1 != h3


# =====================================================================
# CONCURRENCE RÉELLE — vrais threads OS
# =====================================================================


class TestRealThreadConcurrency:
    def test_ten_threads_racing_the_same_key_and_payload_only_one_is_claimed(
        self, monkeypatch
    ):
        _install_fake_db(monkeypatch)
        results = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(10)

        def worker():
            barrier.wait()
            outcome = run(store_mod.claim("race-key", "create_auction", "hashA")).outcome
            with results_lock:
                results.append(outcome)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        claimed = [o for o in results if o is IdempotencyOutcome.CLAIMED]
        in_progress = [o for o in results if o is IdempotencyOutcome.IN_PROGRESS]
        assert len(claimed) == 1, f"exactement un CLAIMED attendu, obtenu {results}"
        assert len(in_progress) == 9


# =====================================================================
# CÂBLAGE — AgriDBMCPServer.call_tool
# =====================================================================


class TestCallToolIdempotencyWiring:
    """Vérifie le câblage dans `infrastructure/mcp/runtime.py`, PAS la
    logique du store (déjà couverte ci-dessus) — `mcp_idempotency_store` est
    monkeypatché directement pour isoler le test du reste de la frontière
    MCP (sécurité/preflight, déjà couverts par `test_mcp_hardening.py`)."""

    _TOOL_NAME = "get_user_by_phone"  # déjà autorisé (voir test_mcp_hardening.py)

    def _install(self, monkeypatch, *, claim_outcome, stored_result=None, handler=None, call_counter=None):
        import ladini.infrastructure.mcp.runtime as runtime_mod
        from ladini.services.database.mcp_idempotency_store import IdempotencyClaim

        async def _fake_claim(key, tool, request_hash):
            return IdempotencyClaim(outcome=claim_outcome, stored_result=stored_result)

        completed = {"called": False, "args": None}
        failed = {"called": False, "args": None}

        async def _fake_complete(key, tool, external_result):
            completed["called"] = True
            completed["args"] = (key, tool, external_result)
            return True

        async def _fake_fail(key, tool, error=None):
            failed["called"] = True
            failed["args"] = (key, tool, error)
            return True

        monkeypatch.setattr(runtime_mod.mcp_idempotency_store, "claim", _fake_claim)
        monkeypatch.setattr(runtime_mod.mcp_idempotency_store, "complete", _fake_complete)
        monkeypatch.setattr(runtime_mod.mcp_idempotency_store, "fail", _fake_fail)

        if handler is not None:
            monkeypatch.setattr(
                runtime_mod.AgriDBMCPServer, "_resolve_tool_fn", lambda self, name: handler
            )
        return completed, failed

    def test_claimed_success_calls_the_handler_once_and_marks_completed(self, monkeypatch):
        from ladini.infrastructure.mcp.runtime import AgriDBMCPServer
        from ladini.services.database.mcp_idempotency_store import IdempotencyOutcome

        call_count = {"n": 0}

        async def handler(**kwargs):
            call_count["n"] += 1
            return {"auction_id": "auc-1"}

        completed, failed = self._install(
            monkeypatch, claim_outcome=IdempotencyOutcome.CLAIMED, handler=handler
        )

        srv = AgriDBMCPServer()
        result = run(
            srv.call_tool(
                self._TOOL_NAME, {"phone": "+22670000000", "_idempotency_key": "k1"}
            )
        )
        assert call_count["n"] == 1
        assert completed["called"] is True
        assert failed["called"] is False
        assert result == {"auction_id": "auc-1"}

    def test_claimed_handler_failure_marks_failed_and_reraises(self, monkeypatch):
        from ladini.infrastructure.mcp.runtime import AgriDBMCPServer
        from ladini.services.database.mcp_idempotency_store import IdempotencyOutcome

        async def handler(**kwargs):
            raise RuntimeError("stock insuffisant")

        completed, failed = self._install(
            monkeypatch, claim_outcome=IdempotencyOutcome.CLAIMED, handler=handler
        )

        srv = AgriDBMCPServer()
        with pytest.raises(RuntimeError, match="stock insuffisant"):
            run(
                srv.call_tool(
                    self._TOOL_NAME, {"phone": "+22670000000", "_idempotency_key": "k2"}
                )
            )
        assert completed["called"] is False
        assert failed["called"] is True

    def test_replay_never_calls_the_handler_and_returns_the_stored_result(self, monkeypatch):
        from ladini.infrastructure.mcp.runtime import AgriDBMCPServer
        from ladini.services.database.mcp_idempotency_store import IdempotencyOutcome

        call_count = {"n": 0}

        async def handler(**kwargs):
            call_count["n"] += 1
            return {"auction_id": "SHOULD_NOT_BE_CALLED"}

        self._install(
            monkeypatch,
            claim_outcome=IdempotencyOutcome.REPLAY,
            stored_result={"auction_id": "auc-original"},
            handler=handler,
        )

        srv = AgriDBMCPServer()
        result = run(
            srv.call_tool(
                self._TOOL_NAME, {"phone": "+22670000000", "_idempotency_key": "k3"}
            )
        )
        assert call_count["n"] == 0, "REPLAY ne doit JAMAIS ré-exécuter l'outil"
        assert result == {"auction_id": "auc-original"}

    def test_conflict_never_calls_the_handler_and_raises_explicitly(self, monkeypatch):
        from ladini.infrastructure.mcp.runtime import AgriDBMCPServer
        from ladini.services.database.mcp_idempotency_store import IdempotencyOutcome

        call_count = {"n": 0}

        async def handler(**kwargs):
            call_count["n"] += 1
            return {}

        self._install(monkeypatch, claim_outcome=IdempotencyOutcome.CONFLICT, handler=handler)

        srv = AgriDBMCPServer()
        with pytest.raises(RuntimeError, match="idempotency_conflict"):
            run(
                srv.call_tool(
                    self._TOOL_NAME, {"phone": "+22670000000", "_idempotency_key": "k4"}
                )
            )
        assert call_count["n"] == 0

    def test_in_progress_never_calls_the_handler_and_raises_a_transient_marked_error(
        self, monkeypatch
    ):
        from ladini.infrastructure.mcp.runtime import AgriDBMCPServer
        from ladini.services.database.mcp_idempotency_store import IdempotencyOutcome

        call_count = {"n": 0}

        async def handler(**kwargs):
            call_count["n"] += 1
            return {}

        self._install(monkeypatch, claim_outcome=IdempotencyOutcome.IN_PROGRESS, handler=handler)

        srv = AgriDBMCPServer()
        with pytest.raises(RuntimeError, match="idempotency_in_progress"):
            run(
                srv.call_tool(
                    self._TOOL_NAME, {"phone": "+22670000000", "_idempotency_key": "k5"}
                )
            )
        assert call_count["n"] == 0

    def test_unavailable_degrades_to_executing_without_dedup_guarantee(self, monkeypatch):
        """DB de dédup injoignable : exécute quand même (best-effort, même
        discipline que le reste du chantier) plutôt que de bloquer tout le
        flux procurement pour une panne d'observabilité annexe."""
        from ladini.infrastructure.mcp.runtime import AgriDBMCPServer
        from ladini.services.database.mcp_idempotency_store import IdempotencyOutcome

        call_count = {"n": 0}

        async def handler(**kwargs):
            call_count["n"] += 1
            return {"ok": True}

        completed, failed = self._install(
            monkeypatch, claim_outcome=IdempotencyOutcome.UNAVAILABLE, handler=handler
        )

        srv = AgriDBMCPServer()
        result = run(
            srv.call_tool(
                self._TOOL_NAME, {"phone": "+22670000000", "_idempotency_key": "k6"}
            )
        )
        assert call_count["n"] == 1
        assert result == {"ok": True}
        # Pas de dédup possible pour cette tentative -> ni complete ni fail.
        assert completed["called"] is False
        assert failed["called"] is False

    def test_no_idempotency_key_bypasses_the_mechanism_entirely(self, monkeypatch):
        """No-op garanti pour les 15+ outils qui ne posent jamais de clé —
        aucun appel à `claim`/`complete`/`fail`."""
        import ladini.infrastructure.mcp.runtime as runtime_mod
        from ladini.infrastructure.mcp.runtime import AgriDBMCPServer

        claim_calls = {"n": 0}

        async def _claim_should_not_be_called(*a, **kw):
            claim_calls["n"] += 1
            raise AssertionError("claim() ne doit jamais être appelé sans _idempotency_key")

        monkeypatch.setattr(runtime_mod.mcp_idempotency_store, "claim", _claim_should_not_be_called)

        async def handler(**kwargs):
            return {"ok": True}

        monkeypatch.setattr(AgriDBMCPServer, "_resolve_tool_fn", lambda self, name: handler)

        srv = AgriDBMCPServer()
        result = run(srv.call_tool(self._TOOL_NAME, {"phone": "+22670000000"}))
        assert claim_calls["n"] == 0
        assert result == {"ok": True}
