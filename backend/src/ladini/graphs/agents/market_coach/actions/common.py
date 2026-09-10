"""Shared utilities for MarketCoach action plugins."""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from ladini.graphs.agents.market_coach.utils import (
    canonical_unit_label,
    is_success_response,
)

logger = logging.getLogger("Agent.MarketCoach.actions")

_EMPTY_SLOT_VALUES = (None, "", [], {})


def require(payload: Mapping[str, Any], key: str) -> Any:
    """Ensure a required value exists in the provided payload."""
    value = payload.get(key) if payload else None
    if value in (None, "", []):
        raise ValueError(f"Missing required field: {key}")
    return value


def require_phone(state: Mapping[str, Any]) -> str:
    """Extract the caller identity from the state."""
    phone = None
    if state:
        phone = state.get("user_phone") or state.get("user_id") or state.get("phone")
    if not phone:
        raise ValueError("Missing required identity: user_phone")
    return str(phone)


_UNIT_TO_KG: Dict[str, float] = {
    "KG": 1.0,
    "KILOGRAMME": 1.0,
    "KILOGRAMMES": 1.0,
    "G": 0.001,
    "GRAMME": 0.001,
    "GRAMMES": 0.001,
    "TONNE": 1000.0,
    "TONNES": 1000.0,
    "T": 1000.0,
    "TON": 1000.0,
    "TONS": 1000.0,
    "TONE": 1000.0,
    "TONES": 1000.0,
    "QUINTAL": 100.0,
    "QUINTAUX": 100.0,
    "SAC": 100.0,
    "SACS": 100.0,
    "PANIER": 25.0,
    "PANIERS": 25.0,
    "CHARRETTE": 250.0,
    "CHARRETTES": 250.0,
}

_NON_MASS_UNITS = frozenset(
    {
        "HEAD",
        "TETE",
        "TÊTES",
        "TETES",
        "HEADS",
        "UNIT",
        "UNITE",
        "UNITÉ",
        "UNITES",
        "UNITÉS",
        "UNITS",
        "PIECE",
        "PIÈCE",
        "PIECES",
        "PIÈCES",
        "L",
        "LITRE",
        "LITRES",
    }
)


def normalize_quantity_to_kg(qty: float, unit_raw: Any) -> Tuple[float, str]:
    """Convert (quantity, unit) to kilograms when the unit is a mass unit."""
    try:
        qty_f = float(qty)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"quantity is not numeric: {qty!r}") from exc

    unit_clean = str(unit_raw or "KG").upper().strip()
    unit_canonical = canonical_unit_label(unit_clean, "KG")

    if unit_clean in _NON_MASS_UNITS:
        logger.info(
            "[UnitNormalize] Non-mass unit '%s' — bypassing conversion (qty=%s)",
            unit_canonical,
            qty_f,
        )
        return qty_f, unit_canonical

    coef = _UNIT_TO_KG.get(unit_clean)
    if coef is None:
        logger.warning(
            "Unknown unit '%s' — assuming KG (qty=%s). Add to _UNIT_TO_KG if recurrent.",
            unit_clean,
            qty_f,
        )
        return qty_f, "KG"
    if coef == 1.0:
        return qty_f, "KG"
    qty_kg = qty_f * coef
    logger.info(
        "[UnitNormalize] %s %s -> %s KG (coef=%s)",
        qty_f,
        unit_canonical,
        qty_kg,
        coef,
    )
    return qty_kg, "KG"


def get_goal_metadata(state: Mapping[str, Any]) -> Dict[str, Any]:
    raw = state.get("goal_metadata") if isinstance(state, Mapping) else None
    return dict(raw or {}) if isinstance(raw, Mapping) else {}


def is_update_mode(state: Mapping[str, Any]) -> bool:
    return bool(get_goal_metadata(state).get("update_mode"))


def require_current_entity(
    state: Mapping[str, Any], *, intent: Optional[str] = None
) -> Dict[str, Any]:
    current_entity = state.get("current_entity") if isinstance(state, Mapping) else None
    if not isinstance(current_entity, Mapping) or not current_entity:
        intent_label = f" '{intent}'" if intent else ""
        raise ValueError(
            f"Update intent{intent_label} requires a loaded current_entity snapshot."
        )
    return dict(current_entity)


def coalesce_entity_value(
    payload: Mapping[str, Any],
    entity: Mapping[str, Any],
    payload_keys: Iterable[str],
    entity_keys: Iterable[str],
) -> Any:
    for key in payload_keys:
        val = payload.get(key)
        if val not in _EMPTY_SLOT_VALUES:
            return val
    for key in entity_keys:
        val = entity.get(key)
        if val not in _EMPTY_SLOT_VALUES:
            return val
    return None


