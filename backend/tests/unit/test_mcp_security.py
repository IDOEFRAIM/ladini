"""`infrastructure/mcp/security.py` — le Shield MCP : LA frontière d'autorisation
entre l'agent (LLM) et toute écriture/lecture en base.

Priorité sécurité maximale : c'est ici qu'un outil DB_SCHEMA_MODIFY est
bloqué, qu'un paiement (HIGH risk) exige confirmation humaine, et qu'une
injection SQL est détectée. Un bug de régression corrigé pendant l'écriture
de ces tests : `SQL_INJECTION_PATTERNS` utilisait des regex doublement
échappées (`r"\\\\bDROP..."`) qui ne matchaient JAMAIS de texte réel — voir
le commit qui accompagne ce fichier.
"""
from __future__ import annotations

import pytest

from tests.conftest import run

# =====================================================================
# _guess_scope — repli fail-safe pour un outil non cartographié
# =====================================================================

class TestGuessScope:
    def test_read_prefixes_map_to_read_only(self):
        from ladini.infrastructure.mcp.security import PermissionScope, _guess_scope
        for name in ("get_thing", "list_things", "search_stuff", "fetch_x", "count_y"):
            assert _guess_scope(name) == PermissionScope.DB_READ_ONLY

    def test_schema_prefixes_map_to_schema_modify(self):
        from ladini.infrastructure.mcp.security import PermissionScope, _guess_scope
        for name in ("migrate_v2", "drop_index", "alter_column", "truncate_logs"):
            assert _guess_scope(name) == PermissionScope.DB_SCHEMA_MODIFY

    def test_unknown_prefix_defaults_to_write_fail_safe(self):
        """Un outil inconnu ne doit JAMAIS être auto-autorisé en lecture —
        le défaut fail-safe est écriture (soumis à scrutin), pas confiance."""
        from ladini.infrastructure.mcp.security import PermissionScope, _guess_scope
        assert _guess_scope("do_something_destructive") == PermissionScope.DB_DATA_WRITE


class TestEnsureScopesFilled:
    def test_is_idempotent_and_does_not_raise(self):
        from ladini.infrastructure.mcp.security import ensure_scopes_filled
        ensure_scopes_filled()
        ensure_scopes_filled()  # 2e appel : no-op silencieux

    def test_autofill_survives_a_broken_handlers_import(self, monkeypatch):
        import ladini.infrastructure.mcp.security as sec

        monkeypatch.setattr(sec, "_scopes_filled", False)
        monkeypatch.setattr(
            sec.importlib, "import_module",
            lambda name: (_ for _ in ()).throw(ImportError("boom")),
        )
        sec._autofill_tool_scopes()  # ne doit pas lever
        assert sec._scopes_filled is True


# =====================================================================
# ToolRateLimiter
# =====================================================================

class TestToolRateLimiter:
    def test_allows_calls_under_the_limit(self):
        from ladini.infrastructure.mcp.security import ToolRateLimiter
        limiter = ToolRateLimiter(max_calls=3, window_seconds=60)
        for _ in range(3):
            allowed, _ = limiter.check("user:tool")
            assert allowed is True

    def test_denies_once_the_limit_is_reached(self):
        from ladini.infrastructure.mcp.security import ToolRateLimiter
        limiter = ToolRateLimiter(max_calls=2, window_seconds=60)
        limiter.check("k")
        limiter.check("k")
        allowed, reason = limiter.check("k")
        assert allowed is False
        assert "rate_limit_exceeded" in reason

    def test_different_keys_have_independent_budgets(self):
        from ladini.infrastructure.mcp.security import ToolRateLimiter
        limiter = ToolRateLimiter(max_calls=1, window_seconds=60)
        assert limiter.check("a")[0] is True
        assert limiter.check("b")[0] is True

    def test_stale_events_are_evicted_from_the_sliding_window(self):
        from ladini.infrastructure.mcp.security import ToolRateLimiter
        limiter = ToolRateLimiter(max_calls=1, window_seconds=60)
        limiter.check("k")
        # Force l'horodatage du seul événement hors fenêtre.
        limiter._events["k"][0] = limiter._events["k"][0] - 61
        allowed, _ = limiter.check("k")
        assert allowed is True

    def test_prunes_stale_keys_once_max_keys_is_exceeded(self):
        from ladini.infrastructure.mcp.security import ToolRateLimiter
        limiter = ToolRateLimiter(max_calls=100, window_seconds=1, max_keys=128)
        for i in range(130):
            limiter.check(f"key-{i}")
        # Vieillir tous les événements pour qu'ils soient élagables.
        for events in limiter._events.values():
            if events:
                events[-1] -= 1000
        limiter._prune_stale(limiter._events.get("key-0", [0])[0] + 1000 if limiter._events else 0)
        # Ne doit pas lever ; le test vérifie surtout l'absence de crash sur un grand volume.
        assert isinstance(limiter._events, dict)

    def test_constructor_clamps_degenerate_values(self):
        from ladini.infrastructure.mcp.security import ToolRateLimiter
        limiter = ToolRateLimiter(max_calls=0, window_seconds=0, max_keys=0)
        assert limiter.max_calls == 1
        assert limiter.window_seconds == 1.0
        assert limiter.max_keys == 128


