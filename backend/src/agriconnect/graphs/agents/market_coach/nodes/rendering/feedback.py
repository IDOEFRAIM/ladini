"""Rendering — ERROR, RECOVERY, INTERRUPTION_HANDLER et fallback CLARIFICATION."""

from __future__ import annotations

from typing import Any, Dict

from agriconnect.graphs.agents.market_coach.interpreter.intent import INTENT_CONFIG
from agriconnect.graphs.agents.market_coach.nodes.rendering.common import (
    FIELD_BUSINESS_REASON,
    RenderContext,
    label_for_field,
    status_component,
)

_RECOVERY_MAX_RETRIES = 2


async def render_error(ctx: RenderContext) -> Dict[str, Any]:
    state = ctx.state
    reason_list = state.get("validation_errors") or []
    reason = reason_list[0] if reason_list else state.get("security_reason") or ""

    if state.get("security_status") == "SCAM_DETECTED":
        text_output = "⚠️ Alerte sécurité : ce message ne peut pas être traité."
        ui_reason = "Sécurité renforcée"
    elif state.get("security_status") == "WARNING":
        text_output = (
            "⚠️ Je ne peux pas finaliser cette action sans unité claire (KG, SAC, TONNE). "
            "Merci de préciser pour continuer."
        )
        ui_reason = "Unité manquante"
    elif reason:
        text_output = f"❌ Opération impossible : {reason}"
        ui_reason = str(reason)
    else:
        text_output = "❌ Une erreur technique est survenue. Réessayez dans un instant."
        ui_reason = "Erreur technique"

    goal_label = (INTENT_CONFIG.get(ctx.goal or "") or {}).get("label", "")
    if goal_label:
        text_output += "\n\nVous pouvez réessayer en reformulant ou dire \"annuler\"."

    return {
        "final_response": text_output,
        "ag_ui_component": status_component("error", reason=ui_reason),
    }


async def render_recovery(ctx: RenderContext) -> Dict[str, Any]:
    state, goal = ctx.state, ctx.goal
    retry_count = int(state.get("retry_count") or 0)
    retry_next = min(retry_count + 1, _RECOVERY_MAX_RETRIES)

    field = state.get("last_missing_field") or ((state.get("missing_fields") or [None])[0])
    if not field:
        form_step = state.get("form_step")
        if form_step not in (None, "", "CONFIRMING", "COMPLETE"):
            field = form_step
    if not field:
        expected_input = str(state.get("expected_input") or "").strip().lower()
        if expected_input and expected_input not in {"none", "confirmation", "selection"}:
            field = expected_input

    label = label_for_field(goal or "", field)
    goal_label = (
        (INTENT_CONFIG.get(goal or "") or {}).get("label", (goal or "").replace("_", " ").lower())
        if goal else "votre opération"
    )
    clean_field = str(field).replace("_mentioned", "").replace("_for_sale", "") if field else ""
    business_reason = FIELD_BUSINESS_REASON.get(field or "", "") or FIELD_BUSINESS_REASON.get(clean_field, "")

    if retry_count >= _RECOVERY_MAX_RETRIES:
        text_output = (
            f"{ctx.salutation}Pas de souci ! L'opération \"{goal_label}\" est mise en pause. "
            "Vous pourrez la reprendre à tout moment. Que puis-je faire d'autre pour vous ?"
        )
    else:
        reason_part = f" ({business_reason})" if business_reason else ""
        text_output = (
            f"🔄 {ctx.salutation}On continue : {goal_label}.\n"
            f"J'ai juste besoin de {label}{reason_part}.\n"
            f"Exemple : tapez simplement la valeur, ou dites « annuler » si vous changez d'avis."
        )
    return {
        "final_response": text_output,
        "retry_count": retry_next,
        "ag_ui_component": status_component("warning", message=text_output),
    }


async def render_interruption(ctx: RenderContext) -> Dict[str, Any]:
    state = ctx.state
    detected_intent = str(state.get("detected_intent") or "").upper().strip()
    if detected_intent == "BUYER_PREORDER_INIT":
        return {
            "final_response": "Je bascule vers la confirmation de votre précommande. Le récapitulatif arrive.",
            "ag_ui_component": None,
        }
    suspended = str(state.get("suspended_goal") or "").upper().strip()
    current_goal = str(state.get("current_goal") or "").upper().strip()
    expected = str(state.get("expected_input") or "").upper().strip()

    suspended_label = (INTENT_CONFIG.get(suspended) or {}).get("label") if suspended else ""
    current_label = (INTENT_CONFIG.get(current_goal) or {}).get("label") if current_goal else ""

    expected_hint = ""
    if expected == "SELECTION":
        expected_hint = "un numéro du menu (ex: 1)"
    elif expected == "CONFIRMATION":
        expected_hint = "oui / non"
    elif expected and expected not in {"NONE", ""}:
        expected_hint = expected.lower()

    head = "Je traite votre nouvelle demande."
    if current_label:
        head = f"Je passe à : *{current_label}*."

    tail = ""
    if suspended_label and expected_hint:
        tail = (
            f"\n\nPour reprendre ensuite *{suspended_label}*, j'attendais {expected_hint}. "
            "Vous pouvez aussi dire *annuler* si vous ne souhaitez plus continuer."
        )
    elif suspended_label:
        tail = (
            f"\n\nL'étape *{suspended_label}* est mise de côté. "
            "Dites *reprendre* pour revenir dessus, ou *annuler*."
        )

    return {"final_response": head + tail, "ag_ui_component": None}


async def render_clarification(ctx: RenderContext) -> Dict[str, Any]:
    """Fallback : coach proactif role-aware."""
    state, salutation = ctx.state, ctx.salutation
    user_role = str(state.get("user_role") or "PRODUCER").upper()
    turn = int(state.get("turn_count") or 0)

    if turn <= 1:
        if user_role == "BUYER":
            fallback_text = (
                f"👋 {salutation}Bienvenue ! Je suis votre assistant d'achat AgriConnect.\n"
                "Je peux vous aider à :\n"
                "• Trouver des produits agricoles\n"
                "• Lancer un appel d'offres\n"
                "• Suivre vos commandes\n\n"
                "Dites-moi ce que vous cherchez !"
            )
        else:
            fallback_text = (
                f"👋 {salutation}Bienvenue ! Je suis votre coach commercial AgriConnect.\n"
                "Je peux vous aider à :\n"
                "• Mettre vos produits en vente\n"
                "• Gérer votre stock\n"
                "• Répondre aux demandes d'acheteurs\n\n"
                "Que souhaitez-vous faire ?"
            )
    else:
        if user_role == "BUYER":
            examples = "chercher un produit, lancer un appel d'offres, ou voir vos commandes"
        else:
            examples = "vendre un produit, gérer votre stock, ou répondre à une enchère"
        fallback_text = (
            f"{salutation}Je n'ai pas bien saisi. Vous pouvez par exemple {examples}. "
            "Dites-moi en quelques mots ce dont vous avez besoin."
        )

    return {"final_response": fallback_text, "ag_ui_component": None}
