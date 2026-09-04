# ruff: noqa: E402
"""Serveur MCP stdio conforme (JSON-RPC 2.0 via le SDK officiel `mcp`).

`AgriConnectMCPEntryPoint` délègue vers
`infrastructure/mcp/runtime.py::AgriDBMCPServer` — les endpoints "Tools"
(actions, avec ou sans effet de bord) et, depuis l'audit MCP/AGUI
2026-08-27, les endpoints "Resources" (lectures PURES, adressables par URI
`agriconnect://catalog/{name}`, réservées au sous-ensemble
`_PUBLIC_CATALOG_TOOLS` — zones, termes bannis — qui n'a besoin d'aucune
identité appelante). Séparer les deux permet à un host MCP de lister ce
qu'il peut consulter sans risque avant de décider d'invoquer un Tool.

Redirection stdout/stderr : le transport stdio exige que stdout ne porte
QUE du JSON-RPC — un import tiers qui printerait avant la redirection
corromprait le flux. Cette redirection n'a donc de sens QUE lorsque ce
fichier tourne comme process serveur réel (voir `if __name__ == "__main__"`
en bas) — jamais à la simple importation du module (tests unitaires,
introspection). L'import du reste de l'app
(`agriconnect.infrastructure.mcp.runtime`, qui cascade sur tout le
backend) est pour cette même raison différé à l'intérieur de
`AgriConnectMCPEntryPoint.__init__`/`_setup_handlers`/`main()`, TOUJOURS
après la redirection dans le cas d'un lancement réel.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys

import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server

logger = logging.getLogger("mcp.agriconnect")


def _redirect_stdio_for_json_rpc() -> None:
    """Redirige stdout/stderr — appelée UNIQUEMENT au lancement réel du
    process stdio (voir `__main__`), jamais à l'import de ce module."""
    import io

    sys.stderr = open("server_debug.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", write_through=True)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)


class AgriConnectMCPEntryPoint:
    def __init__(self):
        # Import différé : évite de cascader sur tout le backend à la simple
        # importation de ce module (voir docstring de fichier).
        from agriconnect.infrastructure.mcp.runtime import AgriDBMCPServer

        # Délégation au backend existant
        self.backend = AgriDBMCPServer()

        self.server = Server("agri-db-server")

        self._setup_handlers()

    def _setup_handlers(self):
        """Délégation des capacités au backend avec typage strict."""
        from agriconnect.infrastructure.mcp.runtime import _PUBLIC_CATALOG_TOOLS

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

                # On retourne l'erreur au client MCP de manière élégante — via
                # `sanitize_error_message` (services/database/errors.py), la
                # MÊME barrière anti-fuite que le reste du backend : un
                # message métier volontaire passe intact, une erreur technique
                # (contrainte SQL, nom de table, dialecte...) devient
                # générique. Ce point d'entrée est PARTAGÉ par les DEUX
                # transports (stdio ET http, voir http_server.py) — le
                # corriger ici les protège tous les deux d'un coup.
                from agriconnect.services.database.errors import (
                    sanitize_error_message,
                )

                payload = json.dumps(
                    {
                        "ok": False,
                        "error": sanitize_error_message(exc, context=f"mcp_tool:{name}"),
                    },
                    ensure_ascii=False,
                    default=str,
                )

                return [types.TextContent(type="text", text=payload)]

        @self.server.list_resources()
        async def list_resources() -> list[types.Resource]:
            """Catalogue public en LECTURE SEULE (`_PUBLIC_CATALOG_TOOLS`) —
            zones, termes bannis : aucun effet de bord, aucune identité
            appelante requise. Distinct des Tools (actions)."""
            return [
                types.Resource(
                    uri=f"agriconnect://catalog/{name}",
                    name=name,
                    description=self.backend.list_tools_by_name(name).get(
                        "description", ""
                    ),
                    mimeType="application/json",
                )
                for name in _PUBLIC_CATALOG_TOOLS
            ]

        @self.server.read_resource()
        async def read_resource(uri) -> str:
            name = str(uri).removeprefix("agriconnect://catalog/")
            if name not in _PUBLIC_CATALOG_TOOLS:
                raise ValueError(f"Resource inconnue: {uri}")
            result = await self.backend.call_tool(name=name, arguments={})
            return json.dumps(result, ensure_ascii=False, default=str)

        # Références directes exposées pour les tests unitaires : les
        # décorateurs `@self.server.*` renvoient la fonction INCHANGÉE (ils
        # n'enregistrent que le wrapper protocolaire en interne), donc ces
        # attributs pointent bien vers les mêmes closures que celles
        # réellement invoquées par le SDK MCP à l'exécution — pas des
        # doublons de logique à maintenir en synchronisation.
        self._list_tools = list_tools
        self._call_tool = call_tool
        self._list_resources = list_resources
        self._read_resource = read_resource

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

    from agriconnect.infrastructure.mcp.runtime import runtime

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
    _redirect_stdio_for_json_rpc()
    asyncio.run(main())
