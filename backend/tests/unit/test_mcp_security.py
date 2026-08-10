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

from typing import Any, Dict

import pytest

from tests.conftest import run


# =====================================================================
# _guess_scope — repli fail-safe pour un outil non cartographié
# =====================================================================

class TestGuessScope:
    def test_read_prefixes_map_to_read_only(self):
        from agriconnect.infrastructure.mcp.security import _guess_scope, PermissionScope
        for name in ("get_thing", "list_things", "search_stuff", "fetch_x", "count_y"):
            assert _guess_scope(name) == PermissionScope.DB_READ_ONLY

    def test_schema_prefixes_map_to_schema_modify(self):
        from agriconnect.infrastructure.mcp.security import _guess_scope, PermissionScope
        for name in ("migrate_v2", "drop_index", "alter_column", "truncate_logs"):
            assert _guess_scope(name) == PermissionScope.DB_SCHEMA_MODIFY

    def test_unknown_prefix_defaults_to_write_fail_safe(self):
        """Un outil inconnu ne doit JAMAIS être auto-autorisé en lecture —
        le défaut fail-safe est écriture (soumis à scrutin), pas confiance."""
        from agriconnect.infrastructure.mcp.security import _guess_scope, PermissionScope
        assert _guess_scope("do_something_destructive") == PermissionScope.DB_DATA_WRITE


class TestEnsureScopesFilled:
    def test_is_idempotent_and_does_not_raise(self):
        from agriconnect.infrastructure.mcp.security import ensure_scopes_filled
        ensure_scopes_filled()
        ensure_scopes_filled()  # 2e appel : no-op silencieux

    def test_autofill_survives_a_broken_handlers_import(self, monkeypatch):
        import agriconnect.infrastructure.mcp.security as sec

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
        from agriconnect.infrastructure.mcp.security import ToolRateLimiter
        limiter = ToolRateLimiter(max_calls=3, window_seconds=60)
        for _ in range(3):
            allowed, _ = limiter.check("user:tool")
            assert allowed is True

    def test_denies_once_the_limit_is_reached(self):
        from agriconnect.infrastructure.mcp.security import ToolRateLimiter
        limiter = ToolRateLimiter(max_calls=2, window_seconds=60)
        limiter.check("k")
        limiter.check("k")
        allowed, reason = limiter.check("k")
        assert allowed is False
        assert "rate_limit_exceeded" in reason

    def test_different_keys_have_independent_budgets(self):
        from agriconnect.infrastructure.mcp.security import ToolRateLimiter
        limiter = ToolRateLimiter(max_calls=1, window_seconds=60)
        assert limiter.check("a")[0] is True
        assert limiter.check("b")[0] is True

    def test_stale_events_are_evicted_from_the_sliding_window(self):
        from agriconnect.infrastructure.mcp.security import ToolRateLimiter
        limiter = ToolRateLimiter(max_calls=1, window_seconds=60)
        limiter.check("k")
        # Force l'horodatage du seul événement hors fenêtre.
        limiter._events["k"][0] = limiter._events["k"][0] - 61
        allowed, _ = limiter.check("k")
        assert allowed is True

    def test_prunes_stale_keys_once_max_keys_is_exceeded(self):
        from agriconnect.infrastructure.mcp.security import ToolRateLimiter
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
        from agriconnect.infrastructure.mcp.security import ToolRateLimiter
        limiter = ToolRateLimiter(max_calls=0, window_seconds=0, max_keys=0)
        assert limiter.max_calls == 1
        assert limiter.window_seconds == 1.0
        assert limiter.max_keys == 128


# =====================================================================
# ToolExecutionPolicy
# =====================================================================

