from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

from .model import DomainContext, DomainResult
from agriconnect.graphs.agents.market_coach.actions.tooling import ToolId
from agriconnect.graphs.agents.market_coach.actions.common import (
    normalize_quantity_to_kg,
    require,
    require_phone,
)


_EMPTY_SLOT_VALUES = (None, "", [], {})


def _pick_first_value(*candidates: Any) -> Any:
    for candidate in candidates:
        if candidate not in _EMPTY_SLOT_VALUES:
            return candidate
    return None


def _resolve_mass_payload(payload: Mapping[str, Any]) -> Tuple[float, str]:
    """Resolve quantity + unit for publish / record intents from raw payload."""

    quantity_raw = _pick_first_value(
        payload.get("original_quantity"),
        payload.get("quantity_display"),
        payload.get("quantity"),
    )
    if quantity_raw is None:
        quantity_raw = require(payload, "quantity")

    unit_raw = _pick_first_value(
        payload.get("original_unit"),
        payload.get("unit_display"),
        payload.get("unit"),
        payload.get("unit"),
    )

    try:
        qty_value = float(str(quantity_raw).replace(",", "."))
    except (TypeError, ValueError):
        raise ValueError(f"Quantité invalide: {quantity_raw!r}")

    qty_kg, canonical_unit = normalize_quantity_to_kg(qty_value, unit_raw)
    return qty_kg, canonical_unit


@dataclass(frozen=True)
class SalesUpdateProductCommand:
    """Strongly-typed command for SALES_UPDATE_PRODUCT domain logic."""

    producer_id: str
    product_id: str
    price: Optional[float] = None
    quantity: Optional[float] = None
    name: Optional[str] = None
    unit: Optional[str] = None


@dataclass(frozen=True)
class SalesUpdateProductionCommand:
    """Typed command for SALES_UPDATE_PRODUCTION (mise à jour d'un lot futur / MarketOffer)."""

    producer_id: str          # en pratique le téléphone (convention DB : arg `phone`)
    cycle_id: str
    price: Optional[float] = None
    quantity: Optional[float] = None
    product_label: Optional[str] = None
    unit: Optional[str] = None
    estimated_available_at: Optional[str] = None
    production_type: Optional[str] = None


@dataclass(frozen=True)
class SalesPublishProductCommand:
    """Strongly-typed command for SALES_PUBLISH_PRODUCT domain logic."""

    producer_id: str
    product: str
    quantity: float
    unit: Optional[str]
    price: float
    description: Optional[str] = None
    category_label: Optional[str] = None


@dataclass(frozen=True)
class SalesRecordDirectCommand:
    """Strongly-typed command for SALES_RECORD_DIRECT domain logic."""

    producer_id: str
    product: str
    quantity: float
    unit: Optional[str]
    price: float


@dataclass(frozen=True)
class MarketGetRequestsCommand:
    """Typed command for MARKET_BROWSE_REQUESTS/MARKET_MY_REQUESTS read intent."""

    phone: str
    status: str = "OPEN"
    view_mode: str = "MARKETPLACE"
    product_name: Optional[str] = None
    zone_name: Optional[str] = None


@dataclass(frozen=True)
class SalesListOrdersCommand:
    """Typed command for SALES_LIST_ORDERS read intent."""

    phone: str
    status: Optional[str] = None
    limit: Optional[int] = None


