"""Helpers extracted from message_flow to reduce file size.

Ce module regroupe l'initialisation DB, protocoles, experts,
services et tracing qui alourdissaient `message_flow.py`.
"""
import logging
import os
from agriconnect.rag.components import get_groq_sdk
from agriconnect.core.setup import AgriContext
from agriconnect.services.memory import (
    UserFarmProfile,
    ProfileExtractor,
    EpisodicMemory,
    ContextOptimizer,
)
from agriconnect.protocols.mcp import MCPDatabaseServer, MCPRagServer, MCPWeatherServer, MCPContextServer
from agriconnect.protocols.ag_ui import WhatsAppRenderer, WebRenderer, SMSRenderer
from agriconnect.services.voice import VoiceEngine
from agriconnect.services.db_handler import AgriDatabase
from agriconnect.core.settings import settings
import agriconnect.core.database as _core_db
from agriconnect.graphs.nodes.sentinelle import ClimateSentinel
from agriconnect.graphs.nodes.formation import FormationCoach
from agriconnect.graphs.nodes.market import MarketCoach
from agriconnect.graphs.nodes.marketplace_v3 import MarketplaceAgentV3
from agriconnect.services.database.database_service import AgriDatabaseService
# ParallelExecutor removed — fan-out now handled by LangGraph Send

logger = logging.getLogger(__name__)


def init_db_and_memory(flow):
    if _core_db._engine and _core_db._SessionLocal:
        flow.db = AgriDatabase(engine=_core_db._engine, session_factory=_core_db._SessionLocal)
        flow.session_factory = _core_db._SessionLocal
    elif settings.DATABASE_URL:
        flow.db = AgriDatabase(db_url=settings.DATABASE_URL)
        flow.session_factory = None
        logger.warning("⚠️  DB: fallback engine propre (core/database.py non initialisé)")
    else:
        flow.db = None
        flow.session_factory = None

    flow.memory = None
    if flow.session_factory:
        try:
            _profile = UserFarmProfile(flow.session_factory)
            _episodic = EpisodicMemory(flow.session_factory, llm_client=flow.llm)
            _extractor = ProfileExtractor(flow.llm, _profile)
            flow.memory = ContextOptimizer(_profile, _episodic, _extractor)
            logger.info("🧠 Mémoire 3 niveaux activée")
        except Exception as e:
            logger.warning("⚠️  Mémoire désactivée: %s", e)


def init_protocols(flow):
    """Initialize MCP + Shield stack.  The Orchestrator is the SOLE HOST.

    Chain: AgriDBMCPServer (backend) → MCPPermissionClient (shield)
           → MCPPermissionHostApp (preflight) → MCPSessionManager (session/UI)

    Experts receive **only** the MCPSessionManager via dependency injection;
    they never instantiate their own MCP clients.
    """
    from agriconnect.protocols.mcp.security import (
        MCPPermissionClient,
        MCPPermissionHostApp,
        MCPSessionManager,
    )
    from agriconnect.protocols.mcp.infrastructure import AgriDBMCPServer

    flow.mcp_db = None
    flow.mcp_rag = None
    flow.mcp_weather = None
    flow.mcp_context = None
    try:
        flow.mcp_db = flow.ctx.mcp.get("db")
        flow.mcp_rag = flow.ctx.mcp.get("rag")
        flow.mcp_weather = flow.ctx.mcp.get("weather")
        flow.mcp_context = flow.ctx.mcp.get("context")
        logger.info("MCP Servers loaded from AgriContext")
    except Exception as e:
        logger.warning("MCP Servers fallback: %s", e)

    # ── Build the Shield stack (single authority) ────────────────────
    db_backend = flow.mcp_db if flow.mcp_db else AgriDBMCPServer()

    # HITL callback placeholder — will be wired to Gradio/WhatsApp later
    async def _hitl_callback(tool_name, args, reason):
        """Default HITL: deny and log. Override at runtime for real UI."""
        logger.warning("HITL requested for '%s' — no UI wired, denying. Reason: %s", tool_name, reason)
        return False

    shield_client = MCPPermissionClient(
        backend=db_backend,
        session_id="orchestrator",
        hitl_callback=_hitl_callback,
    )
    shield_host = MCPPermissionHostApp(client=shield_client)
    shield_session = MCPSessionManager(host=shield_host, session_id="orchestrator")

    # Expose on flow and context for dependency injection into experts
    flow.mcp_shield = shield_client
    flow.mcp_host = shield_host
    flow.mcp_session = shield_session
    setattr(flow.ctx, "mcp_shield", shield_client)
    setattr(flow.ctx, "mcp_host", shield_host)
    setattr(flow.ctx, "mcp_session", shield_session)

    # A2A disabled — local workflow invocations only
    flow.a2a = None

    flow.renderers = {
        "whatsapp": WhatsAppRenderer(),
        "web": WebRenderer(),
        "sms": SMSRenderer(),
    }


