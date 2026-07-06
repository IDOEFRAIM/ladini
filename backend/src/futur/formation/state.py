"""Local state for Formation sub-graph.

Sections
--------
1. Input & Identity — raw user input + DB-resolved identity
2. DB-Driven Personalization — profile, history, plots, cycles from AgriDatabaseService
3. Internal Reasoning & Analysis — intent classification, urgency, topics
4. Agronomic Truth — JSON/INERA crop specs
5. Knowledge Retrieval (RAG & MCP) — retrieved docs, canvas, tool call log
6. Draft & Refine — composition, critique loop artefacts
7. Final Output & Tracking — response, pedagogical tracking
8. AG-UI Protocol — rich UI components for clarification
9. Session Logging — session ID, scores, feedback for DB persistence
10. Status & Guards — workflow control flags
11. Slot-Filling — DRY form engine state (shared with MarketCoach)
"""

from typing import Any, Dict, List, Optional, TypedDict
from typing_extensions import Annotated

from agriconnect.agents.reducers import (
    replace_value,
    replace_list,
    merge_dict,
)


class FormationState(TypedDict, total=False):
    # ── 1. Input & Identity ──────────────────────────────────────────
    user_query: Annotated[str, replace_value]
    learner_profile: Annotated[Dict[str, Any], merge_dict]
    crop_name: Annotated[str, replace_value]
    identified_crop: Annotated[str, replace_value]
    zone_category: Annotated[str, replace_value]
    area_ha: Annotated[float, replace_value]
    cycle_id: Annotated[Optional[str], replace_value]
    db_user_id: Annotated[Optional[str], replace_value]
    user_phone: Annotated[Optional[str], replace_value]

    # ── 2. DB-Driven Personalization ─────────────────────────────────
    profile_loaded: Annotated[bool, replace_value]
    training_history: Annotated[List[Dict[str, Any]], replace_list]
    declared_crops: Annotated[List[str], replace_list]
    user_plots: Annotated[List[Dict[str, Any]], replace_list]
    active_cycles: Annotated[List[Dict[str, Any]], replace_list]
    crop_requirements: Annotated[Optional[Dict[str, Any]], merge_dict]

    # ── 3. Internal Reasoning & Analysis ─────────────────────────────
    intent: Annotated[str, replace_value]
    urgency: Annotated[str, replace_value]
    focus_topics: Annotated[List[str], replace_list]
    field_actions: Annotated[List[str], replace_list]
    safety_flags: Annotated[List[str], replace_list]
    optimized_query: Annotated[str, replace_value]

    # ── 4. Agronomic Truth (The JSON "Seeds") ────────────────────────
    crop_specs: Annotated[Dict[str, Any], merge_dict]

    # ── 5. Knowledge Retrieval (RAG & MCP) ───────────────────────────
    retrieved_context: Annotated[str, replace_value]
    sources: Annotated[List[Dict[str, Any]], replace_list]
    technical_canvas: Annotated[Dict[str, Any], merge_dict]
    canvas_markdown: Annotated[str, replace_value]
    retrieval_mode: Annotated[str, replace_value]
    advisor_error: Annotated[bool, replace_value]
    tool_calls: Annotated[List[Dict[str, Any]], replace_list]
    localized_response: Annotated[str, replace_value]
    localized_status: Annotated[str, replace_value]
    fertilizer_instructions: Annotated[List[str], replace_list]
    fertilizer_breakdown: Annotated[List[Dict[str, Any]], replace_list]

    # ── 6. Draft & Refine ────────────────────────────────────────────
    learning_modules: Annotated[List[str], replace_list]
    prerequisites: Annotated[List[str], replace_list]
    reasoning: Annotated[str, replace_value]
    answer_draft: Annotated[str, replace_value]
    evaluation: Annotated[Dict[str, float], merge_dict]

    # ── 7. Final Output & Tracking ───────────────────────────────────
    final_response: Annotated[str, replace_value]
    agri_response: Annotated[Optional[Dict[str, Any]], merge_dict]
    expert_responses: Annotated[List[Dict[str, Any]], replace_list]
    concepts_appris: Annotated[List[str], replace_list]

    # ── 8. AG-UI Protocol ────────────────────────────────────────────
    ag_ui_component: Annotated[Optional[Dict[str, Any]], replace_value]
    response_strategy: Annotated[str, replace_value]
    missing_info: Annotated[List[str], replace_list]
    ag_ui_payloads: Annotated[List[Dict[str, Any]], replace_list]

    # ── 9. Session Logging ───────────────────────────────────────────
    session_id: Annotated[Optional[str], replace_value]
    session_score: Annotated[Optional[float], replace_value]
    session_feedback: Annotated[Optional[Dict[str, Any]], merge_dict]
    session_logged: Annotated[bool, replace_value]

    # ── 10. Status & Guards ──────────────────────────────────────────
    status: Annotated[str, replace_value]
    warnings: Annotated[List[str], replace_list]
    document_grade: Annotated[int, replace_value]
    critique_retry_count: Annotated[int, replace_value]
    rewrited_retry_count: Annotated[int, replace_value]
    degraded_mode: Annotated[bool, replace_value]
    requires_human: Annotated[bool, replace_value]
    clarification_needed: Annotated[str, replace_value]

    # ── 11. Slot-Filling (DRY form engine) ───────────────────────────
    active_form: Annotated[Optional[str], replace_value]
    form_data: Annotated[Dict[str, Any], merge_dict]
    form_step: Annotated[Optional[str], replace_value]


__all__ = ["FormationState"]
