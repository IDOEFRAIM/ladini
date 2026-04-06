from typing import List
import hashlib

from llama_index.core.schema import Document as LlamaDocument
from llama_index.core.node_parser import MarkdownNodeParser

from agriconnect.core.schemas import RawDocument, DocumentChunk
from agriconnect.domain.ingestion.processors.base_processor import BaseProcessor


class FEWSProcessor(BaseProcessor):
    """
    Processor for FEWS NET style institutional reports.

    Responsibilities:
    - Operate only on `content_markdown` provided by the scraper.
    - Use `MarkdownNodeParser` to split by heading hierarchy.
    - Produce deterministic chunk IDs based on chunk textual content.
    """

    def __init__(self):
        self.parser = MarkdownNodeParser()

    def clean(self, document: RawDocument) -> RawDocument:
        # Operate only on Markdown provided by the scraper
        text = document.content_markdown or ""
        # Minimal normalization only; extraction cleanup belongs to scraper stage.
        document.content_markdown = text.strip()
        return document

    def chunk(self, document: RawDocument) -> List[DocumentChunk]:
        if not (document.content_markdown or "").strip():
            return []

        # Semantic markdown chunking by heading hierarchy
        llama_doc = LlamaDocument(
            text=document.content_markdown,
            metadata={
                "parent_doc_id": document.id,
                "title": document.title,
            },
        )
        nodes = self.parser.get_nodes_from_documents([llama_doc])

        chunks: List[DocumentChunk] = []
        for i, node in enumerate(nodes):
            node_text = (getattr(node, "text", None) or "").strip()
            if not node_text:
                continue

            # Deterministic content hash and content-based chunk ID.
            content_hash = hashlib.sha256(node_text.encode("utf-8")).hexdigest()
            # If page information is available either at node or document level, include it in the id
            node_meta = getattr(node, "metadata", {}) or {}
            page_info = node_meta.get("page") or (document.metadata or {}).get("page_number")
            id_components = f"{document.id}:{content_hash}"
            if page_info is not None:
                id_components = f"{id_components}:{page_info}"
            chunk_id = hashlib.sha256(id_components.encode("utf-8")).hexdigest()

            section = (getattr(node, "metadata", {}) or {}).get("Header 1") or "general"

            chunk = DocumentChunk(
                chunk_id=chunk_id,
                parent_doc_id=document.id,
                chunk_index=i,
                text_content=node_text,
                content_hash=content_hash,
                metadata={**(document.metadata or {}), "section": str(section)},
            )
            chunks.append(chunk)

        return chunks
