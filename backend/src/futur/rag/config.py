from pathlib import Path
from typing import NamedTuple
from agriconnect.core.settings import settings

# Paths
# settings.BASE_DIR points to backend/src — adjust to repository layout
BASE_DIR = settings.BASE_DIR

# --- RAG Source Configuration ---
# Priority: AWS S3 > Local Filesystem
# If S3_BUCKET is configured, we use it as the source of truth.
if settings.S3_BUCKET:
    _bucket = settings.S3_BUCKET
    _prefix = (settings.S3_KEY_PREFIX or "").strip("/")
    # Construct S3 URI: s3://bucket/prefix/raw_data
    # We append 'raw_data' to match the local structure convention
    RAW_DATA_DIR = f"s3://{_bucket}/{_prefix}/raw_data" if _prefix else f"s3://{_bucket}/raw_data"
else:
    # Fallback: Local raw data lives in backend/sources/raw_data (one level up from src)
    RAW_DATA_DIR = BASE_DIR.parent / "sources" / "raw_data"

# RAG DB folder in backend/rag_db (one level up from src)
DB_DIR = BASE_DIR.parent / "rag_db"

# Model Config — single source of truth from settings
EMBEDDING_MODEL_NAME = settings.EMBEDDING_MODEL
EMBEDDING_BACKEND = "llamaindex_huggingface"
EMBEDDING_POOLING = "mean"
EMBEDDING_NORMALIZE_L2 = True
EMBEDDING_DISTANCE_METRIC = "cosine"
_IS_E5 = "e5" in (EMBEDDING_MODEL_NAME or "").lower()
EMBEDDING_QUERY_PREFIX = "query: " if _IS_E5 else ""
EMBEDDING_PASSAGE_PREFIX = "passage: " if _IS_E5 else ""
USE_E5_INSTRUCTION_PREFIXES = bool(_IS_E5)
RERANKER_MODEL_NAME = "cross-encoder/ms-marco-MiniLM-L-6-v2"

# LLM Config
LLM_MODEL_NAME = settings.LLM_MODEL

# RAG Parameters — defaults (backward-compatible)
CHUNK_SIZE = settings.CHUNK_SIZE
_min_overlap = max(1, int(CHUNK_SIZE * 0.10))
_max_overlap = max(_min_overlap, int(CHUNK_SIZE * 0.15))
CHUNK_OVERLAP = min(max(settings.CHUNK_OVERLAP, _min_overlap), _max_overlap)
TOP_K_RETRIEVAL = settings.TOP_K_RETRIEVAL
TOP_K_RERANK = settings.TOP_K_RERANK


# ── Profils RAG adaptatifs ────────────────────────────────────
class RAGProfile(NamedTuple):
    """Paramètres de retrieval adaptés au niveau de l'utilisateur."""
    top_k: int          # Nombre de documents à récupérer
    rerank_k: int       # Nombre de documents après reranking
    use_hyde: bool      # Activer HyDe (latence +1s, précision +++)
    tone: str           # "simple" | "standard" | "technique"


RAG_PROFILES = {
    "debutant": RAGProfile(
        top_k=settings.RAG_DEBUTANT_TOP_K,
        rerank_k=settings.RAG_DEBUTANT_RERANK_K,
        use_hyde=settings.RAG_DEBUTANT_USE_HYDE,
        tone="simple",
    ),
    "intermediaire": RAGProfile(
        top_k=settings.RAG_INTER_TOP_K,
        rerank_k=settings.RAG_INTER_RERANK_K,
        use_hyde=settings.RAG_INTER_USE_HYDE,
        tone="standard",
    ),
    "expert": RAGProfile(
        top_k=settings.RAG_EXPERT_TOP_K,
        rerank_k=settings.RAG_EXPERT_RERANK_K,
        use_hyde=settings.RAG_EXPERT_USE_HYDE,
        tone="technique",
    ),
}


def get_rag_profile(user_level: str = "debutant") -> RAGProfile:
    """Retourne le profil RAG adapté au niveau utilisateur."""
    return RAG_PROFILES.get(user_level, RAG_PROFILES["debutant"])
