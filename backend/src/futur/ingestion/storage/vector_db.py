"""
Vector Database Storage Manager for Redis (wrapper for LlamaIndex/Custom Store).
Adapts ingested schemas to the underlying RAG store.
"""
import logging
import os
from typing import List
from datetime import datetime

from llama_index.core.schema import TextNode

from futur.rag.components import get_vector_store
from agriconnect.core.schemas import DocumentChunk

logger = logging.getLogger(__name__)

class VectorDBManager:
    """
    Persists document chunks to the active Vector Store (Redis).
    """

    def __init__(self):
        # Obtain the configured store (Redis, FAISS, etc.)
        self.store = get_vector_store()
        self.batch_size = max(1, int(os.getenv("INGESTION_VALKEY_BATCH_SIZE", "50") or "50"))
        if not self.store:
            raise RuntimeError("No vector store configured available.")

    def upsert_chunks(self, chunks: List[DocumentChunk]):
        """
        Push a batch of chunks to the vector store.
        """
        if not chunks:
            return

        nodes = []
        for chunk in chunks:
            if not chunk.embedding:
                logger.warning(f"Chunk {chunk.chunk_id} missing embedding. Skipping.")
                continue

            # Enrich metadata for filtering
            # Standardize date format if possible
            meta = chunk.metadata.copy()
            meta["ingested_at"] = datetime.now().isoformat()
            meta["source_id"] = chunk.parent_doc_id
            meta["content_hash"] = chunk.content_hash
            
            # Create LlamaIndex TextNode
            # This is the standard exchange format for most stores in LlamaIndex ecosystem
            node = TextNode(
                id_=chunk.chunk_id,
                text=chunk.text_content,
                embedding=chunk.embedding,
                metadata=meta,
                excluded_embed_metadata_keys=["content_hash", "ingested_at"], # don't embed technical keys
                excluded_llm_metadata_keys=["content_hash", "chunk_id"],       # keep clean context for LLM
            )
            nodes.append(node)

        if not nodes:
            return

        try:
            if not hasattr(self.store, "add"):
                logger.error(f"Vector Store {type(self.store)} has no 'add' method.")
                return

            # LlamaIndex-native stores generally expose add(nodes).
            try:
                self.store.add(nodes)
                logger.info(f"Upserted {len(nodes)} chunks to vector store.")
                return
            except TypeError:
                # Custom Redis stores in this repo expose add(doc_id, text, meta, embedding).
                pass

            if hasattr(self.store, "add_many"):
                self.store.add_many(
                    [
                        {
                            "id": node.id_,
                            "text": node.text,
                            "meta": node.metadata or {},
                            "embedding": node.embedding,
                        }
                        for node in nodes
                    ],
                    batch_size=self.batch_size,
                )
                logger.info("Upserted %s chunks to vector store (batch=%s).", len(nodes), self.batch_size)
                return

            for node in nodes:
                self.store.add(
                    node.id_,
                    node.text,
                    node.metadata or {},
                    node.embedding,
                )
            logger.info(f"Upserted {len(nodes)} chunks to vector store.")

        except Exception as e:
            # Log the error but do not raise to avoid DLQing otherwise valid ingestions
            # when the vector store is temporarily unavailable (network/tunnel/credentials).
            logger.error(f"Failed to upsert chunks to vector store: {e}")
            logger.warning("Proceeding without vector upsert; check vector store connectivity.")
            return

    def delete_by_parent(self, parent_doc_id: str) -> int:
        """
        Delete all vectors belonging to a given parent_doc_id (source_id).
        Returns number of deleted items. Best-effort: attempts store-specific APIs
        and falls back to scanning stored metadata when possible.
        """
        if not parent_doc_id:
            return 0

        deleted = 0
        # Try store-provided helper first
        try:
            if hasattr(self.store, "delete_by_parent"):
                deleted = self.store.delete_by_parent(parent_doc_id)
                logger.info("Deleted %s vectors by parent=%s via store.delete_by_parent", deleted, parent_doc_id)
                return int(deleted or 0)
        except Exception as e:
            logger.debug("store.delete_by_parent failed: %s", e)

        # RedisVectorStore fallback: inspect stored ids and metadata
        try:
            if hasattr(self.store, "_docs_key") and hasattr(self.store, "_doc_key") and hasattr(self.store, "client"):
                ids = [i.decode("utf-8") if isinstance(i, bytes) else i for i in self.store.client.smembers(self.store._docs_key)]
                to_delete = []
                p = self.store.client.pipeline()
                for doc_id in ids:
                    p.hget(self.store._doc_key(doc_id), "meta")
                metas = p.execute()
                for idx, meta_b in enumerate(metas):
                    try:
                        meta = json.loads(meta_b.decode("utf-8")) if isinstance(meta_b, bytes) else (json.loads(meta_b) if meta_b else {})
                    except Exception:
                        meta = {}
                    if meta.get("source_id") == parent_doc_id or meta.get("ingested_from") == parent_doc_id:
                        to_delete.append(ids[idx])

                for doc_id in to_delete:
                    try:
                        if hasattr(self.store, "delete"):
                            self.store.delete(doc_id)
                        else:
                            # best-effort: try remove/add APIs
                            if hasattr(self.store, "remove"):
                                self.store.remove(doc_id)
                        deleted += 1
                    except Exception:
                        logger.debug("Failed to delete vector %s for parent %s", doc_id, parent_doc_id)

                logger.info("Deleted %s vectors for parent_doc_id=%s via Redis fallback", deleted, parent_doc_id)
                return int(deleted)
        except Exception as e:
            logger.debug("Redis fallback deletion failed: %s", e)

        # Generic store: try delete_many or clear by ids if supported
        try:
            if hasattr(self.store, "delete_many"):
                # Not all stores support filtering by metadata; operator must provide ids
                logger.info("Vector store supports delete_many but no parent-based API available.")
        except Exception:
            pass

        logger.warning("Unable to perform delete_by_parent for store type=%s; manual cleanup may be required", type(self.store))
        return int(deleted)
