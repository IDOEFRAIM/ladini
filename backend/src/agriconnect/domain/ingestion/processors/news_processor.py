from typing import List
import hashlib

from llama_index.core.node_parser import SentenceSplitter

from agriconnect.core.schemas import RawDocument, DocumentChunk
from agriconnect.domain.ingestion.processors.base_processor import BaseProcessor

class NewsProcessor(BaseProcessor):
    """
    Processor for News Articles.
    Keeps articles as single chunks if short, or splits by paragraphs.
    Focuses on 'Title + Content' context.
    """

    def __init__(self, chunk_size: int = 512, chunk_overlap: int = 50):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def clean(self, document: RawDocument) -> RawDocument:
        text = (document.content_markdown or "").strip()
        title = (document.title or "").strip()
        if title and not text.startswith("# "):
            text = f"# {title}\n\n{text}"
        document.content_markdown = text
        return document

    def chunk(self, document: RawDocument) -> List[DocumentChunk]:
        # News is often short enough to fit in 1-2 chunks.
        # Ensure Title is prepended to each chunk for context
        full_text = document.content_markdown
        
        splitter = SentenceSplitter(chunk_size=self.chunk_size, chunk_overlap=self.chunk_overlap)
        nodes = splitter.split_text(full_text)
        
        chunks = []
        for i, node_text in enumerate(nodes):
            content_hash = hashlib.sha256(node_text.encode("utf-8")).hexdigest()
            # include page info if available
            page_info = (document.metadata or {}).get("page_number")
            id_components = f"{document.id}:{content_hash}"
            if page_info is not None:
                id_components = f"{id_components}:{page_info}"
            chunk_id = hashlib.sha256(id_components.encode("utf-8")).hexdigest()
            
            chunk = DocumentChunk(
                chunk_id=chunk_id,
                parent_doc_id=document.id,
                chunk_index=i,
                text_content=node_text,
                content_hash=content_hash,
                metadata={
                    **(document.metadata or {}),
                    "is_news": True
                }
            )
            chunks.append(chunk)

        return chunks
