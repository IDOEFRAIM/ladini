"""Shared tooling abstractions for MarketCoach action handlers."""
from __future__ import annotations

from enum import Enum
from typing import Dict, Iterable


class ToolResolutionError(RuntimeError):
    """Raised when a ToolId cannot be resolved to a concrete MCP tool name."""


class ToolId(str, Enum):
    """Canonical identifiers for MCP tools used by MarketCoach.

    IMPORTANT: values are MCP tool *names* as exposed by the DB MCP server.
    This keeps handlers decoupled from raw strings while preserving
    backwards-compatibility.
    """

    CREATE_AUCTION = "create_auction"
    SELECT_WINNING_BID = "select_winning_bid"
    ACCEPT_BID = "accept_bid"
    GET_PENDING_ACTIONS = "get_pending_actions"
    REPORT_ANOMALY = "report_anomaly"
    CREATE_AGENT_ACTION = "create_agent_action"
    COMMIT_STAGED_TRANSACTION = "commit_staged_transaction"
    # Sales & marketplace tools
    GET_STOCKS = "get_stocks"
    GET_AUCTIONS = "get_auctions"
    GET_AUCTIONS_BIDS = "get_auctions_bids"
    GET_MY_ACTIVE_BIDS = "get_my_active_bids"
    GET_MARKET_SNAPSHOT = "get_market_snapshot"
    GET_ZONE_MARKET_OVERVIEW = "get_zone_market_overview"
    SEARCH_PRODUCTS = "search_products"
    GET_ALL_ZONE_MARKET_OVERVIEW = "get_all_zone_market_overview"
    CHECK_PRICE_ANOMALY = "check_price_anomaly"
    GET_PRODUCER_DASHBOARD = "get_producer_dashboard"
    GET_PRODUCER_ORDERS = "get_producer_orders"
    CREATE_PRODUCT = "create_product"
    RECORD_SALE = "record_sale"
    PLACE_BID = "place_bid"
    UPDATE_PRODUCT_PRICE_AND_QTY = "update_product_price_and_qty"
    UPDATE_PRODUCTION_FIELDS = "update_production_fields"
    GET_FARM_STOCKS = "get_farm_stocks"
    GET_STOCK_MOVEMENTS = "get_stock_movements"
    ADD_STOCK = "add_stock"
    ADD_STOCK_MOVEMENT_BY_ID = "add_stock_movement_by_id"
    ADJUST_STOCK_BY_ID = "adjust_stock_by_id"
    REMOVE_STOCK_BY_ID = "remove_stock_by_id"
    DELETE_STOCK_BY_ID = "delete_stock_by_id"
    GET_CROP_CYCLES = "get_crop_cycles"
    GET_CROP_REQUIREMENTS = "get_crop_requirements"
    GET_CYCLE_ECONOMICS = "get_cycle_economics"
    GET_ACTIVE_SANITARY_RISKS = "get_active_sanitary_risks"
    GET_PRODUCER_FARM = "get_producer_farm"
    CREATE_CROP_CYCLE = "create_crop_cycle"
    DECLARE_FUTURE_PRODUCTION = "declare_future_production"
    LOG_INTERVENTION = "log_intervention"
    ADD_GROWTH_LOG = "add_growth_log"
    ADD_CROP_GROWTH_STAGE = "add_crop_growth_stage"
    UPDATE_SOIL_PROFILE = "update_soil_profile"
    GET_OR_CREATE_FARM = "get_or_create_farm"
    UPDATE_FARM = "update_farm"
    GET_EXPENSE_SUMMARY = "get_expense_summary"
    ADD_EXPENSE = "add_expense"
    GET_USER_BY_PHONE = "get_user_by_phone"
    GET_TRUST_SCORE = "get_trust_score"
    GET_USER_CONTEXT = "get_user_context"
    UPDATE_GEO_LOCATION = "update_geo_location"
    UPDATE_COMMUNICATION_PREFS = "update_communication_prefs"


class ToolResolver:
    """Resolves ToolId instances to concrete MCP tool names.

    Handlers are encouraged to return ToolId instead of raw strings.
    Legacy handlers returning strings remain supported.
    """

    _overrides: Dict[ToolId, str] = {}

    @classmethod
    def configure_overrides(cls, overrides: Dict[ToolId, str]) -> None:
        """Install/replace overrides used mainly for tests.

        In production we usually rely on the Enum values directly.
        """

        cls._overrides = dict(overrides or {})

    @classmethod
    def resolve_name(cls, tool: ToolId | str | None) -> str:
        """Return the MCP tool name for *tool* (ToolId or raw string).

        - ToolId: resolved via overrides, then ``tool.value``.
        - str: returned as-is (backwards-compat).
        - None / unsupported: raises ToolResolutionError.
        """

        if tool is None:
            raise ToolResolutionError("Attempted to resolve empty tool identifier")

        # ToolId is a subclass of str — check it first.
        if isinstance(tool, ToolId):
            return cls._overrides.get(tool) or str(tool.value)

        if isinstance(tool, str) and tool.strip():
            return tool

        raise ToolResolutionError(f"Unsupported tool identifier type: {type(tool)!r}")

    @classmethod
    def list_supported_ids(cls) -> Iterable[ToolId]:
        return (tool for tool in ToolId)


__all__ = ["ToolId", "ToolResolver", "ToolResolutionError"]