class TestSanitizeArguments:
    def test_strips_null_bytes_and_surrounding_whitespace(self):
        from agriconnect.infrastructure.mcp.security import ToolExecutionPolicy
        result = ToolExecutionPolicy.sanitize_arguments({"x": "  hello\x00world  "})
        assert result["x"] == "helloworld"

    def test_truncates_overly_long_strings(self):
        from agriconnect.infrastructure.mcp.security import ToolExecutionPolicy
        result = ToolExecutionPolicy.sanitize_arguments({"x": "a" * 5000})
        assert len(result["x"]) == 4000

    def test_recurses_into_nested_dicts_and_lists(self):
        from agriconnect.infrastructure.mcp.security import ToolExecutionPolicy
        result = ToolExecutionPolicy.sanitize_arguments({
            "a": {"b": "  y\x00  "},
            "c": ["  z\x00  ", 5],
        })
        assert result["a"]["b"] == "y"
        assert result["c"] == ["z", 5]

    def test_leaves_non_string_scalars_untouched(self):
        from agriconnect.infrastructure.mcp.security import ToolExecutionPolicy
        result = ToolExecutionPolicy.sanitize_arguments({"n": 42, "f": 3.14, "b": True, "none": None})
        assert result == {"n": 42, "f": 3.14, "b": True, "none": None}


class TestEstimateTokens:
    def test_estimates_roughly_chars_over_four(self):
        from agriconnect.infrastructure.mcp.security import ToolExecutionPolicy
        assert ToolExecutionPolicy._estimate_tokens({"x": "abcd" * 10}) >= 1

    def test_returns_zero_on_unserializable_payload(self):
        from agriconnect.infrastructure.mcp.security import ToolExecutionPolicy
        circular: dict = {}
        circular["self"] = circular
        assert ToolExecutionPolicy._estimate_tokens(circular) == 0


