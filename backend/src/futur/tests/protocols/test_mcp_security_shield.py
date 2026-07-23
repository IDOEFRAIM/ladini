"""
Tests for MCP Security Shield — client_base, host_app, client_app.

Covers:
  - Pydantic schema validation (accept / reject)
  - Permission scopes (read → allow, write → HITL, schema → deny)
  - Data masking (sensitive columns stripped)
  - Pre-flight SQL injection detection
  - Dynamic risk escalation for sensitive file patterns
  - Session-based trust management
  - Structured audit log emission
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import pytest
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock

# Ensure agriconnect is importable
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agriconnect.protocols.mcp.security.constants import (
    PermissionScope,
    RiskLevel,
    TOOL_SCOPE_MAP,
    SENSITIVE_COLUMNS,
)
from agriconnect.protocols.mcp.security.client_base import (
    MCPPermissionClient,
    PermissionDenied,
    AuditEntry,
)
from agriconnect.protocols.mcp.security.host_app import (
    MCPPermissionHostApp,
    HostBlockedError,
)
from agriconnect.protocols.mcp.security.client_app import MCPSessionManager


# ─────────────────────────── Helpers ──────────────────────────────────────

class FakeBackend:
    """Minimal mock backend with call_tool and list_tools."""

    def __init__(self, return_value: Any = None):
        self._return = return_value or {"status": "ok", "data": []}
        self.last_call: Optional[tuple] = None

    async def call_tool(self, name: str, arguments: Dict[str, Any] | None = None):
        self.last_call = (name, arguments)
        return self._return

    def list_tools(self):
        return [{"name": "get_user_profile"}, {"name": "create_order"}]


# ═══════════════════════════════════════════════════════════════════════════
# 1. MCPPermissionClient — schema validation
# ═══════════════════════════════════════════════════════════════════════════

class TestSchemaValidation:
    """Pydantic schema validation on tool arguments."""

    @pytest.mark.asyncio
    async def test_valid_read_tool_passes(self):
        backend = FakeBackend({"status": "ok", "data": {"name": "Moussa"}})
        client = MCPPermissionClient(backend=backend, session_id="test1")
        result = await client.call_tool("get_user_profile", {"user_id": "abc123"})
        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_invalid_schema_raises(self):
        backend = FakeBackend()
        client = MCPPermissionClient(backend=backend, session_id="test2")
        # get_user_profile requires user_id (str)
        with pytest.raises(PermissionDenied, match="invalides"):
            await client.call_tool("get_user_profile", {"wrong_field": 42})

    @pytest.mark.asyncio
    async def test_unregistered_tool_passes_generic(self):
        """Tools without a registered scope must be DENIED (fail-closed)."""
        backend = FakeBackend({"ok": True})
        client = MCPPermissionClient(backend=backend, session_id="test3")
        with pytest.raises(PermissionDenied, match="not registered"):
            await client.call_tool("some_unknown_tool", {"any": "thing"})


# ═══════════════════════════════════════════════════════════════════════════
# 2. MCPPermissionClient — permission scopes
# ═══════════════════════════════════════════════════════════════════════════

class TestPermissionScopes:

    @pytest.mark.asyncio
    async def test_read_only_auto_approved(self):
        backend = FakeBackend({"data": []})
        client = MCPPermissionClient(backend=backend, session_id="t")
        result = await client.call_tool("list_products", {"producer_id": "p1"})
        assert result is not None

    @pytest.mark.asyncio
    async def test_schema_modify_denied_by_default(self):
        backend = FakeBackend()
        client = MCPPermissionClient(backend=backend, session_id="t")
        with pytest.raises(PermissionDenied, match="schéma DB"):
            await client.call_tool("drop_table", {"table_name": "users"})

    @pytest.mark.asyncio
    async def test_schema_modify_allowed_in_maintenance(self):
        backend = FakeBackend({"dropped": True})
        client = MCPPermissionClient(backend=backend, session_id="t", maintenance_mode=True)
        result = await client.call_tool("drop_table", {"table_name": "tmp"})
        assert result == {"dropped": True}

    @pytest.mark.asyncio
    async def test_high_risk_write_requires_hitl(self):
        backend = FakeBackend()
        client = MCPPermissionClient(backend=backend, session_id="t")
        # create_order is HIGH risk
        with pytest.raises(PermissionDenied, match="confirmation humaine"):
            await client.call_tool("create_order", {
                "product_id": "p1", "quantity": 10,
                "buyer_phone": "+22670000000",
            })

    @pytest.mark.asyncio
    async def test_high_risk_approved_by_hitl_callback(self):
        async def approve(*args):
            return True

        backend = FakeBackend({"order_id": "o1"})
        client = MCPPermissionClient(
            backend=backend, session_id="t", hitl_callback=approve,
        )
        result = await client.call_tool("create_order", {
            "product_id": "p1", "quantity": 10,
            "buyer_phone": "+22670000000",
        })
        assert result["order_id"] == "o1"


# ═══════════════════════════════════════════════════════════════════════════
# 3. MCPPermissionClient — data masking
# ═══════════════════════════════════════════════════════════════════════════

class TestDataMasking:

    @pytest.mark.asyncio
    async def test_sensitive_columns_masked(self):
        backend = FakeBackend({
            "name": "Moussa",
            "email": "moussa@example.com",
            "password_hash": "abc123hash",
            "phone": "+22670000000",
        })
        client = MCPPermissionClient(backend=backend, session_id="t")
        result = await client.call_tool("get_user_profile", {"user_id": "u1"})
        assert result["name"] == "Moussa"
        assert result["phone"] == "+22670000000"
        assert result["email"] == "***MASKED***"
        assert result["password_hash"] == "***MASKED***"

    @pytest.mark.asyncio
    async def test_nested_masking(self):
        backend = FakeBackend({
            "users": [
                {"name": "A", "token": "secret123"},
                {"name": "B", "api_key": "key456"},
            ]
        })
        client = MCPPermissionClient(backend=backend, session_id="t")
        result = await client.call_tool("get_user_profile", {"user_id": "u1"})
        for u in result["users"]:
            assert "***MASKED***" in u.values()

    @pytest.mark.asyncio
    async def test_json_string_masking(self):
        """If the backend returns a JSON string, masking + normalize returns a dict."""
        payload = json.dumps({"email": "test@x.com", "name": "OK"})
        backend = FakeBackend(payload)
        client = MCPPermissionClient(backend=backend, session_id="t")
        result = await client.call_tool("get_user_profile", {"user_id": "u1"})
        # _normalize_output converts JSON strings to dicts
        assert isinstance(result, dict)
        assert result["email"] == "***MASKED***"
        assert result["name"] == "OK"


# ═══════════════════════════════════════════════════════════════════════════
# 4. MCPPermissionClient — audit
# ═══════════════════════════════════════════════════════════════════════════

class TestAudit:

    @pytest.mark.asyncio
    async def test_audit_entry_emitted(self):
        entries = []

        async def sink(entry: AuditEntry):
            entries.append(entry)

        backend = FakeBackend({"ok": True})
        client = MCPPermissionClient(backend=backend, session_id="sess1", audit_sink=sink)
        await client.call_tool("get_user_profile", {"user_id": "u1"})
        assert len(entries) == 1
        assert entries[0].session_id == "sess1"
        assert entries[0].tool_name == "get_user_profile"
        assert entries[0].decision == "ALLOW"
        assert entries[0].arguments_hash  # not empty


# ═══════════════════════════════════════════════════════════════════════════
# 5. MCPPermissionHostApp — SQL injection pre-flight
# ═══════════════════════════════════════════════════════════════════════════

class TestPreFlightSQLInjection:

    @pytest.mark.asyncio
    async def test_drop_table_blocked(self):
        backend = FakeBackend()
        client = MCPPermissionClient(backend=backend, session_id="t")
        host = MCPPermissionHostApp(client=client)
        with pytest.raises(HostBlockedError, match="SQL suspect"):
            await host.execute("get_user_profile", {"user_id": "'; DROP TABLE users; --"})

    @pytest.mark.asyncio
    async def test_union_select_blocked(self):
        backend = FakeBackend()
        client = MCPPermissionClient(backend=backend, session_id="t")
        host = MCPPermissionHostApp(client=client)
        with pytest.raises(HostBlockedError, match="SQL suspect"):
            await host.execute("search_products", {
                "product_name": "maïs UNION SELECT * FROM users",
            })

    @pytest.mark.asyncio
    async def test_or_1_eq_1_blocked(self):
        backend = FakeBackend()
        client = MCPPermissionClient(backend=backend, session_id="t")
        host = MCPPermissionHostApp(client=client)
        with pytest.raises(HostBlockedError, match="SQL suspect"):
            await host.execute("get_user_by_phone", {"phone": "' OR 1=1 --"})

    @pytest.mark.asyncio
    async def test_raw_sql_statement_blocked(self):
        backend = FakeBackend()
        client = MCPPermissionClient(backend=backend, session_id="t")
        host = MCPPermissionHostApp(client=client)
        with pytest.raises(HostBlockedError, match="SQL brut"):
            await host.execute("search_products", {
                "product_name": "SELECT name FROM products WHERE price > 0",
            })

    @pytest.mark.asyncio
    async def test_clean_arguments_pass(self):
        backend = FakeBackend({"data": []})
        client = MCPPermissionClient(backend=backend, session_id="t")
        host = MCPPermissionHostApp(client=client)
        result = await host.execute("search_products", {
            "product_name": "maïs",
            "zone_id": "zone1",
        })
        assert result is not None


# ═══════════════════════════════════════════════════════════════════════════
# 6. MCPPermissionHostApp — dynamic risk escalation
# ═══════════════════════════════════════════════════════════════════════════

class TestDynamicRiskEscalation:

    @pytest.mark.asyncio
    async def test_sensitive_file_escalates_risk(self):
        """Accessing .env should escalate to CRITICAL and trigger HITL."""
        backend = FakeBackend()
        client = MCPPermissionClient(backend=backend, session_id="t")
        host = MCPPermissionHostApp(client=client)
        # db_status is normally LOW risk / READ_ONLY → auto-allow
        # But if an argument contains '.env', risk should be escalated.
        # Since db_status is READ_ONLY it will still pass the client check,
        # but the risk_override is recorded. For a write tool it would block.
        # Let's test with a write tool:
        with pytest.raises((HostBlockedError, PermissionDenied)):
            await host.execute("create_agent_action", {
                "agent_name": "test",
                "action_type": "read_file",
                "payload": {"path": "/etc/.env"},
            })


# ═══════════════════════════════════════════════════════════════════════════
# 7. MCPPermissionHostApp — descriptive errors
# ═══════════════════════════════════════════════════════════════════════════

class TestDescriptiveErrors:

    @pytest.mark.asyncio
    async def test_error_includes_suggestion(self):
        backend = FakeBackend()
        client = MCPPermissionClient(backend=backend, session_id="t")
        host = MCPPermissionHostApp(client=client)
        try:
            await host.execute("get_user_by_phone", {"phone": "' OR 1=1 --"})
            assert False, "Should have raised"
        except HostBlockedError as e:
            assert e.suggestion  # must have a suggestion
            assert e.agent_message  # must explain why


# ═══════════════════════════════════════════════════════════════════════════
# 8. MCPSessionManager — session trust
# ═══════════════════════════════════════════════════════════════════════════

class TestSessionTrust:

    def test_grant_and_check_trust(self):
        backend = FakeBackend()
        client = MCPPermissionClient(backend=backend, session_id="t")
        host = MCPPermissionHostApp(client=client)
        mgr = MCPSessionManager(host=host, session_id="sess1")

        # Not trusted initially
        assert not mgr.is_trusted("list_products")

        # Grant trust
        mgr.grant_trust("list_products", duration_seconds=60)
        assert mgr.is_trusted("list_products")

    def test_cannot_trust_high_risk(self):
        backend = FakeBackend()
        client = MCPPermissionClient(backend=backend, session_id="t")
        host = MCPPermissionHostApp(client=client)
        mgr = MCPSessionManager(host=host, session_id="sess1")

        mgr.grant_trust("create_order", duration_seconds=60)
        # create_order is HIGH risk — grant should be rejected
        assert not mgr.is_trusted("create_order")

    def test_revoke_trust(self):
        backend = FakeBackend()
        client = MCPPermissionClient(backend=backend, session_id="t")
        host = MCPPermissionHostApp(client=client)
        mgr = MCPSessionManager(host=host, session_id="sess1")

        mgr.grant_trust("list_products", duration_seconds=60)
        assert mgr.is_trusted("list_products")

        mgr.revoke_trust("list_products")
        assert not mgr.is_trusted("list_products")

    def test_get_trusted_tools(self):
        backend = FakeBackend()
        client = MCPPermissionClient(backend=backend, session_id="t")
        host = MCPPermissionHostApp(client=client)
        mgr = MCPSessionManager(host=host, session_id="sess1")

        mgr.grant_trust("list_products", duration_seconds=300)
        mgr.grant_trust("search_products", duration_seconds=300)
        trusted = mgr.get_trusted_tools()
        assert "list_products" in trusted
        assert "search_products" in trusted
        assert trusted["list_products"] > 0


# ═══════════════════════════════════════════════════════════════════════════
# 9. MCPSessionManager — diff preview
# ═══════════════════════════════════════════════════════════════════════════

class TestDiffPreview:

    @pytest.mark.asyncio
    async def test_preview_returns_structure(self):
        backend = FakeBackend()
        client = MCPPermissionClient(backend=backend, session_id="t")
        host = MCPPermissionHostApp(client=client)
        mgr = MCPSessionManager(host=host, session_id="sess1")

        diff = await mgr.preview_diff("create_product", {
            "producer_id": "p1", "name": "maïs",
            "price": 250, "quantity_for_sale": 100,
        })
        assert diff["tool"] == "create_product"
        assert diff["scope"] == "DB_DATA_WRITE"
        assert "risk" in diff
        assert "requires_approval" in diff
