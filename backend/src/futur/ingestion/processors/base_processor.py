from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
import hashlib
import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

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

    def _normalize_text_for_hash(self, text: str) -> str:
        return re.sub(r"\s+", " ", (text or "").strip())

    def _normalize_source_url_for_hash(self, url: str) -> str:
        raw = (url or "").strip()
        if not raw:
            return ""
        try:
            parsed = urlsplit(raw)
            scheme = parsed.scheme.lower()
            netloc = parsed.netloc.lower()
            path = parsed.path or "/"
            if path != "/":
                path = path.rstrip("/")
            filtered_query = urlencode(
                [
                    (k, v)
                    for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                    if k.lower() not in {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid"}
                ],
                doseq=True,
            )
            return urlunsplit((scheme, netloc, path, filtered_query, ""))
        except Exception:
            return raw.rstrip("/")

    def _compute_content_hash(self, text: str, source_url: str) -> str:
        normalized_text = self._normalize_text_for_hash(text)
        normalized_url = self._normalize_source_url_for_hash(source_url)
        payload = f"{normalized_url}\n{normalized_text}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _build_chunk_id(self, parent_doc_id: str, content_hash: str, chunk_index: int) -> str:
        payload = f"{parent_doc_id}:{content_hash}:{int(chunk_index)}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _generate_metadata(self, document: RawDocument, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        base_meta: Dict[str, Any] = dict(document.metadata or {})
        source_url = str(base_meta.get("source_url") or document.url)
        scraper_name = str(base_meta.get("scraper_name") or base_meta.get("source_engine") or "unknown")

        normalized = {
            **base_meta,
            "source_url": source_url,
            "source_id": base_meta.get("source_id"),
            "source_type": base_meta.get("source_type"),
            "source_engine": base_meta.get("source_engine"),
            "scraper_name": scraper_name,
            "scraper_version": str(base_meta.get("scraper_version") or "unknown"),
            "collected_at": base_meta.get("collected_at"),
            "document_id": document.id,
            "document_title": document.title,
            "ingestion_contract_version": str(base_meta.get("ingestion_contract_version") or "1.0"),
        }
        if document.language and not normalized.get("language"):
            normalized["language"] = document.language
        if extra:
            normalized.update(extra)
        return normalized

    def _build_chunk(self, document: RawDocument, text_content: str, chunk_index: int, extra_metadata: Optional[Dict[str, Any]] = None) -> DocumentChunk:
        source_url = str((document.metadata or {}).get("source_url") or document.url)
        clean_text = (text_content or "").strip()
        content_hash = self._compute_content_hash(clean_text, source_url)
        chunk_id = self._build_chunk_id(document.id, content_hash, chunk_index)
        return DocumentChunk(
            chunk_id=chunk_id,
            parent_doc_id=document.id,
            chunk_index=int(chunk_index),
            text_content=clean_text,
            content_hash=content_hash,
            metadata=self._generate_metadata(document, extra=extra_metadata),
        )

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