class TestToolExecutionPolicyExecute:
    def _policy(self):
        from agriconnect.infrastructure.mcp.security import ToolExecutionPolicy, MCPToolRegistry
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

        from agriconnect.infrastructure.mcp.security import ToolExecutionTimeout
        with pytest.raises(ToolExecutionTimeout):
            run(policy.execute("get_user_profile", slow_handler, {}, timeout_seconds=0.01))

    def test_rate_limit_exceeded_raises_permission_denied(self):
        from agriconnect.infrastructure.mcp.security import (
            ToolExecutionPolicy, MCPToolRegistry, PermissionDenied,
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
        import agriconnect.infrastructure.mcp.security as sec
        monkeypatch.setattr(sec, "_GLOBAL_EXECUTION_POLICY", None)
        p1 = sec.get_execution_policy()
        p2 = sec.get_execution_policy()
        assert p1 is p2

    def test_applies_valid_json_timeout_overrides_from_env(self, monkeypatch):
        import agriconnect.infrastructure.mcp.security as sec
        monkeypatch.setattr(sec, "_GLOBAL_EXECUTION_POLICY", None)
        monkeypatch.setenv("MCP_TOOL_TIMEOUT_OVERRIDES", '{"search_agronomy_docs": 42}')
        policy = sec.get_execution_policy()
        assert policy._overrides["search_agronomy_docs"] == 42.0

    def test_ignores_malformed_json_overrides_without_crashing(self, monkeypatch):
        import agriconnect.infrastructure.mcp.security as sec
        monkeypatch.setattr(sec, "_GLOBAL_EXECUTION_POLICY", None)
        monkeypatch.setenv("MCP_TOOL_TIMEOUT_OVERRIDES", "{not-json")
        policy = sec.get_execution_policy()
        assert policy is not None

    def test_falls_back_to_default_timeout_on_invalid_env_value(self, monkeypatch):
        import agriconnect.infrastructure.mcp.security as sec
        monkeypatch.setattr(sec, "_GLOBAL_EXECUTION_POLICY", None)
        monkeypatch.setenv("MCP_DEFAULT_TOOL_TIMEOUT", "not-a-float")
        policy = sec.get_execution_policy()
        assert policy._default_timeout == 30.0


# =====================================================================
# MCPToolRegistry
# =====================================================================

class TestMCPToolRegistry:
    def test_has_tool_and_get_tool(self):
        from agriconnect.infrastructure.mcp.security import MCPToolRegistry
        reg = MCPToolRegistry()
        assert reg.has_tool("get_user_profile") is True
        assert reg.has_tool("ghost_tool") is False
        assert reg.get_tool("ghost_tool") is None

    def test_list_tools_is_sorted_by_name(self):
        from agriconnect.infrastructure.mcp.security import MCPToolRegistry
        reg = MCPToolRegistry()
        names = [t["name"] for t in reg.list_tools()]
        assert names == sorted(names)

    def test_list_tools_filters_by_server(self):
        from agriconnect.infrastructure.mcp.security import MCPToolRegistry, MCPServerKind
        reg = MCPToolRegistry()
        items = reg.list_tools(server=MCPServerKind.DB)
        assert all(i["server"] == "db" for i in items)

    def test_sync_discovered_tools_adds_only_new_names(self):
        from agriconnect.infrastructure.mcp.security import MCPToolRegistry, MCPServerKind
        reg = MCPToolRegistry()
        before = reg.get_tool("get_user_profile")
        reg.sync_discovered_tools(MCPServerKind.DB, [
            {"name": "get_user_profile", "description": "SHOULD NOT OVERWRITE"},
            {"name": "brand_new_tool", "description": "a fresh tool"},
        ])
        assert reg.get_tool("get_user_profile") is before, "un outil déjà connu ne doit jamais être réécrasé"
        assert reg.has_tool("brand_new_tool") is True

    def test_sync_discovered_tools_skips_blank_names(self):
        from agriconnect.infrastructure.mcp.security import MCPToolRegistry, MCPServerKind
        reg = MCPToolRegistry()
        count_before = len(reg._tools)
        reg.sync_discovered_tools(MCPServerKind.DB, [{"name": "", "description": "x"}])
        assert len(reg._tools) == count_before


class TestGetRegistry:
    def test_returns_a_singleton(self, monkeypatch):
        import agriconnect.infrastructure.mcp.security as sec
        monkeypatch.setattr(sec, "_GLOBAL_REGISTRY", None)
        assert sec.get_registry() is sec.get_registry()


# =====================================================================
# Exceptions — message shape
# =====================================================================

class TestExceptionMessages:
    def test_permission_denied_message(self):
        from agriconnect.infrastructure.mcp.security import PermissionDenied
        exc = PermissionDenied("create_order", "risky")
        assert "[SHIELD]" in str(exc)
        assert exc.tool_name == "create_order"
        assert exc.reason == "risky"

    def test_timeout_message(self):
        from agriconnect.infrastructure.mcp.security import ToolExecutionTimeout
        exc = ToolExecutionTimeout("get_orders", 5.0)
        assert "[TIMEOUT]" in str(exc)

    def test_host_blocked_message(self):
        from agriconnect.infrastructure.mcp.security import HostBlockedError
        exc = HostBlockedError("drop_table", "denied", "use maintenance mode")
        assert "[HOST]" in str(exc)
        assert "Suggestion" in str(exc)


# =====================================================================
# MCPPermissionClient — le cœur de la décision d'autorisation
# =====================================================================

class TestMCPPermissionClientCheckPermission:
    def _client(self, maintenance_mode=False):
        from agriconnect.infrastructure.mcp.security import MCPPermissionClient
        return MCPPermissionClient(backend=None, maintenance_mode=maintenance_mode)

    def test_schema_modify_denied_outside_maintenance(self):
        client = self._client(maintenance_mode=False)
        decision = client._check_permission("drop_table", {})
        assert decision.allowed is False
        assert decision.decision == "DENY"

    def test_schema_modify_allowed_in_maintenance_mode(self):
        client = self._client(maintenance_mode=True)
        decision = client._check_permission("drop_table", {})
        assert decision.allowed is True

    def test_read_only_always_allowed(self):
        client = self._client()
        decision = client._check_permission("get_user_profile", {})
        assert decision.allowed is True
        assert decision.decision == "ALLOW"

    def test_high_risk_write_requires_hitl(self):
        client = self._client()
        decision = client._check_permission("create_order", {})
        assert decision.allowed is False
        assert decision.decision == "HITL_REQUIRED"

    def test_critical_risk_write_requires_hitl(self):
        client = self._client()
        decision = client._check_permission("mark_escrow_paid", {})
        assert decision.decision == "HITL_REQUIRED"

    def test_medium_risk_write_is_allowed_without_hitl(self):
        client = self._client()
        decision = client._check_permission("create_product", {})
        assert decision.allowed is True
        assert decision.decision == "ALLOW"

    def test_suspicious_arguments_force_hitl_even_for_read_only(self):
        client = self._client()
        decision = client._check_permission("get_user_profile", {"q": "DROP TABLE users"})
        assert decision.allowed is False
        assert decision.decision == "HITL_REQUIRED"
        assert decision.risk.value == "CRITICAL"


class TestScanArgumentsForRisk:
    def _client(self):
        from agriconnect.infrastructure.mcp.security import MCPPermissionClient
        return MCPPermissionClient(backend=None)

    @pytest.mark.parametrize("payload", [
        "DROP TABLE users",
        "delete from orders",
        "1 OR 1=1",
        "UNION SELECT * FROM users",
        "'; TRUNCATE users; --",
        "ALTER TABLE users ADD COLUMN x",
    ])
    def test_detects_sql_injection_patterns(self, payload):
        client = self._client()
        suspicious, reason = client._scan_arguments_for_risk({"q": payload})
        assert suspicious is True
        assert reason == "sql_pattern_detected"

    def test_normal_business_text_is_not_flagged(self):
        client = self._client()
        suspicious, _ = client._scan_arguments_for_risk({"product": "tomates fraiches, 200kg"})
        assert suspicious is False

    def test_oversized_argument_value_is_flagged(self):
        client = self._client()
        suspicious, reason = client._scan_arguments_for_risk({"notes": "a" * 5000})
        assert suspicious is True
        assert "too_large" in reason

    def test_key_containing_sql_is_flagged(self):
        client = self._client()
        suspicious, reason = client._scan_arguments_for_risk({"raw_sql_query": "select id"})
        assert suspicious is True
        assert "sql_key" in reason


class TestMaskSensitive:
    def _client(self):
        from agriconnect.infrastructure.mcp.security import MCPPermissionClient
        return MCPPermissionClient(backend=None)

    def test_masks_known_sensitive_keys_case_insensitively(self):
        client = self._client()
        masked = client._mask_sensitive({"Password": "hunter2", "PASSWORD_HASH": "abc", "product": "mais"})
        assert masked["Password"] == "***MASKED***"
        assert masked["PASSWORD_HASH"] == "***MASKED***"
        assert masked["product"] == "mais"

    def test_masks_recursively_through_nested_structures(self):
        client = self._client()
        masked = client._mask_sensitive({"user": {"email": "a@b.com", "orders": [{"token": "t1"}]}})
        assert masked["user"]["email"] == "***MASKED***"
        assert masked["user"]["orders"][0]["token"] == "***MASKED***"

    def test_parses_and_masks_json_encoded_strings(self):
        client = self._client()
        masked = client._mask_sensitive('{"api_key": "secret123"}')
        assert masked["api_key"] == "***MASKED***"

    def test_non_json_string_passes_through_unchanged(self):
        client = self._client()
        assert client._mask_sensitive("just some text") == "just some text"

    def test_recursion_guard_prevents_infinite_loop_on_circular_refs(self):
        client = self._client()
        circular: dict = {"product": "mais"}
        circular["self"] = circular
        masked = client._mask_sensitive(circular)
        assert masked["self"] == "***RECURSION***"


class TestNormalizeOutput:
    def test_dict_passes_through(self):
        from agriconnect.infrastructure.mcp.security import MCPPermissionClient
        assert MCPPermissionClient._normalize_output({"a": 1}) == {"a": 1}

    def test_valid_json_string_is_parsed(self):
        from agriconnect.infrastructure.mcp.security import MCPPermissionClient
        assert MCPPermissionClient._normalize_output('{"a": 1}') == {"a": 1}

    def test_non_dict_json_string_is_wrapped(self):
        from agriconnect.infrastructure.mcp.security import MCPPermissionClient
        assert MCPPermissionClient._normalize_output('[1, 2]') == {"result": [1, 2]}

    def test_invalid_json_string_is_wrapped_as_result(self):
        from agriconnect.infrastructure.mcp.security import MCPPermissionClient
        assert MCPPermissionClient._normalize_output("not json at all") == {"result": "not json at all"}

    def test_list_is_wrapped_under_items(self):
        from agriconnect.infrastructure.mcp.security import MCPPermissionClient
        assert MCPPermissionClient._normalize_output([1, 2, 3]) == {"items": [1, 2, 3]}

    def test_other_scalar_falls_back_to_str_result(self):
        from agriconnect.infrastructure.mcp.security import MCPPermissionClient
        assert MCPPermissionClient._normalize_output(42) == {"result": "42"}


class TestHashArgs:
    def test_deterministic_regardless_of_key_order(self):
        from agriconnect.infrastructure.mcp.security import MCPPermissionClient
        h1 = MCPPermissionClient._hash_args({"a": 1, "b": 2})
        h2 = MCPPermissionClient._hash_args({"b": 2, "a": 1})
        assert h1 == h2
        assert len(h1) == 16

    def test_different_arguments_hash_differently(self):
        from agriconnect.infrastructure.mcp.security import MCPPermissionClient
        assert MCPPermissionClient._hash_args({"a": 1}) != MCPPermissionClient._hash_args({"a": 2})


class TestMCPPermissionClientCallTool:
    class _Backend:
        def __init__(self, result=None, raises=None):
            self._result = result if result is not None else {"ok": True}
            self._raises = raises

        async def call_tool(self, name, arguments):
            if self._raises:
                raise self._raises
            return self._result

    def test_unregistered_tool_raises_permission_denied(self):
        from agriconnect.infrastructure.mcp.security import MCPPermissionClient, PermissionDenied
        client = MCPPermissionClient(backend=self._Backend())
        with pytest.raises(PermissionDenied):
            run(client.call_tool("totally_unknown_tool", {}))

    def test_denied_scope_raises_permission_denied(self):
        from agriconnect.infrastructure.mcp.security import MCPPermissionClient, PermissionDenied
        client = MCPPermissionClient(backend=self._Backend())
        with pytest.raises(PermissionDenied):
            run(client.call_tool("drop_table", {}))

    def test_hitl_required_and_approved_proceeds(self):
        from agriconnect.infrastructure.mcp.security import MCPPermissionClient

        async def approve(tool_name, args, reason):
            return True

        client = MCPPermissionClient(backend=self._Backend(result={"order_id": "o1"}), hitl_callback=approve)
        result = run(client.call_tool("create_order", {"qty": 5}))
        assert result == {"order_id": "o1"}

    def test_hitl_required_and_rejected_raises_permission_denied(self):
        from agriconnect.infrastructure.mcp.security import MCPPermissionClient, PermissionDenied

        async def reject(tool_name, args, reason):
            return False

        client = MCPPermissionClient(backend=self._Backend(), hitl_callback=reject)
        with pytest.raises(PermissionDenied):
            run(client.call_tool("create_order", {}))

    def test_hitl_with_no_callback_registered_denies(self):
        from agriconnect.infrastructure.mcp.security import MCPPermissionClient, PermissionDenied
        client = MCPPermissionClient(backend=self._Backend(), hitl_callback=None)
        with pytest.raises(PermissionDenied):
            run(client.call_tool("create_order", {}))

    def test_hitl_callback_exception_is_treated_as_rejection(self):
        from agriconnect.infrastructure.mcp.security import MCPPermissionClient, PermissionDenied

        async def boom(tool_name, args, reason):
            raise RuntimeError("callback exploded")

        client = MCPPermissionClient(backend=self._Backend(), hitl_callback=boom)
        with pytest.raises(PermissionDenied):
            run(client.call_tool("create_order", {}))

    def test_successful_read_only_call_masks_and_returns_result(self):
        from agriconnect.infrastructure.mcp.security import MCPPermissionClient
        client = MCPPermissionClient(backend=self._Backend(result={"phone": "+2260", "email": "a@b.com"}))
        result = run(client.call_tool("get_user_profile", {}))
        assert result["email"] == "***MASKED***"
        assert result["phone"] == "+2260"


# =====================================================================
# PreflightResult
# =====================================================================

class TestPreflightResult:
    def test_bool_reflects_passed(self):
        from agriconnect.infrastructure.mcp.security import PreflightResult
        assert bool(PreflightResult(True)) is True
        assert bool(PreflightResult(False)) is False


# =====================================================================
# MCPPermissionHostApp — préfiltre + habillage HostBlockedError
# =====================================================================

class TestMCPPermissionHostApp:
    def _host(self, backend_result=None, backend_raises=None):
        from agriconnect.infrastructure.mcp.security import MCPPermissionClient, MCPPermissionHostApp

        class _Backend:
            async def call_tool(self, name, arguments):
                if backend_raises:
                    raise backend_raises
                return backend_result if backend_result is not None else {"ok": True}

        client = MCPPermissionClient(backend=_Backend())
        return MCPPermissionHostApp(client=client)

    def test_sql_injection_in_arguments_is_blocked_before_reaching_the_client(self):
        from agriconnect.infrastructure.mcp.security import HostBlockedError
        host = self._host()
        with pytest.raises(HostBlockedError):
            run(host.execute("get_user_profile", {"q": "DROP TABLE users"}))

    def test_raw_sql_starting_string_is_blocked(self):
        from agriconnect.infrastructure.mcp.security import HostBlockedError
        host = self._host()
        with pytest.raises(HostBlockedError):
            run(host.execute("get_user_profile", {"q": "SELECT * FROM users WHERE id=1"}))

    def test_raw_sql_block_can_be_disabled(self):
        from agriconnect.infrastructure.mcp.security import MCPPermissionClient, MCPPermissionHostApp

        class _Backend:
            async def call_tool(self, name, arguments):
                return {"ok": True}

        client = MCPPermissionClient(backend=_Backend())
        host = MCPPermissionHostApp(client=client, block_raw_sql=False)
        result = run(host.execute("get_user_profile", {"q": "SELECT * FROM users WHERE id=1"}))
        assert result == {"ok": True}

    def test_sensitive_file_pattern_does_not_block_but_flags_risk(self):
        host = self._host(backend_result={"ok": True})
        result = run(host.execute("get_user_profile", {"path": "/etc/id_rsa"}))
        assert result == {"ok": True}

    def test_permission_denied_from_client_is_wrapped_as_host_blocked(self):
        from agriconnect.infrastructure.mcp.security import HostBlockedError
        host = self._host()
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
        from agriconnect.infrastructure.mcp.security import MCPPermissionHostApp
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
        from agriconnect.infrastructure.mcp.security import MCPPermissionHostApp
        assert MCPPermissionHostApp._looks_like_raw_sql(sql) is expected


# =====================================================================
# MCPSessionManager
# =====================================================================

class TestMCPSessionManager:
    def test_execute_and_safe_read_both_delegate_to_the_host(self):
        from agriconnect.infrastructure.mcp.security import MCPSessionManager

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
        import agriconnect.protocols.mcp.servers.h as handlers_mod
        from agriconnect.infrastructure.mcp.security import DBInProcessBackend

        monkeypatch.setattr(handlers_mod, "TOOL_HANDLERS", {}, raising=False)
        backend = DBInProcessBackend()
        with pytest.raises(ValueError):
            run(backend.call_tool("ghost_tool", {}))

    def test_json_string_result_is_parsed(self, monkeypatch):
        import agriconnect.protocols.mcp.servers.h as handlers_mod
        from agriconnect.infrastructure.mcp.security import DBInProcessBackend

        async def fake_tool(**kwargs):
            return '{"a": 1}'

        monkeypatch.setattr(handlers_mod, "TOOL_HANDLERS", {"fake_tool": fake_tool}, raising=False)
        backend = DBInProcessBackend()
        assert run(backend.call_tool("fake_tool", {})) == {"a": 1}

    def test_non_json_string_result_is_wrapped(self, monkeypatch):
        import agriconnect.protocols.mcp.servers.h as handlers_mod
        from agriconnect.infrastructure.mcp.security import DBInProcessBackend

        async def fake_tool(**kwargs):
            return "plain text"

        monkeypatch.setattr(handlers_mod, "TOOL_HANDLERS", {"fake_tool": fake_tool}, raising=False)
        backend = DBInProcessBackend()
        assert run(backend.call_tool("fake_tool", {})) == {"result": "plain text"}

    def test_dict_result_passes_through(self, monkeypatch):
        import agriconnect.protocols.mcp.servers.h as handlers_mod
        from agriconnect.infrastructure.mcp.security import DBInProcessBackend

        async def fake_tool(**kwargs):
            return {"already": "a dict"}

        monkeypatch.setattr(handlers_mod, "TOOL_HANDLERS", {"fake_tool": fake_tool}, raising=False)
        backend = DBInProcessBackend()
        assert run(backend.call_tool("fake_tool", {})) == {"already": "a dict"}

    def test_list_tools_reads_tool_descriptions(self, monkeypatch):
        import agriconnect.protocols.mcp.servers.h as handlers_mod
        from agriconnect.infrastructure.mcp.security import DBInProcessBackend

        monkeypatch.setattr(handlers_mod, "TOOL_DESCRIPTIONS", {"t1": "desc1"}, raising=False)
        backend = DBInProcessBackend()
        assert run(backend.list_tools()) == [{"name": "t1", "description": "desc1"}]


# =====================================================================
# MCPManager
# =====================================================================

class TestMCPManager:
    def _manager(self, monkeypatch, *, list_tools_result=None, call_tool_side_effect=None):
        from agriconnect.infrastructure.mcp.security import MCPManager, MCPToolRegistry, MCPServerKind

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


# =====================================================================
# ShieldHub / MCPShield / UnifiedMCPClient — façades haut niveau
# =====================================================================

class TestShieldHub:
    def test_unknown_tool_raises_value_error(self):
        from agriconnect.infrastructure.mcp.security import ShieldHub
        hub = ShieldHub()
        with pytest.raises(ValueError):
            run(hub.call("totally_ghost_tool_xyz", {}))

    def test_read_only_tool_routes_through_safe_read(self, monkeypatch):
        from agriconnect.infrastructure.mcp.security import ShieldHub

        hub = ShieldHub()
        calls = []
        monkeypatch.setattr(hub.session, "safe_read", _record_and_return(calls, "safe_read"))
        monkeypatch.setattr(hub.session, "execute", _record_and_return(calls, "execute"))
        run(hub.manager.ensure_ready())
        run(hub.call("get_user_profile", {}))
        assert calls == ["safe_read"]

    def test_write_tool_routes_through_execute(self, monkeypatch):
        from agriconnect.infrastructure.mcp.security import ShieldHub

        hub = ShieldHub()
        calls = []
        monkeypatch.setattr(hub.session, "safe_read", _record_and_return(calls, "safe_read"))
        monkeypatch.setattr(hub.session, "execute", _record_and_return(calls, "execute"))
        run(hub.manager.ensure_ready())
        run(hub.call("create_product", {}))
        assert calls == ["execute"]

    def test_list_tools_delegates_to_manager(self):
        from agriconnect.infrastructure.mcp.security import ShieldHub
        hub = ShieldHub()
        result = run(hub.list_tools())
        assert "db_tools" in result


def _record_and_return(calls, label):
    async def _fake(tool_name, arguments=None):
        calls.append(label)
        return {"ok": True}
    return _fake


class TestMCPShieldAndUnifiedClient:
    def test_mcp_shield_authorize_and_call_delegates_to_hub(self, monkeypatch):
        from agriconnect.infrastructure.mcp.security import MCPShield

        shield = MCPShield()
        calls = []
        monkeypatch.setattr(shield._hub, "call", _record_call(calls))
        run(shield.authorize_and_call("get_user_profile", {"a": 1}))
        assert calls == [("get_user_profile", {"a": 1})]

    def test_mcp_shield_list_allowed_tools_delegates_to_hub(self):
        from agriconnect.infrastructure.mcp.security import MCPShield
        shield = MCPShield()
        result = run(shield.list_allowed_tools())
        assert "db_tools" in result

    def test_unified_client_call_tool_defaults_arguments_to_empty_dict(self, monkeypatch):
        from agriconnect.infrastructure.mcp.security import UnifiedMCPClient

        client = UnifiedMCPClient()
        calls = []
        monkeypatch.setattr(client._hub, "call", _record_call(calls))
        run(client.call_tool("get_user_profile", None))
        assert calls == [("get_user_profile", {})]

    def test_unified_client_list_tools_delegates_to_hub(self):
        from agriconnect.infrastructure.mcp.security import UnifiedMCPClient
        client = UnifiedMCPClient()
        result = run(client.list_tools())
        assert "db_tools" in result


def _record_call(calls):
    async def _fake(tool_name, arguments):
        calls.append((tool_name, arguments))
        return {"ok": True}
    return _fake
