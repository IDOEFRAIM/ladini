"""Rendering — ERROR, RECOVERY, INTERRUPTION_HANDLER et fallback CLARIFICATION."""

from __future__ import annotations

from typing import Any, Dict

from ladini.graphs.agents.market_coach.core.pending_interaction import (
    get_pending_interaction,
    to_tunnel_category,
)
from ladini.graphs.agents.market_coach.interpreter.intent import INTENT_CONFIG
from ladini.graphs.agents.market_coach.nodes.rendering.common import (
    FIELD_BUSINESS_REASON,
    RenderContext,
    label_for_field,
    status_component,
)
from ladini.graphs.agents.market_coach.utils import llm_deviation_reply

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

    # (2026-09-11) Ne JAMAIS promettre "dire annuler" ici — `render_error`
    # ne s'exécute QUE sur `status=="ERROR"`, et `post_response_cleanup`
    # (nodes/cleanup.py, dernier nœud du graphe, tourne juste après) efface
    # INCONDITIONNELLEMENT `current_goal` dès que `status=="ERROR"` (aucune
    # des trois conditions `keep_*_channel` n'accepte ce statut). Le goal
    # n'existe donc déjà plus au moment où l'utilisateur pourrait agir sur
    # cette suggestion : "annuler" au tour suivant tombe sur `current_goal
    # =None`, n'a rien à annuler, et retombe sur le fallback générique "je
    # n'ai pas compris" — un aller-retour confus signalé par un utilisateur
    # réel qui suivait pourtant exactement l'instruction donnée.
    goal_label = (INTENT_CONFIG.get(ctx.goal or "") or {}).get("label", "")
    if goal_label:
        text_output += "\n\nVous pouvez reformuler votre demande."

    return {
        "final_response": text_output,
        "ag_ui_component": status_component("error", reason=ui_reason),
    }


