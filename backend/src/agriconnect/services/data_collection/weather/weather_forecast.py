from __future__ import annotations

"""Weather forecast service facade for orchestrators.

This wrapper keeps the legacy interface `scrape_forecast()` while delegating
collection to the service-first `WeatherCollector`.
"""

from typing import Any, Dict

from .weather_collector import WeatherCollector


class WeatherForecastService:
    """Legacy-compatible weather forecast façade."""

    def __init__(self):
        self.collector = WeatherCollector()

    def scrape_forecast(self) -> Dict[str, Any]:
        payload = self.collector.run()
        return {
            "status": "SUCCESS",
            "message": "Weather forecast collected.",
            "results": [payload],
        }


__all__ = ["WeatherForecastService"]
