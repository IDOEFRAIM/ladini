"""
MCP Client for AgriConnect Agents.

This module provides a generic MCP client that connects to local or remote MCP servers
(via stdio transport) and exposes their tools to AgriConnect agents (LangGraph/LangChain).

It is designed to replace direct service imports where appropriate, allowing agents
to interact with tools via the standardized Model Context Protocol.
"""

import sys
import json
import logging
from contextlib import AsyncExitStack
from typing import Any, Dict, List, Optional

# fastmcp might not be installed in all environments; handle gracefully or assume present as per user request
try:
    from fastmcp import Client
    from fastmcp.client.elicitation import ElicitResult
except ImportError:
    # Fallback or re-raise if essential
    Client = None
    ElicitResult = None

logger = logging.getLogger(__name__)

class AgriMCPClient:
    """
    AgriConnect MCP Client.
    
    Manages connections to MCP servers and provides methods for agents to:
    - Discover tools
    - Execute tools
    - manage lifecycle (connect/disconnect)
    """

    def __init__(self, server_script_path: str):
        """
        Initialize the client for a specific server script.
        
        Args:
            server_script_path: Path to the .py server script to run.
        """
        self.server_script_path = server_script_path
        self.exit_stack = AsyncExitStack()
        self.client: Optional[Client] = None
        self._tools_cache: List[Dict[str, Any]] = []

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def connect(self):
        """Establish connection to the MCP server via stdio."""
        if not Client:
            raise ImportError("fastmcp is required to use AgriMCPClient. Please install it.")

        logger.info(f"Connecting to MCP server: {self.server_script_path}")
        
        # We use the same python interpreter as the current process
        self.client = Client(
            self.server_script_path, 
            elicitation_handler=self._handle_elicitation,
            progress_handler=self._handle_progress,
            message_handler=self._handle_message
        )
        
        await self.exit_stack.enter_async_context(self.client)
        logger.info("Connected to MCP server.")
        
        # Pre-fetch tools
        await self.refresh_tools()

    async def close(self):
        """Close connection and clean up resources."""
        logger.info("Closing MCP client connection...")
        await self.exit_stack.aclose()
        self.client = None

    async def refresh_tools(self) -> List[Dict[str, Any]]:
        """Fetch available tools from the server and cache them."""
        if not self.client:
            raise RuntimeError("Client not connected.")
            
        tools_response = await self.client.list_tools()
        
        # Format for OpenAI/Groq function calling (JSON Schema)
        self._tools_cache = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": tool.inputSchema or {"type": "object", "properties": {}},
                }
            }
            for tool in tools_response
        ]
        return self._tools_cache

    def get_tools_for_langchain(self) -> List[Dict[str, Any]]:
        """Return tools in the format expected by LangChain/LangGraph bind_tools."""
        # Typically this is the OpenAI format (type: function, etc.)
        return self._tools_cache

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """Execute a tool on the MCP server."""
        if not self.client:
            raise RuntimeError("Client not connected.")
            
        logger.debug(f"Calling MCP tool: {tool_name} with {arguments}")
        try:
            result = await self.client.call_tool(tool_name, arguments)
            
            # The result content might be a list of TextContent/ImageContent objects
            # We standardize it to a string or structured data for the agent
            if hasattr(result, "content"):
                content = result.content
                if isinstance(content, list):
                    # Join text parts
                    text_parts = [c.text for c in content if hasattr(c, "text")]
                    return "\n".join(text_parts)
                return content
            return result
            
        except Exception as e:
            logger.error(f"Error calling tool {tool_name}: {e}")
            raise

    # --- Handlers required by FastMCP Client ---

    async def _handle_elicitation(self, message: str, response_type: type, params, context):
        """Handle user input requests (for now, auto-decline or log)."""
        logger.warning(f"Server requested elicitation (not supported in headless agent): {message}")
        if ElicitResult:
            return ElicitResult(action="decline")
        return None

    async def _handle_progress(self, progress: float, total: float | None, message: str | None) -> None:
        """Log progress updates."""
        if total:
            logger.debug(f"MCP Progress: {progress}/{total} - {message}")
        else:
            logger.debug(f"MCP Progress: {progress} - {message}")

    async def _handle_message(self, message):
        """Handle server notifications."""
        if hasattr(message, 'root') and hasattr(message.root, 'method'):
            method = message.root.method
            if method == "notifications/tools/list_changed":
                logger.info("MCP Tools list changed, refreshing...")
                await self.refresh_tools()
