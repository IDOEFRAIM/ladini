"""
Services — couche métier AgriConnect.

Structure :
  - models.py         : Modèles SQLAlchemy legacy (flat schema)
  - models_v3.py      : Modèles SQLAlchemy v3 (multi-schema: auth, governance, marketplace, intelligence)
  - db_handler.py     : Accès base de données legacy (sync)
  - database_service.py : Service DB v3 — async, multi-schema
  - voice_engine.py   : Azure TTS / STT (service layer)
  - voice.py          : Re-export de VoiceEngine (compat)
  - llm_clients.py    : Clients LLM (Groq / ChatGroq)
  - memory/           : Mémoire 3 niveaux (profil, épisodique, optimiseur)
  - broadcaster.py    : Diffusion d'alertes multi-canal
  - external_apis/    : Intégrations APIs externes
  - data_collection/  : Collecteurs de données
  - scraper/          : Système de scraping
  - scheduling/       : Orchestration temporelle
  - utils/            : Utilitaires transverses
"""

from .db_handler import AgriDatabase
from .llm_clients import get_groq_client
from .voice_engine import VoiceEngine
from .database.database_service import AgriDatabaseService

__all__ = [
    "AgriDatabase",
    "AgriDatabaseService",
    "get_groq_client",
    "VoiceEngine",
]
