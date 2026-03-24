from typing import Dict, Any, Type
from backend.ingestion.processors.base_processor import BaseProcessor
from backend.ingestion.processors.pdf_processor import PDFProcessor
from backend.ingestion.processors.news_processor import NewsProcessor
from backend.ingestion.processors.fews_processor import FEWSProcessor
from backend.ingestion.processors.weather_processor import WeatherProcessor

# Registry mapping source types to their specific processor
PROCESSOR_REGISTRY: Dict[str, Type[BaseProcessor]] = {
    "pdf": PDFProcessor,
    "news_article": NewsProcessor,
    "fews_report": FEWSProcessor,
    "weather_bulletin": WeatherProcessor,
    "statistical_data": PDFProcessor, # Fallback or specialized
    "technical_resource": PDFProcessor, # Often PDFs
}

def get_processor(source_type: str) -> BaseProcessor:
    """Factory method to get the correct processor for a source type."""
    processor_class = PROCESSOR_REGISTRY.get(source_type, PDFProcessor) # Default to PDF
    return processor_class()
