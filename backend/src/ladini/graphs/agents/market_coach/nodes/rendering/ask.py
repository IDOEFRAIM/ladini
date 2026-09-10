"""Rendering — ONBOARDING et ASK_MISSING_FIELD (collecte d'information)."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from ladini.graphs.agents.market_coach.interpreter.intent import INTENT_CONFIG
from ladini.graphs.agents.market_coach.llm_gateway import (
    LLMGatewayExhausted,
    resolve_gateway,
    resolve_profile,
)
from ladini.graphs.agents.market_coach.nodes.rendering.common import (
    FIELD_BUSINESS_REASON,
    RenderContext,
    apply_corrections,
    indexed_options,
    label_for_field,
    list_menu_component,
)
from ladini.graphs.agents.market_coach.utils import llm_deviation_reply

logger = logging.getLogger("Ladini.Market.Rendering")


async def render_onboarding(ctx: RenderContext) -> Dict[str, Any]:
    prompt = ctx.state.get("onboarding_prompt") or (
        f"{ctx.salutation}Bienvenue sur Ladini ! Quel est votre nom complet ?"
    )
    return apply_corrections(
        ctx.state,
        {
            "final_response": prompt,
            "ag_ui_component": ctx.state.get("ag_ui_component"),
        },
    )


async def generate_llm_question(
    mc_runtime: Any,
    goal: str,
    field: str,
    label: str,
    payload: Dict[str, Any],
    state: Optional[Dict[str, Any]] = None,
) -> str:
    """Question coaching-style via LLM : POURQUOI + exemple + encouragement."""
    llm = getattr(mc_runtime, "llm", None)
    business_reason = FIELD_BUSINESS_REASON.get(field, "pour finaliser votre opération")
    fallback = f"J'ai besoin de connaître {label} {business_reason}. Indiquez-le moi."
    if llm is None:
        return fallback

    goal_label = (INTENT_CONFIG.get(goal) or {}).get(
        "label", goal.replace("_", " ").lower()
    )
    already_known = (
        ", ".join(
            f"{k}={v}"
            for k, v in (payload or {}).items()
            if v not in (None, "", [], {}) and not k.endswith("_id") and k != "phone"
        )
        or "rien"
    )

    progress_ctx = ""
    is_last_field = False
    if state:
        progress = state.get("conversation_progress") or {}
        if progress:
            remaining = len(progress.get("remaining") or [])
            is_last_field = remaining <= 1
            progress_ctx = (
                f" Étape {progress.get('filled', 0) + 1}/{progress.get('total', '?')}."
            )
        user_name = state.get("user_name")
        if user_name:
            progress_ctx += f" Prénom utilisateur : {user_name}."

    last_hint = (
        " C'est la DERNIÈRE info : dis qu'on y est presque." if is_last_field else ""
    )

    # Prompt compact et déterministe : ~70 tokens vs ~180 avant (Phase 3).
    prompt = (
        "Assistant WhatsApp agricole (Burkina Faso). Chaleureux, tutoiement, "
        "français simple, zéro jargon.\n"
        f"Opération : {goal_label}. Déjà connu : {already_known}.{progress_ctx}\n"
        f"Demande : {label} ({business_reason}).\n"
        "Réponds UNIQUEMENT par la question (1-2 phrases), avec un exemple "
        f"concret si utile (ex: 250 FCFA/kg, 5 sacs, Ouagadougou).{last_hint}"
    )
    try:
        # LLM Gateway (2026-09-02) : budget/repli/disjoncteur portés par le
        # Gateway — voir `llm_gateway/gateway.py`.
        completion = await resolve_gateway(mc_runtime).complete(
            profile=resolve_profile(mc_runtime),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=90,
            agent_node="generate_llm_question",
        )
        result = (completion.choices[0].message.content or "").strip()
        return result if result else fallback
    except LLMGatewayExhausted:
        logger.warning("RESPONSE_LLM_TIMEOUT | goal=%s | field=%s", goal, field)
        return fallback
    except Exception as exc:
        logger.warning(
            "RESPONSE_LLM_ERROR | goal=%s | field=%s | error=%s", goal, field, exc
        )
        return fallback


async def render_ask_missing_field(ctx: RenderContext) -> Dict[str, Any]:
    state = ctx.state
    # Réutiliser un final_response pré-calculé en amont s'il existe.
    precomputed_ask = state.get("final_response")
    if precomputed_ask:
        return apply_corrections(
            state,
            {
                "final_response": precomputed_ask,
                "ag_ui_component": state.get("ag_ui_component"),
            },
        )

    if not ctx.goal:
        return apply_corrections(
            state,
            {
                "final_response": f"{ctx.salutation}Que souhaitez-vous faire (vendre, acheter, stock, enchères) ?",
                "ag_ui_component": None,
            },
        )

    missing = state.get("missing_fields") or []
    field = state.get("last_missing_field") or (missing[0] if missing else None)
    label = label_for_field(ctx.goal, field)
    candidates = state.get("expected_candidates") or []

    question = await generate_llm_question(
        ctx.mc_runtime,
        ctx.goal,
        field or "",
        label,
        ctx.payload,
        state=state,
    )
    progress = state.get("conversation_progress") or {}
    if progress:
        total = progress.get("total", 0)
        filled = progress.get("filled", 0)
        if total > 1:
            question = f"[{filled + 1}/{total}] {question}"

    # L'utilisateur a dit quelque chose que l'interprète n'a pas classé comme
    # une réponse exploitable (question, hésitation, remarque du type "vous
    # me tiendrez informé ?") — SANS ce garde-fou, ce nœud générique (utilisé
    # par TOUS les goals à collecte de champs : appel d'offres, vente, stock,
    # cycle de culture...) rejoue juste la question suivante du formulaire,
    # ignorant complètement ce qui vient d'être dit. Même défaut
    # d'adaptivité déjà corrigé dans l'onboarding, `confirmation_gate` et la
    # précommande — corrigé ici au niveau le PLUS générique, pour couvrir
    # tous les goals d'un coup plutôt que d'attendre le prochain rapport de
    # bug par goal. Voir [[precommande-architecture-consolidation-2026-08]].
    event = str(state.get("interpreted_event") or "").upper()
    user_text = str(
        state.get("normalized_text") or state.get("user_query") or ""
    ).strip()
    if event in {"UNKNOWN", "OUT_OF_SCOPE"} and user_text:
        # Incident réel (2026-09-08) : "Merci pour l'information, vous avez
        # indiqué 4000 poulets 🙏" ... suivi immédiatement de "J'ai juste
        # besoin de quantité disponible" — l'agent ACCUSE RÉCEPTION d'une
        # donnée qu'il n'a jamais enregistrée, puis la redemande.
        #
        # Cause : `UNKNOWN` recouvre DEUX situations opposées.
        #   1. UNKNOWN sémantique — le LLM a tourné et n'a sincèrement pas su
        #      classer ("vous me tiendrez informé ?"). Une note adaptative est
        #      alors exactement ce qu'il faut : c'est le cas pour lequel ce
        #      bloc a été écrit.
        #   2. UNKNOWN technique — l'interpréteur était INDISPONIBLE (quota
        #      429 / tous candidats épuisés) et `routing.py` a FORCÉ UNKNOWN
        #      (`raw_analysis.path="llm_crash"` → `unknown_reason=
        #      TECHNICAL_FAILURE`). Le message n'a alors jamais été analysé :
        #      "4000" n'a JAMAIS été extrait ni stocké. Générer une note via
        #      un SECOND appel LLM sur le texte brut produit précisément le
        #      mensonge observé — la note, elle, voit bien "4000 poulets" et
        #      l'acquitte poliment, alors que le slot est resté vide.
        #
        # Distinction déjà matérialisée par `unknown_reason` et déjà
        # consommée par `clarification_node` pour la même raison (voir
        # docs/LLM_GATEWAY_FAILURE_RECOVERY_2026-09-05.md §3.4) — ce renderer
        # générique, lui, ne l'avait jamais reçue. On ne rappelle donc PAS la
        # Gateway qui vient d'échouer sur CE tour (appel réseau pur perte), et
        # on dit honnêtement que le message n'a pas pu être pris en compte
        # plutôt que de faire semblant.
        if str(state.get("unknown_reason") or "").upper() == "TECHNICAL_FAILURE":
            logger.info(
                "[AskRenderer] LLM indisponible ce tour "
                "(unknown_reason=TECHNICAL_FAILURE) — aucune note de déviation "
                "générée, avis honnête à la place"
            )
            question = (
                "🔧 Je n'ai pas pu analyser votre message (service "
                "momentanément indisponible) — il n'a donc pas été pris en "
                f"compte.\n\n{question}"
            )
        else:
            note = await llm_deviation_reply(
                ctx.mc_runtime, user_text, f"répondre à : {label}"
            )
            if note:
                question = f"{note}\n\n{question}"

    if candidates:
        ag_component = list_menu_component(
            f"Choisissez {label}",
            indexed_options(candidates),
            metadata={"field": field, "goal": ctx.goal},
        )
    else:
        ag_component = {
            "lc_type": "constructor",
            "id": ["ag_ui", "FormInputComponent"],
            "kwargs": {
                "title": f"Saisie : {label}",
                "field": field,
                "label": label,
                "placeholder": f"Entrez {label}...",
                "metadata": {"goal": ctx.goal},
            },
        }

    return apply_corrections(
        state, {"final_response": question, "ag_ui_component": ag_component}
    )
