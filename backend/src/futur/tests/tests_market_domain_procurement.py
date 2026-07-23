from __future__ import annotations

from typing import Mapping, Any

from agriconnect.graphs.agents.market_coach.domain import (
    DomainContext,
    DomainResult,
)
from agriconnect.graphs.agents.market_coach.domain.procurement import (
    ProcurementService,
    ProcurementCreateRequestCommand,
    ProcurementSelectWinnerCommand,
    ProcurementAcceptOfferCommand,
)
from agriconnect.graphs.agents.market_coach.actions.tooling import ToolId


def _state() -> Mapping[str, Any]:
    return {
        "user_phone": "+22600000000",
        "user_id": "user-123",
        "role": "buyer",
        "language": "fr",
        "region": "BF",
    }


def _payload_base() -> Mapping[str, Any]:
    return {
        "product": "maïs",
        "quantity_mentioned": 100.0,
        "price_mentioned": 120.0,
        "unit_mentioned": "kg",
        "zone_name": "Ouagadougou",
    }


def test_domain_context_from_state_minimal() -> None:
    ctx = DomainContext.from_state(_state())
    assert ctx.phone == "+22600000000"
    assert ctx.user_id == "user-123"
    assert ctx.role == "buyer"
    assert ctx.region == "BF"


def test_procurement_create_request_builds_expected_args() -> None:
    ctx = DomainContext.from_state(_state())
    service = ProcurementService(context=ctx)
    cmd = ProcurementCreateRequestCommand(
        phone="+22600000000",
        product="maïs",
        quantity=100.0,
        unit="KG",
        max_price=120.0,
        deadline=None,
        delivery_location=None,
        delivery_deadline=None,
        incoterm="DDP",
        auto_extend=True,
        zone_name="Ouagadougou",
    )
    result: DomainResult = service.create_request(cmd)

    assert result.tool_id == ToolId.CREATE_AUCTION
    args = result.tool_args
    assert args["phone"] == "+22600000000"
    assert args["product_query"] == "maïs"
    assert args["qty"] == 100.0
    assert args["unit"].upper() == "KG"
    assert args["max_price"] == 120.0
    assert "deadline" in args
    assert "delivery_location" in args
    assert "delivery_deadline" in args
    assert args["incoterm"] == "DDP"
    assert args["auto_extend"] is True
    assert args["zone_query"] == "Ouagadougou"


def test_procurement_select_winner_and_accept_offer() -> None:
    ctx = DomainContext.from_state(_state())
    service = ProcurementService(context=ctx)
    select_cmd = ProcurementSelectWinnerCommand(
        phone="+22600000000",
        auction_id="auction-1",
        bid_id="bid-1",
    )

    select_result = service.select_winner(select_cmd)
    assert select_result.tool_id == ToolId.SELECT_WINNING_BID
    assert select_result.tool_args == {"bid_id": "bid-1"}

    accept_cmd = ProcurementAcceptOfferCommand(
        phone="+22600000000",
        bid_id="bid-1",
    )
    accept_result = service.accept_offer(accept_cmd)
    assert accept_result.tool_id == ToolId.ACCEPT_BID
    assert accept_result.tool_args == {"bid_id": "bid-1"}
