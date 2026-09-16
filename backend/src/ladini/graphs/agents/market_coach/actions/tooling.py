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
    # (2026-09-13, Deep Intent Architecture Cleanup) : les ToolId sans aucun
    # consumer (SELECT_WINNING_BID/ACCEPT_BID/GET_PENDING_ACTIONS/
    # REPORT_ANOMALY/CREATE_AGENT_ACTION/COMMIT_STAGED_TRANSACTION/
    # GET_AUCTIONS_BIDS/GET_ZONE_MARKET_OVERVIEW/SEARCH_PRODUCTS/
    # GET_ALL_ZONE_MARKET_OVERVIEW/GET_PRODUCER_DASHBOARD/
    # ADD_STOCK_MOVEMENT_BY_ID/ADJUST_STOCK_BY_ID/REMOVE_STOCK_BY_ID/
    # DELETE_STOCK_BY_ID/GET_CROP_CYCLES/GET_CROP_REQUIREMENTS/
    # GET_CYCLE_ECONOMICS/GET_ACTIVE_SANITARY_RISKS/CREATE_CROP_CYCLE/
    # LOG_INTERVENTION/ADD_GROWTH_LOG/ADD_CROP_GROWTH_STAGE/
    # UPDATE_SOIL_PROFILE/GET_TRUST_SCORE/GET_USER_CONTEXT) ont été retirés —
    # leurs intents/handlers propriétaires ont tous été supprimés.
    # Sales & marketplace tools
    GET_STOCKS = "get_stocks"
    GET_AUCTIONS = "get_auctions"
    GET_MY_ACTIVE_BIDS = "get_my_active_bids"
    GET_MARKET_SNAPSHOT = "get_market_snapshot"
    CHECK_PRICE_ANOMALY = "check_price_anomaly"
    GET_PRODUCER_ORDERS = "get_producer_orders"
    CREATE_PRODUCT = "create_product"
    RECORD_SALE = "record_sale"
    PLACE_BID = "place_bid"
    UPDATE_PRODUCT_PRICE_AND_QTY = "update_product_price_and_qty"
    UPDATE_PRODUCTION_FIELDS = "update_production_fields"
    GET_FARM_STOCKS = "get_farm_stocks"
    # (2026-09-13, suite d'audit) : GET_STOCK_MOVEMENTS retiré — plus aucun
    # consommateur dans market_coach (StockService.get_movements supprimée,
    # voir domain/stock.py) ; le tool MCP 'get_stock_movements' reste réel et
    # exposé indépendamment (infrastructure/mcp/exposure.py) pour d'autres
    # consommateurs MCP.
    ADD_STOCK = "add_stock"
    GET_PRODUCER_FARM = "get_producer_farm"
    DECLARE_FUTURE_PRODUCTION = "declare_future_production"
    GET_OR_CREATE_FARM = "get_or_create_farm"
    UPDATE_FARM = "update_farm"
    GET_EXPENSE_SUMMARY = "get_expense_summary"
    ADD_EXPENSE = "add_expense"
    GET_USER_BY_PHONE = "get_user_by_phone"
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
