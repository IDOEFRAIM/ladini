"""
Settings — Configuration centralisée AgriConnect (Pydantic Settings).

Toute la configuration passe par ici. Plus jamais de os.getenv() éparpillé.
Usage:
    from backend.core.settings import settings
    print(settings.DATABASE_URL)
"""

import os
from pathlib import Path
from typing import ClassVar
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Configuration centralisée, lue depuis les variables d'env / .env."""

    # --- Paths ---
    BASE_DIR: Path = Path(__file__).resolve().parent.parent.parent
    AUDIO_OUTPUT_DIR: str = "./audio_output"

    # --- API ---
    APP_NAME: str = "AgriConnect"
    APP_VERSION: str = "2.0.0"
    DEBUG: bool = False
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    ALLOWED_ORIGINS: list[str] = ["*"]

    # --- LLM (Provider-agnostic) ---
    # Valeurs possibles : "groq", "azure", "bedrock"
    LLM_PROVIDER: str = "groq"
    AGRICONNECT_APIKEY: str = ""
    GROQ_API_KEY: str = ""
    LLM_MODEL: str = "llama-3.1-8b-instant"
    LLM_TEMPERATURE: float = 0.0

    @property
    def llm_api_key(self) -> str:
        """Retourne la clé API LLM disponible (Groq ou générique)."""
        return self.AGRICONNECT_APIKEY or self.GROQ_API_KEY

    # --- Azure OpenAI (utilisé si LLM_PROVIDER=azure) ---
    AZURE_OPENAI_API_KEY: str = ""
    AZURE_OPENAI_ENDPOINT: str = ""
    AZURE_OPENAI_DEPLOYMENT_NAME: str = "gpt-4o"
    AZURE_OPENAI_API_VERSION: str = "2024-05-01-preview"

    # --- Database (PostgreSQL) ---
    DATABASE_URL: str = "postgresql://doadmin:AVNS_-TtxFZrkDLQSQ2W8UiX@db-postgresql-fra1-38999-do-user-31802282-0.a.db.ondigitalocean.com:25060/defaultdb?sslmode=require"

    # --- Redis / Celery ---
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = ""
    CELERY_RESULT_BACKEND: str = ""

    @property
    def celery_broker(self) -> str:
        return self.CELERY_BROKER_URL or self.REDIS_URL

    @property
    def celery_backend(self) -> str:
        return self.CELERY_RESULT_BACKEND or "redis://localhost:6379/1"

    # --- Azure Speech (TTS/STT — indépendant du LLM provider) ---
    AZURE_SPEECH_KEY: str = ""
    AZURE_SPEECH_KEY_2: str = ""
    AZURE_REGION: str = "westeurope"
    AZURE_SPEECH_ENDPOINT: str = "https://westeurope.api.cognitive.microsoft.com/"
    USE_AZURE_SPEECH: bool = False

    # --- Twilio / WhatsApp ---
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_WHATSAPP_NUMBER: str = ""

    # --- LangSmith / Observabilité ---
    LANGCHAIN_TRACING_V2: bool = False
    LANGCHAIN_API_KEY: str = ""
    LANGCHAIN_PROJECT: str = "agriconnect"
    LANGCHAIN_ENDPOINT: str = "https://api.smith.langchain.com"
    LANGSMITH_API_KEY: str = ""

    @property
    def langsmith_enabled(self) -> bool:
        """True si le tracing LangSmith est activé et configuré."""
        key = self.LANGCHAIN_API_KEY or self.LANGSMITH_API_KEY
        return bool(self.LANGCHAIN_TRACING_V2 and key)

    # --- RAG (adaptatif par profil) ---
    EMBEDDING_MODEL: str = "sentence-transformers/all-MiniLM-L6-v2"
    CHUNK_SIZE: int = 512
    CHUNK_OVERLAP: int = 50
    # Débutant : rapide, pas de HyDe, peu de résultats
    TOP_K_RETRIEVAL: int = 10
    TOP_K_RERANK: int = 5
    RAG_DEBUTANT_TOP_K: int = 5
    RAG_DEBUTANT_RERANK_K: int = 3
    RAG_DEBUTANT_USE_HYDE: bool = False
    # Intermédiaire : équilibré
    RAG_INTER_TOP_K: int = 10
    RAG_INTER_RERANK_K: int = 5
    RAG_INTER_USE_HYDE: bool = True
    # Expert : précision max, HyDe + rerank lourd
    RAG_EXPERT_TOP_K: int = 20
    RAG_EXPERT_RERANK_K: int = 8
    RAG_EXPERT_USE_HYDE: bool = True

    # Pydantic Settings: utiliser le .env dans le package `agriconnect` (relatif au module)
    env_path: ClassVar[Path] = Path(__file__).resolve().parent.parent / ".env"
    model_config = {
        "env_file": str(env_path),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    # --- Sentry (observabilité erreurs) ---
    SENTRY_DSN: str = ""
    SENTRY_ENVIRONMENT: str = "development"

# Singleton — importable partout
settings = Settings()


# ── LangSmith : exporter les variables d'environnement ───────────────
# LangChain / LangGraph lisent ces variables automatiquement.
# On les exporte ici pour que tout .invoke() soit tracé sans code additionnel.
def _bootstrap_langsmith():
    if not settings.langsmith_enabled:
        return
    _key = settings.LANGCHAIN_API_KEY or settings.LANGSMITH_API_KEY
    os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")
    os.environ.setdefault("LANGCHAIN_API_KEY", _key)
    os.environ.setdefault("LANGCHAIN_PROJECT", settings.LANGCHAIN_PROJECT)
    os.environ.setdefault("LANGCHAIN_ENDPOINT", settings.LANGCHAIN_ENDPOINT)

_bootstrap_langsmith()
