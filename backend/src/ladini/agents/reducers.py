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

Patch semantics of a `merge_dict` channel, per key (the ONLY convention):

  ABSENT        -> unchanged. Removing a key from a dict (`d.pop(k)`,
                   `del d[k]`) before returning it therefore changes NOTHING.
  value         -> replace (nested dicts merge recursively, lists replace).
  None          -> the key is kept with the value None ("known, empty").
                   Every reader treats None like "no value".
  DELETE        -> the key is REMOVED. Use `clear_keys(...)` / `{k: DELETE}`.
  __reset__     -> the whole channel is replaced (see `merge_dict`).

`tests/architecture/test_merge_channel_clears_are_explicit.py` fails the build
when engine code removes a key from a dict it then feeds to a merge channel.
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

# Explicit key deletion inside a `merge_dict` channel. A plain string (not an
# object) on purpose: LangGraph serializes pending writes into checkpoints, and
# the marker must survive that round trip unchanged.
DELETE = "__ladini_delete__"


def clear_keys(*keys: str) -> Dict[str, str]:
    """Patch fragment deleting `keys` from a merge_dict channel:
    ``{"working_memory": clear_keys("active_goal", "step_index")}``."""
    return {key: DELETE for key in keys}


def mark_deleted(patch: Dict[str, Any], *keys: str) -> Dict[str, Any]:
    """In-place variant for a dict that will be returned into a merge channel:
    sets every key in `keys` to DELETE (instead of `pop`, which is a no-op)."""
    for key in keys:
        patch[key] = DELETE
    return patch


def strip_deleted(value: Any) -> Any:
    """Drop DELETE markers from a dict that is read locally (never stored)."""
    if isinstance(value, dict):
        return {k: strip_deleted(v) for k, v in value.items() if v != DELETE}
    return value


# ── Reducers ────────────────────────────────────────────────────────


def replace_value(old: Any, new: Any) -> Any:
    """Pure overwrite. Returns old if new is _KEEP; DELETE clears (None)."""
    if new is _KEEP:
        return old
    if isinstance(new, str) and new == DELETE:
        return None
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
    - Otherwise: recursive merge (nested dicts = merge, lists = REPLACED
      entirely — same rule as the module-level `replace_list` reducer,
      see module docstring — scalars = overwrite).

    Real incident (2026-08-30): `transaction_payload` (a `merge_dict`
    channel) grew a `pricing_tiers` list. Concatenating it here (as this
    function used to do for any list nested in a dict) meant every turn
    that merely re-returned the SAME 2 tiers (unrelated re-render, a
    repeated user message, an UPDATE correction) doubled the list forever —
    a producer's 2-tier offer turned into dozens of duplicate lines in the
    confirmation summary within a few turns. A list value returned by a
    node is the FULL current value, exactly like a scalar — it must replace,
    never accumulate.
    """
    if new is None or (isinstance(new, dict) and not new):
        return old if old is not None else {}
    if isinstance(new, dict) and new.get("__reset__"):
        return {
            k: strip_deleted(v)
            for k, v in new.items()
            if k != "__reset__" and v != DELETE
        }
    merged = dict(old or {})
    for key, value in new.items():
        if isinstance(value, str) and value == DELETE:
            merged.pop(key, None)
            continue
        existing = merged.get(key)
        if isinstance(value, dict) and isinstance(existing, dict):
            merged[key] = merge_dict(existing, value)
        else:
            merged[key] = strip_deleted(value)
    return merged


__all__ = [
    "DELETE",
    "clear_keys",
    "mark_deleted",
    "strip_deleted",
    "_KEEP",
    "_KeepSentinel",
    "load_snapshot",
    "replace_value",
    "replace_list",
    "merge_dict",
]
