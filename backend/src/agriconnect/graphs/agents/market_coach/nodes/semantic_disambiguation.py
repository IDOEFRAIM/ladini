from __future__ import annotations

from typing import Any, Dict, List, Optional

from agriconnect.graphs.agents.market_coach.core.base import get_node_logger
from agriconnect.graphs.agents.market_coach.utils import MarketRuntime, normalize_slot_keys
from agriconnect.graphs.agents.market_coach.interpreter.intent import (
    INTENT_CONFIG,
    INTENT_DISAMBIGUATION,
)

logger = get_node_logger("SemanticDisambiguationNode")

_DISAMBIGUATION_CONFIDENCE_THRESHOLD = 0.85


def _detect_disambiguation_candidates(text_lower: str, role_upper: str | None = None) -> Optional[Dict[str, Any]]:
    """Return the INTENT_DISAMBIGUATION entry with the MOST SPECIFIC matching hint.

    Picking the first dict entry with any substring match (declaration order)
    lets a short generic hint ("suivre") shadow a longer, far more specific
    hint declared later ("suivre mes appels d'offres") — e.g. "Suivre mes
    appels d'offre" matched ORDER_TRACKING_INTENT's generic "suivre" before
    ever reaching AUCTION_TRACKING_INTENT's exact phrase, sending the buyer
    to the wrong menu (commandes instead of enchères). Scoring by the
    longest matched hint across ALL entries makes specificity win regardless
    of declaration order.
    """
    # NOTE (refonte double-rôle) : `role_upper` n'est plus utilisé pour EXCLURE
    # des entrées — un même utilisateur peut déclencher un menu de
    # désambiguïsation producteur OU acheteur selon le texte, quel que soit
    # son rôle de session par défaut. Le paramètre est conservé pour compat
    # de signature (appelants existants) mais n'a plus d'effet filtrant.
    best_entry: Optional[Dict[str, Any]] = None
    best_key: Optional[str] = None
    best_len = 0
    for key, entry in INTENT_DISAMBIGUATION.items():
        hints = entry.get("lexical_hints") or []
        for hint in hints:
            hint_lower = str(hint or "").lower()
            if hint_lower and hint_lower in text_lower and len(hint_lower) > best_len:
                best_len = len(hint_lower)
                best_entry = entry
                best_key = key
    if best_entry is not None:
        return {"id": best_key, **best_entry}
    return None


