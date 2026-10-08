"""Workspace metadata sanitization helpers.

This module centralizes the logic for what can be stored in the
``agri_workspaces.metadata`` column. The goal is to keep only the minimal
navigation context required to resume a conversation, while banning any bulk
business data (catalogs, stocks, traces, etc.).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

LANGGRAPH_STATE_KEY = "_langgraph_state"

ALLOWED_METADATA_KEYS = frozenset(
    {
        "current_node",
        "active_goal",
        "current_goal",
        "form_data",
        "session_id",
        "pending_interaction_kind",
        "available_mapping",
        "expected_candidates",
        "disambiguation_trigger_id",
        "available_mapping_kind",
        "last_active_cart",
        "last_turn",
    }
)
_INTERNAL_METADATA_KEYS = frozenset()
_MAX_FORM_VALUE_LENGTH = 256
# Hard cap on the total serialized metadata column — enforced in filter_metadata_dict.
_MAX_METADATA_BYTES = 48_000  # 48 KB
_MAX_CART_SNAPSHOT_ITEMS = 20  # keep at most 20 cart lines in the metadata snapshot

_FORM_WHITELIST = frozenset(
    {
        "product",
        "product_name",
        "variety",
        "quality_grade",
        "quantity",
        "quantity_mentioned",
        "unit",
        "unit_mentioned",
        "price",
        "price_mentioned",
        "currency",
        "is_negotiable",
        "zone",
        "zone_name",
        "deadline",
        "intent",
        "role",
        "farm_id",
        "stock_id",
        "auction_id",
        "bid_id",
        "resolved_id",
        "confirmation_summary",
    }
)


def _safe_scalar(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return value[:_MAX_FORM_VALUE_LENGTH]
    if isinstance(value, (int, float, bool)):
        return value
    return str(value)[:_MAX_FORM_VALUE_LENGTH]


def clean_form_data(form_data: Any) -> Dict[str, Any]:
    if not isinstance(form_data, dict):
        return {}
    cleaned: Dict[str, Any] = {}
    for k, v in form_data.items():
        key = str(k)
        if key not in _FORM_WHITELIST:
            continue
        cleaned[key] = _safe_scalar(v)
    return cleaned


def clean_mapping(mapping: Any) -> Dict[str, str]:
    if not isinstance(mapping, dict):
        return {}
    cleaned: Dict[str, str] = {}
    for key, value in mapping.items():
        safe_key = str(key)
        safe_val = _safe_scalar(value)
        if safe_val not in (None, ""):
            cleaned[safe_key] = str(safe_val)
    return cleaned


def clean_candidates(candidates: Any) -> List[str]:
    if not isinstance(candidates, (list, tuple)):
        return []
    cleaned: List[str] = []
    for item in candidates:
        safe_item = _safe_scalar(item)
        if safe_item not in (None, ""):
            cleaned.append(str(safe_item))
    return cleaned


_CART_LINE_KEYS = frozenset(
    {
        "product",
        "product_name",
        "quantity",
        "unit",
        "price",
        "vendor_phone",
        "vendor_name",
    }
)


def clean_cart_snapshot(cart: Any) -> List[Dict[str, Any]]:
    """Sanitize a list of cart line dicts down to essential scalar fields only."""
    if not isinstance(cart, (list, tuple)):
        return []
    cleaned: List[Dict[str, Any]] = []
    for item in cart[:_MAX_CART_SNAPSHOT_ITEMS]:
        if not isinstance(item, dict):
            continue
        line: Dict[str, Any] = {}
        for k in _CART_LINE_KEYS:
            v = item.get(k)
            if v is not None:
                line[k] = _safe_scalar(v)
        if line:
            cleaned.append(line)
    return cleaned


_MAX_LAST_TURN_RESPONSE_CHARS = 3500
_MAX_LAST_TURN_INTERACTIVE_BYTES = 4000


def clean_last_turn(value: Any) -> Dict[str, Any]:
    """Résultat du DERNIER tour terminé, persisté avec l'état dans la MÊME écriture (`agri_workspaces`) : permet de REJOUER la réponse d'un message
    livré deux fois (retry de tâche, redélivrance webhook, redémarrage) sans relancer le graphe sur un état déjà avancé. Identité = `message_sid`
    (id fournisseur), jamais le texte. Borné ; `{}` si inexploitable."""
    if not isinstance(value, dict):
        return {}
    sid = value.get("message_sid")
    text = value.get("final_response")
    if not isinstance(sid, str) or not sid or not isinstance(text, str):
        return {}
    out: Dict[str, Any] = {"message_sid": sid[:200], "final_response": text[:_MAX_LAST_TURN_RESPONSE_CHARS]}
    interactive = value.get("interactive")
    if isinstance(interactive, dict) and interactive:
        try:
            if len(json.dumps(interactive, default=str).encode("utf-8")) <= _MAX_LAST_TURN_INTERACTIVE_BYTES:
                out["interactive"] = interactive
        except (TypeError, ValueError):
            pass
    return out


_METADATA_NORMALIZERS = {
    "last_turn": clean_last_turn,
    "form_data": clean_form_data,
    "available_mapping": clean_mapping,
    "expected_candidates": clean_candidates,
    "last_active_cart": clean_cart_snapshot,
}


def build_metadata_from_state(state: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the minimal metadata snapshot from the agent state."""

    snapshot: Dict[str, Any] = {}

    current_node = state.get("current_node") or state.get("last_node")
    if current_node:
        snapshot["current_node"] = str(current_node)

    active_goal = state.get("current_goal") or state.get("active_goal")
    if active_goal:
        snapshot["active_goal"] = str(active_goal)
        snapshot.setdefault("current_goal", str(active_goal))

    current_goal = state.get("current_goal")
    if current_goal:
        snapshot["current_goal"] = str(current_goal)

    # (2026-09-02, "no legacy shim") : projection ops-only du DISCRIMINANT
    # (le `kind`, pas le dict complet — snapshot minimal, cf. docstring de
    # module). `pending_interaction` reste de toute façon disponible pour la
    # reprise réelle via le checkpoint LangGraph normal (DURABLE, voir
    # core/state_profile.py) — cette ligne ne sert QUE l'observabilité de ce
    # snapshot compact, ce n'est pas une 2e source de vérité runtime.
    pending_interaction = state.get("pending_interaction")
    if isinstance(pending_interaction, dict) and pending_interaction.get("kind"):
        snapshot["pending_interaction_kind"] = str(pending_interaction["kind"])

    available_mapping = clean_mapping(state.get("available_mapping"))
    if available_mapping:
        snapshot["available_mapping"] = available_mapping

    expected_candidates = clean_candidates(state.get("expected_candidates"))
    if expected_candidates:
        snapshot["expected_candidates"] = expected_candidates

    working_memory = state.get("working_memory") or {}
    trigger_id = working_memory.get("disambiguation_trigger_id")
    if trigger_id:
        snapshot["disambiguation_trigger_id"] = str(trigger_id)

    mapping_kind = working_memory.get("available_mapping_kind")
    if mapping_kind:
        snapshot["available_mapping_kind"] = str(mapping_kind)

    raw_form_data = state.get("form_data") or state.get("transaction_payload") or {}
    cleaned_form = clean_form_data(raw_form_data)
    if cleaned_form:
        snapshot["form_data"] = cleaned_form

    session_id = (
        state.get("session_id")
        or state.get("workspace_session_id")
        or state.get("thread_id")
    )
    if session_id:
        snapshot["session_id"] = str(session_id)

    # Persist a lightweight cart snapshot so the resolver can restore it if the
    # LangGraph state is dropped (e.g. after a workspace reset).
    raw_cart = state.get("active_cart")
    if not raw_cart:
        raw_cart = (state.get("working_memory") or {}).get("last_active_cart")
    cleaned_cart = clean_cart_snapshot(raw_cart)
    if cleaned_cart:
        snapshot["last_active_cart"] = cleaned_cart

    return snapshot


