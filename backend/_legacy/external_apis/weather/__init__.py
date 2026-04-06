"""
Weather APIs Integration

Interfaces pour services météorologiques externes.

Modules:
- openmeteo: Open-Meteo API for weather forecasts and historical data

Usage:
    from backend.services.external_apis.weather import OpenMeteoClient
    
    client = OpenMeteoClient()
    forecast = client.get_forecast(latitude=12.37, longitude=-1.52)
"""

__version__ = "1.0.0"

__all__ = [
    "openmeteo"
]
