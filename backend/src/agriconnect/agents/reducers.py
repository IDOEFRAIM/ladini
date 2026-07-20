"""Shared LangGraph state reducers — used by both MarketCoach and FormationCoach.

These are pure reducer functions for use with `typing.Annotated` in TypedDict states.
Each node returns a partial dict; the reducer merges it into the accumulated state.

Design rules:
  - Reducers are **pure** — no hidden filtering.
  - `_KEEP` sentinel: nodes return it for fields they don't want to touch.
  - Lists are **replaced entirely** (never silently accumulated).
  - Dicts use **deep-merge** (nested dicts merge, lists concat, scalars overwrite).
  - To force a reset on a dict field, the node writes `{"__reset__": True}`.
  - To reset AND populate in one shot (e.g. restoring a suspended payload
    without inheriting stale data from what interrupted it), the node
    writes `{"__reset__": True, **fresh_data}`.
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List


# ── Sentinel object ─────────────────────────────────────────────────
class _KeepSentinel:
    """Sentinel — when a reducer receives this, it preserves old value."""
    __slots__ = ()

    def __repr__(self) -> str:
        return "KEEP"

    def __bool__(self) -> bool:
        return False


_KEEP = _KeepSentinel()


# ── Reducers ────────────────────────────────────────────────────────

def replace_value(old: Any, new: Any) -> Any:
    """Pure overwrite. Returns old if new is _KEEP."""
    if new is _KEEP:
        return old
    return new


def load_snapshot(old: Any, new: Any) -> Any:
    if new is _KEEP or new is None:
        return old
    try:
        return copy.deepcopy(new)
    except Exception:
        return new


def replace_list(old: List[Any], new: List[Any]) -> List[Any]:
    """Full list replacement. Protects against None (keeps old)."""
    if new is None:
        return old if old is not None else []
    return new


def merge_dict(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    """Deep-merge non-destructif.

    - `new is None` or `new == {}` → preserves `old` (no silent loss).
    - `new == {"__reset__": True}` → explicit reset to `{}`.
    - `new == {"__reset__": True, **data}` → reset THEN populate with `data`
      in one shot (e.g. restoring a suspended payload without inheriting
      whatever the interrupting goal accumulated in the meantime — a single
      node return can't both wipe `old` and set new content otherwise,
      since a second write in the same super-step isn't possible).
    - Otherwise: recursive merge (nested dicts = merge, lists = concat, scalars = overwrite).
    """
    if new is None or (isinstance(new, dict) and not new):
        return old if old is not None else {}
    if isinstance(new, dict) and new.get("__reset__"):
        return {k: v for k, v in new.items() if k != "__reset__"}
    merged = dict(old or {})
    for key, value in new.items():
        existing = merged.get(key)
        if isinstance(value, dict) and isinstance(existing, dict):
            merged[key] = merge_dict(existing, value)
        elif isinstance(value, list) and isinstance(existing, list):
            merged[key] = existing + value
        else:
            merged[key] = value
    return merged


__all__ = [
    "_KEEP",
    "_KeepSentinel",
    "load_snapshot",
    "replace_value",
    "replace_list",
    "merge_dict",
]
