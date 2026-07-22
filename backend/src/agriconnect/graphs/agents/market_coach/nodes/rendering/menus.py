"""Rendering — SELECTION_MENU (menus numérotés AG-UI)."""

from __future__ import annotations

from typing import Any, Dict, List

from agriconnect.graphs.agents.market_coach.nodes.rendering.common import (
    RenderContext,
    apply_corrections,
    indexed_options,
    list_menu_component,
)


async def render_selection_menu(ctx: RenderContext) -> Dict[str, Any]:
    state = ctx.state
    # Réutiliser un final_response pré-calculé (cart, negotiation, tracking...).
    if state.get("final_response"):
        return apply_corrections(state, {
            "final_response": state.get("final_response"),
            "ag_ui_component": state.get("ag_ui_component"),
        })
    if not ctx.goal:
        return apply_corrections(state, {
            "final_response": f"{ctx.salutation}Que souhaitez-vous choisir ?",
            "ag_ui_component": None,
        })

    wm = state.get("working_memory") or {}
    preformatted = wm.get("auction_menu") or wm.get("bids_menu") or wm.get("stocks_menu") or wm.get("generic_menu")
    candidates: List[str] = state.get("expected_candidates") or []

    if preformatted:
        text_output = str(preformatted)
    else:
        base_menu = "\n".join(f"{i}. {c}" for i, c in enumerate(candidates, start=1))
        text_output = "Veuillez choisir une option :\n" + base_menu
        if "répondez" not in text_output.lower():
            text_output += "\n\n👉 Répondez uniquement par le numéro de votre choix (ex: '2')."

    return apply_corrections(state, {
        "final_response": text_output,
        "ag_ui_component": list_menu_component(
            "Sélection", indexed_options(candidates), metadata={"goal": ctx.goal},
        ),
    })
