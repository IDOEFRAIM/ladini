"""
Weather Data Collection & Reconciliation

Unified weather data pipeline containing:
1. WeatherCollector: Orchestrates data fetching (OpenMeteo + Bulletins) and normalization (OpenCage).
2. WeatherReconciler: Merges data sources and computes confidence scores.
3. WeatherStorage: Handles DB persistence and S3 archiving.

Modules:
- weather_collector
- reconciliation
- documents_meteo

Usage:
    from agriconnect.services.data_collection.weather.weather_collector import WeatherCollector
    
    collector = WeatherCollector()
    results = collector.run()
"""

__version__ = "2.0.0"

__all__ = [
    "weather_cron",
    "weather_forecast",
    "documents_meteo",
    "weather_collector",
]
