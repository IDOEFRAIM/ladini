"""Local state for Formation sub-graph."""

from typing import Any, Dict, List, Optional, TypedDict


class FormationState(TypedDict, total=False):
    # --- Input & Identity ---
    user_query: str
    learner_profile: Dict[str, Any]
    crop_name: str           # Nom brut saisi par l'utilisateur
    identified_crop: str     # Type normalisé trouvé par l'IA (ex: "MAIS")
    zone_category: str
    area_ha: float
    cycle_id: Optional[str]  # ID du cycle pour le lien MCP

    # --- Internal Reasoning & Analysis ---
    intent: str              # FORMATION, URGENCE, CONSEIL
    urgency: str             # NORMAL, HAUTE, CRITIQUE
    focus_topics: List[str]
    field_actions: List[str]
    safety_flags: List[str]
    optimized_query: str

    # --- Agronomic Truth (The JSON "Seeds") ---
    crop_specs: Dict[str, Any]  # Les caractéristiques extraites du JSON

    # --- Knowledge Retrieval (RAG & MCP) ---
    retrieved_context: str
    sources: List[Dict[str, Any]]
    technical_canvas: Dict[str, Any]
    canvas_markdown: str
    retrieval_mode: str        # 'JSON_ONLY', 'RAG', 'MCP_FULL'
    advisor_error: bool

    # --- Draft & Refine ---
    learning_modules: List[str]
    prerequisites: List[str]
    reasoning: str
    answer_draft: str
    evaluation: Dict[str, float]

    # --- Final Output & Tracking ---
    final_response: str
    agri_response: Optional[Dict[str, Any]]
    expert_responses: List[Dict[str, Any]]
    concepts_appris: List[str]  # Pour le suivi pédagogique

    # --- AG-UI Protocol ---
    ag_ui_component: Optional[Dict[str, Any]]
    response_strategy: str     # PROVIDE_ANSWER, ASK_CLARIFICATION, DEGRADED_FALLBACK
    missing_info: List[str]    # Champs critiques manquants pour conseil précis

    # --- Status & Guards ---
    status: str                # ANALYZED, VALIDATED, CONSULTING, COMPOSED, ...
    warnings: List[str]
    document_grade: int
    critique_retry_count: int
    rewrited_retry_count: int
    degraded_mode: bool        # True si JSON indisponible
    requires_human: bool
    clarification_needed: str


__all__ = ["FormationState"]
