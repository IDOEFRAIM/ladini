"""Action handlers for the Stock domain."""
from __future__ import annotations

from typing import Any, Dict, Mapping, Tuple

from agriconnect.graphs.agents.market_coach.registry import register_action
from agriconnect.graphs.agents.market_coach.actions.common import (
    coalesce_entity_value,
    is_update_mode,
    normalize_quantity_to_kg,
    require,
    require_current_entity,
    require_phone,
    to_float,
)

@register_action("STOCK_GET_SUMMARY", mode="READ")
def prep_stock_get_summary(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Prépare l'appel pour l'inventaire global structuré ferme par ferme.

    Outil MCP : get_stocks(phone?).
    Depuis la refonte d'identité, `get_stocks` accepte explicitement `phone`
    ou `producer_id` et ne réutilise plus le champ détourné `farm_id`.
    """
    phone = require_phone(state)
    return "get_stocks", {"phone": phone}


@register_action("STOCK_GET_DETAIL", mode="READ")
def prep_stock_get_detail(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Prépare l'appel pour l'inventaire détaillé d'une exploitation.

    Outil MCP : get_farm_stocks(farm_id). Phone n'est pas attendu.
    """
    require_phone(state)  # safety check
    farm_id = str(require(payload, "farm_id"))
    return "get_farm_stocks", {"farm_id": farm_id}


@register_action("STOCK_GET_MOVEMENTS", mode="READ")
def prep_stock_get_movements(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Prépare la consultation de la traçabilité d'un lot.

    Outil MCP : get_stock_movements(stock_id, limit?).
    Phone n'est PAS un paramètre MCP.
    """
    require_phone(state)  # safety check
    stock_id = str(require(payload, "stock_id"))
    return "get_stock_movements", {"stock_id": stock_id}


@register_action("STOCK_REGISTER_HARVEST", mode="WRITE")
def prep_stock_register_harvest(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    require_phone(state)
    farm_id = str(require(payload, "farm_id"))
    product = str(require(payload, "product"))
    qty_raw = float(require(payload, "quantity_mentioned"))
    qty_kg, unit = normalize_quantity_to_kg(qty_raw, payload.get("unit_mentioned"))
    return "add_stock", {
        "farm_id": farm_id,
        "item_name": product,
        "quantity": qty_kg,
        "unit": unit,
        "stock_type": "HARVEST",
        "reason": payload.get("reason") or "Enregistrement récolte via agent",
    }


@register_action("STOCK_RECORD_MOVEMENT", mode="WRITE")
def prep_stock_record_movement(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    phone = require_phone(state)
    stock_id = str(require(payload, "stock_id"))
    movement_type = str(require(payload, "movement_type")).upper().strip()
    qty_raw = float(require(payload, "quantity_mentioned"))
    qty_kg, _ = normalize_quantity_to_kg(qty_raw, payload.get("unit_mentioned"))
    return "add_stock_movement_by_id", {
        "producer_id": phone,
        "stock_id": stock_id,
        "movement_type": movement_type,
        "quantity": qty_kg,
        "reason": payload.get("reason") or f"Mouvement {movement_type} via agent",
    }


@register_action("STOCK_ADJUST", mode="WRITE")
def prep_stock_adjust(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    phone = require_phone(state)
    stock_id = str(require(payload, "stock_id"))
    qty_raw = float(require(payload, "quantity_mentioned"))
    qty_kg, _ = normalize_quantity_to_kg(qty_raw, payload.get("unit_mentioned"))
    return "adjust_stock_by_id", {
        "producer_id": phone,
        "stock_id": stock_id,
        "new_quantity": qty_kg,
        "reason": payload.get("reason") or "Ajustement inventaire physique",
    }


@register_action("STOCK_REMOVE_PARTIAL", mode="WRITE")
def prep_stock_remove_partial(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    phone = require_phone(state)
    stock_id = str(require(payload, "stock_id"))
    qty_raw = float(require(payload, "quantity_mentioned"))
    qty_kg, _ = normalize_quantity_to_kg(qty_raw, payload.get("unit_mentioned"))
    return "remove_stock_by_id", {
        "producer_id": phone,
        "stock_id": stock_id,
        "quantity": qty_kg,
        "reason": payload.get("reason") or "Retrait partiel via agent",
    }


@register_action("STOCK_DELETE", mode="WRITE")
def prep_stock_delete(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    phone = require_phone(state)
    stock_id = str(require(payload, "stock_id"))
    return "delete_stock_by_id", {"producer_id": phone, "stock_id": stock_id}


@register_action("STOCK_UPDATE_LEVEL", mode="WRITE")
def prep_stock_update_level(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    phone = require_phone(state)
    if not is_update_mode(state):
        raise ValueError("STOCK_UPDATE_LEVEL doit être invoqué en mode update.")

    entity = require_current_entity(state, intent="STOCK_UPDATE_LEVEL")
    stock_id = coalesce_entity_value(
        payload,
        entity,
        payload_keys=("stock_id",),
        entity_keys=("stock_id", "id"),
    )
    if not stock_id:
        raise ValueError("Impossible d'identifier le lot de stock à modifier.")

    qty_raw = coalesce_entity_value(
        payload,
        entity,
        payload_keys=("quantity_mentioned", "quantity"),
        entity_keys=("quantity", "quantity_for_sale"),
    )
    qty_value = to_float(qty_raw, field="quantity")
    if qty_value is None:
        raise ValueError("Aucune nouvelle quantité fournie pour la mise à jour du lot.")

    unit = coalesce_entity_value(
        payload,
        entity,
        payload_keys=("unit_mentioned", "unit"),
        entity_keys=("unit",),
    )
    qty_normalized, _ = normalize_quantity_to_kg(qty_value, unit)
    reason = payload.get("reason") or "Ajustement de niveau via update"
    return "adjust_stock_by_id", {
        "producer_id": phone,
        "stock_id": str(stock_id),
        "new_quantity": qty_normalized,
        "reason": reason,
    }
