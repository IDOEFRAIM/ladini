from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from .model import DomainContext, DomainResult
from agriconnect.graphs.agents.market_coach.actions.tooling import ToolId
from agriconnect.graphs.agents.market_coach.actions.common import require, require_phone, normalize_quantity_to_kg


@dataclass
class AgronomyService:
    context: DomainContext

    def get_cycles(self, state: Mapping[str, Any], payload: Mapping[str, Any]) -> DomainResult:
        require_phone(state)
        farm_id = str(require(payload, "farm_id"))
        args: Dict[str, Any] = {"farm_id": farm_id}
        return DomainResult(tool_id=ToolId.GET_CROP_CYCLES, tool_args=args)

    def get_standards(self, state: Mapping[str, Any], payload: Mapping[str, Any]) -> DomainResult:
        product = str(require(payload, "product"))
        args: Dict[str, Any] = {"crop_type": product}
        return DomainResult(tool_id=ToolId.GET_CROP_REQUIREMENTS, tool_args=args)

    def get_economics(self, state: Mapping[str, Any], payload: Mapping[str, Any]) -> DomainResult:
        require_phone(state)
        cycle_id = str(require(payload, "cycle_id"))
        args: Dict[str, Any] = {"cycle_id": cycle_id}
        return DomainResult(tool_id=ToolId.GET_CYCLE_ECONOMICS, tool_args=args)

    def get_risks(self, state: Mapping[str, Any], payload: Mapping[str, Any]) -> DomainResult:
        require_phone(state)
        farm_id = str(require(payload, "farm_id"))
        args: Dict[str, Any] = {"farm_id": farm_id}
        return DomainResult(tool_id=ToolId.GET_ACTIVE_SANITARY_RISKS, tool_args=args)

    def get_my_farms(self, command: FarmGetMyListCommand) -> DomainResult:
        args: Dict[str, Any] = {"phone": str(command.phone)}
        return DomainResult(tool_id=ToolId.GET_PRODUCER_FARM, tool_args=args)

    def start_cycle(self, state: Mapping[str, Any], payload: Mapping[str, Any]) -> DomainResult:
        require_phone(state)
        farm_id = str(require(payload, "farm_id"))
        product = str(require(payload, "product"))
        surface = float(require(payload, "surface"))
        data: Dict[str, Any] = {"crop_type": product, "area_size": surface}
        if payload.get("variety"):
            data["variety"] = str(payload["variety"])
        if payload.get("target_yield"):
            data["target_yield"] = float(payload["target_yield"])
        args: Dict[str, Any] = {"farm_id": farm_id, "data": data}
        return DomainResult(tool_id=ToolId.CREATE_CROP_CYCLE, tool_args=args)

    def declare_crop_cycle(self, state: Mapping[str, Any], payload: Mapping[str, Any]) -> DomainResult:
        phone = require_phone(state)
        farm_id = str(require(payload, "farm_id"))
        product = str(require(payload, "product"))
        production_type = str(require(payload, "production_type")).upper().strip()
        if production_type not in {"CROP", "LIVESTOCK"}:
            raise ValueError("production_type must be CROP or LIVESTOCK")

        qty_raw = float(require(payload, "quantity_mentioned"))
        unit_in = payload.get("unit_mentioned")
        if production_type == "CROP":
            qty, unit = normalize_quantity_to_kg(qty_raw, unit_in)
        else:
            qty = qty_raw
            unit = str(unit_in or "HEAD").upper().strip()

        estimated_available_at = str(require(payload, "estimated_available_at"))
        price_per_unit = float(require(payload, "price_mentioned"))

        production_payload: Dict[str, Any] = {
            "farm_id": farm_id,
            "production_type": production_type,
            "product": product,
            "quantity_mentioned": qty,
            "unit": unit,
            "estimated_available_at": estimated_available_at,
            "price_per_unit": price_per_unit,
            "preorder_enabled": bool(payload.get("preorder_enabled", True)),
            "is_public": bool(payload.get("is_public", True)),
        }

        if payload.get("breed"):
            production_payload["breed"] = str(payload["breed"])
        if payload.get("surface"):
            production_payload["surface"] = payload["surface"]
        if payload.get("area_size"):
            production_payload["area_size"] = payload["area_size"]
        if payload.get("expected_harvest_date"):
            production_payload["expected_harvest_date"] = payload["expected_harvest_date"]
        if payload.get("planted_at"):
            production_payload["planted_at"] = payload["planted_at"]

        args: Dict[str, Any] = {"payload": production_payload, "phone": phone}
        return DomainResult(tool_id=ToolId.DECLARE_FUTURE_PRODUCTION, tool_args=args)

    def record_intervention(self, state: Mapping[str, Any], payload: Mapping[str, Any]) -> DomainResult:
        require_phone(state)
        cycle_id = str(require(payload, "cycle_id"))
        intervention_type = str(require(payload, "intervention_type")).upper().strip()
        data: Dict[str, Any] = {"type": intervention_type}
        if payload.get("input_used"):
            data["input_used"] = str(payload["input_used"])
        if payload.get("quantity_mentioned"):
            data["quantity"] = float(payload["quantity_mentioned"])
        if payload.get("details"):
            data["description"] = str(payload["details"])
        args: Dict[str, Any] = {"cycle_id": cycle_id, "data": data}
        return DomainResult(tool_id=ToolId.LOG_INTERVENTION, tool_args=args)

    def record_observation(self, state: Mapping[str, Any], payload: Mapping[str, Any]) -> DomainResult:
        require_phone(state)
        cycle_id = str(require(payload, "cycle_id"))
        stage = payload.get("stage_code") or payload.get("stage_label")
        try:
            stage_code = int(stage)
        except (TypeError, ValueError):
            stage_code = 0
        args: Dict[str, Any] = {"cycle_id": cycle_id, "stage_code": stage_code}
        return DomainResult(tool_id=ToolId.ADD_GROWTH_LOG, tool_args=args)

    def update_stage(self, state: Mapping[str, Any], payload: Mapping[str, Any]) -> DomainResult:
        require_phone(state)
        cycle_id = str(require(payload, "cycle_id"))
        stage_name = str(require(payload, "stage_name"))
        data: Dict[str, Any] = {"cycle_id": cycle_id, "stage_name": stage_name}
        args: Dict[str, Any] = {"data": data}
        return DomainResult(tool_id=ToolId.ADD_CROP_GROWTH_STAGE, tool_args=args)

    def update_soil(self, state: Mapping[str, Any], payload: Mapping[str, Any]) -> DomainResult:
        require_phone(state)
        farm_id = str(require(payload, "farm_id"))
        ph = float(require(payload, "ph"))
        data: Dict[str, Any] = {"ph": ph}
        if payload.get("organic_matter"):
            data["organic_matter"] = float(payload["organic_matter"])
        args: Dict[str, Any] = {"farm_id": farm_id, "data": data}
        return DomainResult(tool_id=ToolId.UPDATE_SOIL_PROFILE, tool_args=args)

    def create_farm(self, command: FarmCreateCommand) -> DomainResult:
        args: Dict[str, Any] = {
            "producer_id": str(command.phone),
            "farm_name": str(command.farm_name),
            "zone_id": str(command.zone),
        }
        return DomainResult(tool_id=ToolId.GET_OR_CREATE_FARM, tool_args=args)

    def update_farm(self, command: FarmUpdateCommand) -> DomainResult:
        args: Dict[str, Any] = {"farm_id": str(command.farm_id)}
        return DomainResult(tool_id=ToolId.UPDATE_FARM, tool_args=args)

@dataclass(frozen=True)
class FarmGetMyListCommand:
    phone: str


@dataclass(frozen=True)
class FarmCreateCommand:
    phone: str
    farm_name: str
    zone: str
    surface: Optional[float] = None  # optional, not sent to tool for now


@dataclass(frozen=True)
class FarmUpdateCommand:
    phone: str
    farm_id: str
    farm_name: Optional[str] = None
    surface: Optional[float] = None
