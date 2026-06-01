import logging
import os
import threading
from llama_index.core import VectorStoreIndex, QueryBundle, load_index_from_storage
from llama_index.core.schema import NodeWithScore
from typing import List, Optional
from .components import init_settings, get_storage_context, get_groq_sdk
from .config import TOP_K_RETRIEVAL, TOP_K_RERANK, get_rag_profile, RAGProfile
from pathlib import Path
import json

logger = logging.getLogger(__name__)

# Optional reranker (lazy-loaded to avoid heavy model import at module import time)
RERANKER = None


class AgileRetriever:
    def __init__(self):
        init_settings()
        self._fallback_embedder = None
        self._disable_vector_store_fallback = False
        self._pgvector_available = True
        self._pgvector_disabled_reason = None
        self._vector_store_query_timeout_s = float(os.getenv("AGRICONNECT_RAG_VECTOR_QUERY_TIMEOUT", "6"))
        self._primary_backend = (os.getenv("AGRICONNECT_RAG_PRIMARY_BACKEND", "pgvector") or "pgvector").strip().lower()
        self._prefer_pg_only = self._primary_backend in {"pg", "postgres", "postgresql", "pgvector"}

        # Prefer direct Postgres retrieval and skip legacy vector-store init to
        # avoid Redis/FAISS connection noise on MCP startup.
        if self._prefer_pg_only:
            self.index = None
            self.vector_retriever = None
            self.vector_store = None
            self.ready = True
            self.llm = get_groq_sdk()
            return

        storage_context = get_storage_context()
        # Load from persistence
        try:
            structs = storage_context.index_store.index_structs()
            if not structs:
                raise ValueError("No index found in storage.")

            if hasattr(storage_context.vector_store, "client"):
                client = storage_context.vector_store.client
                # Guard access to optional faiss attribute `ntotal` which Redis clients
                # do not provide (avoid AttributeError during logging).
                try:
                    ntotal = getattr(client, "ntotal", None)
                    if ntotal is not None:
                        logger.info("FAISS #total before load: %d", ntotal)
                    else:
                        logger.info("Vector store client present (ntotal unknown)")
                except Exception:
                    logger.info("Vector store client present (unable to read ntotal)")

            # Default to the first index found
            target_index_id = structs[0].index_id

            self.index = load_index_from_storage(storage_context, index_id=target_index_id)
            if hasattr(self.index, "_vector_store") and hasattr(self.index._vector_store, "client"):
                try:
                    ntotal = getattr(self.index._vector_store.client, "ntotal", None)
                    if ntotal is not None:
                        logger.info("FAISS ntotal loaded: %d", ntotal)
                    else:
                        logger.info("Index loaded; vector store client present (ntotal unknown)")
                except Exception:
                    logger.info("Index loaded; unable to determine vector store ntotal")
            # Default retriever (overridden per-search)
            self.vector_retriever = self.index.as_retriever(similarity_top_k=TOP_K_RETRIEVAL)
        except Exception as e:
            logger.warning("Error loading index: %s", e)
            self.index = None
            self.vector_retriever = None
            self.vector_store = None
        finally:
            # expose underlying vector_store (Redis/Faiss) for fallbacks
            try:
                self.vector_store = storage_context.vector_store
            except Exception:
                self.vector_store = None
        try:
            if self.vector_retriever is None:
                if self.vector_store is not None and hasattr(self.vector_store, "query"):
                    if hasattr(self.vector_store, "ready"):
                        self.ready = bool(getattr(self.vector_store, "ready"))
                    else:
                        self.ready = True
                else:
                    self.ready = False
            elif self.vector_store is not None and hasattr(self.vector_store, "ready"):
                self.ready = bool(getattr(self.vector_store, "ready"))
            else:
                self.ready = True
        except Exception:
            self.ready = self.vector_retriever is not None
        self.llm = get_groq_sdk()

    def _embed_query(self, query_str: str):
        """Compute query embedding with the already-configured embed model first.

        This avoids reloading sentence-transformers on every request when the
        main index is unavailable.
        """
        try:
            from llama_index.core import Settings

            embed_model = getattr(Settings, "embed_model", None)
            if embed_model is not None and hasattr(embed_model, "get_query_embedding"):
                vec = embed_model.get_query_embedding(query_str)
                if vec is not None:
                    return list(vec)
        except Exception:
            logger.exception("Primary embedding computation failed")

        try:
            from sentence_transformers import SentenceTransformer

            # Use all-mpnet-base-v2 to match ingestion embedding dimension (768)
            if self._fallback_embedder is None:
                self._fallback_embedder = SentenceTransformer("all-mpnet-base-v2")
            return self._fallback_embedder.encode([query_str])[0].tolist()
        except Exception:
            logger.exception("Fallback embedding computation failed")
            return None

    def generate_hyde_doc(self, query_str: str, tone: str = "standard") -> str:
        """
        HyDe (Hypothetical Document Embeddings):
        Génère un faux document qui répond à la question, puis cherche ce document.
        Le ton s'adapte au profil utilisateur.
        """
        if tone == "technique":
            persona = (
                "Tu es un agronome chercheur spécialisé Sahel/Burkina Faso. "
                "Rédige un paragraphe technique détaillé avec termes scientifiques, "
                "noms latins des pathogènes, doses précises, et références."
            )
        elif tone == "simple":
            persona = (
                "Tu es un conseiller agricole de village. "
                "Rédige un court paragraphe avec des mots simples et des exemples concrets."
            )
        else:
            persona = (
                "Tu es un expert agricole. "
                "Rédige un paragraphe technique qui répond à cette question."
            )

        try:
            prompt = f"{persona}\nQuestion : '{query_str}'"
            chat_completion = self.llm.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model="llama-3.1-8b-instant",
            )
            hypothetical_doc = chat_completion.choices[0].message.content or query_str
            return hypothetical_doc
        except Exception as e:
            logger.warning("HyDe generation warning: %s", e)
            return query_str

    def rerank(self, query: str, nodes: List[NodeWithScore], top_k: int = TOP_K_RERANK) -> List[NodeWithScore]:
        """
        Re-rank retrieved nodes using a CrossEncoder for higher precision.
        Le nombre de résultats dépend du profil (debutant=3, expert=8).
        """
        global RERANKER
        # Lazy initialize CrossEncoder to avoid heavy model downloads during module import
        if RERANKER is None:
            try:
                from sentence_transformers import CrossEncoder

                RERANKER = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
            except Exception:
                RERANKER = None

        if not RERANKER or not nodes:
            return nodes[:top_k]
            
        texts = [n.node.get_content() for n in nodes]
        inputs = [[query, text] for text in texts]
        scores = RERANKER.predict(inputs)

        # Normalize scores to a 0-10 scale for downstream consumers
        try:
            min_s = float(min(scores))
            max_s = float(max(scores))
        except Exception:
            min_s = 0.0
            max_s = 0.0

        normalized = []
        for s in scores:
            try:
                raw = float(s)
            except Exception:
                raw = 0.0
            if max_s > min_s:
                scaled = (raw - min_s) / (max_s - min_s)
            else:
                # fallback when all scores identical
                scaled = 1.0 if raw > 0 else 0.0
            # map to 0-10 integer
            norm10 = max(0.0, min(10.0, scaled * 10.0))
            normalized.append(norm10)

        for i, node in enumerate(nodes):
            node.score = float(normalized[i])

        # Re-sort by relevance (higher first)
        nodes.sort(key=lambda x: x.score if x.score is not None else 0.0, reverse=True)
        return nodes[:top_k]

    def _pg_retrieve(self, query_str: str, top_k: int = 5):
        """Fallback retrieval using Postgres + pgvector directly.

        Returns a list of SimpleNode-compatible objects with `.node.get_content()` and `.score`.
        """
        if not self._pgvector_available:
            return []

        try:
            q_emb = self._embed_query(query_str)
            if q_emb is None:
                return []

            # Build vector literal for psycopg2 '%s::vector' usage
            vec_lit = '[' + ','.join(f"{float(v):.8f}" for v in q_emb) + ']'

            from agriconnect.infrastructure.database import db as _db
            from sqlalchemy import text

            engine = _db.get_engine()
            sql = (
                "SELECT content, metadata, 1 - (embedding <=> %s::vector) AS similarity "
                "FROM public.document_chunks "
                "WHERE embedding IS NOT NULL "
                "ORDER BY similarity DESC "
                "LIMIT %s"
            )

            rows = []
            # Use raw DBAPI connection for stable parameter passing (psycopg2)
            conn = engine.raw_connection()
            try:
                cur = conn.cursor()
                cur.execute(sql, (vec_lit, top_k))
                rows = cur.fetchall()
                cur.close()
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

            out_nodes = []
            for r in rows:
                try:
                    content, metadata, similarity = r
                except Exception:
                    continue

                # Ensure similarity is numeric
                try:
                    sim = float(similarity or 0.0)
                except Exception:
                    sim = 0.0

                class SimpleNode:
                    def __init__(self, text, meta, score):
                        class N:
                            def __init__(self, text, meta):
                                self._text = text or ""
                                self.metadata = meta or {}
                            def get_content(self):
                                return self._text
                        self.node = N(text, meta)
                        self.score = float(score or 0.0)

                out_nodes.append(SimpleNode(content, metadata or {}, sim))

            return out_nodes
        except Exception as exc:
            # Common in local/dev environments: the pgvector tables haven't been
            # migrated/created yet. Treat as a non-fatal degraded mode.
            pgcode = getattr(exc, "pgcode", None)
            cls_name = exc.__class__.__name__
            if pgcode == "42P01" or cls_name == "UndefinedTable":
                self._pgvector_available = False
                if self._pgvector_disabled_reason is None:
                    self._pgvector_disabled_reason = "missing_tables"
                    logger.warning("pgvector tables missing; disabling Postgres RAG retrieval")
                return []

            logger.warning("_pg_retrieve failed: %s", exc)
            return []

    def search(
        self,
        query_str: str,
        user_level: str = "debutant",
        use_hyde: Optional[bool] = None,
    ) -> List[NodeWithScore]:
        """
        Recherche adaptative selon le profil utilisateur.

        - debutant  : pas de HyDe (rapide ~0.5s), top_k=5, rerank_k=3
        - intermediaire : HyDe activé, top_k=10, rerank_k=5
        - expert    : HyDe technique, top_k=20, rerank_k=8 (précision max)
        """
        if not self.vector_retriever:
            logger.info("Index not initialized.")
            # Fallbacks: try Postgres pgvector first, then Redis/Faiss vector_store
            try:
                # Charger le profil adapté (needed for fallback limits)
                profile = get_rag_profile(user_level)

                # Try Postgres pgvector retrieval if available
                pg_nodes = self._pg_retrieve(query_str, profile.top_k)
                if pg_nodes:
                    return pg_nodes[: profile.rerank_k]

                if self.vector_store is not None and hasattr(self.vector_store, "query"):
                    if self._disable_vector_store_fallback:
                        return []
                    if hasattr(self.vector_store, "ready") and not bool(getattr(self.vector_store, "ready")):
                        return []
                    # compute embedding for the query
                    q_emb = self._embed_query(query_str)
                    if q_emb is None:
                        logger.exception("Could not compute query embedding for fallback; returning empty.")
                        return []

                    raw = None
                    query_error = None

                    def _do_query():
                        nonlocal raw, query_error
                        try:
                            raw = self.vector_store.query(q_emb, k=profile.top_k)
                        except Exception as exc:
                            query_error = exc

                    t = threading.Thread(target=_do_query, name="rag-vector-fallback", daemon=True)
                    t.start()
                    t.join(timeout=self._vector_store_query_timeout_s)
                    if t.is_alive():
                        self._disable_vector_store_fallback = True
                        logger.warning(
                            "vector_store fallback query timed out after %.1fs; disabling fallback for this process",
                            self._vector_store_query_timeout_s,
                        )
                        return []
                    if query_error is not None:
                        self._disable_vector_store_fallback = True
                        logger.exception("vector_store fallback query failed; disabling fallback for this process")
                        return []
                    # adapt raw results (which can be dicts or objects) into
                    # simple wrappers with `.node.get_content()` and numeric `.score`.
                    out_nodes = []

                    def _coerce_score(s):
                        # Try common representations, return float or 0.0
                        try:
                            if s is None:
                                return 0.0
                            if isinstance(s, (int, float)):
                                return float(s)
                            # Some vector stores return small wrappers (e.g., VectorStoreQuery)
                            # Try common attributes
                            if hasattr(s, "score"):
                                return float(getattr(s, "score") or 0.0)
                            if hasattr(s, "value"):
                                return float(getattr(s, "value") or 0.0)
                            if hasattr(s, "distance"):
                                # distance may be inverse of score; keep as float
                                return float(getattr(s, "distance") or 0.0)
                            # Last resort: try converting to float from string
                            return float(str(s))
                        except Exception:
                            return 0.0

                    # Convert VectorStoreQueryResult to iterable if needed
                    if hasattr(raw, "nodes") and hasattr(raw, "similarities"):
                        try:
                            _raw_list = []
                            _nodes = getattr(raw, "nodes", []) or []
                            _scores = getattr(raw, "similarities", []) or []
                            for i, node in enumerate(_nodes):
                                score = _scores[i] if i < len(_scores) else 0.0
                                # Mock object with score, compatible with loop below
                                class _WrappedResult:
                                    def __init__(self, n, s):
                                        self.node = n
                                        self.score = s
                                _raw_list.append(_WrappedResult(node, score))
                            raw = _raw_list
                        except Exception:
                            pass

                    for r in raw:
                        # dict-like
                        if isinstance(r, dict):
                            text = r.get("text") or r.get("content") or ""
                            meta = r.get("meta") or r.get("metadata") or {}
                            score = _coerce_score(r.get("score") or r.get("score", None))
                        else:
                            # object-like
                            # Try to extract text from node or attributes
                            meta = {}
                            text = ""
                            score = 0.0
                            # id / metadata
                            if hasattr(r, "node") and hasattr(r.node, "get_content"):
                                try:
                                    text = r.node.get_content()
                                except Exception:
                                    text = ""
                                meta = getattr(r.node, "metadata", {}) or {}
                            else:
                                # direct content methods/attrs
                                if hasattr(r, "get_content"):
                                    try:
                                        text = r.get_content()
                                    except Exception:
                                        text = ""
                                else:
                                    text = str(getattr(r, "text", ""))
                                meta = getattr(r, "metadata", {}) or getattr(r, "meta", {}) or {}
                            score = _coerce_score(getattr(r, "score", None))

                        class SimpleNode:
                            def __init__(self, text, meta, score):
                                class N:
                                    def __init__(self, text, meta):
                                        self._text = text
                                        self.metadata = meta
                                    def get_content(self):
                                        return self._text
                                self.node = N(text, meta)
                                self.score = float(score or 0.0)

                        out_nodes.append(SimpleNode(text, meta, score))
                    return out_nodes[:profile.rerank_k]
            except Exception:
                logger.exception("Fallback vector_store query failed")
            return []

        # Charger le profil adapté
        profile = get_rag_profile(user_level)
        should_hyde = use_hyde if use_hyde is not None else profile.use_hyde

        logger.info(
            "[RAG] Profil=%s | top_k=%d | rerank_k=%d | hyde=%s",
            user_level, profile.top_k, profile.rerank_k, should_hyde,
        )

        # Adapter le retriever au top_k du profil
        search_query = query_str
        if should_hyde:
            hypo_doc = self.generate_hyde_doc(query_str, tone=profile.tone)
            logger.info("[HyDe] Generated hypothetical doc (%d chars, tone=%s)", len(hypo_doc), profile.tone)
            search_query = f"{query_str}\n{hypo_doc}"
        # Primary retrieval: try Postgres pgvector first (preferred)
        try:
            pg_nodes = self._pg_retrieve(search_query, profile.top_k)
            if pg_nodes:
                logger.info("[Retriever][PG] Found %d nodes via Postgres pgvector.", len(pg_nodes))
                final_nodes = self.rerank(query_str, pg_nodes, top_k=profile.rerank_k)
                return final_nodes
        except Exception:
            logger.exception("Primary Postgres retrieval failed; falling back to index/vector store")

        # If Postgres returned nothing, fall back to LlamaIndex vector index if available
        retriever = None
        try:
            if self.index is not None:
                retriever = self.index.as_retriever(similarity_top_k=profile.top_k)
        except Exception:
            logger.exception("Failed to build index retriever; will attempt fallbacks")

        if retriever is not None:
            nodes = retriever.retrieve(search_query)
            logger.info("[Retriever] Found %d raw nodes.", len(nodes))
        else:
            nodes = []
        
        # Rerank using ORIGINAL query, with profile-specific top_k
        final_nodes = self.rerank(query_str, nodes, top_k=profile.rerank_k)
        return final_nodes

    def search_memory(self, user_id: str, query: str, top_k: int = 3):
        """Search lightweight episodic memory stored as JSON in backend/rag_db/memory.json.

        Returns a list of dicts with keys: id, text, category, score
        """
        try:
            base = Path(__file__).resolve().parents[3]
            mem_file = base / "rag_db" / "memory.json"
            if not mem_file.exists():
                return []

            with mem_file.open("r", encoding="utf-8") as fh:
                entries = json.load(fh)

            # entries expected format: list of {"id":..., "user_id":..., "text":..., "category":...}
            results = []
            q = query.lower()
            for e in entries:
                if user_id and e.get("user_id") != user_id:
                    continue
                text = str(e.get("text", ""))
                score = 1.0 if q in text.lower() else 0.0
                if score > 0 or q in e.get("category", "").lower():
                    results.append({
                        "id": e.get("id"),
                        "text": text,
                        "category": e.get("category"),
                        "score": score,
                    })
                if len(results) >= top_k:
                    break

            return results
        except Exception as exc:
            logger.exception("search_memory failed: %s", exc)
            return []
