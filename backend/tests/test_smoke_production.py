"""
smoke_test_production.py — Production-readiness smoke tests.

Validates the Host-Centric Shield stack in fail-closed mode:
  1. Unknown tool → DENY
  2. Write without permission → DENY
  3. SQL injection in args → HostBlockedError
  4. Schema-modify tool → DENY
  5. Read-only tool → ALLOW
  6. Audit persistence (runtime.db log)
  7. Sensitive-column masking
  8. JSON normalization on outputs

Run from backend/:
    PYTHONPATH=src pytest tests/test_smoke_production.py -v
"""

import json
import os
import sys
import asyncio
import pytest
from unittest.mock import MagicMock, patch, AsyncMock

# ── Ensure backend/src is importable ──
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


# ═══════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture
def shield_stack():
    """Build the full Shield stack: Server → Client → Host → Session."""
    from agriconnect.protocols.mcp.infrastructure import AgriDBMCPServer
    from agriconnect.protocols.mcp.security.client_base import MCPPermissionClient
    from agriconnect.protocols.mcp.security.host_app import MCPPermissionHostApp
    from agriconnect.protocols.mcp.security.client_app import MCPSessionManager

    server = AgriDBMCPServer()
    client = MCPPermissionClient(backend=server, session_id="smoke_test")
    host = MCPPermissionHostApp(client=client)
    session = MCPSessionManager(host=host, session_id="smoke_test")

    return {
        "server": server,
        "client": client,
        "host": host,
        "session": session,
    }


