from abc import ABC, abstractmethod
from typing import List

from agriconnect.core.schemas import RawDocument, DocumentChunk


class BaseProcessor(ABC):
    """
    Abstract base for ingestion processors.

    Contract (scraper-centric):
    - Input: `RawDocument` where `content_markdown` is already populated by the
      Scraper/Orchestrator and contains Markdown-only text.
    - Output: List[DocumentChunk]

    Responsibilities:
    - `clean()` : lightweight normalization on already-extracted Markdown
    - `chunk()` : semantic splitting into `DocumentChunk`s

    The public entrypoint is `process()` which MUST call `clean()` then `chunk()`.
    """

    def process(self, document: RawDocument) -> List[DocumentChunk]:
        """Public entrypoint used by the ingestion worker.

        Ensures the scraper-centric contract: `content_markdown` must be present.
        Calls `clean()` then `chunk()` and returns a list of DocumentChunk instances.
        """
        if document is None:
            return []

        # Ensure we operate on markdown-only text
        if not getattr(document, "content_markdown", None):
            # nothing to do
            return []

        cleaned_doc = self.clean(document)
        chunks = self.chunk(cleaned_doc) or []
        return chunks

    @abstractmethod
    def clean(self, document: RawDocument) -> RawDocument:
        """Perform lightweight deterministic cleanup on `content_markdown`.

        Must NOT attempt to parse or clean HTML — the scraper provides Markdown.
        """
        raise NotImplementedError

    @abstractmethod
    def chunk(self, document: RawDocument) -> List[DocumentChunk]:
        """Split cleaned document into a list of DocumentChunk objects."""
        raise NotImplementedError
