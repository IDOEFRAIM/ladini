import asyncio
import logging
import sys
import json

# Configure logging immediately to STDERR only (no stdout usage)
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s | %(name)s | %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("mcp.agriconnect")

from mcp.server import NotificationOptions, Server
from mcp.server.stdio import stdio_server
import mcp.types as types
from mcp.server.models import InitializationOptions

# Importation du backend
try:
    from agriconnect.infrastructure.mcp.runtime import runtime, AgriDBMCPServer
    # TOOL_SCHEMAS is optional; fall back to empty mapping when absent
    try:
        from agriconnect.protocols.mcp.h import TOOL_SCHEMAS  # type: ignore
    except Exception:
        TOOL_SCHEMAS = {}
except ImportError as e:
    logger.critical("Missing dependencies for MCP server: %s", e, exc_info=True)
    sys.exit(1)

class AgriConnectMCPEntryPoint:
    def __init__(self):
        # Délégation au backend existant
        self.backend = AgriDBMCPServer()
        self.server = Server("agri-db-server")
        self._setup_handlers()

    def _setup_handlers(self):
        """Délégation des capacités au backend avec typage strict."""

        @self.server.list_tools()
        async def list_tools() -> list[types.Tool]:
            backend_tools = self.backend.list_tools()
            
            return [
                types.Tool(
                    name=t["name"],
                    description=t["description"],
                    inputSchema=t["inputSchema"] # Transmet le schéma au client (Claude/Inspecteur)
                ) for t in backend_tools
            ]

        @self.server.call_tool()
        async def call_tool(name: str, arguments: dict | None = None) -> list[types.TextContent]:
            """
            Point d'entrée du serveur MCP. 
            Cette fonction reçoit les appels du client et les délègue au Backend.
            """
            try:
                # On délègue l'exécution au backend.
                # Note : On passe 'arguments' tel quel, c'est le backend qui fera 
                # le nettoyage (le .pop("name")) et la fusion.
                result = await self.backend.call_tool(name=name, arguments=arguments)

                # On retourne le résultat formaté pour le protocole MCP
                return [
                    types.TextContent(
                        type="text",
                        text=str(result)
                    )
                ]

            except Exception as exc:
                # Gestion des erreurs pour éviter que le serveur ne plante
                logger.error(f"Erreur lors de l'exécution de l'outil {name}: {exc}")
                
                # On retourne l'erreur au client MCP de manière élégante
                return [
                    types.TextContent(
                        type="text",
                        text=f"Erreur : {str(exc)}"
                    )
                ]
    
    async def run(self):
        """Lance le transport STDIO."""
        async with stdio_server() as (read_stream, write_stream):
            logger.info("✅ Serveur MCP prêt sur STDIO")
            init_opts = self.server.create_initialization_options(
                notification_options=NotificationOptions(),
                experimental_capabilities={},
            )
            await self.server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name=init_opts.server_name or "AgriConnect-Database",
                    server_version=init_opts.server_version or "1.0.0",
                    capabilities=init_opts.capabilities,
                    instructions=init_opts.instructions,
                    website_url=init_opts.website_url,
                    icons=init_opts.icons,
                ),
            )

async def main():
    logger.info("🚀 Initialisation du runtime AgriConnect...")

    try:
        # Initialisation de la base de données et des policies
        if not runtime.start_with_policy():
            logger.error("❌ Impossible de démarrer le runtime (DB Policy failure).")
            sys.exit(1)
            
        app = AgriConnectMCPEntryPoint()
        await app.run()
        
    except Exception as e:
        logger.error(f"💥 Crash fatal : {e}", exc_info=True)
        sys.exit(1)
    finally:
        logger.info("🛑 Nettoyage final...")
        runtime.stop()

        

if __name__ == "__main__":
    # Utilisation de asyncio.run pour un cycle de vie propre
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Arrêt manuel reçu.")