# =====================================================================
# ToolExecutionPolicy
# =====================================================================

class TestSanitizeArguments:
    def test_strips_null_bytes_and_surrounding_whitespace(self):
        from ladini.infrastructure.mcp.security import ToolExecutionPolicy
        result = ToolExecutionPolicy.sanitize_arguments({"x": "  hello\x00world  "})
        assert result["x"] == "helloworld"

    def test_truncates_overly_long_strings(self):
        from ladini.infrastructure.mcp.security import ToolExecutionPolicy
        result = ToolExecutionPolicy.sanitize_arguments({"x": "a" * 5000})
        assert len(result["x"]) == 4000

    def test_recurses_into_nested_dicts_and_lists(self):
        from ladini.infrastructure.mcp.security import ToolExecutionPolicy
        result = ToolExecutionPolicy.sanitize_arguments({
            "a": {"b": "  y\x00  "},
            "c": ["  z\x00  ", 5],
        })
        assert result["a"]["b"] == "y"
        assert result["c"] == ["z", 5]

    def test_leaves_non_string_scalars_untouched(self):
        from ladini.infrastructure.mcp.security import ToolExecutionPolicy
        result = ToolExecutionPolicy.sanitize_arguments({"n": 42, "f": 3.14, "b": True, "none": None})
        assert result == {"n": 42, "f": 3.14, "b": True, "none": None}


class TestEstimateTokens:
    def test_estimates_roughly_chars_over_four(self):
        from ladini.infrastructure.mcp.security import ToolExecutionPolicy
        assert ToolExecutionPolicy._estimate_tokens({"x": "abcd" * 10}) >= 1

    def test_returns_zero_on_unserializable_payload(self):
        from ladini.infrastructure.mcp.security import ToolExecutionPolicy
        circular: dict = {}
        circular["self"] = circular
        assert ToolExecutionPolicy._estimate_tokens(circular) == 0


class TestToolExecutionPolicyExecute:
    def _policy(self):
        from ladini.infrastructure.mcp.security import (
            MCPToolRegistry,
            ToolExecutionPolicy,
        )
        return ToolExecutionPolicy(registry=MCPToolRegistry(), max_calls_per_minute=1000)

    def test_success_path_returns_an_ok_envelope(self):
        policy = self._policy()

        async def handler(**kwargs):
            return {"result": "ok", **kwargs}

        envelope = run(policy.execute("get_user_profile", handler, {"phone": "+2260"}, user_id="u1"))
        assert envelope["ok"] is True
        assert envelope["data"]["phone"] == "+2260"
        assert envelope["meta"]["timed_out"] is False

    def test_timeout_raises_and_records_timed_out_meta(self):
        import asyncio
        policy = self._policy()

        async def slow_handler(**kwargs):
            await asyncio.sleep(10)

        from ladini.infrastructure.mcp.security import ToolExecutionTimeout
        with pytest.raises(ToolExecutionTimeout):
            run(policy.execute("get_user_profile", slow_handler, {}, timeout_seconds=0.01))

    def test_rate_limit_exceeded_raises_permission_denied(self):
        from ladini.infrastructure.mcp.security import (
            MCPToolRegistry,
            PermissionDenied,
            ToolExecutionPolicy,
        )
        policy = ToolExecutionPolicy(registry=MCPToolRegistry(), max_calls_per_minute=1)

        async def handler(**kwargs):
            return {}

        run(policy.execute("get_user_profile", handler, {}, user_id="u1"))
        with pytest.raises(PermissionDenied):
            run(policy.execute("get_user_profile", handler, {}, user_id="u1"))

    def test_per_call_timeout_overrides_registry_default(self):
        policy = self._policy()

        async def handler(**kwargs):
            return {}

        # Ne doit pas lever malgré un default énorme si l'override est généreux.
        envelope = run(policy.execute("get_user_profile", handler, {}, timeout_seconds=5))
        assert envelope["ok"] is True


