"""Action handlers for the Stock domain.

(2026-09-14, Deep Intent Architecture Cleanup) : STOCK_GET_MOVEMENTS/
STOCK_RECORD_MOVEMENT/STOCK_ADJUST/STOCK_REMOVE_PARTIAL/STOCK_DELETE/
STOCK_UPDATE_LEVEL sont SUPPRIMÉS — leurs `tool_name` (`*_by_id`) n'ont
jamais correspondu à une méthode DB réelle, ET aucun outil MCP exposé ne
les sert (voir intent.py pour l'audit complet). Ne restent que les
capacités RÉELLEMENT câblées : consultation (GET_SUMMARY/GET_DETAIL) et
enregistrement d'une récolte (REGISTER_HARVEST)."""

from __future__ import annotations

from typing import Any, Dict, Mapping, Tuple

from ladini.graphs.agents.market_coach.actions.tooling import ToolResolver
from ladini.graphs.agents.market_coach.domain import DomainContext
from ladini.graphs.agents.market_coach.domain.stock import StockService
from ladini.graphs.agents.market_coach.registry import register_action


@register_action("STOCK_GET_SUMMARY", mode="READ")
def prep_stock_get_summary(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    """Prépare l'appel pour l'inventaire global structuré ferme par ferme.

    Outil MCP : get_stocks(phone?).
    Depuis la refonte d'identité, `get_stocks` accepte explicitement `phone`
    ou `producer_id` et ne réutilise plus le champ détourné `farm_id`.
    """
    context = DomainContext.from_state(state)
    service = StockService(context=context)
    result = service.get_summary(state, payload)
    tool_name = ToolResolver.resolve_name(result.tool_id or "get_stocks")
    return tool_name, dict(result.tool_args)


@register_action("STOCK_GET_DETAIL", mode="READ")
def prep_stock_get_detail(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    """Prépare l'appel pour l'inventaire détaillé d'une exploitation.

    Outil MCP : get_farm_stocks(farm_id, producer_phone). `producer_phone`
    est l'identité du tour (épinglée à la session), exigée comme preuve de
    propriété sur `farm_id` — voir `StockService.get_detail`.
    """
    context = DomainContext.from_state(state)
    service = StockService(context=context)
    result = service.get_detail(state, payload)
    tool_name = ToolResolver.resolve_name(result.tool_id or "get_farm_stocks")
    return tool_name, dict(result.tool_args)


@register_action("STOCK_REGISTER_HARVEST", mode="WRITE")
def prep_stock_register_harvest(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    context = DomainContext.from_state(state)
    service = StockService(context=context)
    result = service.register_harvest(state, payload)
    tool_name = ToolResolver.resolve_name(result.tool_id or "add_stock")
    return tool_name, dict(result.tool_args)


