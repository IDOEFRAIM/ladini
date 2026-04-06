import logging
import os
from urllib.parse import quote

logger = logging.getLogger(__name__)

try:
    import faiss
    from llama_index.core import Settings, VectorStoreIndex, StorageContext
    from llama_index.embeddings.huggingface import HuggingFaceEmbedding
    from llama_index.vector_stores.faiss import FaissVectorStore

    # ── LLM : délégué au provider abstrait (Groq/Azure/Bedrock) ──
    from agriconnect.services.llm_clients import get_chat_client, get_sdk_client

    from agriconnect.core.settings import settings as app_settings
    from .config import EMBEDDING_MODEL_NAME, CHUNK_SIZE, CHUNK_OVERLAP, DB_DIR

    # Dimension for sentence-transformers/all-MiniLM-L6-v2
    EMBEDDING_DIM = 384
    INDEX_FILE = os.path.join(DB_DIR, "faiss_index.bin")

    def get_embedding_model():
        return HuggingFaceEmbedding(model_name=EMBEDDING_MODEL_NAME)

    # ── Aliases rétro-compatibles pour tous les imports existants ──
    def get_llm_client():
        """Retourne un client LangChain Chat (provider-agnostic)."""
        return get_chat_client()

    def get_groq_sdk():
        """Retourne un SDK client brut (provider-agnostic)."""
        return get_sdk_client()

    def init_settings():
        Settings.embed_model = get_embedding_model()
        Settings.chunk_size = CHUNK_SIZE
        Settings.chunk_overlap = CHUNK_OVERLAP

    # Path where llama_index FaissVectorStore persists as binary
    DEFAULT_VS_FILE = os.path.join(DB_DIR, "default__vector_store.json")

    def get_vector_store():
        """
        Returns a FaissVectorStore.
        Tries: 1) faiss_index.bin  2) default__vector_store.json (FAISS binary persisted by llama_index)
        Falls back to creating a new empty HNSW index.
        """
        if not os.path.exists(DB_DIR):
            os.makedirs(DB_DIR)

        vector_backend = (os.getenv("AGRICONNECT_VECTOR_BACKEND", "redis") or "redis").strip().lower()

        redis_url = (getattr(app_settings, "REDIS_URL", "") or "").strip()
        valkey_endpoint = (getattr(app_settings, "VALKEY_ENDPOINT", "") or "").strip()
        valkey_token = (getattr(app_settings, "VALKEY_AUTH_TOKEN", "") or "").strip()
        use_tls = bool(getattr(app_settings, "VALKEY_USE_TLS", True))

        # Prefer an explicitly configured REDIS_URL (env or settings). Only fall back
        # to VALKEY_ENDPOINT when REDIS_URL is not provided. This allows local tunnels
        # (e.g., ssh port-forward) to override remote managed endpoints.
        if not redis_url and valkey_endpoint:
            scheme = "rediss" if use_tls else "redis"
            if "://" in valkey_endpoint:
                redis_url = valkey_endpoint
            else:
                if valkey_token:
                    redis_url = f"{scheme}://:{quote(valkey_token)}@{valkey_endpoint}"
                else:
                    redis_url = f"{scheme}://{valkey_endpoint}"
            logger.info("Using VALKEY_ENDPOINT for vector store connection")

        if redis_url and "://" not in redis_url:
            scheme = "rediss" if use_tls else "redis"
            redis_url = f"{scheme}://{redis_url}"

        redis_enabled = bool(redis_url)

        if vector_backend in {"redis", "valkey", "auto"} and redis_enabled:
            try:
                # Prefer RedisSearch-backed store when available (Redis Stack with vector support)
                try:
                    from agriconnect.rag.redis_search_store import RedisSearchVectorStore

                    logger.info("Initializing RedisSearchVectorStore (RAG)")
                    return RedisSearchVectorStore(
                        redis_url,
                        dim=EMBEDDING_DIM,
                        index_name="rag:idx",
                        socket_timeout=20,
                        retry_on_timeout=True,
                        decode_responses=True,
                    )
                except Exception:
                    # Fallback to simple Redis vector store
                    try:
                        from agriconnect.rag.redis_store import RedisVectorStore

                        logger.info("Initializing RedisVectorStore (RAG)")
                        return RedisVectorStore(
                            redis_url,
                            dim=EMBEDDING_DIM,
                            socket_timeout=20,
                            retry_on_timeout=True,
                            decode_responses=True,
                        )
                    except Exception as re:
                        logger.warning("RedisVectorStore init failed, falling back to next backend: %s", re)
            except Exception:
                # redis-related modules not available or import error -> continue to next fallback
                pass

        if vector_backend in {"pgvector", "postgres", "postgresql", "auto"}:
            try:
                from agriconnect.rag.pgvector_store import PgVectorStore

                logger.info("Initializing PgVectorStore (RAG) from DATABASE_URL")
                return PgVectorStore(dim=EMBEDDING_DIM)
            except Exception as pe:
                logger.warning("PgVectorStore init failed, falling back to Redis/FAISS: %s", pe)

        # 1) Explicit FAISS binary
        if os.path.exists(INDEX_FILE):
            try:
                faiss_index = faiss.read_index(INDEX_FILE)
                logger.info("Loaded FAISS from %s: %d vectors", INDEX_FILE, faiss_index.ntotal)
                return FaissVectorStore(faiss_index=faiss_index)
            except Exception as e:
                logger.warning("Could not load existing index: %s", e)

        # 2) llama_index persisted FaissVectorStore (binary despite .json ext)
        if os.path.exists(DEFAULT_VS_FILE):
            try:
                faiss_index = faiss.read_index(DEFAULT_VS_FILE)
                logger.info("Loaded FAISS from %s: %d vectors", DEFAULT_VS_FILE, faiss_index.ntotal)
                return FaissVectorStore(faiss_index=faiss_index)
            except Exception as e:
                logger.warning("Could not load FAISS from %s: %s", DEFAULT_VS_FILE, e)

        # 3) Create new empty index
        faiss_index = faiss.IndexHNSWFlat(EMBEDDING_DIM, 32, faiss.METRIC_INNER_PRODUCT)
        return FaissVectorStore(faiss_index=faiss_index)

    def get_storage_context():
        vector_store = get_vector_store()
        # Check if docstore.json exists to decide whether to load or create new
        docstore_path = os.path.join(DB_DIR, "docstore.json")
        if os.path.exists(docstore_path):
            return StorageContext.from_defaults(vector_store=vector_store, persist_dir=str(DB_DIR))
        else:
            return StorageContext.from_defaults(vector_store=vector_store)

    def save_index(index):
        if hasattr(index.vector_store, "client"):
            faiss.write_index(index.vector_store.client, INDEX_FILE)

except Exception as e:
    # Fail fast: do not provide dummy LLM/SDK fallbacks. The application expects
    # real ML dependencies in production; instruct the operator to install them.
    raise ImportError(
        "Missing ML dependencies for RAG components (faiss / llama_index / huggingface). "
        "Install optional requirements and ensure the environment has access to the Groq SDK. "
        f"Original error: {e}") from e

