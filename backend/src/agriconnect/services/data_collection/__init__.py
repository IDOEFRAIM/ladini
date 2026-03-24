"""
Data Collection Module

Services de collecte et scraping de données externes.

Modules:
- weather: Weather data collection (forecasts, observations)
- documents: Document scraping (PDF, HTML, etc.)
- market: Market price collection and signal extraction

Architecture:
- Tous les collecteurs implémentent une interface commune
- Configuration centralisée dans scraper/config.py
- Error handling robuste avec retry et circuit breaker
"""

__version__ = "1.0.0"

__all__ = [
    "weather",
    "documents",
    "market",
]
