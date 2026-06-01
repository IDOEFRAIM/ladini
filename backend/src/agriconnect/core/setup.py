import logging
from typing import Any, Dict, Optional

# Configuration et Core
from agriconnect.core.settings import settings
import agriconnect.core.database as _core_db
import agriconnect.core.db as core_db_new
from agriconnect.services.db_handler import AgriDatabase
from agriconnect.rag.components import get_groq_sdk
from agriconnect.core.tracing import init_tracing

# Protocoles
from agriconnect.protocols.mcp import (
    MCPDatabaseServer, MCPRagServer, MCPWeatherServer, MCPContextServer
)
from agriconnect.protocols.ag_ui import WhatsAppRenderer, WebRenderer, SMSRenderer

# Mémoire 3 niveaux
from agriconnect.services.memory import (
    UserFarmProfile, ProfileExtractor, EpisodicMemory, ContextOptimizer
)

logger = logging.getLogger(__name__)

class AgriContext:
    """
    Système Nerveux Central AgriConnect.
    Gère le cycle de vie des ressources (DB, MCP, Mémoire).
    """

    def __init__(self, llm_client=None):
        self.llm = llm_client or get_groq_sdk()
        
        # Ressources Core
        self.db: Optional[AgriDatabase] = None
        self.session_factory: Any = None
        self.memory: Optional[ContextOptimizer] = None
        
        # Protocoles & Tracing
        self.mcp: Dict[str, Any] = {}
        self.renderers: Dict[str, Any] = {}
        # MCP Shield (permission client) - injected by orchestrator when available
        self.mcp_shield: Any = None
        self.tracing_enabled: bool = False

    def bootstrap(self):
        """Lance l'initialisation dans l'ordre de dépendance strict."""
        self._init_tracing()
        # Initialise DB and memory first so MCP DB server can reuse
        # the created session factory if available, then initialise MCPs.
        self._init_db_and_memory()
        self._init_mcp_servers()
        self._init_renderers()
        logger.info("AgriContext bootstrap terminé.")
        return self

    def _init_tracing(self):
        """Initialise LangSmith avant tout le reste."""
        try:
            self.tracing_enabled = init_tracing()
        except Exception as e:
            logger.warning(f"Tracing non disponible: {e}")

    def _init_db_and_memory(self):
        """Initialise la persistence et la mémoire épisodique."""
        # Setup Database - prefer new centralized sync DB hub
        try:
            if getattr(core_db_new, '_SYNC_ENGINE', None) is not None and getattr(core_db_new, '_SYNC_SESSION_FACTORY', None) is not None:
                self.db = AgriDatabase(engine=core_db_new._SYNC_ENGINE, session_factory=core_db_new._SYNC_SESSION_FACTORY)
                self.session_factory = core_db_new._SYNC_SESSION_FACTORY
            elif settings.DATABASE_URL:
                self.db = AgriDatabase(db_url=settings.DATABASE_URL)
                logger.warning("DB: Fallback URL utilisé.")
            else:
                # Try lazy init of core_db
                try:
                    eng = core_db_new.get_engine()
                    sf = getattr(core_db_new, '_SYNC_SESSION_FACTORY', None)
                    if eng is not None and sf is not None:
                        self.db = AgriDatabase(engine=eng, session_factory=sf)
                        self.session_factory = sf
                except Exception:
                    # Keep previous behavior of falling back to _core_db if present
                    if getattr(_core_db, '_engine', None) and getattr(_core_db, '_SessionLocal', None):
                        self.db = AgriDatabase(engine=_core_db._engine, session_factory=_core_db._SessionLocal)
                        self.session_factory = _core_db._SessionLocal
        except Exception as e:
            logger.warning(f"DB init fallback triggered: {e}")

        # Setup Mémoire (Dépend de la session DB)
        if self.session_factory:
            try:
                profile = UserFarmProfile(self.session_factory)
                episodic = EpisodicMemory(self.session_factory, llm_client=self.llm)
                extractor = ProfileExtractor(self.llm, profile)
                self.memory = ContextOptimizer(profile, episodic, extractor)
                logger.info("Mémoire 3 niveaux activée")
            except Exception as e:
                logger.error(f"Erreur Mémoire: {e}")

    def _init_mcp_servers(self):
        """Initialise les serveurs MCP (Système de Tools)."""
        try:
            # Use singleton getters to avoid repeated server initialisations
            from agriconnect.protocols.mcp import (
                get_mcp_db_server,
                get_mcp_rag_server,
                get_mcp_weather_server,
                get_mcp_context_server,
            )

            # Attempt to instantiate MCP DB server. Prefer passing the
            # session_factory when available, but fall back to a no-arg
            # constructor if the first attempt returns None.
            try:
                logger.debug("MCPDatabaseServer class: %r", MCPDatabaseServer)
                db_server = get_mcp_db_server(self.session_factory) if self.session_factory else get_mcp_db_server()
                if db_server is None and self.session_factory:
                    db_server = get_mcp_db_server()
            except Exception:
                db_server = None

            self.mcp = {
                "db": db_server,
                "rag": get_mcp_rag_server(),
                "weather": get_mcp_weather_server(llm_client=self.llm),
                "context": None,
            }
            # As a last resort, instantiate the infrastructure wrapper
            # directly so the app has an MCP DB facade available.
            if self.mcp.get("db") is None:
                try:
                    from agriconnect.infrastructure.mcp.runtime import AgriDBMCPServer
                    

                    self.mcp["db"] = AgriDBMCPServer()
                    logger.debug("Instantiated AgriDBMCPServer fallback: %r", self.mcp.get("db"))
                except Exception:
                    logger.debug("No MCP DB server available; continuing with local DB")
            logger.debug("MCP DB server object: %r", self.mcp.get("db"))

            if self.memory:
                self.mcp["context"] = get_mcp_context_server(context_optimizer=self.memory)
            elif self.session_factory:
                self.mcp["context"] = get_mcp_context_server(session_factory=self.session_factory, llm_client=self.llm)

            # Expose the MCP DB server as `mcp_db` for tool/shield usage but
            # keep `self.db` pointing to the local AgriDatabase instance to
            # preserve synchronous persistence APIs used elsewhere.
            if self.mcp.get("db"):
                try:
                    setattr(self, "mcp_db", self.mcp["db"])
                    logger.debug("MCP DB facade attached on AgriContext.mcp_db")
                except Exception:
                    logger.exception("Échec de l'attachement du serveur MCP DB facade")

            logger.info("MCP Servers configurés (singletons)")
        except Exception as e:
            logger.error(f"Erreur MCP: {e}")

    def _init_renderers(self):
        """Initialise les moteurs de rendu AG-UI."""
        self.renderers = {
            "whatsapp": WhatsAppRenderer(),
            "web": WebRenderer(),
            "sms": SMSRenderer(),
        }
