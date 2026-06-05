"""MarketAgentState — Single Source of Truth pour la machine à états MarketCoach.

Contrat strict d'état partagé entre tous les nodes du graphe LangGraph.
Implémente le paradigme d'Analyse Pilotée par l'Attente (Expectation-Driven)
pour le canal WhatsApp.

Règles de conception :
  - Tous les reducers sont **purs** (pas de filtrage caché). Un node qui
    écrit None ou une chaîne vide signale explicitement un reset.
  - Les listes sont **remplacées intégralement** (jamais accumulées en silence).
  - Les dictionnaires utilisent un **merge shallow** (la nouvelle valeur écrase
    l'ancienne pour les clés en collision).
  - Le node `Goal Planner` est SEUL responsable de l'écriture de `current_goal`,
    `goal_stack`, `suspended_goal`. Les autres nodes ne touchent jamais ces clés.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional
from typing_extensions import Annotated, TypedDict


# =====================================================================
# REDUCERS (purs, conformes au contrat AG-UI)
# =====================================================================

def replace_value(old: Any, new: Any) -> Any:
    """Écrasement pur : la nouvelle valeur remplace systématiquement l'ancienne."""
    return new


def replace_list(old: List[Any], new: List[Any]) -> List[Any]:
    """Remplacement total de la liste — pas d'accumulation implicite."""
    return new


