"""Rendering — CONFIRMATION (récapitulatif + QuickReplies)."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from ladini.graphs.agents.market_coach.core.goals import (
    BUYER_PREORDER_GOALS as _BUYER_PREORDER_GOALS,
)
from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    check_invariants,
    get_pending_interaction,
)
from ladini.graphs.agents.market_coach.domain.preorder_draft import PreorderDraft
from ladini.graphs.agents.market_coach.domain.procurement_draft import (
    ProcurementDraft,
    ProcurementDraftStatus,
)
from ladini.graphs.agents.market_coach.domain.recurring_need_draft import (
    RecurringNeedDraft,
    RecurringNeedDraftStatus,
)
from ladini.graphs.agents.market_coach.domain.sales_publish_draft import (
    SalesPublishDraft,
    SalesPublishDraftStatus,
)
from ladini.graphs.agents.market_coach.nodes.rendering.common import (
    RenderContext,
    apply_corrections,
)
from ladini.graphs.agents.market_coach.services.ui.confirmation_summary import (
    build_confirmation_summary,
)

logger = logging.getLogger("Ladini.Market.Rendering.Confirm")


#: Marqueur `working_memory[...]` : `(draft_id, version)` du besoin récurrent dont la confirmation interactive a déjà été
#: montrée, et le message entrant qui l'a émise (distingue un retry du MÊME message — qui ré-émet, l'envoi étant
#: dédupliqué par `ResponseDispatcher` — d'un message DIFFÉRENT). Hors du draft : le flow réécrit le draft à chaque tour.
RECURRING_CONFIRMATION_MARKER = "recurring_confirmation_emitted"
RECURRING_CONFIRMATION_REMINDER = (
    "Le récapitulatif est juste au-dessus. Répondez *Confirmer* pour l'enregistrer, *Annuler* pour l'abandonner, "
    "ou dites-moi ce qui doit changer."
)


def _recurring_confirmation_marker(state: Dict[str, Any], goal: Optional[str]) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """B22 — UNE confirmation interactive par `(draft_id, version)` de besoin récurrent.

    Ce rendu est la couche qui produit réellement le message sortant (il remplace le texte du flow) ET il est atteint par
    tout message non compris pendant l'attente (`cognitive_guard` -> RECOVERY -> CONFIRMATION) : sans garde, chaque
    message qui ne change pas le draft régénère le MÊME récapitulatif. Retourne `(déjà_montrée, marqueur)` : `marqueur`
    est celui à écrire dans `working_memory` quand on émet pour de bon, `None` si ce n'est pas un draft récurrent en
    cours d'édition."""
    if goal != "CREATE_RECURRING_NEED":
        return False, None
    draft = RecurringNeedDraft.from_dict(state.get("recurring_need_draft"))
    if draft is None or draft.status != RecurringNeedDraftStatus.DRAFT:
        return False, None
    sid = state.get("message_sid")
    previous = (state.get("working_memory") or {}).get(RECURRING_CONFIRMATION_MARKER) or {}
    already = previous.get("draft_id") == draft.draft_id and previous.get("version") == draft.version
    retry_of_same_message = sid is not None and previous.get("message_sid") == sid
    marker = {"draft_id": draft.draft_id, "version": draft.version, "message_sid": sid}
    if already and not retry_of_same_message:
        return True, previous
    return False, marker