def init_experts(flow):
    """Initialize expert agents.  Experts receive the Shield via DI — they
    never create their own MCPPermissionClient.

    The ``mcp_session`` (MCPSessionManager) is passed to experts that need
    DB access.  Read-only experts (sentinelle, formation) use it for safe
    reads; write-capable experts (market, marketplace) use it for guarded
    writes with HITL support.
    """
    shield = getattr(flow, "mcp_session", None)

    flow.sentinelle = ClimateSentinel(llm_client=flow.llm, mcp_session=shield)
    # Pass the orchestrator session/permission manager (shield) to FormationCoach
    # so it has the required Shield for MCP tool calls and auditing.
    flow.formation = FormationCoach(
        llm_client=flow.llm,
        mcp_rag=flow.mcp_rag,
        mcp_context=flow.mcp_context,
        shield=shield,
    )
    # Market & Marketplace receive the Shield — NOT a raw MCP server
    flow.market = MarketCoach(llm_client=flow.llm, mcp_session=shield)

    db_service = AgriDatabaseService()
    flow.marketplace = MarketplaceAgentV3(
        llm_client=flow.llm,
        mcp_session=shield,
        db_service=db_service,
    )
    # Ensure marketplace tool uses the Shield client for MCP delegations
    try:
        # MarketplaceAgentV3 creates MarketplaceToolV3(self.tool)
        if hasattr(flow.marketplace, "tool") and getattr(flow, "mcp_shield", None):
            flow.marketplace.tool.mcp_client = flow.mcp_shield
    except Exception:
        logger.debug("Could not attach mcp_shield to marketplace tool; will use local db_service fallback")
    # Legacy MarketplaceAgent removed; MarketplaceAgentV3 is the canonical implementation.

    # Legacy workflow builders and invoker removed: experts are instantiated
    # directly by the Host via EXPERT_MAP and DI (mcp_session / mcp_shield).


def init_services(flow):
    azure_key = settings.AZURE_SPEECH_KEY
    azure_fallback = settings.AZURE_SPEECH_KEY_2 or None
    azure_region = settings.AZURE_REGION
    ci_mode = os.getenv("CI", "false").lower() == "true"
    if ci_mode:
        logger.info("CI mode detected (CI=true): disabling TTS for headless tests")

    tts_enabled = not ci_mode and azure_key and settings.USE_AZURE_SPEECH
    if tts_enabled:
        flow.voice = VoiceEngine(
            api_key=azure_key,
            region=azure_region,
            fallback_key=azure_fallback,
            storage_dir=settings.AUDIO_OUTPUT_DIR,
        )
    else:
        flow.voice = None


def init_tracing(flow):
    from agriconnect.core.tracing import init_tracing
    flow._tracing_ok = init_tracing()
    if flow._tracing_ok:
        logger.info("🔭 LangSmith tracing actif pour l'orchestrateur")
