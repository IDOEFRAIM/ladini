"""`workspace/store.py::WorkspaceStore` — persistance Postgres unique des
Workspaces (JSONB `langgraph_state`). Zéro Postgres réel : `get_db()` est
doublé par un context-manager qui expose une session `_FakeSession`
contrôlée. `WorkspaceStore._table_ready` est un attribut de CLASSE partagé
entre instances — reset obligatoire entre tests (fixture `autouse`)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agriconnect.workspace.models import Workspace
from agriconnect.workspace.store import (
    WorkspaceStore,
    _decode_state_blob,
    _encode_state_blob,
)
from tests.conftest import run


@pytest.fixture(autouse=True)
def _reset_table_ready():
    WorkspaceStore._table_ready = False
    yield
    WorkspaceStore._table_ready = False


# =====================================================================
# _encode_state_blob / _decode_state_blob
# =====================================================================

class TestEncodeDecodeStateBlob:
    def test_small_payload_is_not_compressed(self):
        json_str, metrics = _encode_state_blob({"a": 1})
        assert metrics["compressed"] is False
        assert json.loads(json_str) == {"a": 1}

    def test_large_payload_is_compressed_and_roundtrips(self):
        big_payload = {"blob": "x" * 200_000}
        json_str, metrics = _encode_state_blob(big_payload)
        assert metrics["compressed"] is True
        wrapper = json.loads(json_str)
        assert wrapper["__compressed__"] is True
        decoded = _decode_state_blob(wrapper)
        assert decoded == big_payload

    def test_decode_plain_dict_passes_through(self):
        assert _decode_state_blob({"a": 1}) == {"a": 1}

    def test_decode_non_dict_returns_it_as_is_or_empty(self):
        assert _decode_state_blob(None) == {}
        assert _decode_state_blob("not-a-dict") == "not-a-dict"

    def test_decode_truncated_marker_returns_empty(self):
        assert _decode_state_blob({"__truncated__": True}) == {}

    def test_decode_unsupported_compression_encoding_returns_empty(self):
        assert _decode_state_blob({"__compressed__": True, "encoding": "gzip", "payload": "x"}) == {}

    def test_decode_corrupted_compressed_payload_returns_empty(self):
        result = _decode_state_blob({"__compressed__": True, "encoding": "zlib+base64", "payload": "not-valid-base64-zlib!!"})
        assert result == {}


# =====================================================================
# _is_fatal_connection_error
# =====================================================================

class TestIsFatalConnectionError:
    def test_asyncpg_connection_does_not_exist_is_fatal(self):
        import asyncpg
        exc = asyncpg.exceptions.ConnectionDoesNotExistError("gone")
        assert WorkspaceStore._is_fatal_connection_error(exc) is True

    def test_generic_value_error_is_not_fatal(self):
        assert WorkspaceStore._is_fatal_connection_error(ValueError("business error")) is False

    def test_string_heuristic_detects_closed_connection_message(self):
        assert WorkspaceStore._is_fatal_connection_error(RuntimeError("the connection was closed unexpectedly")) is True

    def test_string_heuristic_detects_program_limit_message(self):
        assert WorkspaceStore._is_fatal_connection_error(RuntimeError("program limit exceeded")) is True


# =====================================================================
# Fixtures pour doubler get_db()
# =====================================================================

class _FakeResult:
    def __init__(self, *, mapping_row=None, scalar_value=None, rowcount=0):
        self._mapping_row = mapping_row
        self._scalar_value = scalar_value
        self.rowcount = rowcount

    def mappings(self):
        return SimpleNamespace(first=lambda: self._mapping_row)

    def scalar(self):
        return self._scalar_value


class _FakeSession:
    def __init__(self, results=None, raise_on_call: int | None = None, exc: Exception | None = None):
        self.executed: list = []
        self._results = list(results or [])
        self.committed = False
        self._raise_on_call = raise_on_call
        self._exc = exc or RuntimeError("db boom")
        self._call_count = 0

    async def execute(self, stmt, params=None):
        self._call_count += 1
        if self._raise_on_call is not None and self._call_count == self._raise_on_call:
            raise self._exc
        self.executed.append((str(stmt), params))
        if self._results:
            return self._results.pop(0)
        return _FakeResult()

    async def commit(self):
        self.committed = True


def _install_fake_get_db(monkeypatch, session):
    import agriconnect.workspace.store as store_mod

    class _CM:
        async def __aenter__(self_inner):
            return session

        async def __aexit__(self_inner, *exc):
            return False

    monkeypatch.setattr(store_mod, "get_db", lambda: _CM())


# =====================================================================
# get()
# =====================================================================

class TestWorkspaceStoreGet:
    def test_returns_none_when_table_bootstrap_fails(self, monkeypatch):
        session = _FakeSession(raise_on_call=1)  # la 1re execute() (DDL) échoue
        _install_fake_get_db(monkeypatch, session)
        store = WorkspaceStore()
        assert run(store.get("phone-1")) is None
        assert WorkspaceStore._table_ready is False

    def test_returns_none_when_row_not_found(self, monkeypatch):
        session = _FakeSession(results=[_FakeResult(), _FakeResult(), _FakeResult(mapping_row=None)])
        _install_fake_get_db(monkeypatch, session)
        store = WorkspaceStore()
        assert run(store.get("phone-1")) is None

    def test_returns_a_workspace_when_row_found(self, monkeypatch):
        row = {
            "workspace_id": "phone-1", "workspace_type": "producer", "active_goal": "",
            "active_form": None, "locked_agent": None,
            "metadata": json.dumps({"some": "meta"}),
            "langgraph_state": json.dumps({"current_goal": "X"}),
            "updated_at": 123.0,
        }
        # 3 appels pour _ensure_table (1 DDL + 2 ALTER), puis le SELECT (4e appel).
        session = _FakeSession(results=[_FakeResult(), _FakeResult(), _FakeResult(), _FakeResult(mapping_row=row)])
        _install_fake_get_db(monkeypatch, session)
        store = WorkspaceStore()
        ws = run(store.get("phone-1"))
        assert isinstance(ws, Workspace)
        assert ws.agent_state == {"current_goal": "X"}

    def test_legacy_state_under_metadata_key_is_used_as_fallback(self, monkeypatch):
        from agriconnect.workspace.metadata import LANGGRAPH_STATE_KEY
        row = {
            "workspace_id": "phone-1", "workspace_type": "producer", "active_goal": "",
            "active_form": None, "locked_agent": None,
            "metadata": json.dumps({LANGGRAPH_STATE_KEY: {"legacy_state": True}}),
            "langgraph_state": json.dumps({}),
            "updated_at": 123.0,
        }
        session = _FakeSession(results=[_FakeResult(), _FakeResult(), _FakeResult(), _FakeResult(mapping_row=row)])
        _install_fake_get_db(monkeypatch, session)
        store = WorkspaceStore()
        ws = run(store.get("phone-1"))
        assert ws.agent_state == {"legacy_state": True}

    def test_fatal_connection_error_triggers_a_row_reset_and_returns_none(self, monkeypatch):
        import asyncpg

        class _RaisingOnSelectSession(_FakeSession):
            async def execute(self, stmt, params=None):
                self._call_count += 1
                if self._call_count == 4:  # 1-3: bootstrap (DDL+2 ALTER), 4: SELECT
                    raise asyncpg.exceptions.ConnectionDoesNotExistError("gone")
                self.executed.append((str(stmt), params))
                if self._results:
                    return self._results.pop(0)
                return _FakeResult()

        session = _RaisingOnSelectSession()
        _install_fake_get_db(monkeypatch, session)
        store = WorkspaceStore()
        assert run(store.get("phone-1")) is None
        # _reset_workspace_row a bien tenté d'écrire (UPDATE puis, si rowcount=0, INSERT).
        assert session.committed is True

    def test_non_fatal_error_during_select_returns_none_without_reset(self, monkeypatch):
        session = _FakeSession(raise_on_call=4)  # 1-3: bootstrap, 4: SELECT explose (générique)
        _install_fake_get_db(monkeypatch, session)
        store = WorkspaceStore()
        assert run(store.get("phone-1")) is None
        # Seuls les 3 appels de bootstrap (_ensure_table) ont abouti — aucune
        # tentative d'écriture de réinitialisation (_reset_workspace_row).
        assert len(session.executed) == 3


# =====================================================================
# save()
# =====================================================================

class TestWorkspaceStoreSave:
    def test_returns_false_when_table_bootstrap_fails(self, monkeypatch):
        session = _FakeSession(raise_on_call=1)
        _install_fake_get_db(monkeypatch, session)
        store = WorkspaceStore()
        ws = Workspace(workspace_id="phone-1", agent_state={"x": 1})
        assert run(store.save(ws)) is False

    # NB : 3 résultats "bulle" pour _ensure_table (1 DDL + 2 ALTER), puis le
    # 4e résultat porte la vraie valeur pour SELECT_FOR_UPDATE (scalar()).

    def test_new_workspace_is_inserted(self, monkeypatch):
        session = _FakeSession(results=[_FakeResult(), _FakeResult(), _FakeResult(), _FakeResult(scalar_value=None)])
        _install_fake_get_db(monkeypatch, session)
        store = WorkspaceStore()
        ws = Workspace(workspace_id="phone-1", agent_state={"x": 1})
        assert run(store.save(ws)) is True
        last_query, _ = session.executed[-1]
        assert "INSERT INTO" in last_query

    def test_existing_workspace_is_updated(self, monkeypatch):
        session = _FakeSession(results=[_FakeResult(), _FakeResult(), _FakeResult(), _FakeResult(scalar_value="phone-1")])
        _install_fake_get_db(monkeypatch, session)
        store = WorkspaceStore()
        ws = Workspace(workspace_id="phone-1", agent_state={"x": 1})
        assert run(store.save(ws)) is True
        last_query, _ = session.executed[-1]
        assert "UPDATE agri_workspaces" in last_query

    def test_oversized_metadata_is_saved_as_empty_without_failing(self, monkeypatch):
        session = _FakeSession(results=[_FakeResult(), _FakeResult(), _FakeResult(), _FakeResult(scalar_value=None)])
        _install_fake_get_db(monkeypatch, session)
        store = WorkspaceStore()
        ws = Workspace(workspace_id="phone-1", metadata={"huge": "x" * 100_000}, agent_state={})
        assert run(store.save(ws)) is True
        _, params = session.executed[-1]
        assert json.loads(params["metadata"]) == {}

    def test_oversized_state_after_compression_is_truncated(self, monkeypatch):
        import agriconnect.workspace.store as store_mod
        monkeypatch.setattr(store_mod, "_MAX_STATE_BYTES", 100)
        session = _FakeSession(results=[_FakeResult(), _FakeResult(), _FakeResult(), _FakeResult(scalar_value=None)])
        _install_fake_get_db(monkeypatch, session)
        store = WorkspaceStore()
        ws = Workspace(workspace_id="phone-1", agent_state={"blob": "y" * 10_000})
        assert run(store.save(ws)) is True
        _, params = session.executed[-1]
        saved_state = json.loads(params["langgraph_state"])
        assert saved_state.get("__truncated__") is True

    def test_exception_during_upsert_returns_false(self, monkeypatch):
        session = _FakeSession(results=[_FakeResult(), _FakeResult(), _FakeResult()], raise_on_call=4)
        _install_fake_get_db(monkeypatch, session)
        store = WorkspaceStore()
        ws = Workspace(workspace_id="phone-1", agent_state={"x": 1})
        assert run(store.save(ws)) is False

    def test_legacy_state_under_metadata_key_is_used_when_agent_state_empty(self, monkeypatch):
        from agriconnect.workspace.metadata import LANGGRAPH_STATE_KEY
        session = _FakeSession(results=[_FakeResult(), _FakeResult(), _FakeResult(), _FakeResult(scalar_value=None)])
        _install_fake_get_db(monkeypatch, session)
        store = WorkspaceStore()
        ws = Workspace(workspace_id="phone-1", agent_state={}, metadata={LANGGRAPH_STATE_KEY: {"legacy": True}})
        assert run(store.save(ws)) is True
        _, params = session.executed[-1]
        assert json.loads(params["langgraph_state"]) == {"legacy": True}