async def semantic_disambiguation(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
) -> Dict[str, Any]:
    """Render a pedagogical AG-UI list when intent detection is ambiguous."""
    event = str(state.get("interpreted_event") or "").upper()
    confidence = float(state.get("interpreter_confidence") or 1.0)
    expected_input = str(state.get("expected_input") or "NONE").upper()
    text_lower = (state.get("normalized_text") or state.get("user_query") or "").lower()

    if event not in {"NEW_TASK", "UNKNOWN"}:
        return {}
    if expected_input != "NONE" or not text_lower:
        return {}

    role_upper = str(state.get("forced_role") or state.get("user_role") or "").upper().strip()

    # LE LLM DÉCIDE EN PREMIER. S'il a classé l'intention de façon SPÉCIFIQUE et
    # CONFIANTE, on ne lui superpose PAS un menu de désambiguïsation : il a déjà
    # répondu à la question que ce menu poserait.
    #
    # Le garde précédent était INOPÉRANT : `confidence >= seuil AND
    # len(candidates) < 2` — or TOUTE entrée de INTENT_DISAMBIGUATION a ≥ 2
    # candidats (c'est la définition d'un menu), donc la seconde condition était
    # toujours fausse et la sortie anticipée ne se déclenchait JAMAIS. Résultat :
    # des indices lexicaux FIGÉS et très larges ("j'ai", "combien", "statut",
    # "besoin de"…) détournaient systématiquement une classification LLM sûre
    # vers un menu — observé en prod avec `conf=0.90 trigger=
    # STOCK_OR_SALES_DECLARATION` : le producteur savait ce qu'il voulait, le
    # LLM l'avait compris, et on lui demandait quand même de choisir.
    #
    # On ne désambiguïse donc plus que dans le cas où c'est LÉGITIME : le LLM
    # n'a pas su trancher (UNKNOWN) ou n'est pas assez sûr de lui.
    detected_intent = str(state.get("detected_intent") or "").upper().strip()
    if detected_intent not in ("", "UNKNOWN") and confidence >= _DISAMBIGUATION_CONFIDENCE_THRESHOLD:
        logger.info(
            "[Disambiguation] SKIP — le LLM a tranché (intent=%s conf=%.2f ≥ %.2f)",
            detected_intent, confidence, _DISAMBIGUATION_CONFIDENCE_THRESHOLD,
        )
        return {}

    entry = _detect_disambiguation_candidates(text_lower, role_upper)
    if not entry:
        return {}

    options = entry.get("options") or []
    if len(options) < 2:
        return {}

    title = entry.get("title") or "Que souhaitez-vous faire exactement ?"
    pedagogical_intro = entry.get("pedagogical_hint") or ""
    mapping: Dict[str, str] = {}
    labels: List[str] = []
    lines = [f"🤔 *{title}*"]
    if pedagogical_intro:
        lines.append(f"\n{pedagogical_intro}\n")

    for i, opt in enumerate(options, start=1):
        if isinstance(opt, (tuple, list)) and len(opt) >= 2:
            intent_key, label = opt[0], opt[1]
        elif isinstance(opt, dict):
            intent_key, label = opt.get("intent"), opt.get("label")
        else:
            continue
        if not intent_key:
            continue
        mapping[str(i)] = str(intent_key)
        labels.append(str(label or intent_key))
        intent_label = (INTENT_CONFIG.get(str(intent_key)) or {}).get("label", "")
        if intent_label and intent_label != label:
            lines.append(f"{i}. *{label or intent_key}* — {intent_label}")
        else:
            lines.append(f"{i}. {label or intent_key}")

    if len(mapping) < 2:
        return {}

    lines.append("\nRépondez simplement par le numéro de votre choix.")
    logger.info(
        "[Disambiguation] event=%s conf=%.2f trigger=%s candidates=%d",
        event,
        confidence,
        entry.get("id"),
        len(mapping),
    )

    # Stash any entities already extracted from the triggering utterance
    # (e.g. "J'ai 958 kg de tomates à 375 FCFA") into transaction_payload so
    # they survive the disambiguation turn. Without this the menu short-circuits
    # before memory_update promotes them, post_response_cleanup wipes
    # extracted_entities, and the chosen tunnel restarts its form from scratch.
    stashed_payload = dict(state.get("transaction_payload") or {})
    extracted = normalize_slot_keys(dict(state.get("extracted_entities") or {}))
    for key, value in extracted.items():
        if value in (None, "", [], {}):
            continue
        stashed_payload.setdefault(key, value)

    return {
        "status": "WAITING_INPUT",
        "current_goal": "DISAMBIGUATION_PENDING",
        "goal_status": "WAITING_INPUT",
        "expected_input": "SELECTION",
        "expected_candidates": labels,
        "available_mapping": mapping,
        "transaction_payload": stashed_payload,
        "working_memory": {
            **(state.get("working_memory") or {}),
            "available_mapping_kind": "intent_disambiguation",
            "disambiguation_pending": True,
            "disambiguation_trigger_id": entry.get("id"),
        },
        "response_strategy": "SELECTION_MENU",
        "final_response": "\n".join(lines),
        "ag_ui_component": {
            "lc_type": "constructor",
            "id": ["ag_ui", "ListMenu"],
            "kwargs": {
                "title": title,
                "options": [
                    {"index": str(i), "label": lbl}
                    for i, lbl in enumerate(labels, start=1)
                ],
                "metadata": {"kind": "intent_disambiguation"},
            },
        },
    }


__all__ = [
    "semantic_disambiguation",
    "_detect_disambiguation_candidates",
    "_DISAMBIGUATION_CONFIDENCE_THRESHOLD",
]