class TestGetExecutionPolicy:
    def test_returns_a_singleton(self, monkeypatch):
        import ladini.infrastructure.mcp.security as sec
        monkeypatch.setattr(sec, "_GLOBAL_EXECUTION_POLICY", None)
        p1 = sec.get_execution_policy()
        p2 = sec.get_execution_policy()
        assert p1 is p2

    def test_applies_valid_json_timeout_overrides_from_env(self, monkeypatch):
        import ladini.infrastructure.mcp.security as sec
        monkeypatch.setattr(sec, "_GLOBAL_EXECUTION_POLICY", None)
        monkeypatch.setenv("MCP_TOOL_TIMEOUT_OVERRIDES", '{"search_agronomy_docs": 42}')
        policy = sec.get_execution_policy()
        assert policy._overrides["search_agronomy_docs"] == 42.0

    def test_ignores_malformed_json_overrides_without_crashing(self, monkeypatch):
        import ladini.infrastructure.mcp.security as sec
        monkeypatch.setattr(sec, "_GLOBAL_EXECUTION_POLICY", None)
        monkeypatch.setenv("MCP_TOOL_TIMEOUT_OVERRIDES", "{not-json")
        policy = sec.get_execution_policy()
        assert policy is not None

    def test_falls_back_to_default_timeout_on_invalid_env_value(self, monkeypatch):
        import ladini.infrastructure.mcp.security as sec
        monkeypatch.setattr(sec, "_GLOBAL_EXECUTION_POLICY", None)
        monkeypatch.setenv("MCP_DEFAULT_TOOL_TIMEOUT", "not-a-float")
        policy = sec.get_execution_policy()
        assert policy._default_timeout == 30.0


# =====================================================================
# MCPToolRegistry
# =====================================================================

