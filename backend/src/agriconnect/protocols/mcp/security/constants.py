"""
Shared constants for the MCP Security Shield.
"""
from __future__ import annotations

from enum import Enum
from typing import FrozenSet


# ────────────────────────────── Permission Scopes ──────────────────────────

class PermissionScope(str, Enum):
    """Granular DB access levels.

    DB_READ_ONLY    — SELECT-like tools, auto-approved on non-sensitive tables.
    DB_DATA_WRITE   — INSERT/UPDATE-like tools, require HITL confirmation.
    DB_SCHEMA_MODIFY — DDL-like tools, always DENY unless maintenance mode.
    """
    DB_READ_ONLY = "DB_READ_ONLY"
    DB_DATA_WRITE = "DB_DATA_WRITE"
    DB_SCHEMA_MODIFY = "DB_SCHEMA_MODIFY"


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


# ────────────────────────────── Tool → Scope mapping ──────────────────────

# Map every known MCP DB tool to its default permission scope.
# Tools NOT listed here default to DB_DATA_WRITE (safe default).
TOOL_SCOPE_MAP: dict[str, PermissionScope] = {
    # Read-only tools
    "get_user_profile":          PermissionScope.DB_READ_ONLY,
    "get_user_by_phone":         PermissionScope.DB_READ_ONLY,
    "list_products":             PermissionScope.DB_READ_ONLY,
    "search_products":           PermissionScope.DB_READ_ONLY,
    "get_orders":                PermissionScope.DB_READ_ONLY,
    "get_pending_actions":       PermissionScope.DB_READ_ONLY,
    "get_open_auctions":         PermissionScope.DB_READ_ONLY,
    "get_farm_stocks":           PermissionScope.DB_READ_ONLY,
    "get_stocks":                PermissionScope.DB_READ_ONLY,
    "get_stock_history":         PermissionScope.DB_READ_ONLY,
    "get_clients":               PermissionScope.DB_READ_ONLY,
    "get_farms":                 PermissionScope.DB_READ_ONLY,
    "get_expense_summary":       PermissionScope.DB_READ_ONLY,
    "get_expenses":              PermissionScope.DB_READ_ONLY,
    "db_status":                 PermissionScope.DB_READ_ONLY,
    # Data-write tools
    "identify_or_create_user":   PermissionScope.DB_DATA_WRITE,
    "create_product":            PermissionScope.DB_DATA_WRITE,
    "create_order":              PermissionScope.DB_DATA_WRITE,
    "create_auction":            PermissionScope.DB_DATA_WRITE,
    "place_bid":                 PermissionScope.DB_DATA_WRITE,
    "update_stock_with_movement": PermissionScope.DB_DATA_WRITE,
    "prepare_transaction_staging": PermissionScope.DB_DATA_WRITE,
    "commit_staged_transaction": PermissionScope.DB_DATA_WRITE,
    "create_agent_action":       PermissionScope.DB_DATA_WRITE,
    "update_action_status":      PermissionScope.DB_DATA_WRITE,
    "add_stock":                 PermissionScope.DB_DATA_WRITE,
    "remove_stock":              PermissionScope.DB_DATA_WRITE,
    "add_expense":               PermissionScope.DB_DATA_WRITE,
    "update_order_status":       PermissionScope.DB_DATA_WRITE,
    "update_farm":               PermissionScope.DB_DATA_WRITE,
    "register_surplus_offer":    PermissionScope.DB_DATA_WRITE,
    # Schema modification — DENY by default
    "migrate_schema":            PermissionScope.DB_SCHEMA_MODIFY,
    "drop_table":                PermissionScope.DB_SCHEMA_MODIFY,
    "alter_table":               PermissionScope.DB_SCHEMA_MODIFY,
}


# ────────────────────────────── Tool → Static Risk mapping ────────────────

TOOL_RISK_MAP: dict[str, RiskLevel] = {
    "get_user_profile":          RiskLevel.LOW,
    "get_user_by_phone":         RiskLevel.LOW,
    "list_products":             RiskLevel.LOW,
    "search_products":           RiskLevel.LOW,
    "get_orders":                RiskLevel.LOW,
    "get_pending_actions":       RiskLevel.LOW,
    "get_open_auctions":         RiskLevel.LOW,
    "get_farm_stocks":           RiskLevel.LOW,
    "get_stocks":                RiskLevel.LOW,
    "db_status":                 RiskLevel.LOW,
    "identify_or_create_user":   RiskLevel.MEDIUM,
    "create_product":            RiskLevel.MEDIUM,
    "create_order":              RiskLevel.HIGH,
    "create_auction":            RiskLevel.MEDIUM,
    "place_bid":                 RiskLevel.MEDIUM,
    "update_stock_with_movement": RiskLevel.MEDIUM,
    "prepare_transaction_staging": RiskLevel.HIGH,
    "commit_staged_transaction": RiskLevel.HIGH,
    "create_agent_action":       RiskLevel.MEDIUM,
    "update_action_status":      RiskLevel.MEDIUM,
    "add_stock":                 RiskLevel.MEDIUM,
    "remove_stock":              RiskLevel.MEDIUM,
    "migrate_schema":            RiskLevel.CRITICAL,
    "drop_table":                RiskLevel.CRITICAL,
    "alter_table":               RiskLevel.CRITICAL,
}


# ────────────────────────────── Sensitive Data ────────────────────────────

# Column names that must NEVER be forwarded to the LLM context.
SENSITIVE_COLUMNS: FrozenSet[str] = frozenset({
    "password_hash",
    "password",
    "hashed_password",
    "email",
    "token",
    "secret",
    "api_key",
    "id_rsa",
    "private_key",
    "refresh_token",
    "access_token",
    "credit_card",
    "ssn",
    "otp",
})


# ────────────────────────────── SQL Injection Patterns ────────────────────

# Regex patterns that signal potential SQL injection attempts when found
# inside tool argument values.  Used by the pre-flight analyser in host_app.
SQL_INJECTION_PATTERNS: list[str] = [
    r"(?i)\bDROP\s+TABLE\b",
    r"(?i)\bDELETE\s+FROM\b",
    r"(?i)\bTRUNCATE\b",
    r"(?i)\bALTER\s+TABLE\b",
    r"(?i)\bINSERT\s+INTO\b.*;\s*--",
    r"(?i)\bUPDATE\b.*\bSET\b.*;\s*--",
    r"(?i)\bOR\s+1\s*=\s*1",
    r"(?i)\bOR\s+'[^']*'\s*=\s*'[^']*'",
    r"(?i);\s*--",
    r"(?i)\bUNION\s+(ALL\s+)?SELECT\b",
    r"(?i)\bEXEC(\s|\()",
    r"(?i)\bxp_cmdshell\b",
    r"(?i)\b(CHAR|CONCAT)\s*\(",
    r"(?i)'\s*;\s*DROP\b",
]


# ────────────────────────────── Sensitive File Patterns ───────────────────

# If a tool argument value matches one of these, the risk is escalated
# to CRITICAL regardless of the tool's static risk level.
SENSITIVE_FILE_PATTERNS: list[str] = [
    r"\.env",
    r"id_rsa",
    r"\.pem$",
    r"\.key$",
    r"credentials",
    r"secrets?\.(json|yaml|yml|toml)",
    r"\.git/config",
]
