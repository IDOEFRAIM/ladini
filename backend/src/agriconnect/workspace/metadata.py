"""Workspace metadata sanitization helpers.

This module centralizes the logic for what can be stored in the
``agri_workspaces.metadata`` column. The goal is to keep only the minimal
navigation context required to resume a conversation, while banning any bulk
business data (catalogs, stocks, traces, etc.).
"""
from __future__ import annotations

from typing import Any, Dict, List

LANGGRAPH_STATE_KEY = "_langgraph_state"

ALLOWED_METADATA_KEYS = frozenset(
    {
        "current_node",
        "active_goal",
        "current_goal",
        "form_data",
        "session_id",
        "expected_input",
        "available_mapping",
        "expected_candidates",
        "disambiguation_trigger_id",
        "available_mapping_kind",
    }
)
_INTERNAL_METADATA_KEYS = frozenset()
_MAX_FORM_VALUE_LENGTH = 256

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


_METADATA_NORMALIZERS = {
    "form_data": clean_form_data,
    "available_mapping": clean_mapping,
    "expected_candidates": clean_candidates,
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

    expected_input = state.get("expected_input")
    if expected_input:
        snapshot["expected_input"] = str(expected_input)

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

    session_id = state.get("session_id") or state.get("workspace_session_id") or state.get("thread_id")
    if session_id:
        snapshot["session_id"] = str(session_id)

    return snapshot


def filter_metadata_dict(meta: Any) -> Dict[str, Any]:
    """Filter an existing metadata dict down to the allowed keys only."""

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
    return filtered
