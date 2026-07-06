from typing import List

from llama_index.core.schema import Document as LlamaDocument
from llama_index.core.node_parser import MarkdownNodeParser

from agriconnect.core.schemas import RawDocument, DocumentChunk
from futur.ingestion.processors.base_processor import BaseProcessor


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

    def process(self, document: RawDocument) -> List[DocumentChunk]:
        return super().process(document)

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
        chunk_index = 0
        for node in nodes:
            node_text = (getattr(node, "text", None) or "").strip()
            if not node_text:
                continue

            node_meta = getattr(node, "metadata", {}) or {}
            section = node_meta.get("Header 1") or "general"
            chunk = self._build_chunk(
                document=document,
                text_content=node_text,
                chunk_index=chunk_index,
                extra_metadata={"section": str(section), **node_meta},
            )
            chunks.append(chunk)
            chunk_index += 1

        return chunks