class TestMCPToolRegistry:
    def test_has_tool_and_get_tool(self):
        from ladini.infrastructure.mcp.security import MCPToolRegistry
        reg = MCPToolRegistry()
        assert reg.has_tool("get_user_profile") is True
        assert reg.has_tool("ghost_tool") is False
        assert reg.get_tool("ghost_tool") is None

    def test_list_tools_is_sorted_by_name(self):
        from ladini.infrastructure.mcp.security import MCPToolRegistry
        reg = MCPToolRegistry()
        names = [t["name"] for t in reg.list_tools()]
        assert names == sorted(names)

    def test_list_tools_filters_by_server(self):
        from ladini.infrastructure.mcp.security import MCPServerKind, MCPToolRegistry
        reg = MCPToolRegistry()
        items = reg.list_tools(server=MCPServerKind.DB)
        assert all(i["server"] == "db" for i in items)

    def test_sync_discovered_tools_never_overwrites_an_already_known_tool(self):
        from ladini.infrastructure.mcp.security import MCPServerKind, MCPToolRegistry
        reg = MCPToolRegistry()
        before = reg.get_tool("get_user_profile")
        reg.sync_discovered_tools(MCPServerKind.DB, [
            {"name": "get_user_profile", "description": "SHOULD NOT OVERWRITE"},
        ])
        assert reg.get_tool("get_user_profile") is before, "un outil déjà connu ne doit jamais être réécrasé"

    def test_sync_discovered_tools_skips_blank_names(self):
        from ladini.infrastructure.mcp.security import MCPServerKind, MCPToolRegistry
        reg = MCPToolRegistry()
        count_before = len(reg._tools)
        reg.sync_discovered_tools(MCPServerKind.DB, [{"name": "", "description": "x"}])
        assert len(reg._tools) == count_before

    def test_sync_discovered_tools_never_grants_write_privileges_to_an_unmapped_tool(self):
        """Chantier fail-closed (2026-08-27) : `sync_discovered_tools`
        attribuait auparavant `DB_DATA_WRITE` par défaut à tout outil absent
        de `TOOL_SCOPE_MAP` — une posture fail-OPEN isolée. Un outil
        totalement inconnu ne doit désormais RECEVOIR AUCUN privilège, pas
        même en lecture : il ne doit tout simplement pas être enregistré."""
        from ladini.infrastructure.mcp.security import MCPServerKind, MCPToolRegistry
        reg = MCPToolRegistry()
        count_before = len(reg._tools)
        reg.sync_discovered_tools(MCPServerKind.DB, [
            {"name": "brand_new_tool", "description": "a fresh, unmapped tool"},
        ])
        assert reg.has_tool("brand_new_tool") is False
        assert reg.get_tool("brand_new_tool") is None
        assert len(reg._tools) == count_before, "aucune entrée ne doit être créée pour un outil non cartographié"

    def test_sync_discovered_tools_logs_a_warning_for_an_unmapped_tool(self, caplog):
        import logging

        from ladini.infrastructure.mcp.security import MCPServerKind, MCPToolRegistry

        reg = MCPToolRegistry()
        with caplog.at_level(logging.WARNING, logger="MCP.Core.Security"):
            reg.sync_discovered_tools(MCPServerKind.DB, [
                {"name": "brand_new_tool", "description": "a fresh, unmapped tool"},
            ])
        assert any(
            "brand_new_tool" in r.message and "MCP_SCOPE_GAP" in r.message
            for r in caplog.records
        )

    def test_sync_discovered_tools_registers_a_newly_discovered_but_mapped_tool_with_its_declared_scope(self):
        """Non-régression : un outil absent du registre à l'instanciation
        (ex: ajouté à TOOL_SCOPE_MAP après coup, ou registre reconstruit
        avant que `register_defaults` n'ait tourné) mais déjà déclaré dans
        TOOL_SCOPE_MAP doit être enregistré avec SON scope réel, pas un
        DB_DATA_WRITE générique."""
        from ladini.infrastructure.mcp.security import (
            MCPServerKind,
            MCPToolRegistry,
            PermissionScope,
        )
        reg = MCPToolRegistry()
        del reg._tools["get_user_profile"]  # simule un outil pas encore synchronisé
        reg.sync_discovered_tools(MCPServerKind.DB, [
            {"name": "get_user_profile", "description": "profil utilisateur"},
        ])
        meta = reg.get_tool("get_user_profile")
        assert meta is not None
        assert meta.scope == PermissionScope.DB_READ_ONLY


class TestGetRegistry:
    def test_returns_a_singleton(self, monkeypatch):
        import ladini.infrastructure.mcp.security as sec
        monkeypatch.setattr(sec, "_GLOBAL_REGISTRY", None)
        assert sec.get_registry() is sec.get_registry()


# =====================================================================
# Exceptions — message shape
# =====================================================================

class TestExceptionMessages:
    def test_permission_denied_message(self):
        from ladini.infrastructure.mcp.security import PermissionDenied
        exc = PermissionDenied("create_order", "risky")
        assert "[SHIELD]" in str(exc)
        assert exc.tool_name == "create_order"
        assert exc.reason == "risky"

    def test_timeout_message(self):
        from ladini.infrastructure.mcp.security import ToolExecutionTimeout
        exc = ToolExecutionTimeout("get_orders", 5.0)
        assert "[TIMEOUT]" in str(exc)

    def test_host_blocked_message(self):
        from ladini.infrastructure.mcp.security import HostBlockedError
        exc = HostBlockedError("drop_table", "denied", "use maintenance mode")
        assert "[HOST]" in str(exc)
        assert "Suggestion" in str(exc)


