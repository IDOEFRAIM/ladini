"""Rendering — ONBOARDING et ASK_MISSING_FIELD (collecte d'information)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from agriconnect.graphs.agents.market_coach.interpreter.intent import INTENT_CONFIG
from agriconnect.graphs.agents.market_coach.nodes.rendering.common import (
    FIELD_BUSINESS_REASON,
    RenderContext,
    apply_corrections,
    indexed_options,
    label_for_field,
    list_menu_component,
)

logger = logging.getLogger("AgriConnect.Market.Rendering")


async def render_onboarding(ctx: RenderContext) -> Dict[str, Any]:
    prompt = ctx.state.get("onboarding_prompt") or (
        f"{ctx.salutation}Bienvenue sur AgriConnect ! Quel est votre nom complet ?"
    )
    return apply_corrections(ctx.state, {
        "final_response": prompt,
        "ag_ui_component": ctx.state.get("ag_ui_component"),
    })


async def generate_llm_question(
    mc_runtime: Any, goal: str, field: str, label: str,
    payload: Dict[str, Any], state: Optional[Dict[str, Any]] = None,
) -> str:
    """Question coaching-style via LLM : POURQUOI + exemple + encouragement."""
    llm = getattr(mc_runtime, "llm", None)
    business_reason = FIELD_BUSINESS_REASON.get(field, "pour finaliser votre opération")
    fallback = f"J'ai besoin de connaître {label} {business_reason}. Indiquez-le moi."
    if llm is None:
        return fallback

    goal_label = (INTENT_CONFIG.get(goal) or {}).get("label", goal.replace("_", " ").lower())
    already_known = ", ".join(
        f"{k}={v}" for k, v in (payload or {}).items()
        if v not in (None, "", [], {}) and not k.endswith("_id") and k != "phone"
    ) or "rien"

    progress_ctx = ""
    is_last_field = False
    if state:
        progress = state.get("conversation_progress") or {}
        if progress:
            remaining = len(progress.get("remaining") or [])
            is_last_field = remaining <= 1
            progress_ctx = f" Étape {progress.get('filled', 0) + 1}/{progress.get('total', '?')}."
        user_name = state.get("user_name")
        if user_name:
            progress_ctx += f" Prénom utilisateur : {user_name}."

    last_hint = " C'est la DERNIÈRE info : dis qu'on y est presque." if is_last_field else ""

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
        completion = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: llm.chat.completions.create(
                    model=getattr(mc_runtime, "model_answer", "llama-3.3-70b-versatile"),
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.3,
                    max_tokens=90,
                )
            ),
            timeout=10.0,
        )
        result = (completion.choices[0].message.content or "").strip()
        return result if result else fallback
    except asyncio.TimeoutError:
        logger.warning("RESPONSE_LLM_TIMEOUT | goal=%s | field=%s", goal, field)
        return fallback
    except Exception as exc:
        logger.warning("RESPONSE_LLM_ERROR | goal=%s | field=%s | error=%s", goal, field, exc)
        return fallback


async def render_ask_missing_field(ctx: RenderContext) -> Dict[str, Any]:
    state = ctx.state
    # Réutiliser un final_response pré-calculé en amont s'il existe.
    precomputed_ask = state.get("final_response")
    if precomputed_ask:
        return apply_corrections(state, {
            "final_response": precomputed_ask,
            "ag_ui_component": state.get("ag_ui_component"),
        })

    if not ctx.goal:
        return apply_corrections(state, {
            "final_response": f"{ctx.salutation}Que souhaitez-vous faire (vendre, acheter, stock, enchères) ?",
            "ag_ui_component": None,
        })

    missing = state.get("missing_fields") or []
    field = state.get("last_missing_field") or (missing[0] if missing else None)
    label = label_for_field(ctx.goal, field)
    candidates = state.get("expected_candidates") or []

    question = await generate_llm_question(
        ctx.mc_runtime, ctx.goal, field or "", label, ctx.payload, state=state,
    )
    progress = state.get("conversation_progress") or {}
    if progress:
        total = progress.get("total", 0)
        filled = progress.get("filled", 0)
        if total > 1:
            question = f"[{filled + 1}/{total}] {question}"

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

    return apply_corrections(state, {"final_response": question, "ag_ui_component": ag_component})
