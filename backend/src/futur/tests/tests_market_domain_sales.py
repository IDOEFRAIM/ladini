from __future__ import annotations

from typing import Mapping, Any

from agriconnect.graphs.agents.market_coach.domain import DomainContext, DomainResult
from agriconnect.graphs.agents.market_coach.domain.sales import (
    MarketGetRequestsCommand,
    SalesListOrdersCommand,
    SalesPublishProductCommand,
    SalesRecordDirectCommand,
    SalesService,
    SalesUpdateProductCommand,
)
from agriconnect.graphs.agents.market_coach.actions.tooling import ToolId


def _state() -> Mapping[str, Any]:
    return {
        "user_phone": "+22600000000",
        "user_id": "user-123",
        "role": "producer",
        "language": "fr",
        "region": "BF",
    }


def _payload_publish() -> Mapping[str, Any]:
    return {
        "product": "maïs",
        "quantity_mentioned": 50.0,
        "price_mentioned": 100.0,
        "unit_mentioned": "kg",
        "description": "Maïs jaune de qualité",
        "category_label": "CEREAL",
    }


def test_sales_publish_product_domain_logic() -> None:
    ctx = DomainContext.from_state(_state())
    service = SalesService(context=ctx)
    cmd = SalesPublishProductCommand(
        producer_id="+22600000000",
        product="maïs",
        quantity=50.0,
        unit="KG",
        price=100.0,
        description="Maïs jaune de qualité",
        category_label="CEREAL",
    )
    result: DomainResult = service.publish_product(cmd)  # type: ignore[arg-type]

    assert result.tool_id == ToolId.CREATE_PRODUCT
    args = result.tool_args
    assert args["producer_id"] == "+22600000000"
    assert args["name"] == "maïs"
    assert args["price"] == 100.0
    assert args["quantity_for_sale"] == 50.0
    assert args["unit"].upper() == "KG"
    assert args["description"] == "Maïs jaune de qualité"
    assert args["category_label"] == "CEREAL"


def test_sales_record_direct_domain_logic() -> None:
    ctx = DomainContext.from_state(_state())
    service = SalesService(context=ctx)
    cmd = SalesRecordDirectCommand(
        producer_id="+22600000000",
        product="maïs",
        quantity=50.0,
        unit="KG",
        price=100.0,
    )

    result = service.record_direct_sale(cmd)

    assert result.tool_id == ToolId.RECORD_SALE
    args = result.tool_args
    assert args["phone"] == "+22600000000"
    assert args["product_name"] == "maïs"
    assert args["quantity"] == 50.0
    assert args["total_price"] == 100.0
    assert args["unit"].upper() == "KG"


def test_sales_update_product_domain_logic() -> None:
    ctx = DomainContext.from_state(_state())
    service = SalesService(context=ctx)
    command = SalesUpdateProductCommand(
        producer_id="+22600000000",
        product_id="prod-1",
        price=95.0,
        quantity=45.0,
    )

    result = service.update_product(command)
    assert result.tool_id == ToolId.UPDATE_PRODUCT_PRICE_AND_QTY
    args = result.tool_args
    assert args["phone"] == "+22600000000"
    assert args["product_id"] == "prod-1"
    assert args["price"] == 95.0
    assert args["quantity"] == 45.0


def test_sales_list_orders_domain_logic() -> None:
    ctx = DomainContext.from_state(_state())
    service = SalesService(context=ctx)
    command = SalesListOrdersCommand(
        phone="+22600000000",
        status="COMPLETED",
        limit=10,
    )

    result = service.list_orders(command)
    assert result.tool_id == ToolId.GET_PRODUCER_ORDERS
    args = result.tool_args
    assert args["phone"] == "+22600000000"
    assert args["status"] == "COMPLETED"
    assert args["limit"] == 10


def test_market_get_requests_domain_logic() -> None:
    ctx = DomainContext.from_state(_state())
    service = SalesService(context=ctx)
    command = MarketGetRequestsCommand(
        phone="+22600000000",
        status="OPEN",
        view_mode="MARKETPLACE",
        product_name="maïs",
        zone_name="Z1",
    )

    result = service.get_market_requests(command)
    assert result.tool_id == ToolId.GET_AUCTIONS
    args = result.tool_args
    assert args["phone"] == "+22600000000"
    assert args["status"] == "OPEN"
    assert args["view_mode"] == "MARKETPLACE"
    assert args["product_name"] == "maïs"
    assert args["zone_name"] == "Z1"
