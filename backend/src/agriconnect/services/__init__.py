"""Service layer modules for AgriConnect.

Principaux blocs encore supportés :
  - ``db_handler``          : accès PostgreSQL synchrone (hérité).
  - ``database``            : AgriDatabaseService v3 (async/multi-schema).
  - ``voice_engine``        : synthèse et transcription vocale Azure.
  - ``llm_clients``         : clients Groq et wrappers SDK.
  - ``memory``              : profil utilisateur, mémoire épisodique, optimiseur de contexte.
  - ``data_collection`` / ``scraper`` : collecte météo + orchestrateur de scrapers.
  - ``persistence`` / ``rag_service`` : couches utilitaires utilisées par les agents.
"""

from typing import Any

__all__ = ["AgriDatabase", "AgriDatabaseService", "get_groq_client", "VoiceEngine"]


def __getattr__(name: str) -> Any:
  # Keep package import lightweight: avoid importing DB/voice stacks at module import time.
  if name == "AgriDatabase":
    from .db_handler import AgriDatabase

    return AgriDatabase
  if name == "AgriDatabaseService":
    from .database import AgriDatabaseService

    return AgriDatabaseService
  if name == "get_groq_client":
    from .llm_clients import get_groq_client

    return get_groq_client
  if name == "VoiceEngine":
    from .voice_engine import VoiceEngine

    return VoiceEngine
  raise AttributeError(name)