def merge_dict(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    """Merge shallow : les clés de `new` écrasent celles de `old`."""
    return {**(old or {}), **(new or {})}


# =====================================================================
# EVENT TYPES
# =====================================================================

UserEvent = Literal[
    "NEW_TASK",
    "ANSWER",
    "CONFIRM",
    "REJECT",
    "SELECTION",
    "UPDATE",
    "INTERRUPTION",
    "RESUME",
    "OUT_OF_SCOPE",
    "UNKNOWN",
    "ONBOARDING_INPUT",
]


# =====================================================================
# MAIN STATE
# =====================================================================

class MarketAgentState(TypedDict, total=False):

    # ================================================================
    # 1. RAW INPUT LAYER
    # ================================================================

    user_query: Annotated[str, replace_value]

    normalized_text: Annotated[str, replace_value]

    detected_language: Annotated[str, replace_value]

    translated_text: Annotated[str, replace_value]

    audio_file_path: Annotated[Optional[str], replace_value]

    transcribed_audio: Annotated[Optional[str], replace_value]

    timestamp: Annotated[float, replace_value]

    # ================================================================
    # 2. USER / SESSION CONTEXT
    # ================================================================

    user_phone: Annotated[str, replace_value]

    session_id: Annotated[str, replace_value]

    user_role: Annotated[str, replace_value]

    user_name: Annotated[Optional[str], replace_value]

    zone_name: Annotated[Optional[str], replace_value]

    zone_id: Annotated[Optional[str], replace_value]

    user_context_loaded: Annotated[bool, replace_value]

    user_id: Annotated[Optional[str], replace_value]

    is_onboarding: Annotated[bool, replace_value]

    onboarding_step: Annotated[Optional[str], replace_value]

    turn_count: Annotated[int, replace_value]

    user_farms_cache: Annotated[Optional[List[Dict[str, Any]]], replace_value]

    proactive_hint: Annotated[Optional[str], replace_value]

    conversation_progress: Annotated[Optional[Dict[str, Any]], replace_value]

    # ================================================================
    # 3. SECURITY / TRUST
    # ================================================================

    security_status: Annotated[
        Literal[
            "SAFE",
            "SUSPICIOUS",
            "SCAM_DETECTED",
            "BLOCKED"
        ],
        replace_value
    ]

    security_reason: Annotated[Optional[str], replace_value]

    trust_score: Annotated[Optional[float], replace_value]

    requires_human: Annotated[bool, replace_value]

    # ================================================================
    # 4. INTERPRETER OUTPUT
    # ================================================================

    interpreted_event: Annotated[UserEvent, replace_value]

    detected_intent: Annotated[str, replace_value]

    interpreter_confidence: Annotated[float, replace_value]

    extracted_entities: Annotated[
        Dict[str, Any],
        merge_dict
    ]

    raw_analysis: Annotated[
        Dict[str, Any],
        merge_dict
    ]

    intent_competition: Annotated[
        List[Dict[str, Any]],
        replace_list
    ]

    cognitive_decision: Annotated[
        Dict[str, Any],
        merge_dict
    ]

    # ================================================================
    # 5. GOAL MANAGEMENT
    # ================================================================

    current_goal: Annotated[Optional[str], replace_value]

    pending_goal: Annotated[
        Optional[str],
        replace_value
    ]

    goal_stack: Annotated[
        List[str],
        replace_list
    ]

    goal_status: Annotated[
        Literal[
            "ACTIVE",
            "IDLE",
            "WAITING_INPUT",
            "WAITING_CONFIRMATION",
            "EXECUTING",
            "COMPLETED",
            "FAILED",
            "INTERRUPTED"
        ],
        replace_value
    ]

    current_plan_id: Annotated[
        Optional[str],
        replace_value
    ]

    # ================================================================
    # 6. EXPECTATION ENGINE (Focus Formulaire IHM)
    # ================================================================

    expected_input: Annotated[
        Optional[
            Literal[
                "PRODUCT",
                "PRICE",
                "QUANTITY",
                "UNIT",
                "CONFIRMATION",
                "SELECTION",
                "LOCATION",
                "DATE",
                "NONE"
            ]
        ],
        replace_value
    ]

    last_agent_question: Annotated[
        Optional[str],
        replace_value
    ]

    expected_candidates: Annotated[
        List[str],
        replace_list
    ]

    last_missing_field: Annotated[
        Optional[str],
        replace_value
    ]

    # ================================================================
    # 7. WORKING MEMORY
    # ================================================================

    working_memory: Annotated[
        Dict[str, Any],
        merge_dict
    ]

    transaction_payload: Annotated[
        Dict[str, Any],
        merge_dict
    ]

    draft_payload: Annotated[
        Dict[str, Any],
        merge_dict
    ]

    stable_entities: Annotated[
        Dict[str, Any],
        merge_dict
    ]

    volatile_entities: Annotated[
        Dict[str, Any],
        merge_dict
    ]

    available_mapping: Annotated[
        Dict[str, str],
        replace_value
    ]

    # ================================================================
    # 8. SLOT TRACKING (Deltas du validateur)
    # ================================================================

    required_fields: Annotated[
        List[str],
        replace_list
    ]

    missing_fields: Annotated[
        List[str],
        replace_list
    ]

    completed_fields: Annotated[
        List[str],
        replace_list
    ]

    validation_errors: Annotated[
        List[str],
        replace_list
    ]

    warnings: Annotated[
        List[str],
        replace_list
    ]

    # ================================================================
    # 9. INTERRUPTIONS / MULTI-TASK
    # ================================================================

    interruption_detected: Annotated[
        bool,
        replace_value
    ]

    interruption_type: Annotated[
        Optional[str],
        replace_value
    ]

    interruption_payload: Annotated[
        Dict[str, Any],
        merge_dict
    ]

    suspended_goal: Annotated[
        Optional[str],
        replace_value
    ]

    suspended_payload: Annotated[
        Dict[str, Any],
        merge_dict
    ]

    # ================================================================
    # 10. CONFIRMATION / EXECUTION
    # ================================================================

    waiting_for_confirmation: Annotated[
        bool,
        replace_value
    ]

    is_certified: Annotated[
        bool,
        replace_value
    ]

    confirmation_summary: Annotated[
        Optional[str],
        replace_value
    ]

    execution_authorized: Annotated[
        bool,
        replace_value
    ]

    execution_result: Annotated[
        Dict[str, Any],
        merge_dict
    ]

    # ================================================================
    # 11. MCP / TOOL EXECUTION
    # ================================================================

    selected_tool: Annotated[
        Optional[str],
        replace_value
    ]

    selected_tool_args: Annotated[
        Dict[str, Any],
        merge_dict
    ]

    tool_execution_history: Annotated[
        List[Dict[str, Any]],
        replace_list
    ]

    retry_count: Annotated[
        int,
        replace_value
    ]

    # ================================================================
    # 12. RESPONSE GENERATION
    # ================================================================

    final_response: Annotated[
        Optional[str],
        replace_value
    ]

    ag_ui_component: Annotated[
        Optional[Dict[str, Any]],
        replace_value
    ]

    reply_audio_url: Annotated[
        Optional[str],
        replace_value
    ]

    response_strategy: Annotated[
        Optional[
            Literal[
                "ASK_MISSING_FIELD",
                "CONFIRMATION",
                "SELECTION_MENU",
                "SUCCESS",
                "ERROR",
                "RECOVERY",
                "CLARIFICATION",
                "INTERRUPTION_HANDLER",
                "ONBOARDING",
            ]
        ],
        replace_value
    ]

    onboarding_prompt: Annotated[Optional[str], replace_value]

    # ================================================================
    # 13. SYSTEM FLAGS
    # ================================================================

    status: Annotated[
        Literal[
            "START",
            "INTERPRETING",
            "PLANNING",
            "VALIDATING",
            "WAITING_INPUT",
            "WAITING_CONFIRMATION",
            "EXECUTING",
            "COMPLETED",
            "ERROR",
            "BLOCKED"
        ],
        replace_value
    ]

    is_locked: Annotated[
        bool,
        replace_value
    ]

    should_replan: Annotated[
        bool,
        replace_value
    ]

    should_interrupt: Annotated[
        bool,
        replace_value
    ]


__all__ = [
    "MarketAgentState",
    "UserEvent",
    "replace_value",
    "replace_list",
    "merge_dict",
]