"""Smoke-test manuel du chemin RÉEL de résolution d'outils MCP.

Adapté (audit MCP/AGUI 2026-08-26) : appelait auparavant ``MCPShield``, une
façade de haut niveau (``ShieldHub``) supprimée parce qu'elle n'était jamais
empruntée en production — seul ce script la construisait. Exerce désormais
directement ``AgriDBMCPServer`` (``infrastructure/mcp/runtime.py``), la
classe que le serveur stdio (``protocols/mcp/servers/db_server.py``) ET le
graphe de l'agent utilisent réellement pour chaque appel d'outil.
"""

import asyncio
import time

from agriconnect.infrastructure.mcp.runtime import AgriDBMCPServer, runtime


async def main():
    if not runtime.start_with_policy():
        print("Runtime MCP indisponible (base de données injoignable).")
        return

    backend = AgriDBMCPServer()

    # 1. Lister les outils pour vérifier que le registre charge bien
    tools = backend.list_tools()
    print(f"Outils détectés: {len(tools)}")
    start = time.time()
    # 2. Appeler un outil de lecture simple
    try:
        result = await backend.call_tool(
            "get_producer_orders",
            {
                "phone": "+212782901759",
                "producer_id": "0669b8b0-8e8b-4838-81de-aaef50538974",
            },
        )
        print("Résultat:", result)
        print(f"Le temps mis:{time.time() - start}")
    except Exception as e:
        print(f"Erreur lors de l'appel: {e}")


if __name__ == "__main__":
    print("=" * 20)
    print("We re starting")

    asyncio.run(main())
    print("=" * 20)
