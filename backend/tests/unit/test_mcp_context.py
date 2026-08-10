"""`infrastructure/mcp/context.py` — serveur MCP Context (cache sémantique,
budget tokens, enrichissement d'état) + le wrapper de compatibilité
`MCPContextServer` (API synchrone legacy).

Les méthodes SYNCHRONES (`call_tool`, `read_resource`, `read_user_context`)
sont appelées directement (PAS via `run()`/`asyncio.run`) — `_sync_await`
interdit explicitement d'être invoqué depuis une boucle asyncio déjà active,
donc les tester depuis un test sync normal est le chemin correct.
"""
from __future__ import annotations

import json
import sys
import types

import pytest

from tests.conftest import run


@pytest.fixture(autouse=True)
def _reset_context_module_state():
    """Les globals (`_optimizer`, `_session_factory`, `_llm_client`,
    `_context_cache`) sont partagés au niveau module — reset entre tests
    pour éviter toute pollution croisée."""
    import agriconnect.infrastructure.mcp.context as mod
    mod._optimizer = None
    mod._session_factory = None
    mod._llm_client = None
    mod._context_cache.clear()
    yield
    mod._optimizer = None
    mod._session_factory = None
    mod._llm_client = None
    mod._context_cache.clear()


# =====================================================================
# Contexte scoping (ContextVar)
# =====================================================================

class TestMcpContextScope:
    def test_default_context_is_none(self):
        from agriconnect.infrastructure.mcp.context import get_mcp_context
        assert get_mcp_context() is None

    def test_set_and_get_roundtrip(self):
        from agriconnect.infrastructure.mcp.context import FarmerContext, set_mcp_context, get_mcp_context
        ctx = FarmerContext(user_id="u1", phone_number="+2260", session_id="s1")
        set_mcp_context(ctx)
        assert get_mcp_context() is ctx
        set_mcp_context(None)

    def test_scope_context_manager_restores_previous_value_on_exit(self):
        from agriconnect.infrastructure.mcp.context import FarmerContext, mcp_context_scope, get_mcp_context
        outer = FarmerContext(user_id="outer", phone_number="+2260", session_id="s1")
        inner = FarmerContext(user_id="inner", phone_number="+2261", session_id="s2")
        from agriconnect.infrastructure.mcp.context import set_mcp_context
        set_mcp_context(outer)
        with mcp_context_scope(inner):
            assert get_mcp_context() is inner
        assert get_mcp_context() is outer
        set_mcp_context(None)


# =====================================================================
# Cache (TTL + LRU-ish pruning)
# =====================================================================

class TestContextCache:
    def test_set_then_get_within_ttl(self):
        import agriconnect.infrastructure.mcp.context as mod
        mod._cache_set("u1", {"combined_context": "x"})
        assert mod._cache_get("u1") == {"combined_context": "x"}

    def test_get_missing_key_returns_none(self):
        import agriconnect.infrastructure.mcp.context as mod
        assert mod._cache_get("ghost") is None

    def test_expired_entry_returns_none_and_is_evicted(self):
        import agriconnect.infrastructure.mcp.context as mod
        import time
        mod._context_cache["u1"] = mod._CacheEntry(payload={"x": 1}, expires_at=time.monotonic() - 1)
        assert mod._cache_get("u1") is None
        assert "u1" not in mod._context_cache

    def test_prune_removes_stale_entries(self):
        import agriconnect.infrastructure.mcp.context as mod
        import time
        mod._context_cache["stale"] = mod._CacheEntry(payload={}, expires_at=time.monotonic() - 1)
        mod._context_cache["fresh"] = mod._CacheEntry(payload={}, expires_at=time.monotonic() + 300)
        mod._prune_context_cache()
        assert "stale" not in mod._context_cache
        assert "fresh" in mod._context_cache

    def test_cache_is_bounded_to_512_entries(self):
        import agriconnect.infrastructure.mcp.context as mod
        import time
        far_future = time.monotonic() + 300
        for i in range(520):
            mod._context_cache[f"u{i}"] = mod._CacheEntry(payload={}, expires_at=far_future)
        mod._prune_context_cache()
        assert len(mod._context_cache) <= 512
        assert "u0" not in mod._context_cache, "les entrées les plus anciennes doivent être évincées en premier"


# =====================================================================
# _configure / _lazy_optimizer
# =====================================================================

