"""`infrastructure/mcp/context.py` — contexte d'identité MCP (ContextVar)."""
from __future__ import annotations

# =====================================================================
# Contexte scoping (ContextVar)
# =====================================================================

class TestMcpContextScope:
    def test_default_context_is_none(self):
        from ladini.infrastructure.mcp.context import get_mcp_context
        assert get_mcp_context() is None

    def test_set_and_get_roundtrip(self):
        from ladini.infrastructure.mcp.context import (
            FarmerContext,
            get_mcp_context,
            set_mcp_context,
        )
        ctx = FarmerContext(user_id="u1", phone_number="+2260", session_id="s1")
        set_mcp_context(ctx)
        assert get_mcp_context() is ctx
        set_mcp_context(None)

    def test_scope_context_manager_restores_previous_value_on_exit(self):
        from ladini.infrastructure.mcp.context import (
            FarmerContext,
            get_mcp_context,
            mcp_context_scope,
        )
        outer = FarmerContext(user_id="outer", phone_number="+2260", session_id="s1")
        inner = FarmerContext(user_id="inner", phone_number="+2261", session_id="s2")
        from ladini.infrastructure.mcp.context import set_mcp_context
        set_mcp_context(outer)
        with mcp_context_scope(inner):
            assert get_mcp_context() is inner
        assert get_mcp_context() is outer
        set_mcp_context(None)
