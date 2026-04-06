"""Wrapper to start Agri DB MCP server in require-db mode for local runs.
This script imports the module and calls start_server(require_db=True).
"""
from agriconnect.protocols.mcp.servers.agri_db_server import start_server

if __name__ == "__main__":
    # Start with require_db=True to force DB connectivity checks
    start_server(require_db=True)