# =====================================================================
# _normalize_tool_output — utilisée par ToolExecutionPolicy.execute (chemin
# RÉELLEMENT actif en production). Extraite de l'ancien
# MCPPermissionClient._normalize_output lors de la suppression du moteur de
# décision de risque orphelin (audit MCP/AGUI 2026-08-26 — voir NOTE dans
# security.py juste après MCPManager, et nodes/confirmation_gate.py pour le
# VRAI point de confirmation humaine).
# =====================================================================

class TestNormalizeToolOutput:
    def test_dict_passes_through(self):
        from ladini.infrastructure.mcp.security import _normalize_tool_output
        assert _normalize_tool_output({"a": 1}) == {"a": 1}

    def test_valid_json_string_is_parsed(self):
        from ladini.infrastructure.mcp.security import _normalize_tool_output
        assert _normalize_tool_output('{"a": 1}') == {"a": 1}

    def test_non_dict_json_string_is_wrapped(self):
        from ladini.infrastructure.mcp.security import _normalize_tool_output
        assert _normalize_tool_output('[1, 2]') == {"result": [1, 2]}

    def test_invalid_json_string_is_wrapped_as_result(self):
        from ladini.infrastructure.mcp.security import _normalize_tool_output
        assert _normalize_tool_output("not json at all") == {"result": "not json at all"}

    def test_list_is_wrapped_under_items(self):
        from ladini.infrastructure.mcp.security import _normalize_tool_output
        assert _normalize_tool_output([1, 2, 3]) == {"items": [1, 2, 3]}

    def test_other_scalar_falls_back_to_str_result(self):
        from ladini.infrastructure.mcp.security import _normalize_tool_output
        assert _normalize_tool_output(42) == {"result": "42"}


# =====================================================================
# PreflightResult
# =====================================================================

class TestPreflightResult:
    def test_bool_reflects_passed(self):
        from ladini.infrastructure.mcp.security import PreflightResult
        assert bool(PreflightResult(True)) is True
        assert bool(PreflightResult(False)) is False


# =====================================================================
# MCPPermissionHostApp — préfiltre + habillage HostBlockedError
# =====================================================================