def filter_metadata_dict(meta: Any) -> Dict[str, Any]:
    """Filter an existing metadata dict down to the allowed keys only.

    Also enforces ``_MAX_METADATA_BYTES``: if the filtered dict is still too
    large (e.g. because of a big ``last_active_cart``), the cart snapshot is
    dropped first, then the entire dict if still over budget.
    """
    if not isinstance(meta, dict):
        return {}

    internal: Dict[str, Any] = {}
    for key in _INTERNAL_METADATA_KEYS:
        if key in meta:
            internal[key] = meta[key]

    filtered: Dict[str, Any] = {}
    for key in ALLOWED_METADATA_KEYS:
        if key not in meta:
            continue
        if key in _METADATA_NORMALIZERS:
            cleaned_value = _METADATA_NORMALIZERS[key](meta.get(key))
            if cleaned_value not in (None, {}, [], ""):
                filtered[key] = cleaned_value
            continue

        value = meta.get(key)
        safe_value = _safe_scalar(value)
        if safe_value not in (None, ""):
            filtered[key] = safe_value

    filtered.update(internal)

    # Size guard — drop optional bulk fields first, then give up entirely.
    if (
        len(json.dumps(filtered, ensure_ascii=False).encode("utf-8"))
        > _MAX_METADATA_BYTES
    ):
        filtered.pop("last_active_cart", None)
    if (
        len(json.dumps(filtered, ensure_ascii=False).encode("utf-8"))
        > _MAX_METADATA_BYTES
    ):
        return {}

    return filtered
