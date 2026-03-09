import logging
from llama_index.core import VectorStoreIndex, QueryBundle, load_index_from_storage
from llama_index.core.schema import NodeWithScore
from typing import List, Optional
from .components import init_settings, get_storage_context, get_groq_sdk
from .config import TOP_K_RETRIEVAL, TOP_K_RERANK, get_rag_profile, RAGProfile
from pathlib import Path
import json

logger = logging.getLogger(__name__)

# Optional: Load Reranker if available
RERANKER = None
try:
    from sentence_transformers import CrossEncoder
    # Initialize light reranker
    RERANKER = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
except ImportError:
    pass

class AgileRetriever:
    def __init__(self):
        init_settings()
        storage_context = get_storage_context()
        # Load from persistence
        try:
            structs = storage_context.index_store.index_structs()
            if not structs:
                raise ValueError("No index found in storage.")

            if hasattr(storage_context.vector_store, "client"):
                client = storage_context.vector_store.client
                if hasattr(client, "ntotal"):
                    logger.info("FAISS #total before load: %d", client.ntotal)
                else:
                    logger.info("Vector store client present (no ntotal attribute)")

            # Default to the first index found
            target_index_id = structs[0].index_id

            self.index = load_index_from_storage(storage_context, index_id=target_index_id)
            if hasattr(self.index, "_vector_store") and hasattr(self.index._vector_store, "client"):
                logger.info("FAISS ntotal loaded: %d", self.index._vector_store.client.ntotal)
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
        self.llm = get_groq_sdk()

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
        if not RERANKER or not nodes:
            return nodes[:top_k]
            
        texts = [n.node.get_content() for n in nodes]
        inputs = [[query, text] for text in texts]
        scores = RERANKER.predict(inputs)
        
        for i, node in enumerate(nodes):
            node.score = float(scores[i])
            
        # Re-sort by relevance
        nodes.sort(key=lambda x: x.score if x.score is not None else 0, reverse=True)
        return nodes[:top_k]

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
            logger.warning("Index not initialized.")
            # Fallback: if we have a RedisSearch/Redis vector_store, query it directly
            try:
                # Charger le profil adapté (needed for fallback limits)
                profile = get_rag_profile(user_level)
                if self.vector_store is not None and hasattr(self.vector_store, "query"):
                    # compute embedding for the query
                    try:
                        from sentence_transformers import SentenceTransformer
                        embedder = SentenceTransformer("all-MiniLM-L6-v2")
                        q_emb = embedder.encode([query_str])[0].tolist()
                    except Exception:
                        logger.exception("Could not compute query embedding for fallback; returning empty.")
                        return []

                    raw = self.vector_store.query(q_emb, k=profile.top_k)
                    # adapt raw to NodeWithScore-like simple list: we'll create minimal wrappers
                    out_nodes = []
                    for r in raw:
                        # r: {id, text, meta, score} or {id, text, metadata, score}
                        text = r.get("text") or r.get("content") or ""
                        meta = r.get("meta") or r.get("metadata") or {}
                        score = r.get("score", 0.0)

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
        retriever = self.index.as_retriever(similarity_top_k=profile.top_k)

        search_query = query_str
        if should_hyde:
            hypo_doc = self.generate_hyde_doc(query_str, tone=profile.tone)
            logger.info("[HyDe] Generated hypothetical doc (%d chars, tone=%s)", len(hypo_doc), profile.tone)
            search_query = f"{query_str}\n{hypo_doc}"
            
        nodes = retriever.retrieve(search_query)
        logger.info("[Retriever] Found %d raw nodes.", len(nodes))
        
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
