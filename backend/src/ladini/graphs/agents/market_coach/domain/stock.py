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


@dataclass
class StockService:
    """(2026-09-14, Deep Intent Architecture Cleanup) : `record_movement`/
    `adjust`/`remove_partial`/`delete`/`update_level` SUPPRIMÉES avec leurs
    intents (aucun outil MCP réel, tool_name jamais câblé — voir
    intent.py). `StockUpdateLevelCommand` supprimée avec `update_level`,
    sans autre appelant.

    (2026-09-13, suite de l'audit) : `get_movements` supprimée à son tour —
    reconsidérée après vérification qu'aucun `@register_action` de
    market_coach ne l'appelle plus (zéro consommateur dans ce module depuis
    le retrait de `STOCK_GET_MOVEMENTS` du catalogue classifiable). Le tool
    MCP `get_stock_movements` reste réel, testé et exposé indépendamment
    (infrastructure/mcp/exposure.py, security.py) pour d'autres consommateurs
    MCP éventuels — seul ce wrapper spécifique à market_coach, devenu mort,
    est retiré."""

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
