# ruff: noqa: E402
# Les imports `mcp.*`/`agriconnect.*` ci-dessous suivent DÉLIBÉRÉMENT la
# redirection stdout/stderr : le transport MCP stdio exige que stdout ne
# porte QUE du JSON-RPC — un import tiers qui printerait avant la
# redirection corromprait le flux. Ne pas réordonner.
import asyncio
import io
import json
import logging
import sys

# REDIRECTION TOTALE IMMÉDIATE

# On ferme stdout/stderr originaux pour les forcer à un état propre

sys.stderr = open("server_debug.log", "a", encoding="utf-8", buffering=1)

# stdout DOIT rester un flux texte propre pour le JSON

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", write_through=True)


# Configuration du logging vers le fichier

logging.basicConfig(level=logging.INFO, stream=sys.stderr)

logger = logging.getLogger("mcp.agriconnect")


import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server

from agriconnect.infrastructure.mcp.runtime import AgriDBMCPServer, runtime


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
                    inputSchema=t[
                        "inputSchema"
                    ],  # Transmet le schéma au client (Claude/Inspecteur)
                )
                for t in backend_tools
            ]

        @self.server.call_tool()
        async def call_tool(
            name: str, arguments: dict | None = None
        ) -> list[types.TextContent]:
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

                if isinstance(result, str):
                    payload = result

                else:
                    payload = json.dumps(result, ensure_ascii=False, default=str)

                return [types.TextContent(type="text", text=payload)]

            except Exception as exc:
                # Gestion des erreurs pour éviter que le serveur ne plante

                logger.error(f"Erreur lors de l'exécution de l'outil {name}: {exc}")

                # On retourne l'erreur au client MCP de manière élégante

                payload = json.dumps(
                    {"ok": False, "error": str(exc)}, ensure_ascii=False, default=str
                )

                return [types.TextContent(type="text", text=payload)]

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
    asyncio.run(main())
