from __future__ import annotations

import json
import logging
import os
import sys
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

# Tentative d'import de FastMCP
try:
    from fastmcp import Client
    # Correction : Vérifier le chemin exact selon la version de FastMCP
    # Souvent ElicitResult est dans fastmcp.client.base ou similaire
    from fastmcp.client.elicitation import ElicitResult 
except ImportError:
    Client = None
    ElicitResult = None

# Assurez-vous que ce chemin est correct dans votre structure de projet
from agriconnect.infrastructure.mcp.security import ShieldHub

logger = logging.getLogger(__name__)

class AgriMCPClient:
    """MCP client SDK utilisant FastMCP avec gestion automatique du PYTHONPATH."""

    _MAX_RECONNECT_ATTEMPTS = 2
    _RECONNECT_DELAY_S = 1.0

    def __init__(self, server_script_path: str):
        self.server_script_path = server_script_path
        self.exit_stack = AsyncExitStack()
        self.client: Optional[Client] = None
        self._tools_cache: List[Dict[str, Any]] = []
        self._raw_tools_cache: List[Any] = []
        self._connected: bool = False
        self._last_connect_ts: float = 0.0

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def connect(self):
        if not Client:
            raise ImportError("fastmcp est requis. Installez-le avec 'pip install fastmcp'.")

        server_path = Path(os.fspath(self.server_script_path)).resolve()

        transport: Any
        if server_path.exists() and server_path.suffix.lower() == ".py":
            from fastmcp.client.transports.stdio import PythonStdioTransport

            env = dict(os.environ)

            backend_src: Optional[Path] = None
            for parent in server_path.parents:
                if parent.name == "src" and parent.parent.name == "backend":
                    backend_src = parent
                    break

            if backend_src:
                existing = env.get("PYTHONPATH", "")
                env["PYTHONPATH"] = (
                    str(backend_src)
                    + (os.pathsep + existing if existing else "")
                )
                cwd = str(backend_src.parent.parent)
            else:
                cwd = str(server_path.parent)

            transport = PythonStdioTransport(
                script_path=str(server_path),
                env=env,
                cwd=cwd,
                python_cmd=sys.executable,
            )
            logger.info(f"Connecting to MCP server (stdio): {server_path}")
        else:
            transport = os.fspath(self.server_script_path)
            logger.info(f"Connecting to MCP server: {transport}")

        self.client = Client(
            transport,
            elicitation_handler=self._handle_elicitation,
            progress_handler=self._handle_progress,
            message_handler=self._handle_message,
        )
        await self.exit_stack.enter_async_context(self.client)
        self._connected = True
        self._last_connect_ts = time.monotonic()
        await self.refresh_tools()

    async def close(self):
        self._connected = False
        await self.exit_stack.aclose()
        self.client = None

    async def reconnect(self):
        """Ferme et ré-ouvre la connexion MCP (keep-alive / recovery)."""
        logger.warning("MCP reconnect triggered")
        try:
            await self.close()
        except Exception:
            pass
        self.exit_stack = AsyncExitStack()
        await self.connect()

    async def refresh_tools(self) -> List[Dict[str, Any]]:
        if not self.client:
            raise RuntimeError("Client non connecté.")

        tools_response = await self.client.list_tools()
        self._raw_tools_cache = list(tools_response)
        self._tools_cache = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": getattr(tool, "input_schema", tool.inputSchema) or {"type": "object", "properties": {}},
                },
            }
            for tool in tools_response
        ]
        return self._tools_cache

    async def list_tools(self) -> List[Dict[str, Any]]:
        """Retourne les outils au format OpenAI/LangChain (avec cache)."""
        if not self._tools_cache and self.client:
            await self.refresh_tools()
        return self._tools_cache

    def get_tools_for_langchain(self) -> List[Dict[str, Any]]:
        """Retourne les outils au format OpenAI/LangChain."""
        return self._tools_cache

    @staticmethod
    def _sanitize_arguments(arguments: Any) -> Dict[str, Any]:
        """Postel's Law gate: supprime les None et convertit les types invalides."""
        if not isinstance(arguments, dict):
            return {}
        cleaned: Dict[str, Any] = {}
        for k, v in arguments.items():
            if v is None:
                continue
            cleaned[k] = v
        return cleaned

    async def call_tool(self, tool_name: str, arguments) -> Any:
        if not self.client or not self._connected:
            raise RuntimeError("Client non connecté.")

        safe_args = self._sanitize_arguments(arguments)

        logger.info(
            "MCP_CALL_AUDIT | tool=%s | args=%s",
            tool_name,
            json.dumps(safe_args, default=str, ensure_ascii=False),
        )

        last_exc: Optional[Exception] = None
        for attempt in range(1, self._MAX_RECONNECT_ATTEMPTS + 1):
            try:
                result = await self.client.call_tool(tool_name, safe_args)
                break
            except (ConnectionError, OSError, RuntimeError) as exc:
                last_exc = exc
                logger.warning(
                    "MCP call_tool attempt %d/%d failed (%s): %s",
                    attempt, self._MAX_RECONNECT_ATTEMPTS, tool_name, exc,
                )
                if attempt < self._MAX_RECONNECT_ATTEMPTS:
                    try:
                        await self.reconnect()
                    except Exception as re_exc:
                        logger.error("MCP reconnect failed: %s", re_exc)
                else:
                    raise last_exc  # type: ignore[misc]
            except Exception:
                raise
        else:
            raise RuntimeError(f"MCP call_tool {tool_name} failed after {self._MAX_RECONNECT_ATTEMPTS} attempts")

        if hasattr(result, "content") and isinstance(result.content, list):
            raw_output = "\n".join([c.text for c in result.content if hasattr(c, "text")])
            try:
                return json.loads(raw_output)
            except (json.JSONDecodeError, TypeError):
                return raw_output

        return result

    async def _handle_elicitation(self, message, response_type, params, context):
        logger.warning(f"Server requested elicitation: {message}")
        if ElicitResult:
            return ElicitResult(action="decline")
        return None

    async def _handle_progress(self, progress: float, total: Optional[float], message: Optional[str]) -> None:
        log_msg = f"MCP Progress: {progress}/{total if total else '?'} - {message}"
        logger.debug(log_msg)

    async def _handle_message(self, message):
        # Gestion du rafraîchissement si le serveur notifie un changement d'outils
        if hasattr(message, "method") and message.method == "notifications/tools/list_changed":
            await self.refresh_tools()