class TestConfigureAndLazyOptimizer:
    def test_configure_sets_module_globals(self):
        import agriconnect.infrastructure.mcp.context as mod
        sentinel_opt, sentinel_sf, sentinel_llm = object(), object(), object()
        mod._configure(context_optimizer=sentinel_opt, session_factory=sentinel_sf, llm_client=sentinel_llm)
        assert mod._optimizer is sentinel_opt
        assert mod._session_factory is sentinel_sf
        assert mod._llm_client is sentinel_llm

    def test_configure_with_none_args_does_not_clear_existing_values(self):
        import agriconnect.infrastructure.mcp.context as mod
        sentinel = object()
        mod._configure(context_optimizer=sentinel)
        mod._configure()  # tous None
        assert mod._optimizer is sentinel

    def test_lazy_optimizer_returns_none_without_a_session_factory(self):
        import agriconnect.infrastructure.mcp.context as mod
        assert mod._lazy_optimizer() is None

    def test_lazy_optimizer_returns_the_already_configured_instance(self):
        import agriconnect.infrastructure.mcp.context as mod
        sentinel = object()
        mod._optimizer = sentinel
        assert mod._lazy_optimizer() is sentinel


# =====================================================================
# build_context
# =====================================================================

class TestBuildContext:
    def test_no_optimizer_returns_an_error_payload(self):
        from agriconnect.infrastructure.mcp.context import build_context
        raw = run(build_context(user_id="u1", query="bonjour"))
        payload = json.loads(raw)
        assert payload["error"]
        assert payload["combined_context"] == ""

    def test_optimizer_success_is_cached(self):
        import agriconnect.infrastructure.mcp.context as mod

        class _FakeOptimizer:
            def build_context(self, user_id, query, zone=None, crop=None):
                return {"combined_context": "ctx", "token_estimate": 42}

        mod._optimizer = _FakeOptimizer()
        raw = run(mod.build_context(user_id="u1", query="bonjour"))
        payload = json.loads(raw)
        assert payload["combined_context"] == "ctx"
        assert mod._cache_get("u1") == payload


# =====================================================================
# get_token_budget
# =====================================================================

class TestGetTokenBudget:
    def test_returns_real_token_budgets_when_importable(self):
        from agriconnect.infrastructure.mcp.context import get_token_budget
        raw = run(get_token_budget())
        payload = json.loads(raw)
        assert "total_target" in payload or "total" in payload

    def test_falls_back_to_defaults_on_import_failure(self, monkeypatch):
        from agriconnect.infrastructure.mcp.context import get_token_budget
        monkeypatch.setitem(
            sys.modules, "agriconnect.services.memory.context_optimizer",
            types.ModuleType("dummy_without_token_budgets"),
        )
        raw = run(get_token_budget())
        payload = json.loads(raw)
        assert payload == {"profile": 80, "episodes": 120, "metadata": 50, "total": 350}


# =====================================================================
# enrich_state
# =====================================================================

class TestEnrichState:
    def test_no_optimizer_returns_the_state_unchanged(self):
        from agriconnect.infrastructure.mcp.context import enrich_state
        raw = run(enrich_state(json.dumps({"a": 1})))
        assert json.loads(raw) == {"a": 1}

    def test_accepts_a_dict_directly_not_just_a_json_string(self):
        from agriconnect.infrastructure.mcp.context import enrich_state
        raw = run(enrich_state({"a": 1}))
        assert json.loads(raw) == {"a": 1}

    def test_optimizer_success_returns_the_enriched_state(self):
        import agriconnect.infrastructure.mcp.context as mod

        class _FakeOptimizer:
            def enrich_state(self, state):
                return {**state, "enriched": True}

        mod._optimizer = _FakeOptimizer()
        raw = run(mod.enrich_state(json.dumps({"a": 1})))
        assert json.loads(raw) == {"a": 1, "enriched": True}

    def test_optimizer_failure_falls_back_to_the_original_state(self):
        import agriconnect.infrastructure.mcp.context as mod

        class _BoomOptimizer:
            def enrich_state(self, state):
                raise RuntimeError("boom")

        mod._optimizer = _BoomOptimizer()
        raw = run(mod.enrich_state(json.dumps({"a": 1})))
        assert json.loads(raw) == {"a": 1}


# =====================================================================
# record_interaction
# =====================================================================

class TestRecordInteraction:
    def test_no_optimizer_is_skipped(self):
        from agriconnect.infrastructure.mcp.context import record_interaction
        raw = run(record_interaction(user_id="u1", query="q", response="r", agent="market"))
        payload = json.loads(raw)
        assert payload["status"] == "skipped"

    def test_optimizer_success_records(self):
        import agriconnect.infrastructure.mcp.context as mod
        calls = []

        class _FakeOptimizer:
            def record_interaction(self, **kwargs):
                calls.append(kwargs)

        mod._optimizer = _FakeOptimizer()
        raw = run(mod.record_interaction(user_id="u1", query="q", response="r", agent="market"))
        assert json.loads(raw) == {"status": "recorded"}
        assert calls[0]["user_id"] == "u1"

    def test_optimizer_failure_returns_an_error_status(self):
        import agriconnect.infrastructure.mcp.context as mod

        class _BoomOptimizer:
            def record_interaction(self, **kwargs):
                raise RuntimeError("db down")

        mod._optimizer = _BoomOptimizer()
        raw = run(mod.record_interaction(user_id="u1", query="q", response="r", agent="market"))
        payload = json.loads(raw)
        assert payload["status"] == "error"