async def render_recovery(ctx: RenderContext) -> Dict[str, Any]:
    state, goal = ctx.state, ctx.goal
    retry_count = int(state.get("retry_count") or 0)
    retry_next = min(retry_count + 1, _RECOVERY_MAX_RETRIES)

    field = (
        state.get("last_missing_field") or ((state.get("missing_fields") or [None])[0])
    )
    if not field:
        form_step = state.get("form_step")
        if form_step not in (None, "", "CONFIRMING", "COMPLETE"):
            field = form_step
    if not field:
        expected_input = to_tunnel_category(get_pending_interaction(state)).strip().lower()
        if expected_input and expected_input not in {
            "none",
            "confirmation",
            "selection",
        }:
            field = expected_input

    label = label_for_field(goal or "", field)
    goal_label = (
        (INTENT_CONFIG.get(goal or "") or {}).get(
            "label", (goal or "").replace("_", " ").lower()
        )
        if goal
        else "votre opération"
    )
    clean_field = (
        str(field).replace("_mentioned", "").replace("_for_sale", "") if field else ""
    )
    business_reason = FIELD_BUSINESS_REASON.get(
        field or "", ""
    ) or FIELD_BUSINESS_REASON.get(clean_field, "")

    if retry_count >= _RECOVERY_MAX_RETRIES:
        text_output = (
            f'{ctx.salutation}Pas de souci ! L\'opération "{goal_label}" est mise en pause. '
            "Vous pourrez la reprendre à tout moment. Que puis-je faire d'autre pour vous ?"
        )
    else:
        reason_part = f" ({business_reason})" if business_reason else ""
        text_output = (
            f"🔄 {ctx.salutation}On continue : {goal_label}.\n"
            f"J'ai juste besoin de {label}{reason_part}.\n"
            f"Exemple : tapez simplement la valeur, ou dites « annuler » si vous changez d'avis."
        )
        # Ce nœud rend TOUT écart classé UNKNOWN pendant un tunnel actif, quel
        # que soit le goal (cognitive_guard::recover_active_tunnel) — même
        # défaut d'adaptivité que render_ask_missing_field.py, corrigé de la
        # même façon : reconnaître ce que l'utilisateur a dit avant de
        # rejouer la question, plutôt que le même texte figé en boucle. Voir
        # [[precommande-architecture-consolidation-2026-08]].
        user_text = str(
            state.get("normalized_text") or state.get("user_query") or ""
        ).strip()
        note = await llm_deviation_reply(
            ctx.mc_runtime, user_text, f"répondre à : {label}"
        )
        if note:
            text_output = f"{note}\n\n{text_output}"
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
    expected = to_tunnel_category(get_pending_interaction(state))

    suspended_label = (
        (INTENT_CONFIG.get(suspended) or {}).get("label") if suspended else ""
    )
    current_label = (
        (INTENT_CONFIG.get(current_goal) or {}).get("label") if current_goal else ""
    )

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
    """Fallback : coach proactif — refonte double-rôle (vendre ET acheter).

    Bug réel, systémique (2026-08-14) : `nodes/clarification.py` fait déjà
    un appel LLM pour générer une réponse CONTEXTUELLE (`needs_clarification`)
    et pose `final_response` + `response_strategy="CLARIFICATION"` quand il
    réussit. Mais CE renderer — le seul de tout `nodes/rendering/` à ne PAS
    réutiliser un `final_response` précalculé (voir `ask.py`, `menus.py`,
    `confirm.py`) — recalculait TOUJOURS son propre texte générique depuis
    zéro, écrasant silencieusement la réponse LLM à chaque fois. C'est
    `render_clarification` qui est choisi par défaut pour TOUTE stratégie
    non reconnue (`response_handlers.py::_select_handler`), y compris
    littéralement "CLARIFICATION" (absente des deux dictionnaires de
    routage) — donc ce bug touchait 100% des tours passant par
    `clarification_node`, expliquant l'omniprésence du même texte figé
    "Je n'ai pas bien saisi..." à travers des dizaines de messages
    utilisateur pourtant très différents. Voir
    [[precommande-architecture-consolidation-2026-08]].
    """
    state, salutation = ctx.state, ctx.salutation
    precomputed = state.get("final_response")
    if precomputed:
        return {
            "final_response": precomputed,
            "ag_ui_component": state.get("ag_ui_component"),
        }
    turn = int(state.get("turn_count") or 0)

    if turn <= 1:
        fallback_text = (
            f"👋 {salutation}Bienvenue ! Je suis votre assistant Ladini.\n"
            "Je peux vous aider à :\n"
            "• Mettre vos produits en vente / gérer votre stock\n"
            "• Trouver des produits agricoles / lancer un appel d'offres\n"
            "• Suivre vos commandes ou celles reçues\n\n"
            "Que souhaitez-vous faire ?"
        )
    else:
        # Un goal qui vient de se terminer en ERROR/FAILED laisse une trace
        # d'un tour (nodes/cleaner.py::state_cleaner_node) — si l'utilisateur
        # répond ensuite par quelque chose d'incompréhensible (ex: "annuler"
        # après un "Produit inconnu"), le fallback doit le reconnaître au
        # lieu de faire comme si la conversation venait de commencer. Voir
        # [[market-coach-turn-boundary-state]].
        terminated_goal = str(state.get("last_terminated_goal") or "").upper().strip()
        terminated_label = (
            (INTENT_CONFIG.get(terminated_goal) or {}).get("label")
            if terminated_goal
            else ""
        )

        if terminated_label:
            fallback_text = (
                f"{salutation}Pas de souci, on reprend : *{terminated_label}* n'a pas abouti. "
                "Vous pouvez réessayer avec d'autres informations, ou dites-moi ce que vous "
                "voulez faire à la place."
            )
        else:
            examples = (
                "vendre un produit, gérer votre stock, chercher un produit, "
                "lancer un appel d'offres, ou suivre une commande"
            )
            fallback_text = (
                f"{salutation}Je n'ai pas bien saisi. Vous pouvez par exemple {examples}. "
                "Dites-moi en quelques mots ce dont vous avez besoin."
            )

    patch: Dict[str, Any] = {"final_response": fallback_text, "ag_ui_component": None}
    if state.get("last_terminated_goal"):
        patch["last_terminated_goal"] = None  # hint consommée en un coup
    return patch
