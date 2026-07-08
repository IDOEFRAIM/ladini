"""State compaction utilities — single source of truth.

Trims conversation history, tool execution logs, and order-tracking
caches to bounded windows so the LangGraph state does not grow
unboundedly across turns.

Used by ``nodes/validation.py`` and ``nodes/memory.py``.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

MAX_MESSAGE_HISTORY = 5
MAX_TOOL_HISTORY = 10
MAX_MAPPING_CACHE_SIZE = 10


def trim_sequence_window(items: Any, limit: int) -> Optional[List[Any]]:
    if isinstance(items, list) and len(items) > limit:
        return items[-limit:]
    return None


def compact_order_tracking_context(
    raw_ctx: Any,
    *,
    strategy: str = "trim",
) -> Optional[Dict[str, Any]]:
    """Compact the order-tracking context.

    strategy:
        "trim"  — keep at most ``MAX_MAPPING_CACHE_SIZE`` entries (default).
        "drop"  — remove ``mapping_cache`` entirely.
    """
    if not isinstance(raw_ctx, dict):
        return None

    mapping = raw_ctx.get("mapping_cache")

    if strategy == "drop":
        if "mapping_cache" in raw_ctx:
            compacted = dict(raw_ctx)
            compacted.pop("mapping_cache", None)
            return compacted
        return None

    if isinstance(mapping, dict) and len(mapping) > MAX_MAPPING_CACHE_SIZE:
        trimmed_entries = list(mapping.items())[:MAX_MAPPING_CACHE_SIZE]
        compacted = dict(raw_ctx)
        compacted["mapping_cache"] = dict(trimmed_entries)
        return compacted
    return None


def build_compaction_patch(state: Dict[str, Any], *, tracking_strategy: str = "trim") -> Dict[str, Any]:
    patch: Dict[str, Any] = {}
    for key in ("messages", "history", "conversation_history"):
        trimmed = trim_sequence_window(state.get(key), MAX_MESSAGE_HISTORY)
        if trimmed is not None:
            patch[key] = trimmed

    trimmed_tool_history = trim_sequence_window(
        state.get("tool_execution_history"), MAX_TOOL_HISTORY
    )
    if trimmed_tool_history is not None:
        patch["tool_execution_history"] = trimmed_tool_history

    compacted_tracking = compact_order_tracking_context(
        state.get("order_tracking_context"), strategy=tracking_strategy
    )
    if compacted_tracking is not None:
        patch["order_tracking_context"] = compacted_tracking

    return patch


__all__ = [
    "MAX_MESSAGE_HISTORY",
    "MAX_TOOL_HISTORY",
    "MAX_MAPPING_CACHE_SIZE",
    "trim_sequence_window",
    "compact_order_tracking_context",
    "build_compaction_patch",
]
