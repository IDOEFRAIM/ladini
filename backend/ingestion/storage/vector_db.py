"""
Vector Database Storage Manager for Redis (wrapper for LlamaIndex/Custom Store).
Adapts ingested schemas to the underlying RAG store.
"""
import logging
from typing import List
from datetime import datetime

from llama_index.core.schema import TextNode

from agriconnect.rag.components import get_vector_store
from backend.ingestion.schema import DocumentChunk

logger = logging.getLogger(__name__)

class VectorDBManager:
    """
    Persists document chunks to the active Vector Store (Redis).
    """

    def __init__(self):
        # Obtain the configured store (Redis, FAISS, etc.)
        self.store = get_vector_store()
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
            meta["source_id"] = chunk.parent_doc_source_id
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
            # Most LlamaIndex VectorStore implementations support .add(nodes)
            # If self.store is a custom class that deviates, we might need a try-except block
            if hasattr(self.store, "add"):
                # Check signature... usually add(nodes: List[BaseNode]) -> List[str]
                self.store.add(nodes)
                logger.info(f"Upserted {len(nodes)} chunks to vector store.")
            else:
                logger.error(f"Vector Store {type(self.store)} has no 'add' method.")

        except Exception as e:
            logger.error(f"Failed to upsert chunks to vector store: {e}")
            raise
