"""MCP main entry point.

Delegates to ``server.run()`` which builds and starts the unified FastMCP
server with all tool handlers from ``handlers.py``.

For the full MCPServerApp (with providers), use ``server.build_mcp_app()``
instead.
"""
from __future__ import annotations

import logging

from agriconnect.protocols.mcp.server import run as _run

logger = logging.getLogger("MCP.Main")


def run() -> None:
    _run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logger.info("Starting unified MCP server")
    run()
