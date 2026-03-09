"""
Tests unitaires — Imports & Chargement des modules AgriConnect.
"""

import pytest


class TestCoreImports:
    """Vérifie que tous les modules core se chargent sans erreur."""

    def test_import_settings(self):
        from agriconnect.core.settings import settings
        assert settings is not None

    def test_import_database(self):
        from agriconnect.core.database import init_db, close_db, check_connection
        assert callable(init_db)
        assert callable(close_db)
        assert callable(check_connection)

    def test_import_logger(self):
        from agriconnect.core import logger  # noqa: F401

    def test_import_tracing(self):
        from agriconnect.core.tracing import init_tracing, get_tracing_config
        assert callable(init_tracing)
        assert callable(get_tracing_config)


class TestAgentImports:
    """Vérifie que chaque agent/node se charge correctement."""

    def test_import_formation(self):
        from agriconnect.graphs.nodes.formation import FormationCoach
        assert FormationCoach is not None

    def test_import_sentinelle(self):
        from agriconnect.graphs.nodes.sentinelle import ClimateSentinel
        assert ClimateSentinel is not None

    def test_import_market(self):
        from agriconnect.graphs.nodes.market import MarketCoach
        assert MarketCoach is not None

    def test_import_marketplace(self):
        from agriconnect.graphs.nodes.marketplace import MarketplaceAgent
        assert MarketplaceAgent is not None

    def test_import_voice_agent(self):
        from agriconnect.graphs.nodes.voice import VoiceAgent
        assert VoiceAgent is not None


class TestServiceImports:
    """Vérifie les services."""

    def test_import_db_handler(self):
        from agriconnect.services.db_handler import AgriDatabase
        assert AgriDatabase is not None

    def test_import_models(self):
        from agriconnect.services.models import (
            Base, User, Zone, Alert, Conversation, ConversationMessage,
            MarketItem, WeatherData, SurplusOffer, AgentAction,
        )
        assert Base is not None
        assert User.__tablename__ == "users"
        assert Conversation.__tablename__ == "conversations"

    def test_import_voice_engine(self):
        from agriconnect.services.voice_engine import VoiceEngine
        assert VoiceEngine is not None

    def test_import_voice_reexport(self):
        from agriconnect.services.voice import VoiceEngine
        assert VoiceEngine is not None

    def test_import_llm_clients(self):
        from agriconnect.services.llm_clients import get_groq_client
        assert callable(get_groq_client)


class TestProtocolImports:
    """Vérifie les protocoles MCP / A2A / AG-UI."""

    def test_import_mcp_db(self):
        from agriconnect.protocols.mcp import MCPDatabaseServer
        assert MCPDatabaseServer is not None

    def test_import_mcp_rag(self):
        from agriconnect.protocols.mcp import MCPRagServer
        assert MCPRagServer is not None

    def test_import_mcp_weather(self):
        from agriconnect.protocols.mcp import MCPWeatherServer
        assert MCPWeatherServer is not None

    def test_import_mcp_context(self):
        from agriconnect.protocols.mcp import MCPContextServer
        assert MCPContextServer is not None

    def test_import_a2a(self):
        from agriconnect.protocols.a2a import A2ADiscovery, A2AMessage, MessageType
        assert A2ADiscovery is not None
        assert A2AMessage is not None

    def test_import_ag_ui(self):
        from agriconnect.protocols.ag_ui import AgriResponse, WhatsAppRenderer, WebRenderer
        assert AgriResponse is not None


class TestToolImports:
    """Vérifie que les outils se chargent."""

    def test_import_tools_package(self):
        from agriconnect.tools import (
            HealthDoctorTool, AgrimarketTool, BurkinaCropTool,
            MeteoAdvisorTool, FloodRiskTool, SoilDoctorTool,
            SubventionTool, SentinelleTool,
        )
        assert HealthDoctorTool is not None
        assert SentinelleTool is not None

    def test_import_tools_db_handler(self):
        from agriconnect.tools.db_handler import get_db
        assert callable(get_db)


class TestOrchestratorImports:
    """Vérifie l'orchestrateur."""

    def test_import_message_flow(self):
        from agriconnect.graphs.orchestrateur.message_flow import MessageResponseFlow
        assert MessageResponseFlow is not None

    def test_import_state(self):
        from agriconnect.graphs.state import GlobalAgriState, ExpertResponse
        assert GlobalAgriState is not None
