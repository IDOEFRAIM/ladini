from __future__ import annotations

from typing import Any, Mapping

from agriconnect.graphs.agents.market_coach.domain import DomainContext
from agriconnect.graphs.agents.market_coach.domain.agro import (
    AgronomyService,
    FarmGetMyListCommand,
    FarmCreateCommand,
    FarmUpdateCommand,
)
from agriconnect.graphs.agents.market_coach.actions.tooling import ToolId


def _state() -> Mapping[str, Any]:
    return {"user_phone": "+22600000003"}


def test_agro_declare_crop_cycle_domain_logic() -> None:
    ctx = DomainContext.from_state(_state())
    service = AgronomyService(context=ctx)
    payload: Mapping[str, Any] = {
        "farm_id": "F1",
        "product": "tomate",
        "production_type": "CROP",
        "quantity_mentioned": 100.0,
        "unit_mentioned": "kg",
        "estimated_available_at": "2026-01-01",
        "price_mentioned": 200.0,
    }
    result = service.declare_crop_cycle(_state(), payload)
    assert result.tool_id == ToolId.DECLARE_FUTURE_PRODUCTION
    args = result.tool_args["payload"]
    assert args["farm_id"] == "F1"
    assert args["product"] == "tomate"
    assert args["quantity_mentioned"] == 100.0
    assert args["unit"].upper() == "KG"


def test_agro_update_soil_domain_logic() -> None:
    ctx = DomainContext.from_state(_state())
    service = AgronomyService(context=ctx)
    payload: Mapping[str, Any] = {"farm_id": "F1", "ph": 6.5, "organic_matter": 2.5}
    result = service.update_soil(_state(), payload)
    assert result.tool_id == ToolId.UPDATE_SOIL_PROFILE
    args = result.tool_args
    assert args["farm_id"] == "F1"
    assert args["data"]["ph"] == 6.5
    assert args["data"]["organic_matter"] == 2.5


def test_farm_get_my_list_command_builds_expected_args() -> None:
    ctx = DomainContext.from_state(_state())
    service = AgronomyService(context=ctx)
    cmd = FarmGetMyListCommand(phone="+22600000003")
    result = service.get_my_farms(cmd)
    assert result.tool_id == ToolId.GET_PRODUCER_FARM
    assert result.tool_args["phone"] == "+22600000003"


def test_farm_create_command_builds_expected_args() -> None:
    ctx = DomainContext.from_state(_state())
    service = AgronomyService(context=ctx)
    cmd = FarmCreateCommand(
        phone="+22600000003",
        farm_name="Ferme A",
        zone="Z-1",
        surface=2.5,
    )
    result = service.create_farm(cmd)
    assert result.tool_id == ToolId.GET_OR_CREATE_FARM
    args = result.tool_args
    assert args["producer_id"] == "+22600000003"
    assert args["farm_name"] == "Ferme A"
    assert args["zone_id"] == "Z-1"


def test_farm_update_command_builds_expected_args() -> None:
    ctx = DomainContext.from_state(_state())
    service = AgronomyService(context=ctx)
    cmd = FarmUpdateCommand(
        phone="+22600000003",
        farm_id="F-1",
        farm_name=None,
        surface=None,
    )
    result = service.update_farm(cmd)
    assert result.tool_id == ToolId.UPDATE_FARM
    assert result.tool_args["farm_id"] == "F-1"
