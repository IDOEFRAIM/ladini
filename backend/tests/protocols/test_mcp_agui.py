"""
Tests unitaires — MCP Servers (Database, RAG, Weather, Context) + AG-UI Renderers.
"""

import pytest
from unittest.mock import MagicMock, patch


class TestMCPDatabaseServer:
    """Teste le serveur MCP Database."""

    def test_init(self):
        from agriconnect.protocols.mcp import MCPDatabaseServer

        mock_factory = MagicMock()
        server = MCPDatabaseServer(mock_factory)
        assert server is not None

    def test_call_tool_get_user(self):
        from agriconnect.protocols.mcp.mcp_db import MCPDatabaseServer

        mock_factory = MagicMock()
        mock_session = MagicMock()
        mock_factory.return_value = mock_session
        mock_session.execute.return_value.fetchone.return_value = None

        server = MCPDatabaseServer(mock_factory)
        result = server.call_tool("get_user", {"phone": "+22670000000"})
        assert isinstance(result, dict)


class TestMCPRagServer:
    """Teste le serveur MCP RAG."""

    def test_init(self):
        from agriconnect.protocols.mcp import MCPRagServer

        server = MCPRagServer()
        assert server is not None

    def test_list_tools(self):
        from agriconnect.protocols.mcp.mcp_rag import MCPRagServer

        server = MCPRagServer()
        tools = server.list_tools()
        assert isinstance(tools, list)
        assert len(tools) > 0
        # Verify tool names match actual implementation
        tool_names = [t["name"] for t in tools]
        assert "search_agronomy_docs" in tool_names


class TestMCPWeatherServer:
    """Teste le serveur MCP Weather."""

    def test_init(self):
        from agriconnect.protocols.mcp import MCPWeatherServer

        server = MCPWeatherServer()
        assert server is not None

    def test_list_tools(self):
        from agriconnect.protocols.mcp.mcp_weather import MCPWeatherServer

        server = MCPWeatherServer()
        tools = server.list_tools()
        assert isinstance(tools, list)
        tool_names = [t["name"] for t in tools]
        assert any("weather" in name.lower() for name in tool_names)


class TestMCPContextServer:
    """Teste le serveur MCP Context."""

    def test_init_with_optimizer(self):
        from agriconnect.protocols.mcp import MCPContextServer

        mock_optimizer = MagicMock()
        server = MCPContextServer(context_optimizer=mock_optimizer)
        assert server is not None

    def test_list_tools(self):
        from agriconnect.protocols.mcp.mcp_context import MCPContextServer

        mock_optimizer = MagicMock()
        server = MCPContextServer(context_optimizer=mock_optimizer)
        tools = server.list_tools()
        assert isinstance(tools, list)
        assert len(tools) > 0


class TestAGUIComponents:
    """Teste les composants AG-UI."""

    def test_agri_response_creation(self):
        from agriconnect.protocols.ag_ui.components import AgriResponse

        resp = AgriResponse(agent_name="FormationCoach")
        assert resp.agent == "FormationCoach"
        assert resp.components == []

    def test_agri_response_add_text(self):
        from agriconnect.protocols.ag_ui.components import AgriResponse

        resp = AgriResponse(agent_name="sentinelle")
        resp.add_text("Il pleut demain", voice="Il pleut demain à Ouaga")
        assert len(resp.components) == 1
        assert resp.components[0].content == "Il pleut demain"

    def test_agri_response_to_dict(self):
        from agriconnect.protocols.ag_ui.components import AgriResponse

        resp = AgriResponse(agent_name="market")
        resp.add_text("Prix du maïs: 250 FCFA")
        d = resp.to_dict()
        assert d["agent"] == "market"
        assert len(d["components"]) == 1

    def test_agri_response_add_card(self):
        from agriconnect.protocols.ag_ui.components import AgriResponse

        resp = AgriResponse(agent_name="formation")
        resp.add_card(title="Compostage", body="Étapes pour faire du compost")
        assert len(resp.components) == 1


class TestAGUIRenderers:
    """Teste les renderers AG-UI."""

    def test_whatsapp_renderer(self):
        from agriconnect.protocols.ag_ui import WhatsAppRenderer, AgriResponse

        renderer = WhatsAppRenderer()
        resp = AgriResponse(agent_name="market")
        resp.add_text("Test message")
        rendered = renderer.render(resp)
        assert isinstance(rendered, dict)
        assert rendered["channel"] == "whatsapp"
        assert len(rendered["messages"]) >= 1

    def test_web_renderer(self):
        from agriconnect.protocols.ag_ui import WebRenderer, AgriResponse

        renderer = WebRenderer()
        resp = AgriResponse(agent_name="sentinelle")
        resp.add_text("Alerte météo")
        rendered = renderer.render(resp)
        assert rendered is not None

    def test_sms_renderer(self):
        from agriconnect.protocols.ag_ui import SMSRenderer, AgriResponse

        renderer = SMSRenderer()
        resp = AgriResponse(agent_name="formation")
        resp.add_text("Info courte")
        rendered = renderer.render(resp)
        assert isinstance(rendered, (str, dict))

    def test_whatsapp_empty_response(self):
        from agriconnect.protocols.ag_ui import WhatsAppRenderer, AgriResponse

        renderer = WhatsAppRenderer()
        resp = AgriResponse(agent_name="test")
        rendered = renderer.render(resp)
        assert rendered["messages"] == []