def to_float(value: Any, *, field: str) -> Optional[float]:
    if value in _EMPTY_SLOT_VALUES:
        return None
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Valeur numérique invalide pour {field}: {value!r}") from exc


def _snapshot_to_payload(kind: str, snapshot: Mapping[str, Any]) -> Dict[str, Any]:
    kind_up = str(kind or "").upper().strip()
    snap = dict(snapshot or {})
    if kind_up == "PRODUCT":
        return {
            "product_id": snap.get("product_id") or snap.get("id"),
            "product": snap.get("name")
            or snap.get("product")
            or snap.get("product_name"),
            "price": snap.get("price") or snap.get("price_fcfa"),
            "quantity": snap.get("quantity") or snap.get("quantity_for_sale"),
            "unit": snap.get("unit"),
        }
    if kind_up == "STOCK":
        return {
            "stock_id": snap.get("stock_id") or snap.get("id"),
            "farm_id": snap.get("farm_id"),
            "product": snap.get("item_name")
            or snap.get("product_name")
            or snap.get("product"),
            "quantity": snap.get("quantity"),
            "unit": snap.get("unit"),
        }
    if kind_up == "AUCTION":
        return {
            "auction_id": snap.get("auction_id") or snap.get("id"),
            "product": snap.get("product") or snap.get("product_name"),
            "quantity": snap.get("qty") or snap.get("quantity"),
            "unit": snap.get("unit"),
            "price": snap.get("max_price") or snap.get("max_price_per_unit"),
        }
    if kind_up in {"ORDER", "PREORDER"}:
        return {
            "order_id": snap.get("order_id") or snap.get("id"),
            "preorder_id": snap.get("preorder_id")
            or snap.get("order_id")
            or snap.get("id"),
            "status": snap.get("status"),
            "order_type": snap.get("order_type"),
        }
    return snap


async def load_entity_snapshot(
    mc_runtime: Any,
    goal: str,
    entity_id: str,
    payload: Mapping[str, Any] | None = None,
    *,
    phone: str,
    entity_kind: str,
) -> Dict[str, Any]:
    kind = str(entity_kind or "").strip().lower()
    goal_up = str(goal or "").upper().strip()
    entity_id_str = str(entity_id or "").strip()
    if not entity_id_str:
        raise ValueError("entity_id is required")

    if kind == "product":
        tool_name = "list_products"
        kwargs: Dict[str, Any] = {"phone": str(phone)}
        match_key = "product_id"
    elif kind == "stock":
        tool_name = "get_producer_stocks"
        kwargs = {"phone": str(phone)}
        match_key = "stock_id"
    elif kind == "auction":
        tool_name = "get_auctions"
        kwargs = {"status": "OPEN", "view_mode": "MARKETPLACE"}
        match_key = "auction_id"
    elif kind in {"order", "preorder"}:
        tool_name = "get_producer_orders"
        kwargs = {"phone": str(phone)}
        match_key = "order_id"
    else:
        raise ValueError(f"Unsupported entity_kind: {entity_kind!r}")

    logger.info(
        "[StatefulUpdate] loading snapshot tool=%s kind=%s goal=%s",
        tool_name,
        kind,
        goal_up,
    )
    result = await mc_runtime.call_db(tool_name, **kwargs)
    if not is_success_response(result):
        msg = (
            result.get("message")
            or f"Impossible de charger l'état actuel ({tool_name})."
        )
        return {
            "status": "ERROR",
            "validation_errors": ["snapshot_load_failed"],
            "response_strategy": "ERROR",
            "final_response": msg,
            "ag_ui_component": None,
        }

    data = result.get("data") or []
    if not isinstance(data, list):
        data = []

    snapshot = next(
        (
            it
            for it in data
            if isinstance(it, dict)
            and str(it.get(match_key) or it.get("id") or "").strip() == entity_id_str
        ),
        None,
    )
    if not snapshot:
        return {
            "status": "ERROR",
            "validation_errors": ["snapshot_not_found"],
            "response_strategy": "ERROR",
            "final_response": "Désolé, je n'arrive pas à retrouver l'élément à modifier.",
            "ag_ui_component": None,
        }

    snapshot_payload = _snapshot_to_payload(kind, snapshot)
    merged_payload: Dict[str, Any] = dict(snapshot_payload)
    merged_payload.update(dict(payload or {}))
    return {
        "original_entity": dict(snapshot),
        "current_entity": dict(snapshot),
        "transaction_payload": merged_payload,
        "ag_ui_component": None,
    }


__all__ = [
    "require",
    "require_phone",
    "load_entity_snapshot",
    "normalize_quantity_to_kg",
]
