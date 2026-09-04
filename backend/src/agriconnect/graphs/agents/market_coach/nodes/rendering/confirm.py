"""Rendering — CONFIRMATION (récapitulatif + QuickReplies)."""

from __future__ import annotations

import logging
from typing import Any, Dict

from agriconnect.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    check_invariants,
    get_pending_interaction,
)
from agriconnect.graphs.agents.market_coach.nodes.rendering.common import (
    RenderContext,
    apply_corrections,
)
from agriconnect.graphs.agents.market_coach.services.ui.confirmation_summary import (
    build_confirmation_summary,
)

logger = logging.getLogger("AgriConnect.Market.Rendering.Confirm")


async def render_confirmation(ctx: RenderContext) -> Dict[str, Any]:
    state = ctx.state

    # (2026-09-02, garde-fou Invariant 2) : avec le fix de routage
    # (core/router.py::_cart_guard + interpreter/strategy.py, voir leurs
    # docstrings), ce rendu ne devrait plus jamais être atteint pendant un
    # tunnel panier actif ni sans contexte de confirmation cohérent. On logue
    # les violations plutôt que de lever — un rendu dégradé explicite reste
    # préférable à un crash de tour, mais cette ligne rend le cas observable
    # (Langfuse/logs) au lieu de disparaître silencieusement.
    violations = check_invariants(state)
    if violations:
        logger.warning(
            "[render_confirmation] invariants violés : %s (goal=%s)",
            violations,
            ctx.goal,
        )

    if not ctx.goal or get_pending_interaction(state).kind != InteractionKind.CONFIRM_ACTION:
        return apply_corrections(
            state,
            {
                "final_response": (
                    f"{ctx.salutation}Je n'ai plus de récapitulatif en attente de "
                    "votre part. Dites-moi ce que vous voulez faire."
                ),
                "response_strategy": "CLARIFICATION",
                "ag_ui_component": None,
            },
        )

    # `confirmation_summary` est normalement posé par `confirmation_gate`
    # (source unique du récap). Le repli ci-dessous ne sert que si un chemin
    # atteint ce rendu sans passer par la gate ; il délègue au MÊME
    # constructeur (`build_confirmation_summary`) au lieu d'un format maison
    # divergent — l'ancien `_build_summary` local produisait un récap
    # différent ("Vente de X\nQuantité : Y") qui contournait toute la logique
    # d'unités (prix par KG vs quantité en tonnes, avertissement de mismatch).
    #
    # Incident réel (2026-08-27) : un acheteur commandait des chèvres, mais
    # la confirmation affichée parlait de "35 KG de champignons" — un résumé
    # PÉRIMÉ d'une tentative abandonnée bien plus tôt dans la conversation.
    # Certains chemins (event=UNKNOWN forcé par un échec LLM, avec
    # expected_input déjà à CONFIRMATION — voir interpreter/strategy.py)
    # atteignent ce rendu SANS repasser par `confirmation_gate`, donc sans
    # jamais régénérer `confirmation_summary` pour la transaction courante.
    # Le GOAL seul ne suffit PAS à détecter la péremption (chèvres et
    # champignons relevaient tous deux de `BUYER_PREORDER_INIT`) : on compare
    # aussi le PAYLOAD complet ayant servi à construire le résumé. Le moindre
    # écart (produit, quantité, prix...) invalide le résumé et force une
    # reconstruction à la volée depuis le payload courant. Une comparaison
    # trop stricte ne fait au pire que reconstruire inutilement un résumé
    # déjà correct (même contenu) — jamais afficher un résumé faux.
    summary = state.get("confirmation_summary")
    stale = (
        state.get("confirmation_summary_goal") != ctx.goal
        or state.get("confirmation_summary_payload") != ctx.payload
    )
    if summary and stale:
        summary = None
    if not summary and ctx.payload:
        summary = build_confirmation_summary(ctx.goal, ctx.payload)

    text_output = (
        f"{ctx.salutation}Voici le récapitulatif :\n{summary}\n\nConfirmez-vous ?"
    )

    # Un écart (question/correction/remarque) pendant l'attente de
    # confirmation a produit une réponse adaptée générée par le LLM
    # (`confirmation_gate.py::_llm_deviation_reply`) — on la place AVANT le
    # récap plutôt que de simplement le répéter mot pour mot. Voir
    # [[onboarding-adaptive-questions-2026-08]] (même principe, appliqué ici
    # à l'étape de confirmation).
    deviation_note = state.get("confirmation_deviation_note")
    if deviation_note:
        text_output = f"{deviation_note}\n\n{text_output}"

    return apply_corrections(
        state,
        {
            "final_response": text_output,
            # QuickReplies (audit UX interactive 2026-08-27) : ce nœud tourne
            # APRÈS confirmation_gate.py dans le graphe (dispatcher
            # response_handlers.py, clé "CONFIRMATION") et ÉCRASE son
            # ag_ui_component — c'est donc CE build-ci qui atteint
            # réellement orchestrator.py::_interactive_hint. FormConfirmation
            # n'était lu par rien ; les id "CONFIRM"/"REJECT" correspondent
            # au bypass zéro-token (interpreter/routing.py).
            "ag_ui_component": {
                "lc_type": "constructor",
                "id": ["ag_ui", "QuickReplies"],
                "kwargs": {
                    "body": text_output,
                    "buttons": [
                        {"id": "CONFIRM", "title": "✅ Confirmer"},
                        {"id": "REJECT", "title": "❌ Annuler"},
                    ],
                    "metadata": {"goal": ctx.goal},
                },
            },
        },
    )
