"""Onboarding state resolution — extracted from input_normalizer.

Centralises the logic that determines whether the current turn is an
onboarding turn and which onboarding step is active.
"""
from __future__ import annotations

from typing import Any, Dict

_SENTINEL = object()


def resolve_onboarding_state(state: Dict[str, Any], updates: Dict[str, Any]) -> bool:
    """Determine whether onboarding is active and patch *updates* accordingly.

    Returns ``True`` if onboarding is active for this turn.
    """
    raw_internal = updates.get("onboarding_internal_step", _SENTINEL)
    internal_step = raw_internal if raw_internal is not _SENTINEL else state.get("onboarding_internal_step")
    if internal_step == "__NONE__":
        internal_step = None

    raw_display = updates.get("onboarding_step", _SENTINEL)
    display_step = raw_display if raw_display is not _SENTINEL else state.get("onboarding_step")
    display_label = str(display_step or "").upper()

    step_active = bool(internal_step) or display_label not in {"", "COMPLETED"}

    explicit_onboarding = updates.get("is_onboarding", _SENTINEL)
    if explicit_onboarding is not _SENTINEL:
        onboarding_active = bool(explicit_onboarding)
    else:
        onboarding_active = bool(state.get("is_onboarding") or step_active)

    if onboarding_active:
        updates["is_onboarding"] = True
        updates["interpreted_event"] = "ONBOARDING_INPUT"
        updates["detected_intent"] = "ONBOARDING"
        updates["interpreter_confidence"] = 1.0
        updates.setdefault("status", "WAITING_INPUT")
    else:
        updates["is_onboarding"] = False

    return onboarding_active


__all__ = ["resolve_onboarding_state"]
