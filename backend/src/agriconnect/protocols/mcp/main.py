from __future__ import annotations

import logging
import os

from agriconnect.protocols.mcp.mcp_config import build_mcp_app

logger = logging.getLogger("MCP.Main")


def run() -> None:
    app = build_mcp_app()
    env_name = (os.getenv("APP_ENV") or os.getenv("ENV") or "development").strip().lower()
    default_transport = "sse" if env_name in {"prod", "production", "staging"} else "stdio"
    transport = os.getenv("MCP_TRANSPORT", default_transport)
    host = os.getenv("MCP_HOST")
    port = os.getenv("MCP_PORT")
    app.run(transport=transport, host=host, port=int(port) if port else None)


if __name__ == "__main__":
    logger.info("Starting unified MCP server")
    run()
