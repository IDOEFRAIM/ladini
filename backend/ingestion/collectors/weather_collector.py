"""
Collector for Weather Data.
Wraps the legacy WeatherCollector service to produce standardized RawDocument objects.
"""
from typing import List, Dict, Any
import logging
import json
import hashlib
from datetime import datetime, timezone

from backend.ingestion.schema import RawDocument
from agriconnect.services.data_collection.weather.weather_collector import WeatherCollector as LegacyWeatherCollector

logger = logging.getLogger(__name__)

class WeatherCollector:
    """Wrapper that adapts the legacy weather service to the new pipeline."""

    def __init__(self):
        self.legacy_collector = LegacyWeatherCollector()

    def run(self) -> List[Dict[str, Any]]:
        """
        Executes the weather collection logic and returns a list of RawDocuments.
        """
        raw_docs = []
        try:
            logger.info("Starting weather collection via legacy service...")
            weather_data = self.legacy_collector.run()
            logger.info(f"Legacy run() returned type={type(weather_data)}")
            
            # If default implementation returns an integer count or None
            # We assume no data was returned or we need alternative method.
            if isinstance(weather_data, int):
                 logger.warning(f"Legacy collector returned integer count ({weather_data}) instead of data dict. Skipping.")
                 return []
            
            if not isinstance(weather_data, dict):
                 logger.warning(f"Unexpected data type from legacy collector: {type(weather_data)}. Skipping.")
                 return []
            
            # The legacy run() returns a large dictionary with 'advisory_paths', 'alerts', etc.
            # We treat this entire structure as one "Bulletin" document for now.
            # Debug keys
            logger.info(f"Weather Data Keys: {list(weather_data.keys())}")
            
            advisory_paths = weather_data.get("advisory_paths", [])
            if isinstance(advisory_paths, int):
                # Maybe it returned a count instead of list?
                zone_count = advisory_paths
            elif isinstance(advisory_paths, list):
                zone_count = len(advisory_paths)
            else:
                zone_count = 0

            content_str = json.dumps(weather_data, default=str)
            content_hash = hashlib.sha256(content_str.encode("utf-8")).hexdigest()
            
            # Create standardized document
            doc = RawDocument(
                source_id=f"weather-{content_hash[:10]}",
                source_type="weather_bulletin",
                title=f"Weather Bulletin - {datetime.now().strftime('%Y-%m-%d')}",
                url="api:weather",  # Virtual URL
                content=content_str,
                metadata={
                    "zone_count": zone_count,
                    "alert_count": len(weather_data.get("alerts", [])) if isinstance(weather_data.get("alerts"), list) else 0,
                    "generated_at": datetime.now(timezone.utc).isoformat()
                }
            )
            raw_docs.append(doc.model_dump())
            logger.info("Weather collection complete. Generated 1 summary document.")
            
        except Exception as e:
            logger.error(f"Weather collection failed: {e}")
            # We swallow exception here to allow pipeline to continue with other sources?
            # Or re-raise? Default to swallow and log for now as per other collectors.
        
        return raw_docs