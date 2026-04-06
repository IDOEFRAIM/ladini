from typing import List
import hashlib
from llama_index.core.node_parser import SentenceSplitter

from agriconnect.core.schemas import RawDocument, DocumentChunk
from agriconnect.domain.ingestion.processors.base_processor import BaseProcessor

class WeatherProcessor(BaseProcessor):
    """
    Processor for Weather Data (often JSON or tabular text).
    Focuses on 'Region + Forecast' context.
    DO NOT split aggressively; keep short forecasts as single chunks.
    """

    def __init__(self, chunk_size: int = 512, chunk_overlap: int = 50):
        # Weather updates are usually short, chunk size can be smaller or match standard
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def clean(self, document: RawDocument) -> RawDocument:
        # Assuming content is already formatted text from JSON collector
        # Remove technical keys if present as text
        # e.g. "coord: {lat: ...}" -> "Coordinates: ..."
        return document

    def chunk(self, document: RawDocument) -> List[DocumentChunk]:
        # Weather is highly structured.
        # Often, we want 1 chunk per region or per day.
        # For now, treat as single block unless very long.
        
        full_text = f"REGION: {document.metadata.get('region', 'Unknown')}\nDATE: {document.metadata.get('date', 'Unknown')}\n\n{document.content_markdown}"
        
        if len(full_text) < self.chunk_size:
            # Single chunk is best
            nodes = [full_text]
        else:
            splitter = SentenceSplitter(chunk_size=self.chunk_size, chunk_overlap=self.chunk_overlap)
            nodes = splitter.split_text(full_text)
        
        chunks = []
        for i, node_text in enumerate(nodes):
            content_hash = hashlib.sha256(node_text.encode("utf-8")).hexdigest()
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
                    "is_weather": True
                }
            )
            chunks.append(chunk)

        return chunks
