from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping

from ladini.graphs.agents.market_coach.actions.common import (
    normalize_quantity_to_kg,
    require,
    require_phone,
)
from ladini.graphs.agents.market_coach.actions.tooling import ToolId
from ladini.graphs.agents.market_coach.domain.model import (
    DomainContext,
    DomainResult,
)


@dataclass(frozen=True)
class StockUpdateLevelCommand:
    """Strongly-typed command for STOCK_UPDATE_LEVEL domain logic."""

    producer_id: str
    stock_id: str
    new_quantity_kg: float
    reason: str


@dataclass
class StockService:
    context: DomainContext

    def get_summary(
        self, state: Mapping[str, Any], payload: Mapping[str, Any]
    ) -> DomainResult:
        phone = require_phone(state)
        args: Dict[str, Any] = {"phone": phone}
        return DomainResult(tool_id=ToolId.GET_STOCKS, tool_args=args)

    def get_detail(
        self, state: Mapping[str, Any], payload: Mapping[str, Any]
    ) -> DomainResult:
        # `phone` transmis (et non plus seulement validé) : `get_farm_stocks`
        # l'exige comme preuve de propriété sur `farm_id` — voir
        # `MarketplaceMixin.get_farm_stocks` et l'audit sécurité agent
        # 2026-09-10. Épinglé à la session par `schema_resolver`, donc
        # non usurpable via le message.
        phone = require_phone(state)
        farm_id = str(require(payload, "farm_id"))
        args: Dict[str, Any] = {"farm_id": farm_id, "producer_phone": phone}
        return DomainResult(tool_id=ToolId.GET_FARM_STOCKS, tool_args=args)

    def get_movements(
        self, state: Mapping[str, Any], payload: Mapping[str, Any]
    ) -> DomainResult:
        # `phone` transmis (et non plus seulement validé) : il autorise la
        # lecture de `stock_id` (audit sécurité agent 2026-09-10 — sans lui,
        # `get_stock_movements` exposait la rotation d'inventaire de n'importe
        # quel producteur).
        phone = require_phone(state)
        stock_id = str(require(payload, "stock_id"))
        args: Dict[str, Any] = {"stock_id": stock_id, "producer_phone": phone}
        return DomainResult(tool_id=ToolId.GET_STOCK_MOVEMENTS, tool_args=args)

    def register_harvest(
        self, state: Mapping[str, Any], payload: Mapping[str, Any]
    ) -> DomainResult:
        # `phone` n'est PLUS jeté après validation : il est transmis à
        # `add_stock` comme preuve d'autorisation sur `farm_id` (audit sécurité
        # agent 2026-09-10). Auparavant `require_phone` ne servait qu'à
        # valider la présence du téléphone, puis les args partaient sans lui —
        # `farm_id`, une valeur venue du payload, était donc le seul critère
        # de ciblage côté base.
        phone = require_phone(state)
        farm_id = str(require(payload, "farm_id"))
        product = str(require(payload, "product"))
        qty_raw = float(require(payload, "quantity"))
        qty_kg, unit = normalize_quantity_to_kg(qty_raw, payload.get("unit"))
        args: Dict[str, Any] = {
            "farm_id": farm_id,
            "producer_phone": phone,
            "item_name": product,
            "quantity": qty_kg,
            "unit": unit,
            "stock_type": "HARVEST",
            "reason": payload.get("reason") or "Enregistrement récolte via agent",
        }
        return DomainResult(tool_id=ToolId.ADD_STOCK, tool_args=args)

    def record_movement(
        self, state: Mapping[str, Any], payload: Mapping[str, Any]
    ) -> DomainResult:
        phone = require_phone(state)
        stock_id = str(require(payload, "stock_id"))
        movement_type = str(require(payload, "movement_type")).upper().strip()
        qty_raw = float(require(payload, "quantity"))
        qty_kg, _ = normalize_quantity_to_kg(qty_raw, payload.get("unit"))
        args: Dict[str, Any] = {
            "producer_id": phone,
            "stock_id": stock_id,
            "movement_type": movement_type,
            "quantity": qty_kg,
            "reason": payload.get("reason") or f"Mouvement {movement_type} via agent",
        }
        return DomainResult(tool_id=ToolId.ADD_STOCK_MOVEMENT_BY_ID, tool_args=args)

    def adjust(
        self, state: Mapping[str, Any], payload: Mapping[str, Any]
    ) -> DomainResult:
        phone = require_phone(state)
        stock_id = str(require(payload, "stock_id"))
        qty_raw = float(require(payload, "quantity"))
        qty_kg, _ = normalize_quantity_to_kg(qty_raw, payload.get("unit"))
        args: Dict[str, Any] = {
            "producer_id": phone,
            "stock_id": stock_id,
            "new_quantity": qty_kg,
            "reason": payload.get("reason") or "Ajustement inventaire physique",
        }
        return DomainResult(tool_id=ToolId.ADJUST_STOCK_BY_ID, tool_args=args)

    def remove_partial(
        self, state: Mapping[str, Any], payload: Mapping[str, Any]
    ) -> DomainResult:
        phone = require_phone(state)
        stock_id = str(require(payload, "stock_id"))
        qty_raw = float(require(payload, "quantity"))
        qty_kg, _ = normalize_quantity_to_kg(qty_raw, payload.get("unit"))
        args: Dict[str, Any] = {
            "producer_id": phone,
            "stock_id": stock_id,
            "quantity": qty_kg,
            "reason": payload.get("reason") or "Retrait partiel via agent",
        }
        return DomainResult(tool_id=ToolId.REMOVE_STOCK_BY_ID, tool_args=args)

    def delete(
        self, state: Mapping[str, Any], payload: Mapping[str, Any]
    ) -> DomainResult:
        phone = require_phone(state)
        stock_id = str(require(payload, "stock_id"))
        args: Dict[str, Any] = {"producer_id": phone, "stock_id": stock_id}
        return DomainResult(tool_id=ToolId.DELETE_STOCK_BY_ID, tool_args=args)

    def update_level(self, command: StockUpdateLevelCommand) -> DomainResult:
        """Domain logic for adjusting a stock level based on a typed command."""

        args: Dict[str, object] = {
            "producer_id": command.producer_id,
            "stock_id": command.stock_id,
            "new_quantity": command.new_quantity_kg,
            "reason": command.reason,
        }
        return DomainResult(tool_id=ToolId.ADJUST_STOCK_BY_ID, tool_args=args)
