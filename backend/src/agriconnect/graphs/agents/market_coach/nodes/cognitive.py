from __future__ import annotations

from typing import Any, Dict, List, Optional

from agriconnect.graphs.agents.market_coach.core.base import get_node_logger
from agriconnect.graphs.agents.market_coach.interpreter.intent import INTENT_CONFIG
from agriconnect.graphs.agents.market_coach.utils import MarketRuntime
from agriconnect.graphs.agents.market_coach.nodes.semantic_disambiguation import (
    _DISAMBIGUATION_CONFIDENCE_THRESHOLD,
    _detect_disambiguation_candidates,
)
from agriconnect.graphs.agents.market_coach.nodes.response_handlers import _label_for_field

logger = get_node_logger("CognitiveNode")

_PROGRESS_AUTO_FIELDS = frozenset({"farm_id", "phone"})


def _compute_progress(goal: Optional[str], payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not goal:
        return None
    config = INTENT_CONFIG.get(goal) or {}
    required = list(config.get("required") or [])
    if not required:
        return None
    user_fields = [f for f in required if f not in _PROGRESS_AUTO_FIELDS]
    if not user_fields:
        return None
    filled = [f for f in user_fields if payload.get(f) not in (None, "", [], {})]
    remaining = [f for f in user_fields if f not in filled]
    return {
        "total": len(user_fields),
        "filled": len(filled),
        "remaining": remaining,
        "pct": round(len(filled) / len(user_fields) * 100),
    }


def _build_proactive_hint(
    goal: Optional[str],
    progress: Optional[Dict[str, Any]],
    payload: Dict[str, Any],
) -> Optional[str]:
    if not goal or not progress:
        return None
    pct = progress.get("pct", 0)
    remaining = progress.get("remaining") or []
    goal_label = (INTENT_CONFIG.get(goal) or {}).get("label", goal)

    if pct == 0:
        return f"Nouvelle opération : {goal_label}"
    if pct >= 100:
        return "Toutes les informations sont réunies, prêt pour confirmation."
    if len(remaining) == 1:
        field_label = _label_for_field(goal, remaining[0])
        return f"Plus qu'une info : {field_label}."
    return f"Progression : {pct}% — encore {len(remaining)} infos nécessaires."


def _entity_carry_forward(
    state: Dict[str, Any],
    current_goal: Optional[str],
    in_tunnel: bool,
) -> Optional[Dict[str, Any]]:
    if not (in_tunnel and current_goal):
        return None
    stable = state.get("stable_entities") or {}
    entities = dict(state.get("extracted_entities") or {})
    carried = False
    for key in ("product", "unit", "zone_name"):
        if not entities.get(key) and stable.get(key):
            entities[key] = stable[key]
            carried = True
    return entities if carried else None


def _should_trigger_disambiguation(
    competition: List[Dict[str, Any]],
    confidence: float,
) -> bool:
    intents = {str(c.get("intent")) for c in competition if c.get("intent")}
    return len(intents) > 1 and confidence < _DISAMBIGUATION_CONFIDENCE_THRESHOLD


async def cognitive_guard(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
) -> Dict[str, Any]:
    event = str(state.get("interpreted_event") or "").upper()
    detected_intent = str(state.get("detected_intent") or "UNKNOWN").upper()
    confidence = float(state.get("interpreter_confidence") or 0.0)
    expected_input = str(state.get("expected_input") or "NONE").upper()
    current_goal = state.get("current_goal")
    payload = dict(state.get("transaction_payload") or {})
    text_lower = (state.get("normalized_text") or state.get("user_query") or "").lower()
    retry_count = int(state.get("retry_count") or 0)
    in_tunnel = bool(current_goal and expected_input not in {"NONE", ""})

    user_role = str(state.get("forced_role") or state.get("user_role") or "").upper()

    updates: Dict[str, Any] = {}
    decision: Dict[str, Any] = {
        "event": event,
        "intent": detected_intent,
        "confidence": confidence,
        "expected_input": expected_input,
        "in_tunnel": in_tunnel,
        "bounded": True,
    }
    competition: List[Dict[str, Any]] = []

    entry = _detect_disambiguation_candidates(text_lower, user_role)
    if entry:
        for intent_key in entry.get("candidates") or []:
            competition.append(
                {
                    "intent": intent_key,
                    "source": "lexical_disambiguation",
                    "trigger": entry.get("id"),
                }
            )
    if detected_intent != "UNKNOWN":
        competition.append(
            {
                "intent": detected_intent,
                "source": "llm_interpreter",
                "confidence": confidence,
            }
        )

    carried_entities = _entity_carry_forward(state, current_goal, in_tunnel)
    if carried_entities:
        updates["extracted_entities"] = carried_entities
        decision["entity_carry_forward"] = True

    if current_goal and event == "NEW_TASK" and detected_intent not in {"UNKNOWN", str(current_goal).upper()}:
        updates.update(
            {
                "interpreted_event": "INTERRUPTION",
                "intent_competition": competition,
                "cognitive_decision": {**decision, "action": "suspend_current_goal"},
            }
        )
        return updates

    if event == "UNKNOWN" and in_tunnel:
        if retry_count >= 2:
            logger.warning(
                "[CognitiveGuard] Max retries reached for goal=%s — abandoning tunnel",
                current_goal,
            )
            updates.update(
                {
                    "current_goal": None,
                    "goal_status": "IDLE",
                    "status": "WAITING_INPUT",
                    # merge_dict-reduced fields: a plain {} is a NO-OP under
                    # merge_dict (agents/reducers.py) — it PRESERVES the old
                    # value instead of clearing it. Only {"__reset__": True}
                    # actually empties the field. Writing plain {} here was
                    # the root cause of quantity/product/price from an
                    # abandoned goal silently surviving into the next goal's
                    # transaction_payload (current_goal was correctly reset
                    # to None, but the stale data underneath it was not).
                    "transaction_payload": {"__reset__": True},
                    "stable_entities": {"__reset__": True},
                    "missing_fields": [],
                    "completed_fields": [],
                    "last_missing_field": None,
                    "expected_input": "NONE",
                    "expected_candidates": [],
                    "available_mapping": {},
                    "retry_count": 0,
                    "waiting_for_confirmation": False,
                    "confirmation_summary": None,
                    "selected_tool": None,
                    "selected_tool_args": {"__reset__": True},
                    "execution_result": {"__reset__": True},
                    "ag_ui_component": None,
                    "response_strategy": "CLARIFICATION",
                    "intent_competition": competition,
                    "cognitive_decision": {**decision, "action": "abandon_tunnel_max_retries"},
                    "proactive_hint": "L'opération a été annulée. Dites-moi ce que vous souhaitez faire.",
                }
            )
            return updates

        updates.update(
            {
                "status": "WAITING_INPUT",
                "current_goal": current_goal,
                "goal_status": "WAITING_INPUT",
                "response_strategy": "RECOVERY",
                "intent_competition": competition,
                "cognitive_decision": {**decision, "action": "recover_active_tunnel", "retry": retry_count},
            }
        )
        return updates

    entities = state.get("extracted_entities") or {}
    entity_count = sum(1 for v in entities.values() if v not in (None, "", [], {}))
    if in_tunnel and entity_count >= 2 and event == "ANSWER":
        decision["express_mode"] = True
        decision["entity_richness"] = entity_count

    progress_payload = {**payload}
    for k, v in entities.items():
        if v not in (None, "", [], {}):
            progress_payload[k] = v
    progress = _compute_progress(current_goal, progress_payload)
    if progress:
        updates["conversation_progress"] = progress
        hint = _build_proactive_hint(current_goal, progress, progress_payload)
        if hint:
            updates["proactive_hint"] = hint

    updates.update(
        {
            "intent_competition": competition,
            "cognitive_decision": {**decision, "action": "continue"},
        }
    )
    return updates


async def cognitive_orchestrator(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
) -> Dict[str, Any]:
    if state.get("is_onboarding"):
        event = str(state.get("interpreted_event") or "").upper()
        intent = str(state.get("detected_intent") or "UNKNOWN").upper()
        confidence = float(state.get("interpreter_confidence") or 0.0)
        return {
            "cognitive_decision": {
                "phase": "reason",
                "next_step": "onboarding",
                "reason": "onboarding",
                "loop": ["perceive", "think", "decide", "act", "observe", "reason", "retry"],
                "event": event,
                "intent": intent,
                "current_goal": None,
                "confidence": confidence,
            },
            "should_replan": False,
        }

    event = str(state.get("interpreted_event") or "").upper()
    intent = str(state.get("detected_intent") or "UNKNOWN").upper()
    current_goal = str(state.get("current_goal") or "").upper()
    expected_input = str(state.get("expected_input") or "NONE").upper()
    strategy = str(state.get("response_strategy") or "").upper()
    confidence = float(state.get("interpreter_confidence") or 0.0)
    competition = list(state.get("intent_competition") or [])
    in_tunnel = bool(current_goal and expected_input not in {"", "NONE"})

    phase = "perceive"
    next_step = "continue"
    reason = "nominal"

    if strategy in {"CLARIFICATION", "RECOVERY"}:
        phase = "reason"
        next_step = "respond"
        reason = strategy.lower()
    elif event == "INTERRUPTION":
        phase = "decide"
        next_step = "replan"
        reason = "interruption"
    elif _should_trigger_disambiguation(competition, confidence):
        phase = "think"
        next_step = "clarify"
        reason = "intent_competition"
    elif in_tunnel and event in {"ANSWER", "UPDATE", "SELECTION", "CONFIRM", "REJECT"}:
        phase = "act"
        next_step = "continue_tunnel"
        reason = "active_goal"
    elif intent == "UNKNOWN" and event in {"UNKNOWN", "OUT_OF_SCOPE"}:
        phase = "reason"
        next_step = "clarify"
        reason = "unknown_intent"

    return {
        "cognitive_decision": {
            "phase": phase,
            "next_step": next_step,
            "reason": reason,
            "loop": ["perceive", "think", "decide", "act", "observe", "reason", "retry"],
            "event": event,
            "intent": intent,
            "current_goal": current_goal or None,
            "confidence": confidence,
        },
        "should_replan": next_step == "replan",
    }


__all__ = [
    "cognitive_guard",
    "cognitive_orchestrator",
    "_compute_progress",
]
