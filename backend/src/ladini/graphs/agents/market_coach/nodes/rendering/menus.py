"""Rendering — SELECTION_MENU (menus numérotés AG-UI)."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from ladini.graphs.agents.market_coach.nodes.rendering.common import (
    RenderContext,
    apply_corrections,
    indexed_options,
    list_menu_component,
)
from ladini.graphs.agents.market_coach.utils import llm_deviation_reply

logger = logging.getLogger("Ladini.Market.Rendering")


async def render_selection_menu(ctx: RenderContext) -> Dict[str, Any]:
    state = ctx.state
    # Réutiliser un final_response pré-calculé (cart, negotiation, tracking...).
    if state.get("final_response"):
        return apply_corrections(
            state,
            {
                "final_response": state.get("final_response"),
                "ag_ui_component": state.get("ag_ui_component"),
            },
        )
    if not ctx.goal:
        return apply_corrections(
            state,
            {
                "final_response": f"{ctx.salutation}Que souhaitez-vous choisir ?",
                "ag_ui_component": None,
            },
        )

    wm = state.get("working_memory") or {}
    preformatted = (
        wm.get("auction_menu")
        or wm.get("bids_menu")
        or wm.get("stocks_menu")
        or wm.get("generic_menu")
    )
    candidates: List[str] = state.get("expected_candidates") or []

    # Invariant (B3) : un menu numéroté a TOUJOURS au moins une option visible. Sans menu
    # préformaté ni candidat, il n'y a rien à choisir — jamais « Répondez par le numéro » dans le
    # vide : on re-pose la question en attente (récupération) plutôt que d'inventer un menu.
    if not preformatted and not candidates:
        from ladini.graphs.agents.market_coach.nodes.rendering.feedback import (
            render_recovery,
        )

        logger.warning(
            "BUYER_SELECTION_DECISION candidate_count=0 decision=EMPTY_MENU_SUPPRESSED goal=%s",
            ctx.goal,
        )
        recovered: Dict[str, Any] = apply_corrections(state, await render_recovery(ctx))
        return recovered

    if preformatted:
        text_output = str(preformatted)
    else:
        base_menu = "\n".join(f"{i}. {c}" for i, c in enumerate(candidates, start=1))
        text_output = "Veuillez choisir une option :\n" + base_menu
        if "répondez" not in text_output.lower():
            text_output += (
                "\n\n👉 Répondez uniquement par le numéro de votre choix (ex: '2')."
            )

    # Un écart (question, remarque) au lieu d'un numéro clair — reconnaître
    # ce qui a été dit avant de rejouer le même menu, même principe que
    # ask.py/feedback.py. Voir [[precommande-architecture-consolidation-2026-08]].
    event = str(state.get("interpreted_event") or "").upper()
    user_text = str(
        state.get("normalized_text") or state.get("user_query") or ""
    ).strip()
    # (2026-09-14, incident WhatsApp #9) : `interruption_unresolved` (posé par
    # `interpreter/routing.py` — voir son commentaire) signifie que la route
    # SELECTION a DÉJÀ jugé ce message sans rapport avec le menu affiché, et
    # que la reclassification qui a suivi n'a pas pu identifier d'intention
    # métier. Générer ici une note du type "je comprends que vous voulez X"
    # laisserait croire que le menu ci-dessous (potentiellement un tout autre
    # tunnel périmé) répond à ce message — observé en prod : "confirmer" sur
    # une vente producteur non résolu, note "vous voulez confirmer..." collée
    # à un menu ACHETEUR sans rapport (commande déjà confirmée).
    if (
        event in {"UNKNOWN", "OUT_OF_SCOPE"}
        and user_text
        and not state.get("interruption_unresolved")
    ):
        note = await llm_deviation_reply(
            ctx.mc_runtime, user_text, "choisir une option dans le menu ci-dessous"
        )
        if note:
            text_output = f"{note}\n\n{text_output}"

    return apply_corrections(
        state,
        {
            "final_response": text_output,
            "ag_ui_component": list_menu_component(
                "Sélection",
                indexed_options(candidates),
                metadata={"goal": ctx.goal},
            ),
        },
    )
