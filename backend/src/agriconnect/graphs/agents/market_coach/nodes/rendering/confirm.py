"""Rendering — CONFIRMATION (récapitulatif + FormConfirmation)."""

from __future__ import annotations

from typing import Any, Dict, List

from agriconnect.graphs.agents.market_coach.nodes.rendering.common import (
    RenderContext,
    apply_corrections,
)


def _build_summary(goal: str, payload: Dict[str, Any], state: Dict[str, Any]) -> str:
    prod = payload.get("product")
    qty = payload.get("quantity")
    unit = payload.get("unit", "KG")
    price = payload.get("price")
    budget = payload.get("max_budget") or payload.get("target_price")
    zone = payload.get("zone") or state.get("zone")
    parts: List[str] = []

    if "PUBLISH" in goal or "SELL" in goal:
        parts.append(f"Vente de {prod or '—'}")
        if qty:
            parts.append(f"Quantité : {qty} {unit}")
        if price:
            parts.append(f"Prix : {price} FCFA")
    elif "AUCTION" in goal or "SEARCH" in goal or "BUY" in goal:
        parts.append(f"Achat de {prod or '—'}")
        if qty:
            parts.append(f"Quantité : {qty} {unit}")
        if budget or price:
            parts.append(f"Prix plafond : {budget or price} FCFA")
    elif "BID" in goal:
        parts.append("Soumission d'enchère")
        if price:
            parts.append(f"Proposition : {price} FCFA")
    else:
        for k, v in payload.items():
            if v and not k.endswith("_id") and k != "phone":
                parts.append(f"{k.replace('_', ' ').title()} : {v}")

    if zone:
        parts.append(f"Zone : {zone}")
    return "\n".join(parts) if parts else f"Opération : {goal}"


async def render_confirmation(ctx: RenderContext) -> Dict[str, Any]:
    state = ctx.state
    if not ctx.goal:
        return apply_corrections(state, {
            "final_response": f"{ctx.salutation}Que souhaitez-vous confirmer exactement ?",
            "ag_ui_component": None,
        })

    summary = state.get("confirmation_summary")
    if not summary and ctx.payload:
        summary = _build_summary(ctx.goal, ctx.payload, state)

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
