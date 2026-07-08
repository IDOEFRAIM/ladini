"""MCP Schema Resolution & Argument Building.

Handles tool-schema discovery, argument lookup/casting, PII masking,
ASCII folding, and self-healing arg repair.  Extracted from the former
monolithic ``nodes/executor.py``.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import re
import unicodedata
from typing import Any, Awaitable, Callable, Dict, List, Optional

from agriconnect.core.logging import get_logger

logger = get_logger("AgriConnect.Market.SchemaResolver")


# =====================================================================
# TOOL SCHEMA EXTRACTION
# =====================================================================

def extract_tool_schema(tool_item: Any) -> Dict[str, Any]:
    if isinstance(tool_item, dict):
        for key in ("inputSchema", "input_schema", "parameters"):
            if isinstance(tool_item.get(key), dict):
                return tool_item[key]
        fn = tool_item.get("function")
        if isinstance(fn, dict) and isinstance(fn.get("parameters"), dict):
            return fn["parameters"]
    for attr in ("inputSchema", "input_schema", "parameters"):
        schema = getattr(tool_item, attr, None)
        if isinstance(schema, dict):
            return schema
    return {"type": "object", "properties": {}, "required": []}


def extract_tool_name(tool_item: Any) -> Optional[str]:
    if isinstance(tool_item, dict):
        fn = tool_item.get("function")
        if isinstance(fn, dict) and fn.get("name"):
            return str(fn["name"])
        if tool_item.get("name"):
            return str(tool_item["name"])
    name = getattr(tool_item, "name", None)
    return str(name) if name else None


async def list_mcp_tools(mc_runtime: Any) -> List[Any]:
    client = getattr(mc_runtime, "db_client", None)
    if client is None:
        return []

    for method_name in ("list_tools", "get_tools_for_langchain"):
        fn = getattr(client, method_name, None)
        if fn is None:
            continue
        raw = fn()
        if inspect.isawaitable(raw) or asyncio.iscoroutine(raw):
            raw = await raw
        if isinstance(raw, list) and raw:
            return raw

    raw = getattr(client, "_tools_cache", None)
    if isinstance(raw, list) and raw:
        return raw
    return []


async def get_tool_schema(mc_runtime: Any, tool_name: str) -> Dict[str, Any]:
    tools = await list_mcp_tools(mc_runtime)
    for tool_item in tools:
        if extract_tool_name(tool_item) == tool_name:
            return extract_tool_schema(tool_item)
    return {"type": "object", "properties": {}, "required": []}


# =====================================================================
# ARGUMENT LOOKUP & CASTING
# =====================================================================

IDENTITY_ALIASES = frozenset({"user_phone", "phone", "user_id", "producer_id"})

ARG_ALIASES: Dict[str, List[str]] = {
    "phone": ["user_phone"],
    "user_id": ["producer_id"],
    "producer_id": ["user_id"],
    "product_name": ["product", "name", "item_name", "product_query"],
    "product": ["product_name", "name", "item_name", "product_query"],
    "item_name": ["product", "product_name", "name"],
    "name": ["product", "product_name", "item_name"],
    "farm_name": ["farm_label", "farm_title"],
    "quantity": ["quantity_mentioned", "qty", "quantity_for_sale"],
    "qty": ["quantity_mentioned", "quantity"],
    "quantity_for_sale": ["quantity_mentioned", "quantity", "qty"],
    "unit": ["unit_mentioned"],
    "price": ["price_mentioned", "max_price", "offered_price", "proposed_price"],
    "max_price": ["price_mentioned", "price", "target_price"],
    "offered_price": ["price_mentioned", "price"],
    "proposed_price": ["price_mentioned", "price"],
    "zone": ["zone_name", "zone_query", "zone_id"],
    "zone_name": ["zone", "zone_query", "zone_id"],
    "zone_query": ["zone_name", "zone"],
    "zone_id": ["zone_name", "zone"],
    "latitude": ["lat"],
    "longitude": ["lon", "lng"],
    "lat": ["latitude"],
    "lon": ["longitude", "lng"],
    "transaction_id": ["bid_id", "staging_id"],
    "staging_id": ["transaction_id", "bid_id"],
}


def lookup_arg_value(
    param_name: str,
    state: Dict[str, Any],
    payload: Dict[str, Any],
    initial_args: Dict[str, Any],
) -> Any:
    if param_name in {"producer_id", "user_id"}:
        uuid_val = state.get("user_id")
        if uuid_val:
            return uuid_val
        if state.get("user_phone"):
            return state.get("user_phone")

    if param_name in {"user_phone", "phone"} and state.get("user_phone"):
        return state.get("user_phone")

    sources = [
        initial_args,
        payload,
        state.get("extracted_entities") or {},
        state.get("stable_entities") or {},
        state.get("working_memory") or {},
    ]
    candidate_names = [param_name] + ARG_ALIASES.get(param_name, [])
    for candidate_name in candidate_names:
        for source in sources:
            if source and source.get(candidate_name) not in (None, "", [], {}):
                return source[candidate_name]
    return None


def cast_arg_value(value: Any, json_type: str) -> Any:
    if value in (None, "", [], {}):
        return None
    try:
        if json_type == "number":
            return float(str(value).replace(",", ".").replace(" ", ""))
        if json_type == "integer":
            return int(float(str(value).replace(",", ".").replace(" ", "")))
        if json_type == "boolean":
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in {"1", "true", "yes", "oui", "y", "on"}
        if json_type == "array":
            if isinstance(value, (list, tuple, set)):
                return list(value)
            try:
                parsed = json.loads(value) if isinstance(value, str) else None
                if isinstance(parsed, list):
                    return parsed
            except Exception:
                pass
            return value
        if json_type == "object":
            if isinstance(value, dict):
                return value
            if hasattr(value, "model_dump"):
                try:
                    dumped = value.model_dump()
                    if isinstance(dumped, dict):
                        return dumped
                except Exception:
                    pass
            try:
                parsed = json.loads(value) if isinstance(value, str) else None
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
            return value
        return str(value)
    except Exception:
        return value


class MissingRequiredMCPArgs(ValueError):
    def __init__(self, tool_name: str, missing_args: List[str]):
        self.tool_name = tool_name
        self.missing_args = missing_args
        super().__init__(f"Missing required MCP args for {tool_name}: {', '.join(missing_args)}")


def build_resolved_tool_args(
    tool_name: str,
    schema: Dict[str, Any],
    state: Dict[str, Any],
    payload: Dict[str, Any],
    initial_args: Dict[str, Any],
) -> Dict[str, Any]:
    properties: Dict[str, Any] = schema.get("properties") or {}
    required: List[str] = list(schema.get("required") or [])

    if not properties:
        return {k: v for k, v in (initial_args or {}).items() if v is not None}

    resolved_args: Dict[str, Any] = {}
    resolved_identity = False
    missing_required: List[str] = []
    for param_name, param_schema in properties.items():
        json_type = str(param_schema.get("type") or "string").lower()
        value = lookup_arg_value(param_name, state, payload, initial_args)

        if param_name in IDENTITY_ALIASES and value is not None:
            resolved_identity = True

        if value in (None, "", [], {}):
            if param_name in required:
                default = param_schema.get("default")
                if default not in (None, "", [], {}):
                    resolved_args[param_name] = cast_arg_value(default, json_type)
                else:
                    missing_required.append(param_name)
            continue

        resolved_args[param_name] = cast_arg_value(value, json_type)

    if missing_required:
        raise MissingRequiredMCPArgs(tool_name, missing_required)

    return resolved_args


# =====================================================================
# SANITIZATION, PII MASKING, ASCII FOLDING
# =====================================================================

def sanitize_mcp_args(args: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in args.items() if v is not None}


_PII_ARG_KEYS = frozenset({"phone", "user_phone", "user_id", "producer_id"})


def mask_pii_args(args: Dict[str, Any]) -> Dict[str, Any]:
    masked = {}
    for k, v in args.items():
        if k in _PII_ARG_KEYS:
            s = str(v or "")
            masked[k] = f"***{s[-4:]}" if len(s) >= 4 else "***"
        else:
            masked[k] = v
    return masked


_ASCII_FOLD_TRANSLATION = str.maketrans({
    "œ": "oe",
    "Œ": "OE",
    "æ": "ae",
    "Æ": "AE",
    "'": "'",
    "“": '"',
    "”": '"',
})


def _ascii_fold_str(text: str) -> str:
    if not text:
        return text
    text = text.translate(_ASCII_FOLD_TRANSLATION)
    normalized = unicodedata.normalize("NFKD", text)
    return normalized.encode("ascii", "ignore").decode("ascii")


def ascii_fold_value(value: Any) -> Any:
    if isinstance(value, str):
        return _ascii_fold_str(value)
    if isinstance(value, list):
        return [ascii_fold_value(v) for v in value]
    if isinstance(value, tuple):
        return tuple(ascii_fold_value(v) for v in value)
    if isinstance(value, dict):
        return {k: ascii_fold_value(v) for k, v in value.items()}
    return value


def sanitize_and_fold(args: Dict[str, Any]) -> Dict[str, Any]:
    return ascii_fold_value(sanitize_mcp_args(args))


# =====================================================================
# SELF-HEALING ARG REPAIR
# =====================================================================

def attempt_arg_repair(raw_value: Any, expected_type: str) -> Optional[Any]:
    if raw_value is None:
        return None
    s = str(raw_value).strip().lower()

    if expected_type in ("number", "integer"):
        for suffix in ("kg", "tonnes", "tonne", "t", "sacs", "sac", "fcfa", "cfa", "f"):
            if s.endswith(suffix):
                s = s[:-len(suffix)].strip()
                break
        s = s.replace(",", ".").replace(" ", "").replace("\xa0", "")
        _WORD_NUMS = {
            "un": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5,
            "six": 6, "sept": 7, "huit": 8, "neuf": 9, "dix": 10,
            "vingt": 20, "trente": 30, "cinquante": 50, "cent": 100, "mille": 1000,
        }
        if s in _WORD_NUMS:
            return float(_WORD_NUMS[s]) if expected_type == "number" else _WORD_NUMS[s]
        try:
            val = float(s)
            return val if expected_type == "number" else int(val)
        except (ValueError, TypeError):
            return None

    if expected_type == "boolean":
        return s in ("1", "true", "yes", "oui", "ok", "y")

    return str(raw_value)


__all__ = [
    "extract_tool_schema",
    "extract_tool_name",
    "list_mcp_tools",
    "get_tool_schema",
    "IDENTITY_ALIASES",
    "ARG_ALIASES",
    "lookup_arg_value",
    "cast_arg_value",
    "MissingRequiredMCPArgs",
    "build_resolved_tool_args",
    "sanitize_mcp_args",
    "mask_pii_args",
    "ascii_fold_value",
    "sanitize_and_fold",
    "attempt_arg_repair",
]