@dataclass
class SalesService:
    """Business logic for Sales & Marketplace intents.

    Handlers call into this service and translate the returned
    ``DomainResult`` into ToolProvider calls.
    """

    context: DomainContext

    def get_catalog(self, state: Mapping[str, Any], payload: Mapping[str, Any]) -> DomainResult:
        phone = require_phone(state)
        farm_id = (
            payload.get("farm_id")
            or (state.get("transaction_payload") or {}).get("farm_id")
            or (state.get("stable_entities") or {}).get("farm_id")
        )
        if not farm_id:
            raise ValueError("L'identifiant de la ferme est requis pour consulter le catalogue.")

        args: Dict[str, Any] = {"farm_id": str(farm_id)}
        return DomainResult(tool_id=ToolId.GET_STOCKS, tool_args=args)

    def get_market_requests(self, command: MarketGetRequestsCommand) -> DomainResult:
        args: Dict[str, Any] = {
            "phone": command.phone,
            "status": command.status,
            "view_mode": command.view_mode,
        }
        if command.product_name:
            args["product_name"] = command.product_name
        if command.zone_name:
            args["zone_name"] = command.zone_name
        return DomainResult(tool_id=ToolId.GET_AUCTIONS, tool_args=args)

    def get_request_detail(self, state: Mapping[str, Any], payload: Mapping[str, Any]) -> DomainResult:
        phone = require_phone(state)
        auction_id = str(require(payload, "auction_id"))
        args: Dict[str, Any] = {
            "phone": phone,
            "auction_id": auction_id,
            "status": payload.get("status") or "OPEN",
        }
        return DomainResult(tool_id=ToolId.GET_AUCTIONS_BIDS, tool_args=args)

    def get_my_proposals(self, state: Mapping[str, Any], payload: Mapping[str, Any]) -> DomainResult:
        phone = require_phone(state)
        return DomainResult(tool_id=ToolId.GET_MY_ACTIVE_BIDS, tool_args={"phone": phone})

    def market_snapshot(self, state: Mapping[str, Any], payload: Mapping[str, Any]) -> DomainResult:
        args: Dict[str, Any] = {}
        zone = payload.get("zone") or payload.get("zone_name")
        if zone:
            args["zone"] = str(zone)
        return DomainResult(tool_id=ToolId.GET_MARKET_SNAPSHOT, tool_args=args)

    def market_snapshot_zonal(self, state: Mapping[str, Any], payload: Mapping[str, Any]) -> DomainResult:
        zone = str(require(payload, "zone"))
        return DomainResult(tool_id=ToolId.GET_ZONE_MARKET_OVERVIEW, tool_args={"zone_id": zone})

    def dashboard_producer(self, state: Mapping[str, Any], payload: Mapping[str, Any]) -> DomainResult:
        phone = require_phone(state)
        return DomainResult(tool_id=ToolId.GET_PRODUCER_DASHBOARD, tool_args={"producer_id": phone})

    def search_products(self, state: Mapping[str, Any], payload: Mapping[str, Any]) -> DomainResult:
        product = str(require(payload, "product"))
        phone = require_phone(state)
        args: Dict[str, Any] = {"product": product, "phone": phone}
        zone = payload.get("zone_name") or payload.get("zone")
        if zone:
            args["zone_id"] = str(zone)
        return DomainResult(tool_id=ToolId.SEARCH_PRODUCTS, tool_args=args)

    def search_nearby(self, state: Mapping[str, Any], payload: Mapping[str, Any]) -> DomainResult:
        require_phone(state)
        require(payload, "latitude")
        require(payload, "longitude")
        # The underlying tool uses contextual location; no direct args today.
        return DomainResult(tool_id=ToolId.GET_ALL_ZONE_MARKET_OVERVIEW, tool_args={})

    def validate_price(self, state: Mapping[str, Any], payload: Mapping[str, Any]) -> DomainResult:
        product = str(require(payload, "product"))
        price = float(require(payload, "price"))
        zone = str(require(payload, "zone"))
        args = {
            "product_name": product,
            "proposed_price": price,
            "zone_id": zone,
        }
        return DomainResult(tool_id=ToolId.CHECK_PRICE_ANOMALY, tool_args=args)

    def publish_product(self, command: SalesPublishProductCommand) -> DomainResult:
        qty_kg, unit = _resolve_mass_payload(
            {
                "quantity": command.quantity,
                "unit": command.unit,
            }
        )
        args: Dict[str, Any] = {
            "producer_id": command.producer_id,
            "name": command.product,
            "price": command.price,
            "quantity_for_sale": qty_kg,
            "unit": unit,
        }
        if command.description:
            args["description"] = command.description
        if command.category_label:
            args["category_label"] = command.category_label
        return DomainResult(tool_id=ToolId.CREATE_PRODUCT, tool_args=args)

    def record_direct_sale(self, command: SalesRecordDirectCommand) -> DomainResult:
        qty_kg, unit = _resolve_mass_payload(
            {
                "quantity": command.quantity,
                "unit": command.unit,
            }
        )
        args = {
            "phone": command.producer_id,
            "product_name": command.product,
            "quantity": qty_kg,
            "total_price": command.price,
            "unit": unit,
        }
        return DomainResult(tool_id=ToolId.RECORD_SALE, tool_args=args)

    def place_bid(self, state: Mapping[str, Any], payload: Mapping[str, Any]) -> DomainResult:
        phone = require_phone(state)
        auction_id = str(require(payload, "auction_id"))
        price = float(require(payload, "price"))
        args: Dict[str, Any] = {"phone": phone, "auction_id": auction_id, "offered_price": price}
        if payload.get("message"):
            args["message"] = str(payload["message"])
        return DomainResult(tool_id=ToolId.PLACE_BID, tool_args=args)

    def accept_contract(self, state: Mapping[str, Any], payload: Mapping[str, Any]) -> DomainResult:
        require_phone(state)
        transaction_id = str(require(payload, "bid_id"))
        args = {"transaction_id": transaction_id, "approved": True}
        return DomainResult(tool_id=ToolId.COMMIT_STAGED_TRANSACTION, tool_args=args)

    def update_product(self, command: SalesUpdateProductCommand) -> DomainResult:
        """Domain logic for updating a published product from a typed command."""

        if command.price is None and command.quantity is None and command.name is None and command.unit is None:
            raise ValueError(
                "Indiquez au moins un champ à modifier (prix, quantité, nom ou unité)."
            )

        args: Dict[str, Any] = {
            "phone": command.producer_id,
            "product_id": command.product_id,
        }
        if command.price is not None:
            args["price"] = command.price
        if command.quantity is not None:
            args["quantity"] = command.quantity
        if command.name is not None:
            args["name"] = command.name
        if command.unit is not None:
            args["unit"] = command.unit

        return DomainResult(tool_id=ToolId.UPDATE_PRODUCT_PRICE_AND_QTY, tool_args=args)

    def update_production(self, command: SalesUpdateProductionCommand) -> DomainResult:
        """Domain logic : mise à jour partielle d'un lot futur (MarketOffer)."""
        optional = {
            "price": command.price,
            "quantity": command.quantity,
            "product_label": command.product_label,
            "unit": command.unit,
            "estimated_available_at": command.estimated_available_at,
            "production_type": command.production_type,
        }
        provided = {k: v for k, v in optional.items() if v is not None}
        if not provided:
            raise ValueError(
                "Indiquez au moins un champ à modifier (prix, quantité, nom, unité, date ou type)."
            )
        args: Dict[str, Any] = {
            "phone": command.producer_id,
            "cycle_id": command.cycle_id,
            **provided,
        }
        return DomainResult(tool_id=ToolId.UPDATE_PRODUCTION_FIELDS, tool_args=args)

    def list_orders(self, command: SalesListOrdersCommand) -> DomainResult:
        args: Dict[str, Any] = {"phone": command.phone}
        if command.status:
            args["status"] = command.status
        if command.limit is not None:
            args["limit"] = command.limit
        return DomainResult(tool_id=ToolId.GET_PRODUCER_ORDERS, tool_args=args)
