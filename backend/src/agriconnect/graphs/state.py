import operator
from typing import TypedDict, Annotated, List, Dict, Any, Union, Optional
from enum import Enum


class Severity(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Alert(TypedDict):
    source: str
    message: str
    severity: Severity


class ExpertResponse(TypedDict):
    """Réponse individuelle d'un expert (fan-out/fan-in pattern)."""
    expert: str           # Nom de l'expert (sentinelle, formation, market)
    response: str         # Contenu de la réponse
    is_lead: bool         # True si c'est l'expert principal (sa réponse sera la base)
    has_alerts: bool      # True si contient des alertes critiques


# Niveaux d'utilisateur pour le RAG adaptatif
USER_LEVELS = ("debutant", "intermediaire", "expert")


class GlobalAgriState(TypedDict):
    # --- Context ---
    zone_id: str
    requete_utilisateur: Optional[str]  # Optionnel car peut être un rapport auto
    user_id: str
    user_phone: Optional[str]           # Numéro WhatsApp (pour MarketplaceAgent)
    crop: str                           # Culture principale (ex: Maïs, Coton)
    user_reliability_score: float       # Pilier 2 — Note de confiance (0.0 à 1.0)
    is_sms_mode: bool                   # Pilier 4 — Mode SMS activé
    user_level: str                     # "debutant" | "intermediaire" | "expert"

    # --- Flow Control ---
    flow_type: str                      # "MESSAGE" ou "REPORT"
    is_agricultural: Optional[bool]     # True si question agricole, False si hors-sujet
    off_topic_reason: Optional[str]     # Raison du rejet si hors-sujet
    needs: Optional[Dict[str, Any]]     # Analyse de l'intention et besoins experts

    # --- Data Lake (Données collectées) ---
    meteo_data: Optional[Dict[str, Any]]
    soil_data: Optional[Dict[str, Any]]
    health_data: Optional[Dict[str, Any]]
    market_data: Optional[Dict[str, Any]]
    health_raw_data: Optional[Dict[str, Any]]  # Input spécifique de l'utilisateur

    # --- Memory & Outputs (Reducers: LangGraph additionne automatiquement) ---
    global_alerts: Annotated[List[Alert], operator.add]
    execution_path: Annotated[List[str], operator.add]
    expert_responses: Annotated[List[ExpertResponse], operator.add]  # Fan-out/fan-in
    final_report: Optional[Dict[str, Any]]
    final_response: Optional[str]

    # --- Audio Output ---
    audio_url: Optional[str]  # Chemin du fichier .wav généré par TTS

    # --- Mémoire 3 Niveaux (injectés par ContextOptimizer) ---
    memory_profile: Optional[str]       # Niveau 1: Fiche Ferme JSON (~80 tokens)
    memory_episodes: Optional[str]      # Niveau 2: Résumés épisodiques (~120 tokens)
    memory_context: Optional[str]       # Combiné: Profil + Épisodes (~200 tokens)
    memory_token_estimate: Optional[int] # Estimation tokens du contexte mémoire

    # --- Community Benchmark (reports) ---
    community_benchmark: Optional[Dict[str, Any]]

    # --- HITL (Human-in-the-Loop) ---
    requires_validation: Optional[bool]       # True si action risquée en attente
    pending_action_id: Optional[str]          # ID de l'action figée
    pending_action_payload: Optional[Dict[str, Any]]  # Données de l'action en attente
    hitl_status: Optional[str]                # "PENDING" | "APPROVED" | "REJECTED"

    # --- LangGraph Send (fan-out per-expert) ---
    _expert_target: Optional[str]             # Expert name for Send() dispatch
    _expert_is_lead: Optional[bool]           # True if primary expert in fan-out

    # --- Context Elicitation ---
    awaiting_context: Optional[bool]          # True si données manquantes
    missing_fields: Optional[List[Dict[str, str]]]  # Champs à demander