def _with_marker(result: Dict[str, Any], marker: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if marker is not None:
        result["working_memory"] = {**(result.get("working_memory") or {}), RECURRING_CONFIRMATION_MARKER: marker}
    return result


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

    # (2026-09-14, incident WhatsApp #12) : logguer la violation puis
    # continuer quand même produisait un récapitulatif littéralement "None"
    # ("Voici le récapitulatif :\nNone\n\nConfirmez-vous ?") — observé en
    # prod sur un `pending_interaction=CONFIRM_ACTION` ORPHELIN (fuite d'un
    # tunnel `BUYER_LIST_ORDERS` abandonné des jours plus tôt, jamais nettoyé)
    # qui interceptait via le fast-path déterministe une réponse "confirmer"
    # sans AUCUN rapport (une notification producteur toute nouvelle). Sans
    # `confirmation_summary` NI `transaction_payload`, il n'y a structurellement
    # RIEN à confirmer — ce cas doit dégrader EXACTEMENT comme `not ctx.goal`
    # ci-dessous, jamais tenter un rendu qui n'a aucune donnée à afficher.
    # EXCLUT `_BUYER_PREORDER_GOALS` : ce flux tire légitimement son récap de
    # `PreorderDraft` (voir plus bas), jamais de `confirmation_summary`/
    # `transaction_payload` — l'invariant ci-dessus ne le sait pas et le
    # signalerait à tort à CHAQUE précommande.
    if (
        not ctx.goal
        or get_pending_interaction(state).kind != InteractionKind.CONFIRM_ACTION
        or (violations and ctx.goal not in _BUYER_PREORDER_GOALS)
    ):
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

    already_shown, recurring_marker = _recurring_confirmation_marker(state, ctx.goal)
    if already_shown:
        logger.info("RECURRING_CONFIRMATION_DEDUPED")
        return _with_marker(apply_corrections(
            state,
            {
                "final_response": RECURRING_CONFIRMATION_REMINDER,
                # Ni CONFIRMATION ni WAITING_CONFIRMATION : `Orchestrator._interactive_hint` y verrait une demande de
                # boutons et enverrait une SECONDE confirmation interactive. Le pending CONFIRM_ACTION reste intact.
                "response_strategy": "CLARIFICATION",
                "status": "WAITING_INPUT",
                "ag_ui_component": None,
            },
        ), recurring_marker)

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
    # PRÉCOMMANDE : le récapitulatif canonique vit dans `PreorderDraft`
    # (items, paliers, total, multi-producteur), JAMAIS dans
    # `transaction_payload` — `build_confirmation_summary` n'a d'ailleurs
    # aucun gabarit `BUYER_PREORDER_*` et retomberait sur le fallback
    # "Confirmez-vous cette opération ?" (récap vide). Incident réel
    # (2026-09-09) : "je veux 50 kg" → panier → "okay" affichait
    # "Voici le récapitulatif :\nConfirmez-vous cette opération ?\n\nConfirmez-vous ?"
    # — aucun détail produit/quantité/prix. On rend donc `draft.render_summary()`
    # (même projection PURE que le flux `apply_response_plan`), sans le
    # préfixe "Voici le récapitulatif" (render_summary porte déjà son propre
    # en-tête "📋 *Récapitulatif de votre précommande :*").
    text_output: str
    _preorder_draft = None
    _sales_draft = None
    _procurement_draft = None
    if ctx.goal in _BUYER_PREORDER_GOALS:
        _preorder_draft = PreorderDraft.from_dict(state.get("preorder_draft"))
    elif ctx.goal == "SALES_PUBLISH_PRODUCT":
        _sales_draft = SalesPublishDraft.from_dict(state.get("sales_publish_draft"))
    elif ctx.goal == "PROCUREMENT_CREATE_REQUEST":
        _procurement_draft = ProcurementDraft.from_dict(state.get("procurement_draft"))
    if _preorder_draft is not None:
        text_output = f"{ctx.salutation}{_preorder_draft.render_summary()}\n\nConfirmez-vous ?"
    elif _sales_draft is not None and _sales_draft.status == SalesPublishDraftStatus.DRAFT:
        # Phase B1 (mandat étape 12) : la confirmation d'une publication est une projection PURE
        # du draft certifié — JAMAIS reconstruite depuis `transaction_payload`. Reconstruire le
        # récap depuis le payload brut (`build_confirmation_summary`) affichait « 500 FCFA/SAC »
        # puis « le prix sera appliqué par LITRE » alors que le draft portait tout autre chose.
        text_output = (
            f"{ctx.salutation}Voici le récapitulatif :\n{_sales_draft.render_summary()}"
            "\n\nConfirmez-vous ?"
        )
    elif _procurement_draft is not None and _procurement_draft.status == ProcurementDraftStatus.DRAFT:
        # Mandat B2c.5 : même garde-fou que SALES_PUBLISH_PRODUCT ci-dessus — un prix plafond
        # TOTAL_LOT/budget ne doit jamais retomber sur `build_confirmation_summary` (payload brut,
        # aucune notion de base certifiée) dans ce chemin de secours.
        text_output = (
            f"{ctx.salutation}Voici le récapitulatif :\n{_procurement_draft.render_summary()}"
            "\n\nConfirmez-vous ?"
        )
    else:
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

    if recurring_marker is not None:
        logger.info("RECURRING_CONFIRMATION_EMITTED")
    return _with_marker(apply_corrections(
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
    ), recurring_marker)
