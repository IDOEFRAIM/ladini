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
    """Return the first INTENT_DISAMBIGUATION entry matching lexical hints for the active role."""
    active_role = (role_upper or "").upper().strip()
    for key, entry in INTENT_DISAMBIGUATION.items():
        hints = entry.get("lexical_hints") or []
        allowed_roles = entry.get("roles") or []
        if allowed_roles and active_role and active_role not in {r.upper() for r in allowed_roles}:
            continue
        for hint in hints:
            if hint and str(hint).lower() in text_lower:
                return {"id": key, **entry}
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

    entry = _detect_disambiguation_candidates(text_lower, role_upper)
    if not entry:
        return {}
    if confidence >= _DISAMBIGUATION_CONFIDENCE_THRESHOLD and len(entry.get("candidates") or []) < 2:
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