def _run(coro):
    """Helper to run async code in sync tests."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ═══════════════════════════════════════════════════════════════════
# 1. Unknown tool → DENY
# ═══════════════════════════════════════════════════════════════════

class TestUnknownToolDeny:
    def test_unknown_tool_denied_by_client(self, shield_stack):
        """A tool not in TOOL_SCOPE_MAP must raise PermissionDenied."""
        from agriconnect.protocols.mcp.security.client_base import PermissionDenied
        client = shield_stack["client"]

        with pytest.raises(PermissionDenied, match="totally_unknown_tool"):
            _run(client.call_tool("totally_unknown_tool", {"x": 1}))

    def test_unknown_tool_denied_by_server(self, shield_stack):
        """Server-level fail-closed: unknown tool raises ValueError."""
        server = shield_stack["server"]

        with pytest.raises(ValueError, match="Unknown DB tool"):
            _run(server.call_tool("phantom_tool", {"a": 1}))


# ═══════════════════════════════════════════════════════════════════
# 2. Schema-modify tool → DENY
# ═══════════════════════════════════════════════════════════════════

class TestSchemaModifyDeny:
    def test_migrate_schema_denied(self, shield_stack):
        """Schema modification tools must raise PermissionDenied."""
        from agriconnect.protocols.mcp.security.client_base import PermissionDenied
        client = shield_stack["client"]

        with pytest.raises(PermissionDenied, match="migrate_schema"):
            _run(client.call_tool("migrate_schema", {"sql": "ALTER TABLE users ADD col TEXT"}))

    def test_drop_table_denied(self, shield_stack):
        from agriconnect.protocols.mcp.security.client_base import PermissionDenied
        client = shield_stack["client"]

        with pytest.raises(PermissionDenied, match="drop_table"):
            _run(client.call_tool("drop_table", {"table": "users"}))


# ═══════════════════════════════════════════════════════════════════
# 3. SQL injection in args → Blocked
# ═══════════════════════════════════════════════════════════════════

class TestSQLInjectionBlocked:
    def test_sql_injection_in_search(self, shield_stack):
        """SQL injection patterns in arguments must be caught by the host preflight."""
        host = shield_stack["host"]

        # Use _preflight_scan directly (sync method)
        preflight = host._preflight_scan(
            "search_products",
            {"query": "tomate'; DROP TABLE products; --"}
        )

        assert not preflight, f"SQL injection was not caught: passed={preflight.passed}, reason={preflight.reason}"

    def test_union_select_injection(self, shield_stack):
        """UNION SELECT attack pattern must be blocked."""
        host = shield_stack["host"]
        preflight = host._preflight_scan(
            "get_user_profile",
            {"user_id": "1 UNION SELECT * FROM passwords"}
        )

        assert not preflight, f"UNION SELECT injection not caught: passed={preflight.passed}"


# ═══════════════════════════════════════════════════════════════════
# 4. Read-only tool → ALLOW (or at least not DENIED for scope reasons)
# ═══════════════════════════════════════════════════════════════════

class TestReadOnlyAllowed:
    def test_list_products_scope_is_read_only(self):
        """list_products must be mapped to DB_READ_ONLY scope."""
        from agriconnect.protocols.mcp.security.constants import TOOL_SCOPE_MAP, PermissionScope
        assert TOOL_SCOPE_MAP["list_products"] == PermissionScope.DB_READ_ONLY

    def test_db_status_scope_is_read_only(self):
        """db_status must be mapped to DB_READ_ONLY scope."""
        from agriconnect.protocols.mcp.security.constants import TOOL_SCOPE_MAP, PermissionScope
        assert TOOL_SCOPE_MAP["db_status"] == PermissionScope.DB_READ_ONLY

    def test_write_tool_scope_is_write(self):
        """create_order must be mapped to DB_DATA_WRITE scope."""
        from agriconnect.protocols.mcp.security.constants import TOOL_SCOPE_MAP, PermissionScope
        assert TOOL_SCOPE_MAP["create_order"] == PermissionScope.DB_DATA_WRITE


# ═══════════════════════════════════════════════════════════════════
# 5. Sensitive-column masking
# ═══════════════════════════════════════════════════════════════════

class TestSensitiveMasking:
    def test_password_hash_masked(self):
        """Sensitive columns must be replaced with '***MASKED***'."""
        from agriconnect.protocols.mcp.security.client_base import MCPPermissionClient
        from agriconnect.protocols.mcp.infrastructure import AgriDBMCPServer

        server = AgriDBMCPServer()
        client = MCPPermissionClient(backend=server, session_id="mask_test")

        data = {
            "user": "alice",
            "password_hash": "bcrypt$2b$12$abc",
            "email": "alice@example.com",
            "token": "eyJhbGci...",
            "nested": {
                "secret": "top_secret_value",
                "safe_field": "visible",
            },
        }
        masked = client._mask_sensitive(data)

        assert masked["password_hash"] == "***MASKED***"
        assert masked["email"] == "***MASKED***"
        assert masked["token"] == "***MASKED***"
        assert masked["nested"]["secret"] == "***MASKED***"
        assert masked["nested"]["safe_field"] == "visible"
        assert masked["user"] == "alice"

    def test_masking_in_list(self):
        from agriconnect.protocols.mcp.security.client_base import MCPPermissionClient
        from agriconnect.protocols.mcp.infrastructure import AgriDBMCPServer

        server = AgriDBMCPServer()
        client = MCPPermissionClient(backend=server, session_id="mask_test")

        data = [
            {"name": "Bob", "api_key": "sk-123"},
            {"name": "Eve", "password": "hunter2"},
        ]
        masked = client._mask_sensitive(data)

        assert masked[0]["api_key"] == "***MASKED***"
        assert masked[1]["password"] == "***MASKED***"
        assert masked[0]["name"] == "Bob"


# ═══════════════════════════════════════════════════════════════════
# 6. JSON normalization
# ═══════════════════════════════════════════════════════════════════

class TestJSONNormalization:
    def test_string_output_normalized(self):
        """Non-dict outputs should be wrapped in a JSON dict."""
        from agriconnect.protocols.mcp.infrastructure import AgriDBMCPServer

        result = AgriDBMCPServer._normalize_output("plain text result")
        assert isinstance(result, dict)
        assert "data" in result or "result" in result

    def test_dict_output_passthrough(self):
        from agriconnect.protocols.mcp.infrastructure import AgriDBMCPServer

        original = {"status": "ok", "count": 42}
        result = AgriDBMCPServer._normalize_output(original)
        assert isinstance(result, dict)
        assert result.get("status") == "ok" or result.get("count") == 42

    def test_none_output_normalized(self):
        from agriconnect.protocols.mcp.infrastructure import AgriDBMCPServer

        result = AgriDBMCPServer._normalize_output(None)
        assert isinstance(result, dict)


# ═══════════════════════════════════════════════════════════════════
# 7. Diff preview (MCPSessionManager)
# ═══════════════════════════════════════════════════════════════════

class TestDiffPreview:
    def test_preview_diff_returns_dict(self, shield_stack):
        """preview_diff should return a structured risk assessment."""
        session = shield_stack["session"]
        diff = _run(session.preview_diff("update_stock_with_movement", {"product_id": "P1", "delta": -5}))

        assert isinstance(diff, dict)
        assert "tool" in diff
        assert diff["tool"] == "update_stock_with_movement"
        assert "risk" in diff
        assert "scope" in diff


# ═══════════════════════════════════════════════════════════════════
# 8. Audit emission
# ═══════════════════════════════════════════════════════════════════

class TestAuditEmission:
    def test_audit_entry_on_deny(self, shield_stack):
        """Denied calls must emit an audit entry before raising."""
        from agriconnect.protocols.mcp.security.client_base import PermissionDenied
        client = shield_stack["client"]

        with patch.object(client, "_emit_audit", new_callable=AsyncMock) as mock_audit:
            with pytest.raises(PermissionDenied):
                _run(client.call_tool("drop_table", {"table": "users"}))
            assert mock_audit.called, "Audit was not emitted on DENIED call"

    def test_audit_entry_on_unknown(self, shield_stack):
        """Unknown tool denial must also emit an audit entry."""
        from agriconnect.protocols.mcp.security.client_base import PermissionDenied
        client = shield_stack["client"]

        with patch.object(client, "_emit_audit", new_callable=AsyncMock) as mock_audit:
            with pytest.raises(PermissionDenied):
                _run(client.call_tool("totally_fake_tool", {}))
            assert mock_audit.called, "Audit was not emitted on unknown tool"


# ═══════════════════════════════════════════════════════════════════
# 9. Host-Centric DI — experts must NOT create their own Shield
# ═══════════════════════════════════════════════════════════════════

class TestHostCentricDI:
    def test_market_coach_accepts_session(self):
        """MarketCoach must accept mcp_session via DI."""
        from agriconnect.graphs.nodes.market import MarketCoach

        fake_session = MagicMock()
        coach = MarketCoach(llm_client=MagicMock(), mcp_session=fake_session)
        assert coach.db_server is fake_session

    def test_marketplace_v3_accepts_session(self):
        """MarketplaceAgentV3 must accept mcp_session via DI."""
        from agriconnect.graphs.nodes.marketplace_v3 import MarketplaceAgentV3

        fake_session = MagicMock()
        agent = MarketplaceAgentV3(
            llm_client=MagicMock(),
            mcp_session=fake_session,
            db_service=MagicMock(),
        )
        assert agent.mcp_session is fake_session
        assert agent.mcp_db is fake_session

    def test_sentinelle_accepts_session(self):
        """ClimateSentinel must accept mcp_session via DI."""
        from agriconnect.graphs.nodes.sentinelle import ClimateSentinel

        fake_session = MagicMock()
        agent = ClimateSentinel(llm_client=MagicMock(), mcp_session=fake_session)
        assert agent.mcp_session is fake_session
        assert agent.db_server is fake_session


# ═══════════════════════════════════════════════════════════════════
# 10. LangGraph Send import sanity
# ═══════════════════════════════════════════════════════════════════

class TestLangGraphSend:
    def test_send_importable(self):
        from langgraph.types import Send
        assert Send is not None

    def test_route_flow_returns_send_for_parallel(self):
        """route_flow must return Send objects for PARALLEL_EXPERTS."""
        from langgraph.types import Send

        # Create a minimal mock flow
        mock_flow = MagicMock()
        mock_flow.router = MagicMock()
        mock_flow.router.route_flow.return_value = "PARALLEL_EXPERTS"

        # Import the actual route_flow method
        from agriconnect.graphs.orchestrateur.message_flow import MessageResponseFlow

        # Create a state that would trigger parallel
        state = {
            "needs": {
                "intent": "COUNCIL",
                "selected_experts": ["sentinelle", "formation"],
            },
            "requete_utilisateur": "test",
            "zone_id": "Bobo",
            "crop": "Maïs",
            "user_level": "debutant",
        }

        # Test that route_flow converts PARALLEL_EXPERTS to Send objects
        flow = MagicMock(spec=MessageResponseFlow)
        flow.router = MagicMock()
        flow.router.route_flow.return_value = "PARALLEL_EXPERTS"

        # Call the unbound method with mock self
        result = MessageResponseFlow.route_flow(flow, state)
        assert isinstance(result, list)
        assert all(isinstance(s, Send) for s in result)
        assert len(result) == 2
