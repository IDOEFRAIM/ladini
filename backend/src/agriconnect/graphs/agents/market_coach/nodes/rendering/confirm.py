"""Rendering — CONFIRMATION (récapitulatif + FormConfirmation)."""

from __future__ import annotations

from typing import Any, Dict

from agriconnect.graphs.agents.market_coach.nodes.rendering.common import (
    RenderContext,
    apply_corrections,
)
from agriconnect.graphs.agents.market_coach.services.ui.confirmation_summary import (
    build_confirmation_summary,
)


async def render_confirmation(ctx: RenderContext) -> Dict[str, Any]:
    state = ctx.state
    if not ctx.goal:
        return apply_corrections(state, {
            "final_response": f"{ctx.salutation}Que souhaitez-vous confirmer exactement ?",
            "ag_ui_component": None,
        })

    # `confirmation_summary` est normalement posé par `confirmation_gate`
    # (source unique du récap). Le repli ci-dessous ne sert que si un chemin
    # atteint ce rendu sans passer par la gate ; il délègue au MÊME
    # constructeur (`build_confirmation_summary`) au lieu d'un format maison
    # divergent — l'ancien `_build_summary` local produisait un récap
    # différent ("Vente de X\nQuantité : Y") qui contournait toute la logique
    # d'unités (prix par KG vs quantité en tonnes, avertissement de mismatch).
    summary = state.get("confirmation_summary")
    if not summary and ctx.payload:
        summary = build_confirmation_summary(ctx.goal, ctx.payload)

    text_output = f"{ctx.salutation}Voici le récapitulatif :\n{summary}\n\nConfirmez-vous ?"

    return apply_corrections(state, {
        "final_response": text_output,
        "ag_ui_component": {
            "lc_type": "constructor",
            "id": ["ag_ui", "FormConfirmation"],
            "kwargs": {
                "title": "Confirmation requise",
                "summary": summary,
                "submit_label": "Confirmer",
                "cancel_label": "Annuler",
                "metadata": {"goal": ctx.goal},
            },
        },
    })
