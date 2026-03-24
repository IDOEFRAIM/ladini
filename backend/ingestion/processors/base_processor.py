from abc import ABC, abstractmethod
from typing import List, Dict, Any
from backend.ingestion.schema import RawDocument, DocumentChunk

class BaseProcessor(ABC):
    """
    Abstract Base Class for all Ingestion Processors.
    Each processor must implement:
    1. clean(): Normalize text, remove boilerplate.
    2. chunk(): Split text into semantically meaningful chunks.
    """

    @abstractmethod
    def clean(self, document: RawDocument) -> RawDocument:
        """
        Cleans and normalizes the raw content of the document.
        """
        pass

    @abstractmethod
    def chunk(self, document: RawDocument) -> List[DocumentChunk]:
        """
        Splits the cleaned document into chunks ready for embedding.
        """
        pass
