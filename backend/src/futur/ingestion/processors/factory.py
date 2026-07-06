from typing import Dict, Tuple, Type

from agriconnect.core.schemas import RawDocument
from futur.ingestion.processors.base_processor import BaseProcessor
from futur.ingestion.processors.pdf_processor import PDFProcessor
from futur.ingestion.processors.news_processor import NewsProcessor
from futur.ingestion.processors.fews_processor import FEWSProcessor
from futur.ingestion.processors.weather_processor import WeatherProcessor


class UnknownSourceTypeError(ValueError):
    """Raised when no processor mapping exists for a source type/engine."""

# Registry mapping source_type / orchestrator engine to processors.
PROCESSOR_REGISTRY: Dict[str, Type[BaseProcessor]] = {
    # Canonical source types (should match orchestrator `source_type` values)
    "institutional_pdf": PDFProcessor,
    "news_article": NewsProcessor,
    "fews_report": FEWSProcessor,
    "weather_bulletin": WeatherProcessor,
    "technical_resource": PDFProcessor,

    # Common legacy aliases (kept for backward compatibility)
    "pdf": PDFProcessor,
    "news": NewsProcessor,
    "technical": PDFProcessor,
}


def get_processor(source_type: str) -> BaseProcessor:
    """Instantiate the processor class for a given source_type key.

    The `source_type` should be provided by the orchestrator metadata and
    must match one of the keys in `PROCESSOR_REGISTRY`.

    Unknown keys fail fast to avoid silently polluting the knowledge base with
    wrong segmentation behaviour.
    """
    key = (source_type or "").strip().lower()
    processor_cls = PROCESSOR_REGISTRY.get(key)
    if processor_cls is None:
        raise UnknownSourceTypeError(
            f"Unknown source_type '{source_type}'. Register it in PROCESSOR_REGISTRY before ingestion."
        )
    return processor_cls()


def resolve_processor_key_from_document(document: RawDocument) -> str:
    """Resolve processor key from a RawDocument's metadata.

    Priority:
    1. `metadata.source_type` (canonical)
    2. `metadata.source_engine` (orchestrator engine)
    3. A small set of safe legacy aliases
    4. Fail fast
    """
    metadata = getattr(document, "metadata", {}) or {}

    source_type = str(metadata.get("source_type") or "").strip().lower()
    if source_type and source_type in PROCESSOR_REGISTRY:
        return source_type

    source_engine = str(metadata.get("source_engine") or "").strip().lower()
    if source_engine and source_engine in PROCESSOR_REGISTRY:
        return source_engine

    # Lightweight alias mapping for legacy payloads
    alias_map = {
        "fao_publication": "institutional_pdf",
        "institutional_document": "institutional_pdf",
        "data_platform": "institutional_pdf",
        "fews": "fews_report",
        "weather": "weather_bulletin",
    }
    if source_type in alias_map:
        return alias_map[source_type]

    raise UnknownSourceTypeError(
        "Could not resolve processor key from document metadata. "
        f"source_type='{source_type}' source_engine='{source_engine}'"
    )


def get_processor_for_document(document: RawDocument) -> Tuple[str, BaseProcessor]:
    key = resolve_processor_key_from_document(document)
    return key, get_processor(key)
