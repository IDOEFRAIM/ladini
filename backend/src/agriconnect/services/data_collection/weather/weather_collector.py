"""
Refactored Weather Data Collection Pipeline
- Normalization: OpenCage
- Sources: OpenMeteo (API), ANAM Scraper (Unstructured)
- Storage: S3 (Raw Payloads), Postgres (Structured Observations)
- Quality: Confidence Scoring, Alert Detection
"""
import json
import logging
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
from sqlalchemy import create_engine, text

from agriconnect.core.settings import settings
from agriconnect.services.data_collection.weather.documents_meteo import DocumentScraper

# Optional boto3 import
try:
    import boto3
except ImportError:
    boto3 = None

logger = logging.getLogger(__name__)

# --- Constants ---
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
OPENCAGE_URL = "https://api.opencagedata.com/geocode/v1/json"
DEFAULT_ZONES = {
    "Bobo-Dioulasso": (11.1771, -4.2979),
    "Ouagadougou": (12.3714, -1.5197),
    "Koudougou": (12.2526, -2.3627),
    "Fada N'Gourma": (12.0616, 0.3584),
    "Dori": (14.0354, -0.0345),
}


@dataclass
class WeatherSignal:
    zone_name: str
    latitude: float
    longitude: float
    opencage_place_id: Optional[str]
    opencage_formatted: Optional[str]
    observed_at: datetime
    forecast_date: datetime
    temperature_c: Optional[float]
    precipitation_mm: Optional[float]
    humidity_pct: Optional[float]
    source: str
    source_type: str
    confidence_score: float
    raw_payload_link: Optional[str]
    bulletin_excerpt: str


class LocationService:
    """Handles geocoding and location normalization using OpenCage."""
    
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("OPENCAGE_API_KEY", "").strip()
        self._cache: Dict[str, Dict[str, Any]] = {}

    def normalize(self, zone_name: str, fallback_lat: float, fallback_lon: float) -> Dict[str, Any]:
        if zone_name in self._cache:
            return self._cache[zone_name]

        normalization = {
            "latitude": fallback_lat,
            "longitude": fallback_lon,
            "opencage_place_id": None,
            "opencage_formatted": zone_name,
        }

        if not self.api_key:
            self._cache[zone_name] = normalization
            return normalization

        try:
            resp = requests.get(
                OPENCAGE_URL,
                params={"q": f"{zone_name}, Burkina Faso", "key": self.api_key, "limit": 1, "language": "fr"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])
            
            if results:
                first = results[0]
                geometry = first.get("geometry", {})
                normalization = {
                    "latitude": float(geometry.get("lat", fallback_lat)),
                    "longitude": float(geometry.get("lng", fallback_lon)),
                    "opencage_place_id": first.get("annotations", {}).get("geohash") or first.get("formatted"),
                    "opencage_formatted": first.get("formatted") or zone_name,
                }
        except Exception as e:
            logger.warning("Location normalization failed for %s: %s", zone_name, e)

        self._cache[zone_name] = normalization
        return normalization


