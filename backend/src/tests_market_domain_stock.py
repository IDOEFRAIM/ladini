from __future__ import annotations

from typing import Any, Mapping

from agriconnect.graphs.agents.market_coach.domain import DomainContext
from agriconnect.graphs.agents.market_coach.domain.stock import StockService, StockUpdateLevelCommand
from agriconnect.graphs.agents.market_coach.actions.tooling import ToolId


def _state() -> Mapping[str, Any]:
    return {"user_phone": "+22600000002", "update_mode": True, "current_entity": {"stock_id": "S1", "quantity": 10}}


def _payload_register() -> Mapping[str, Any]:
    return {"farm_id": "F1", "product": "maïs", "quantity_mentioned": 100.0, "unit_mentioned": "kg"}


def test_stock_register_harvest_domain_logic() -> None:
    ctx = DomainContext.from_state(_state())
    service = StockService(context=ctx)
    result = service.register_harvest(_state(), _payload_register())
    assert result.tool_id == ToolId.ADD_STOCK
    args = result.tool_args
    assert args["farm_id"] == "F1"
    assert args["item_name"] == "maïs"
    assert args["quantity"] == 100.0
    assert args["unit"].upper() == "KG"


def test_stock_update_level_domain_logic() -> None:
    ctx = DomainContext.from_state(_state())
    service = StockService(context=ctx)
    command = StockUpdateLevelCommand(
        producer_id="+22600000002",
        stock_id="S1",
        new_quantity_kg=50.0,
        reason="Ajustement de niveau via update",
    )
    result = service.update_level(command)
    assert result.tool_id == ToolId.ADJUST_STOCK_BY_ID
    args = result.tool_args
    assert args["producer_id"] == "+22600000002"
    assert args["stock_id"] == "S1"
    assert args["new_quantity"] == 50.0