class TestMCPPermissionHostApp:
    def _host(self, backend_result=None, backend_raises=None, deny_reason=None):
        """``client`` est un simple objet duck-typé (``call_tool``), pas
        l'ancien ``MCPPermissionClient`` (supprimé — audit MCP/AGUI
        2026-08-26). ``MCPPermissionHostApp`` reste actif en production
        (``runtime.py::_run_preflight``, toujours avec ``client=None`` — seul
        ``_preflight_scan``/``_suggest_fix`` y sont utilisés) ; ces tests
        couvrent en plus le pass-through de ``.execute()`` vers un client
        arbitraire, pour n'importe quel appelant qui en fournirait un."""
        from ladini.infrastructure.mcp.security import (
            MCPPermissionHostApp,
            PermissionDenied,
        )

        class _FakeClient:
            async def call_tool(self, name, arguments):
                if deny_reason:
                    raise PermissionDenied(name, deny_reason)
                if backend_raises:
                    raise backend_raises
                return backend_result if backend_result is not None else {"ok": True}

        return MCPPermissionHostApp(client=_FakeClient())

    def test_sql_injection_in_arguments_is_blocked_before_reaching_the_client(self):
        from ladini.infrastructure.mcp.security import HostBlockedError
        host = self._host()
        with pytest.raises(HostBlockedError):
            run(host.execute("get_user_profile", {"q": "DROP TABLE users"}))

    def test_raw_sql_starting_string_is_blocked(self):
        from ladini.infrastructure.mcp.security import HostBlockedError
        host = self._host()
        with pytest.raises(HostBlockedError):
            run(host.execute("get_user_profile", {"q": "SELECT * FROM users WHERE id=1"}))

    def test_raw_sql_block_can_be_disabled(self):
        from ladini.infrastructure.mcp.security import MCPPermissionHostApp

        class _FakeClient:
            async def call_tool(self, name, arguments):
                return {"ok": True}

        host = MCPPermissionHostApp(client=_FakeClient(), block_raw_sql=False)
        result = run(host.execute("get_user_profile", {"q": "SELECT * FROM users WHERE id=1"}))
        assert result == {"ok": True}

    def test_sensitive_file_pattern_does_not_block_but_flags_risk(self):
        host = self._host(backend_result={"ok": True})
        result = run(host.execute("get_user_profile", {"path": "/etc/id_rsa"}))
        assert result == {"ok": True}

    def test_permission_denied_from_client_is_wrapped_as_host_blocked(self):
        from ladini.infrastructure.mcp.security import HostBlockedError
        host = self._host(deny_reason="Schema modification denied")
        with pytest.raises(HostBlockedError):
            run(host.execute("drop_table", {}))

    def test_clean_call_passes_through(self):
        host = self._host(backend_result={"data": "ok"})
        result = run(host.execute("get_user_profile", {"name": "tomates"}))
        assert result == {"data": "ok"}

    def test_suggest_fix_for_schema_modify(self):
        host = self._host()
        assert "maintenance" in host._suggest_fix("drop_table", "Schema modification denied").lower()

    def test_suggest_fix_for_sql_reason(self):
        host = self._host()
        msg = host._suggest_fix("get_user_profile", "SQL suspect pattern: xyz")
        assert "structured arguments" in msg

    def test_suggest_fix_default(self):
        host = self._host()
        msg = host._suggest_fix("create_order", "Human confirmation required")
        assert "create_order" in msg

    def test_flatten_strings_walks_nested_structures(self):
        from ladini.infrastructure.mcp.security import MCPPermissionHostApp
        flat = MCPPermissionHostApp._flatten_strings({"a": "x", "b": ["y", {"c": "z"}]})
        assert set(flat) == {"x", "y", "z"}

    @pytest.mark.parametrize("sql,expected", [
        ("SELECT * FROM users", True),
        ("INSERT INTO users VALUES (1)", True),
        ("DELETE FROM users WHERE id=1", True),
        ("just a normal sentence", False),
        ("SELECT without secondary keyword", False),
    ])
    def test_looks_like_raw_sql(self, sql, expected):
        from ladini.infrastructure.mcp.security import MCPPermissionHostApp
        assert MCPPermissionHostApp._looks_like_raw_sql(sql) is expected


# =====================================================================
# MCPSessionManager
# =====================================================================

class TestMCPSessionManager:
    def test_execute_and_safe_read_both_delegate_to_the_host(self):
        from ladini.infrastructure.mcp.security import MCPSessionManager

        calls = []

        class _FakeHost:
            async def execute(self, tool_name, arguments=None):
                calls.append((tool_name, arguments))
                return {"ok": True}

        session = MCPSessionManager(host=_FakeHost())
        run(session.execute("get_user_profile", {}))
        run(session.safe_read("get_user_profile", {}))
        assert len(calls) == 2


# =====================================================================
# DBInProcessBackend
# =====================================================================

class TestDBInProcessBackend:
    def test_unknown_tool_raises_value_error(self, monkeypatch):
        import ladini.protocols.mcp.servers.h as handlers_mod
        from ladini.infrastructure.mcp.security import DBInProcessBackend

        monkeypatch.setattr(handlers_mod, "TOOL_HANDLERS", {}, raising=False)
        backend = DBInProcessBackend()
        with pytest.raises(ValueError):
            run(backend.call_tool("ghost_tool", {}))

    def test_json_string_result_is_parsed(self, monkeypatch):
        import ladini.protocols.mcp.servers.h as handlers_mod
        from ladini.infrastructure.mcp.security import DBInProcessBackend

        async def fake_tool(**kwargs):
            return '{"a": 1}'

        monkeypatch.setattr(handlers_mod, "TOOL_HANDLERS", {"fake_tool": fake_tool}, raising=False)
        backend = DBInProcessBackend()
        assert run(backend.call_tool("fake_tool", {})) == {"a": 1}

    def test_non_json_string_result_is_wrapped(self, monkeypatch):
        import ladini.protocols.mcp.servers.h as handlers_mod
        from ladini.infrastructure.mcp.security import DBInProcessBackend

        async def fake_tool(**kwargs):
            return "plain text"

        monkeypatch.setattr(handlers_mod, "TOOL_HANDLERS", {"fake_tool": fake_tool}, raising=False)
        backend = DBInProcessBackend()
        assert run(backend.call_tool("fake_tool", {})) == {"result": "plain text"}

    def test_dict_result_passes_through(self, monkeypatch):
        import ladini.protocols.mcp.servers.h as handlers_mod
        from ladini.infrastructure.mcp.security import DBInProcessBackend

        async def fake_tool(**kwargs):
            return {"already": "a dict"}

        monkeypatch.setattr(handlers_mod, "TOOL_HANDLERS", {"fake_tool": fake_tool}, raising=False)
        backend = DBInProcessBackend()
        assert run(backend.call_tool("fake_tool", {})) == {"already": "a dict"}

    def test_list_tools_reads_tool_descriptions(self, monkeypatch):
        import ladini.protocols.mcp.servers.h as handlers_mod
        from ladini.infrastructure.mcp.security import DBInProcessBackend

        monkeypatch.setattr(handlers_mod, "TOOL_DESCRIPTIONS", {"t1": "desc1"}, raising=False)
        backend = DBInProcessBackend()
        assert run(backend.list_tools()) == [{"name": "t1", "description": "desc1"}]