class UnifiedMCPClient:
    """Point d'entrée unique avec enforcement de sécurité (Shield)."""

    def __init__(self, session_id: str = "unknown") -> None:
        # Assurez-vous que ShieldHub est bien initialisé
        self._hub = ShieldHub(session_id=session_id)

    async def call_tool(self, tool_name: str, arguments) -> Any:
        return await self._hub.call(tool_name, arguments or {})

    async def list_tools(self) -> Dict[str, List[Dict[str, Any]]]:
        return await self._hub.list_tools()


async def main():
    if len(sys.argv) < 2:
        print("Usage: python client.py <path_to_server.py>")
        sys.exit(1)

    server_script = sys.argv[1]
    logging.basicConfig(level=logging.INFO)

    try:
        async with AgriMCPClient(server_script) as client:
            print("\n--- Liste des outils disponibles ---")
            tools = await client.refresh_tools()
            #for t in tools:
             #   func = t["function"]
              #  print(f"Outil: {func['name']} - {func['description']}")

            # --- AJOUT DU TEST D'APPEL ---
            print("\n--- Test d'appel d'outil ---")
            
            # Test 1 : Normalisation d'unité (test simple sans DB)
            norm = await client.call_tool("get_producer_dashboard", {"producer_id":"3cadb350-59e5-4ad8-ae95-5ff7cc1350bd"})
            print(f"Normalisation (5 tonnes) : {norm}")

            # Test 2 : Liste des produits (test avec DB)
            # Attention : remplacez l'ID par un ID existant dans votre DB DigitalOcean
            # products = await client.call_tool("list_products", {"producer_id": "votre-id-uuid"})
            # print(f"Produits : {products}")

    except Exception as e:
        print(f"Erreur : {e}")



if __name__ == "__main__":
    import asyncio

    # Lancement de la boucle d'événements
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterruption par l'utilisateur (Ctrl+C).")
    except Exception as e:
        print(f"Erreur fatale : {e}")
        sys.exit(1)