class WeatherFetcher:
    """Fetches weather data from structured and unstructured sources."""

    def __init__(self, location_service: LocationService):
        self.location_service = location_service
        self._bulletin_cache: Optional[str] = None

    def fetch_bulletin_summary(self) -> str:
        if self._bulletin_cache is not None:
            return self._bulletin_cache
        
        try:
            scraper = DocumentScraper()
            data = scraper.scrape_bulletins()
            docs = data.get("results", [])
            snippets = []
            for doc in docs[:4]:
                title = (doc.get("title") or "Bulletin").strip()
                content = (doc.get("content") or "").strip().replace("\n", " ")[:280]
                if content:
                    snippets.append(f"{title}: {content}")
            summary = " | ".join(snippets)
            self._bulletin_cache = summary
            return summary
        except Exception as e:
            logger.warning("Scraping bulletins failed: %s", e)
            return ""

    def fetch_forecast(self, zone_name: str, lat: float, lon: float) -> Optional[WeatherSignal]:
        normalized = self.location_service.normalize(zone_name, lat, lon)
        bulletin = self.fetch_bulletin_summary()

        try:
            params = {
                "latitude": normalized["latitude"],
                "longitude": normalized["longitude"],
                "current": "temperature_2m,relative_humidity_2m,precipitation",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
                "forecast_days": 3,
                "timezone": "auto",
            }
            resp = requests.get(OPEN_METEO_URL, params=params, timeout=15)
            resp.raise_for_status()
            payload = resp.json()

            current = payload.get("current", {})
            daily = payload.get("daily", {})
            forecast_ts = daily.get("time", [])
            forecast_date = datetime.now(timezone.utc)
            if forecast_ts:
                try:
                    forecast_date = datetime.fromisoformat(forecast_ts[0]).replace(tzinfo=timezone.utc)
                except ValueError:
                    pass

            return WeatherSignal(
                zone_name=zone_name,
                latitude=normalized["latitude"],
                longitude=normalized["longitude"],
                opencage_place_id=normalized.get("opencage_place_id"),
                opencage_formatted=normalized.get("opencage_formatted"),
                observed_at=datetime.now(timezone.utc),
                forecast_date=forecast_date,
                temperature_c=current.get("temperature_2m"),
                precipitation_mm=current.get("precipitation"),
                humidity_pct=current.get("relative_humidity_2m"),
                source="api_openmeteo",
                source_type="api_openmeteo",
                confidence_score=0.85, # initial score
                raw_payload_link=None, # populated later
                bulletin_excerpt=bulletin,
            )
        except Exception as e:
            logger.error("Fetch forecast failed for %s: %s", zone_name, e)
            return None


class WeatherQuality:
    """Calculates confidence scores based on peer comparison and statistical anomalies."""

    @staticmethod
    def compute_score(signal: WeatherSignal, stats: Dict[str, float]) -> float:
        score = 0.9

        # Range checks
        if signal.humidity_pct is not None and not (0 <= signal.humidity_pct <= 100):
            score -= 0.35
        if signal.precipitation_mm is not None and signal.precipitation_mm < 0:
            score -= 0.35

        # Statistical anomaly
        mean = stats.get("mean_temp")
        std = stats.get("std_temp")
        if mean is not None and std and std > 0 and signal.temperature_c is not None:
            z_score = abs(signal.temperature_c - mean) / std
            if z_score > 3:
                score -= 0.3

        # Peer comparison
        peer_temp = stats.get("peer_temp")
        if peer_temp is not None and signal.temperature_c is not None:
            if abs(signal.temperature_c - peer_temp) >= 7:
                score -= 0.2

        # Source bonus
        if signal.source_type.startswith("scraping_"):
            score += 0.05

        return max(0.0, min(1.0, round(score, 3)))


