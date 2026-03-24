# Ingestion Processors — The logic for cleaning and chunking diverse data sources.
# This makes ingestion "plug & play" for PDFs, News, Weather, and FEWS NET reports.

from .base_processor import BaseProcessor
from .pdf_processor import PDFProcessor
from .news_processor import NewsProcessor
from .fews_processor import FEWSProcessor
from .weather_processor import WeatherProcessor
from .factory import get_processor

__all__ = ["BaseProcessor", "get_processor", "PDFProcessor", "NewsProcessor", "FEWSProcessor", "WeatherProcessor"]
