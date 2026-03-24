from typing import List, Dict, Any
import hashlib
import uuid
import re

from llama_index.core.node_parser import SentenceSplitter

from backend.ingestion.schema import RawDocument, DocumentChunk
from backend.ingestion.processors.base_processor import BaseProcessor

class PDFProcessor(BaseProcessor):
    """
    Standard Processor for PDF content (already extracted as text).
    Handles cleaning of headers/footers and standard chunking.
    """

    def __init__(self, chunk_size: int = 512, chunk_overlap: int = 50):
        self.splitter = SentenceSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap
        )

    def clean(self, document: RawDocument) -> RawDocument:
        text = document.content
        # Basic cleanup: remove excessive whitespace
        text = re.sub(r'\s+', ' ', text).strip()
        # Remove common PDF artifacts (e.g. "Page 1 of 10")
        text = re.sub(r'Page \d+ of \d+', '', text)
        
        document.content = text
        return document

    def chunk(self, document: RawDocument) -> List[DocumentChunk]:
        chunks = []
        nodes = self.splitter.split_text(document.content)

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
                    "chunk_size": len(node_text)
                }
            )
            chunks.append(chunk)

        return chunks
