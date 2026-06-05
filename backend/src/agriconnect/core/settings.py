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
    # Default to repository root `ca-certificate.crt` (developer-provided file)
    DB_CA_PATH: str = str(BASE_DIR.parent.parent / "ca-certificate.crt")
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
        # Prefer explicit project key, then provider-specific key, then environment
        key = self.AGRICONNECT_APIKEY or self.GROQ_API_KEY
        if not key:
            # allow direct env override for interactive sessions
            key = os.getenv("GROQ_API_KEY") or os.getenv("AGRICONNECT_APIKEY") or ""
        return key
        
    # --- Azure OpenAI (utilisé si LLM_PROVIDER=azure) ---
    AZURE_OPENAI_API_KEY: str = ""
    AZURE_OPENAI_ENDPOINT: str = ""
    AZURE_OPENAI_DEPLOYMENT_NAME: str = "gpt-4o"
    AZURE_OPENAI_API_VERSION: str = "2024-05-01-preview"

    # --- Database (PostgreSQL) ---
    DATABASE_URL: str = ""
    # Provider-specific override (DigitalOcean): prefer when set.
    DO_DATABASE_URL: str = ""
    # SSL policy for PostgreSQL connections:
    # - verify-full: TLS + certificate verification (recommended)
    # - require: TLS without certificate verification (dev fallback)
    # - disable: no TLS (local-only)
    DB_SSL_MODE: str = "require"
    # --- Redis / Celery ---
    REDIS_URL: str = "a"
    VALKEY_ENDPOINT: str = ""
    VALKEY_AUTH_TOKEN: str = ""
    VALKEY_USE_TLS: bool = True
    CELERY_BROKER_URL: str = ""
    CELERY_RESULT_BACKEND: str = ""
    # Lean mode default: run tasks directly without broker/workers.
    USE_ASYNC_QUEUE: bool = False
    # Streams are optional in lean mode and enabled when async queue is enabled.
    USE_REDIS_STREAMS: bool = False
    # Gold storage backend: pgvector on Postgres by default (RDS-only).
    VECTOR_BACKEND: str = "pgvector"
    # Fallback strategy when RedisSearch (FT.*) is unavailable:
    # - "manual": force Redis HGET + cosine in Python (Tier 1)
    # - "pgvector": skip Redis fallback and use PgVector
    # - "auto": try manual Redis first, then PgVector
    RAG_REDIS_FALLBACK_MODE: str = "manual"

    # --- MCP Runtime Startup ---
    MCP_ALLOW_DEGRADED_START: bool = False
    MCP_DB_STARTUP_RETRIES: int = 2
    MCP_DB_STARTUP_RETRY_DELAY_SEC: float = 1.5
    MCP_DB_SERVER_HOST: str = "localhost"
    MCP_DB_SERVER_PORT: int = 8003

    @property
    def celery_broker(self) -> str:
        return self.CELERY_BROKER_URL or self.REDIS_URL

    @property
    def celery_backend(self) -> str:
        return self.CELERY_RESULT_BACKEND or "a"

    # --- Azure Speech (TTS/STT — indépendant du LLM provider) ---
    AZURE_SPEECH_KEY: str = ""
    AZURE_SPEECH_KEY_2: str = ""
    AZURE_REGION: str = "westeurope"
    AZURE_SPEECH_ENDPOINT: str = "a"
    USE_AZURE_SPEECH: bool = False

    # --- Twilio / WhatsApp ---
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_WHATSAPP_NUMBER: str = ""

    # --- LangSmith / Observabilité ---
    LANGCHAIN_TRACING_V2: bool = False
    LANGCHAIN_API_KEY: str = ""
    LANGCHAIN_PROJECT: str = "agriconnect"
    LANGCHAIN_ENDPOINT: str = "a"
    LANGSMITH_API_KEY: str = ""

    @property
    def langsmith_enabled(self) -> bool:
        """True si le tracing LangSmith est activé et configuré."""
        key = self.LANGCHAIN_API_KEY or self.LANGSMITH_API_KEY
        return bool(self.LANGCHAIN_TRACING_V2 and key)

    # --- RAG (adaptatif par profil) ---
    # Default RAG embedding model aligned with 768D pgvector schema.
    EMBEDDING_MODEL: str = "BAAI/bge-base-en-v1.5"
    # Enforce RAG embedding dimensionality throughout the codebase.
    RAG_EMBEDDING_DIM: int = 768
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
    # --- PDF processor thresholds ---
    PDF_MAX_CHUNK_CHARS: int = 1500
    PDF_MIN_MERGE_CHARS: int = 50
    # Ingestion audit thresholds
    INGESTION_AUDIT_FAILURE_THRESHOLD: float = 0.1
    INGESTION_AUDIT_PREFIX: str = "ingestion_audit"

    # Pydantic Settings: prefer .env inside the package, but fall back to the
    # repository root `.env` (e.g. backend/.env) to support developer workflows.
    _pkg_env = Path(__file__).resolve().parent.parent / ".env"
    _root_env = Path(__file__).resolve().parent.parent.parent.parent / ".env"
    env_path: ClassVar[Path] = _pkg_env if _pkg_env.exists() else _root_env
    model_config = {
        "env_file": str(env_path),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    # --- Sentry (observabilité erreurs) ---
    SENTRY_DSN: str = ""
    SENTRY_ENVIRONMENT: str = "development"

    # --- AWS S3 (optional) ---
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_SESSION_TOKEN: str = ""
    AWS_REGION: str = ""
    S3_BUCKET: str = ""
    S3_KEY_PREFIX: str = ""
    S3_REGION: str = ""
    # S3 ingestion hygiene: prefixes to exclude from raw ingestion (comma-separated or list)
    # Extended with common legacy snapshot/metadata prefixes discovered during audit
    INGESTION_S3_EXCLUDE_PREFIXES: list[str] = [
        "crawl_snapshots",
        "snapshots",
        "crawl",
        "data_platforms",
        "fao_publications",
        "fews_net",
    ]

# Singleton — importable partout
settings = Settings()

# Normalize DB URLs: remove surrounding quotes and whitespace so all code
# sees a canonical value. Support DO_DATABASE_URL as an explicit override.
def _normalize_db_url(url: str | None) -> str | None:
    if url is None:
        return None
    s = str(url).strip()
    if len(s) >= 2 and ((s[0] == s[-1] == '"') or (s[0] == s[-1] == "'")):
        s = s[1:-1].strip()
    return s or None

# Prefer explicit provider URL when present (loaded via pydantic from env).
_do_db = _normalize_db_url(settings.DO_DATABASE_URL or os.getenv("DO_DATABASE_URL") or os.getenv("AGRICONNECT_DO_DATABASE_URL"))
_db = _normalize_db_url(settings.DATABASE_URL or os.getenv("DATABASE_URL"))
if _do_db:
    settings.DO_DATABASE_URL = _do_db
    settings.DATABASE_URL = _do_db
elif _db:
    settings.DATABASE_URL = _db
else:
    settings.DATABASE_URL = ""


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
