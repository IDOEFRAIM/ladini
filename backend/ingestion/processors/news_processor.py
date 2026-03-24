from typing import List, Dict, Any
import hashlib
import uuid
import re

from llama_index.core.node_parser import SentenceSplitter

from backend.ingestion.schema import RawDocument, DocumentChunk
from backend.ingestion.processors.base_processor import BaseProcessor

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
        # News pages often have "Read more", "Subscribe now", ads
        text = document.content
        text = re.sub(r'Subscribe.*', '', text, flags=re.IGNORECASE)
        text = re.sub(r'Read more.*', '', text, flags=re.IGNORECASE)
        document.content = text.strip()
        return document

    def chunk(self, document: RawDocument) -> List[DocumentChunk]:
        # News is often short enough to fit in 1-2 chunks.
        # Ensure Title is prepended to each chunk for context
        full_text = f"TITLE: {document.title}\n\n{document.content}"
        
        splitter = SentenceSplitter(chunk_size=self.chunk_size, chunk_overlap=self.chunk_overlap)
        nodes = splitter.split_text(full_text)
        
        chunks = []
        for i, node_text in enumerate(nodes):
            chunk_id = str(uuid.uuid4())
            content_hash = hashlib.md5(node_text.encode('utf-8')).hexdigest()
            
            chunk = DocumentChunk(
                chunk_id=chunk_id,
                parent_doc_source_id=document.source_id,
                chunk_index=i,
                text_content=node_text,
                content_hash=content_hash,
                metadata={
                    **document.metadata,
                    "is_news": True
                }
            )
            chunks.append(chunk)

        return chunks