# =====================================================================
# context_status resource
# =====================================================================

class TestContextStatus:
    def test_reports_cache_size_and_optimizer_readiness(self):
        import agriconnect.infrastructure.mcp.context as mod
        mod._cache_set("u1", {"x": 1})
        raw = run(mod.context_status())
        payload = json.loads(raw)
        assert payload["cached_users"] == 1
        assert payload["optimizer_ready"] is False


# =====================================================================
# MCPContextServer — wrapper de compatibilité synchrone
# =====================================================================

class TestMCPContextServer:
    def test_init_configures_module_globals(self):
        from agriconnect.infrastructure.mcp.context import MCPContextServer
        import agriconnect.infrastructure.mcp.context as mod
        sentinel = object()
        MCPContextServer(context_optimizer=sentinel)
        assert mod._optimizer is sentinel

    def test_list_tools_and_resources_shape(self):
        from agriconnect.infrastructure.mcp.context import MCPContextServer
        tools = MCPContextServer.list_tools()
        assert {t["name"] for t in tools} == {"build_context", "get_token_budget", "enrich_state", "record_interaction"}
        resources = MCPContextServer.list_resources()
        assert resources[0]["uri"] == "context://status"

    def test_call_tool_unknown_name_raises_inside_dispatch(self):
        from agriconnect.infrastructure.mcp.context import MCPContextServer
        server = MCPContextServer()
        result = server.call_tool("ghost_tool", {})
        assert result["status"] == "error"

    def test_call_tool_success_wraps_the_raw_text(self):
        from agriconnect.infrastructure.mcp.context import MCPContextServer
        server = MCPContextServer()
        result = server.call_tool("get_token_budget", {})
        assert result["status"] == "ok"
        assert result["content"][0]["type"] == "text"

    def test_call_tool_from_a_running_loop_is_reported_as_an_error(self):
        """`_sync_await` interdit explicitement d'être appelé depuis une
        boucle déjà active — `call_tool` doit convertir ça en dict d'erreur,
        pas laisser l'exception remonter brute."""
        from agriconnect.infrastructure.mcp.context import MCPContextServer
        server = MCPContextServer()

        async def _call_from_loop():
            return server.call_tool("get_token_budget", {})

        result = run(_call_from_loop())
        assert result["status"] == "error"
        assert "running event loop" in result["error"]

    def test_read_resource_success(self):
        from agriconnect.infrastructure.mcp.context import MCPContextServer
        server = MCPContextServer()
        result = server.read_resource("context://status")
        assert result["status"] == "ok"
        assert result["contents"][0]["uri"] == "context://status"

    def test_read_user_context_cache_hit_skips_rebuild(self):
        import agriconnect.infrastructure.mcp.context as mod
        from agriconnect.infrastructure.mcp.context import MCPContextServer
        mod._cache_set("u1", {"combined_context": "cached"})
        server = MCPContextServer()
        result = server.read_user_context("u1", query="")
        assert result == {"combined_context": "cached"}

    def test_read_user_context_cache_miss_builds_fresh(self):
        import agriconnect.infrastructure.mcp.context as mod
        from agriconnect.infrastructure.mcp.context import MCPContextServer

        class _FakeOptimizer:
            def build_context(self, user_id, query, zone=None, crop=None):
                return {"combined_context": "fresh", "token_estimate": 10}

        mod._optimizer = _FakeOptimizer()
        server = MCPContextServer()
        result = server.read_user_context("u2", query="bonjour")
        assert result["combined_context"] == "fresh"

    def test_read_user_context_error_payload_is_normalized(self):
        from agriconnect.infrastructure.mcp.context import MCPContextServer
        server = MCPContextServer()  # pas d'optimizer -> build_context renvoie {"error": ...}
        result = server.read_user_context("u3", query="bonjour")
        assert result == {"user_id": "u3", "cached": False}

    def test_check_required_fields_reports_missing(self):
        from agriconnect.infrastructure.mcp.context import MCPContextServer
        result = MCPContextServer.check_required_fields({"a": 1}, ["a", "b"])
        assert result["error"] == "INSUFFICIENT_CONTEXT"
        assert result["missing"] == ["b"]

    def test_check_required_fields_all_present(self):
        from agriconnect.infrastructure.mcp.context import MCPContextServer
        result = MCPContextServer.check_required_fields({"a": 1, "b": 2}, ["a", "b"])
        assert result == {"status": "ok"}