class WeatherStorage:
    """Handles S3 archiving and Postgres persistence."""

    def __init__(self, db_url: str, advisories_dir: Path):
        self.engine = create_engine(db_url, pool_pre_ping=True)
        self.advisories_dir = advisories_dir
        self.advisories_dir.mkdir(parents=True, exist_ok=True)
        self.s3_client = None
        if getattr(settings, "S3_BUCKET", "") and boto3:
            # Fallback to us-east-1 if region is empty to avoid Invalid Endpoint error
            region = settings.S3_REGION or "us-east-1"
            self.s3_client = boto3.client("s3", region_name=region)

    def ensure_schema(self):
        """Idempotent schema migration."""
        ddl = [
            "CREATE SCHEMA IF NOT EXISTS agri_weather",
            """
            CREATE TABLE IF NOT EXISTS agri_weather.observations (
                id BIGSERIAL PRIMARY KEY,
                zone_name TEXT NOT NULL,
                latitude DOUBLE PRECISION NOT NULL,
                longitude DOUBLE PRECISION NOT NULL,
                opencage_place_id TEXT,
                opencage_formatted TEXT,
                observed_at TIMESTAMPTZ NOT NULL,
                forecast_date DATE NOT NULL,
                temperature_c DOUBLE PRECISION,
                precipitation_mm DOUBLE PRECISION,
                humidity_pct DOUBLE PRECISION,
                confidence_score DOUBLE PRECISION NOT NULL DEFAULT 0.85,
                raw_payload_link TEXT,
                bulletin_excerpt TEXT,
                source TEXT NOT NULL,
                source_type TEXT NOT NULL DEFAULT 'api_openmeteo',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (zone_name, observed_at, source),
                CONSTRAINT chk_weather_humidity_range CHECK (humidity_pct IS NULL OR (humidity_pct >= 0 AND humidity_pct <= 100)),
                CONSTRAINT chk_weather_confidence_range CHECK (confidence_score >= 0 AND confidence_score <= 1)
            )
            """,
            # Migrations for existing tables
            "ALTER TABLE agri_weather.observations ADD COLUMN IF NOT EXISTS opencage_place_id TEXT",
            "ALTER TABLE agri_weather.observations ADD COLUMN IF NOT EXISTS opencage_formatted TEXT",
            "ALTER TABLE agri_weather.observations ADD COLUMN IF NOT EXISTS confidence_score DOUBLE PRECISION DEFAULT 0.85",
            "ALTER TABLE agri_weather.observations ADD COLUMN IF NOT EXISTS raw_payload_link TEXT",
            "ALTER TABLE agri_weather.observations ADD COLUMN IF NOT EXISTS bulletin_excerpt TEXT",
            "ALTER TABLE agri_weather.observations ADD COLUMN IF NOT EXISTS source_type TEXT DEFAULT 'api_openmeteo'",
            
            # Robustly add constraints if table exists but constraints check failed or were added later
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = 'chk_weather_humidity_range'
                ) THEN
                    ALTER TABLE agri_weather.observations
                    ADD CONSTRAINT chk_weather_humidity_range
                    CHECK (humidity_pct IS NULL OR (humidity_pct >= 0 AND humidity_pct <= 100));
                END IF;
            END$$;
            """,
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = 'chk_weather_confidence_range'
                ) THEN
                    ALTER TABLE agri_weather.observations
                    ADD CONSTRAINT chk_weather_confidence_range
                    CHECK (confidence_score >= 0 AND confidence_score <= 1);
                END IF;
            END$$;
            """,
            "CREATE INDEX IF NOT EXISTS idx_weather_obs_observed_brin ON agri_weather.observations USING BRIN (observed_at)",
            "CREATE INDEX IF NOT EXISTS idx_weather_obs_place_time ON agri_weather.observations (opencage_place_id, observed_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_weather_obs_zone_observed ON agri_weather.observations (zone_name, observed_at DESC)",
            """
            CREATE TABLE IF NOT EXISTS agri_weather.alerts (
                id BIGSERIAL PRIMARY KEY,
                zone_name TEXT NOT NULL,
                alert_type TEXT NOT NULL,
                severity TEXT NOT NULL,
                details TEXT NOT NULL,
                forecast_date DATE NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                is_consumed BOOLEAN NOT NULL DEFAULT FALSE
            )
            """,
        ]
        with self.engine.begin() as conn:
            for statement in ddl:
                try:
                    conn.execute(text(statement))
                except Exception as e:
                    # Ignore "relation already exists" etc if IF NOT EXISTS fails for constraints
                    logger.debug("DDL warning (safe to ignore): %s", e)

    def upload_advisory(self, chunk: Dict[str, Any], idx: int) -> str:
        """Saves advisory JSON locally and optionally uploads to S3."""
        # Use a simpler key name without colons/spaces if possible
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        raw_zone = str(chunk.get("metadata", {}).get("zone", "zone"))
        safe_zone = "".join(c if c.isalnum() else "_" for c in raw_zone)
        filename = f"weather_{safe_zone}_{ts}_{idx}.json"
        
        local_path = self.advisories_dir / filename
        
        try:
            with local_path.open("w", encoding="utf-8") as f:
                json.dump(chunk, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error("Failed to write local advisory %s: %s", filename, e)
            return ""

        if self.s3_client and settings.S3_BUCKET:
            try:
                key_prefix = (settings.S3_KEY_PREFIX or "").lstrip("/")
                key = f"{key_prefix}/{filename}" if key_prefix else filename
                # Re-read as binary for upload
                with local_path.open("rb") as f:
                    self.s3_client.put_object(Bucket=settings.S3_BUCKET, Key=key, Body=f.read())
                return f"s3://{settings.S3_BUCKET}/{key}"
            except Exception as e:
                logger.warning("S3 upload failed for %s: %s", filename, e)
        
        return str(local_path)

    def fetch_stats(self, zone_name: str, source_type: str) -> Dict[str, float]:
        query = text("""
            SELECT AVG(temperature_c) as avg_t, STDDEV(temperature_c) as std_t
            FROM agri_weather.observations
            WHERE zone_name = :zone
              AND observed_at >= NOW() - INTERVAL '30 days'
        """)
        peer_query = text("""
            SELECT temperature_c FROM agri_weather.observations
            WHERE zone_name = :zone AND source_type <> :src
            ORDER BY observed_at DESC LIMIT 1
        """)
        
        stats = {}
        with self.engine.connect() as conn:
            try:
                res = conn.execute(query, {"zone": zone_name}).mappings().first()
                if res:
                    stats["mean_temp"] = res["avg_t"]
                    stats["std_temp"] = res["std_t"]
                
                res_peer = conn.execute(peer_query, {"zone": zone_name, "src": source_type}).mappings().first()
                if res_peer:
                    stats["peer_temp"] = res_peer["temperature_c"]
            except Exception as e:
                logger.warning("Failed to fetch stats for %s: %s", zone_name, e)
        return stats

    def persist(self, signal: WeatherSignal):
        upsert = text("""
            INSERT INTO agri_weather.observations (
                zone_name, latitude, longitude, opencage_place_id, opencage_formatted,
                observed_at, forecast_date, temperature_c, precipitation_mm, humidity_pct,
                confidence_score, raw_payload_link, bulletin_excerpt, source, source_type
            ) VALUES (
                :zone_name, :latitude, :longitude, :opencage_place_id, :opencage_formatted,
                :observed_at, :forecast_date, :temperature_c, :precipitation_mm, :humidity_pct,
                :confidence_score, :raw_payload_link, :bulletin_excerpt, :source, :source_type
            )
            ON CONFLICT (zone_name, observed_at, source) DO UPDATE SET
                forecast_date = EXCLUDED.forecast_date,
                temperature_c = EXCLUDED.temperature_c,
                precipitation_mm = EXCLUDED.precipitation_mm,
                humidity_pct = EXCLUDED.humidity_pct,
                confidence_score = EXCLUDED.confidence_score,
                raw_payload_link = EXCLUDED.raw_payload_link,
                bulletin_excerpt = EXCLUDED.bulletin_excerpt,
                opencage_place_id = EXCLUDED.opencage_place_id,
                opencage_formatted = EXCLUDED.opencage_formatted,
                source_type = EXCLUDED.source_type
        """)
        
        with self.engine.begin() as conn:
            conn.execute(upsert, asdict(signal))

    def persist_alerts(self, alerts: List[Dict[str, Any]]):
        if not alerts:
            return
        insert = text("""
            INSERT INTO agri_weather.alerts (zone_name, alert_type, severity, details, forecast_date)
            VALUES (:zone_name, :alert_type, :severity, :details, :forecast_date)
        """)
        with self.engine.begin() as conn:
            for alert in alerts:
                conn.execute(insert, alert)


class WeatherCollector:
    """Orchestrator for the weather collection pipeline."""

    def __init__(self, zones: Optional[Dict[str, Tuple[float, float]]] = None):
        self.zones = zones or DEFAULT_ZONES
        
        self.location_service = LocationService()
        self.fetcher = WeatherFetcher(self.location_service)
        # Handle path resolution robustly
        base_dir = Path(settings.BASE_DIR).parent if hasattr(settings, "BASE_DIR") else Path(".")
        if base_dir.name == "src": base_dir = base_dir.parent.parent # defensive walking
        
        adv_dir = base_dir / "sources" / "raw_data" / "weather_advisories"
        # Fallback if path handling above is shaky
        if not adv_dir.exists():
            adv_dir = Path("sources/raw_data/weather_advisories")

        self.storage = WeatherStorage(settings.DATABASE_URL, adv_dir)

    def run(self) -> Dict[str, Any]:
        self.storage.ensure_schema()
        
        counts = {"signals": 0, "persisted": 0, "alerts": 0, "advisory_docs": 0}
        advisory_paths = []

        for zone_name, (lat, lon) in self.zones.items():
            # 1. Fetch
            signal = self.fetcher.fetch_forecast(zone_name, lat, lon)
            if not signal:
                continue
            counts["signals"] += 1

            # 2. Score
            stats = self.storage.fetch_stats(signal.zone_name, signal.source_type)
            signal.confidence_score = WeatherQuality.compute_score(signal, stats)

            # 3. Enrich (Advisory & Upload)
            # We do this AFTER scoring so the advisory metadata includes the score if we wanted
            chunk = self._build_advisory(signal)
            path = self.storage.upload_advisory(chunk, counts["signals"])
            signal.raw_payload_link = path
            advisory_paths.append(path)
            counts["advisory_docs"] += 1

            # 4. Persist
            self.storage.persist(signal)
            counts["persisted"] += 1

            # 5. Alerts
            alerts = self._detect_alerts(signal)
            self.storage.persist_alerts(alerts)
            counts["alerts"] += len(alerts)

        return {**counts, "advisory_paths": advisory_paths}

    def _build_advisory(self, signal: WeatherSignal) -> Dict[str, Any]:
        """Creates a vectorizable document chunk from the signal."""
        advice = (
            f"Zone {signal.zone_name}: temp={signal.temperature_c}C, "
            f"pluie={signal.precipitation_mm}mm, hum={signal.humidity_pct}%. "
            "Surveiller l'évapotranspiration."
        )
        return {
            "title": f"Météo {signal.zone_name}",
            "content": advice,
            "metadata": {
                "doc_type": "weather_advisory",
                "zone": signal.zone_name,
                "forecast_date": signal.forecast_date.date().isoformat(),
                "observed_at": signal.observed_at.isoformat(),
                "source": signal.source,
                "confidence": signal.confidence_score,
                "bulletin_excerpt": signal.bulletin_excerpt[:1000]
            }
        }

    def _detect_alerts(self, signal: WeatherSignal) -> List[Dict[str, Any]]:
        alerts = []
        if signal.precipitation_mm is not None and signal.precipitation_mm <= 0.2:
            alerts.append({
                "zone_name": signal.zone_name,
                "alert_type": "drought_risk",
                "severity": "HIGH",
                "details": "Sécheresse possible (pluie <= 0.2mm)",
                "forecast_date": signal.forecast_date.date()
            })
        if signal.temperature_c is not None and signal.temperature_c >= 38:
            alerts.append({
                "zone_name": signal.zone_name,
                "alert_type": "heat_stress",
                "severity": "MEDIUM",
                "details": "Chaleur excessive (>= 38C)",
                "forecast_date": signal.forecast_date.date()
            })
        return alerts

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    collector = WeatherCollector()
    print(json.dumps(collector.run(), ensure_ascii=False, indent=2))