# =====================================================================
# MCPManager
# =====================================================================

class TestMCPManager:
    def _manager(self, monkeypatch, *, list_tools_result=None, call_tool_side_effect=None):
        from ladini.infrastructure.mcp.security import (
            MCPManager,
            MCPServerKind,
            MCPToolRegistry,
        )

        registry = MCPToolRegistry()
        manager = MCPManager(registry=registry)

        class _FakeBackend:
            def __init__(self):
                self.calls = 0

            async def list_tools(self):
                return list_tools_result or []

            async def call_tool(self, name, arguments):
                self.calls += 1
                if call_tool_side_effect is not None:
                    return call_tool_side_effect(self.calls)
                return {"ok": True}

        fake_backend = _FakeBackend()
        manager._backends[MCPServerKind.DB] = fake_backend
        return manager, fake_backend

    def test_ensure_ready_is_idempotent(self, monkeypatch):
        manager, backend = self._manager(monkeypatch)
        run(manager.ensure_ready())
        run(manager.ensure_ready())
        assert manager._ready is True

    def test_unknown_tool_raises_value_error(self, monkeypatch):
        manager, _ = self._manager(monkeypatch)
        with pytest.raises(ValueError):
            run(manager.call_tool("totally_ghost_tool", {}))

    def test_success_on_first_attempt(self, monkeypatch):
        manager, backend = self._manager(monkeypatch)
        result = run(manager.call_tool("get_user_profile", {}))
        assert result == {"ok": True}
        assert backend.calls == 1

    def test_retries_then_succeeds_within_the_retry_budget(self, monkeypatch):
        def flaky(call_number):
            if call_number == 1:
                raise ConnectionError("transient")
            return {"ok": True, "attempt": call_number}

        manager, backend = self._manager(monkeypatch, call_tool_side_effect=flaky)
        result = run(manager.call_tool("get_user_profile", {}))
        assert result["attempt"] == 2

    def test_raises_runtime_error_after_exhausting_all_retries(self, monkeypatch):
        def always_fails(call_number):
            raise ConnectionError(f"fail-{call_number}")

        manager, backend = self._manager(monkeypatch, call_tool_side_effect=always_fails)
        with pytest.raises(RuntimeError, match="MCP call failed"):
            run(manager.call_tool("get_user_profile", {}))

    def test_list_tools_exposes_only_db_tools(self, monkeypatch):
        manager, _ = self._manager(monkeypatch)
        result = run(manager.list_tools())
        assert "db_tools" in result
        assert isinstance(result["db_tools"], list)

# NOTE (audit MCP/AGUI 2026-08-26) : la suite `TestShieldHub` /
# `TestMCPShieldAndUnifiedClient` a été retirée avec `ShieldHub` / `MCPShield`
# / `UnifiedMCPClient` (infrastructure/mcp/security.py) — ces façades
# n'étaient jamais empruntées en production, voir la NOTE laissée dans
# security.py à l'endroit exact de leur suppression.
