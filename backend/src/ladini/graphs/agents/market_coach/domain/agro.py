from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from ladini.domain.commercial_offer import (
    CommercialQuantity,
    convert_commercial_quantity_to_base_unit,
)
from ladini.graphs.agents.market_coach.actions.common import (
    normalize_quantity_to_kg,
    require,
    require_phone,
)
from ladini.graphs.agents.market_coach.actions.tooling import ToolId

from .model import DomainContext, DomainResult


@dataclass
class AgronomyService:
    """(2026-09-14, Deep Intent Architecture Cleanup) : nettoyée — les
    méthodes `get_cycles`/`get_standards`/`get_economics`/`get_risks`/
    `start_cycle`/`record_intervention`/`record_observation`/
    `update_stage`/`update_soil` sont SUPPRIMÉES (leurs intents
    correspondants n'existent plus dans `INTENT_CONFIG`, aucun autre
    appelant trouvé — code mort confirmé). Ne restent que la production
    future (marketplace) et les exploitations (FARM_*)."""

    context: DomainContext

    def get_my_farms(self, command: FarmGetMyListCommand) -> DomainResult:
        args: Dict[str, Any] = {"phone": str(command.phone)}
        return DomainResult(tool_id=ToolId.GET_PRODUCER_FARM, tool_args=args)

    def declare_crop_cycle(
        self, state: Mapping[str, Any], payload: Mapping[str, Any]
    ) -> DomainResult:
        phone = require_phone(state)
        farm_id = str(require(payload, "farm_id"))
        product = str(require(payload, "product"))
        production_type = str(require(payload, "production_type")).upper().strip()
        if production_type not in {"CROP", "LIVESTOCK"}:
            raise ValueError("production_type must be CROP or LIVESTOCK")

        qty_raw = float(require(payload, "quantity"))
        unit_in = payload.get("unit")
        price_per_unit = float(require(payload, "price"))
        if production_type == "CROP":
            qty, unit = normalize_quantity_to_kg(qty_raw, unit_in)
            # (2026-09-28, hardening P0 — même bug que `sales.py::publish_product`,
            # confirmé par audit) : `price_per_unit` est documenté comme "par
            # `unit`" — si la quantité a été convertie (TONNE -> KG), le prix
            # doit être re-basé dans la même proportion, jamais laissé
            # inchangé pendant que `unit` change sous ses pieds.
            converted = convert_commercial_quantity_to_base_unit(
                CommercialQuantity(qty_raw, str(unit_in or "KG")), unit
            )
            price_rescale_factor = converted[1] if converted is not None else 1.0
            price_per_unit = price_per_unit / price_rescale_factor
        else:
            qty = qty_raw
            unit = str(unit_in or "HEAD").upper().strip()

        estimated_available_at = str(require(payload, "estimated_available_at"))

        production_payload: Dict[str, Any] = {
            "farm_id": farm_id,
            "production_type": production_type,
            "product": product,
            "quantity": qty,
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
            production_payload["expected_harvest_date"] = payload[
                "expected_harvest_date"
            ]
        if payload.get("planted_at"):
            production_payload["planted_at"] = payload["planted_at"]

        args: Dict[str, Any] = {"payload": production_payload, "phone": phone}
        return DomainResult(tool_id=ToolId.DECLARE_FUTURE_PRODUCTION, tool_args=args)

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
