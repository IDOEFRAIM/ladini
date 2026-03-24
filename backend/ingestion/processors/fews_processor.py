from typing import List, Dict, Any
import hashlib
import uuid
import re

from llama_index.core.node_parser import SentenceSplitter

from backend.ingestion.schema import RawDocument, DocumentChunk
from backend.ingestion.processors.base_processor import BaseProcessor

class FEWSProcessor(BaseProcessor):
    """
    Standard Processor for FEWS NET Reports.
    Handles semantic splitting by sections (Key Messages, Security, Weather).
    """

    def __init__(self, chunk_size: int = 512, chunk_overlap: int = 50):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def clean(self, document: RawDocument) -> RawDocument:
        # FEWS NET specific cleaning
        text = document.content
        # Remove disclaimer boilerplate
        text = re.sub(r'The Famine Early Warning Systems Network.*', '', text, flags=re.IGNORECASE)
        document.content = text.strip()
        return document

    def chunk(self, document: RawDocument) -> List[DocumentChunk]:
        # Semantic splitting: Try to keep sections intact if possible
        # For MVP, we stick to SentenceSplitter but with larger chunks for context
        splitter = SentenceSplitter(chunk_size=self.chunk_size, chunk_overlap=self.chunk_overlap)
        nodes = splitter.split_text(document.content)
        
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
                    "section": "general" # In v2, detect section header
                }
            )
            chunks.append(chunk)

        # TODO: Implement reliability score calculation based on specific keywords
        return chunks
