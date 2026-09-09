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
    internal_step = (
        raw_internal
        if raw_internal is not _SENTINEL
        else state.get("onboarding_internal_step")
    )
    if internal_step == "__NONE__":
        internal_step = None

    raw_display = updates.get("onboarding_step", _SENTINEL)
    display_step = (
        raw_display if raw_display is not _SENTINEL else state.get("onboarding_step")
    )
    display_label = str(display_step or "").upper()

    step_active = bool(internal_step) or display_label not in {"", "COMPLETED"}

    explicit_onboarding = updates.get("is_onboarding", _SENTINEL)
    if explicit_onboarding is not _SENTINEL:
        onboarding_active = bool(explicit_onboarding)
    else:
        onboarding_active = bool(state.get("is_onboarding") or step_active)

    if onboarding_active:
        updates["is_onboarding"] = True
        # (2026-09-08, revue de validation du bloc refondu) : ne plus écrire
        # `interpreted_event`/`detected_intent`/`interpreter_confidence` —
        # ce sont les 3 champs du CONTRAT DE SORTIE d'`input_interpreter`
        # (voir `interpreter/routing.py::_emit_onboarding`), qui les
        # réécrit de toute façon INCONDITIONNELLEMENT sur les 3 chemins
        # onboarding (no_llm/llm_crash/llm_success — voir
        # `_input_interpreter_impl`, branches `onboarding_active`). Cette
        # fonction (appelée par `session_bootstrap`, en AMONT
        # d'`input_interpreter` dans le graphe) écrivait donc une valeur
        # 100% redondante, toujours écrasée avant la fin du tour — un cas
        # concret du "session_bootstrap empiète sur l'interprétation
        # d'intention" identifié lors de la revue de validation. Seul
        # `is_onboarding` (contexte utilisateur, pas interprétation) reste
        # écrit ici.
        updates.setdefault("status", "WAITING_INPUT")
    else:
        updates["is_onboarding"] = False

    return onboarding_active


__all__ = ["resolve_onboarding_state"]
