# Centralized mapping of MCP tool names to logical server scopes used by the Shield
# Add additional tools here as MCP tools are implemented.
TOOL_SCOPE_MAP = {
    "search_agronomy_docs": "RAG_SERVER",
    "persist_conversation": "DB_SERVER",
    "read_user_context": "DB_SERVER",
    "get_user_profile": "DB_SERVER",
    "get_user_by_phone": "DB_SERVER",
    "identify_or_create_user": "DB_SERVER",
}
