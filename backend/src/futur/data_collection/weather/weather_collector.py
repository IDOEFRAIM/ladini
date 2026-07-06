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
import re
import hashlib
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
from sqlalchemy import text

from agriconnect.core.settings import settings
from agriconnect.core.db import get_engine, resolve_database_url
from futur.data_collection.weather.documents_meteo import DocumentScraper

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
    trace_id: str
    upload_status: str
    raw_payload_link: Optional[str]
    bulletin_excerpt: str


class LocationService:
    """Handles geocoding and location normalization using OpenCage."""
    
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("OPENCAGE_API_KEY", "").strip()
        self._cache: Dict[str, Dict[str, Any]] = {}
        self.db_url = resolve_database_url(required=False)
        self.engine = get_engine(self.db_url) if self.db_url else None
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        if not self.engine:
            return
        ddl = [
            "CREATE SCHEMA IF NOT EXISTS agri_core",
            """
            CREATE TABLE IF NOT EXISTS agri_core.locations (
                zone_name TEXT PRIMARY KEY,
                latitude DOUBLE PRECISION NOT NULL,
                longitude DOUBLE PRECISION NOT NULL,
                opencage_place_id TEXT,
                opencage_formatted TEXT,
                geocode_source TEXT NOT NULL DEFAULT 'fallback',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """,
        ]
        with self.engine.begin() as conn:
            for statement in ddl:
                conn.execute(text(statement))

    def _get_from_db(self, zone_name: str) -> Optional[Dict[str, Any]]:
        if not self.engine:
            return None
        query = text(
            """
            SELECT latitude, longitude, opencage_place_id, opencage_formatted
            FROM agri_core.locations
            WHERE zone_name = :zone_name
            """
        )
        with self.engine.connect() as conn:
            row = conn.execute(query, {"zone_name": zone_name}).mappings().first()
        if not row:
            return None
        return {
            "latitude": float(row["latitude"]),
            "longitude": float(row["longitude"]),
            "opencage_place_id": row.get("opencage_place_id"),
            "opencage_formatted": row.get("opencage_formatted") or zone_name,
        }

    def _save_to_db(self, zone_name: str, normalization: Dict[str, Any], source: str) -> None:
        if not self.engine:
            return
        upsert = text(
            """
            INSERT INTO agri_core.locations (
                zone_name, latitude, longitude, opencage_place_id, opencage_formatted, geocode_source, updated_at
            ) VALUES (
                :zone_name, :latitude, :longitude, :opencage_place_id, :opencage_formatted, :geocode_source, NOW()
            )
            ON CONFLICT (zone_name) DO UPDATE SET
                latitude = EXCLUDED.latitude,
                longitude = EXCLUDED.longitude,
                opencage_place_id = EXCLUDED.opencage_place_id,
                opencage_formatted = EXCLUDED.opencage_formatted,
                geocode_source = EXCLUDED.geocode_source,
                updated_at = NOW()
            """
        )
        with self.engine.begin() as conn:
            conn.execute(
                upsert,
                {
                    "zone_name": zone_name,
                    "latitude": normalization["latitude"],
                    "longitude": normalization["longitude"],
                    "opencage_place_id": normalization.get("opencage_place_id"),
                    "opencage_formatted": normalization.get("opencage_formatted"),
                    "geocode_source": source,
                },
            )

    def normalize(self, zone_name: str, fallback_lat: float, fallback_lon: float) -> Dict[str, Any]:
        if zone_name in self._cache:
            return self._cache[zone_name]

        from_db = self._get_from_db(zone_name)
        if from_db:
            self._cache[zone_name] = from_db
            return from_db

        normalization = {
            "latitude": fallback_lat,
            "longitude": fallback_lon,
            "opencage_place_id": None,
            "opencage_formatted": zone_name,
        }

        if not self.api_key:
            self._save_to_db(zone_name, normalization, "fallback")
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
                self._save_to_db(zone_name, normalization, "opencage")
        except Exception as e:
            logger.warning("Location normalization failed for %s: %s", zone_name, e)
            self._save_to_db(zone_name, normalization, "fallback_error")

        self._cache[zone_name] = normalization
        return normalization


class WeatherFetcher:
    """Fetches weather data from structured and unstructured sources."""

    def __init__(self, location_service: LocationService, storage: Optional["WeatherStorage"] = None):
        self.location_service = location_service
        # storage is used to read previously-scraped bulletins from DB (agri_vector.document_chunks)
        self.storage = storage
        # per-zone bulletin cache
        self._bulletin_cache: Dict[str, str] = {}

    def _sanitize_bulletin_text(self, text: str) -> str:
        cleaned = " ".join((text or "").split())
        if not cleaned:
            return ""

        # Remove repeated site-navigation boilerplate that pollutes scraped excerpts.
        cleaned = re.sub(
            r"(?i)(alertes|s.?abonner|langue|french|english|spanish|arabic|chinese|recherche|contactez[- ]?nous|retour)",
            " ",
            cleaned,
        )
        cleaned = re.sub(r"[|:]{2,}", "|", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" |:-")

        # If content is mostly menu residue, drop it instead of storing noisy metadata.
        if len(cleaned) < 45:
            return ""
        return cleaned

    @staticmethod
    def _build_forecast_params(latitude: float, longitude: float) -> Dict[str, Any]:
        return {
            "latitude": latitude,
            "longitude": longitude,
            "current": "temperature_2m,relative_humidity_2m,precipitation",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
            "forecast_days": 3,
            "timezone": "auto",
        }

    @staticmethod
    def _parse_forecast_date(payload: Dict[str, Any]) -> datetime:
        daily = payload.get("daily", {})
        forecast_ts = daily.get("time", [])
        if not forecast_ts:
            return datetime.now(timezone.utc)
        try:
            return datetime.fromisoformat(forecast_ts[0]).replace(tzinfo=timezone.utc)
        except ValueError:
            return datetime.now(timezone.utc)

    def _request_forecast_payload(self, normalized: Dict[str, Any], zone_name: str) -> Optional[Dict[str, Any]]:
        params = self._build_forecast_params(normalized["latitude"], normalized["longitude"])
        try:
            resp = requests.get(OPEN_METEO_URL, params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error("Fetch forecast failed for %s: %s", zone_name, e)
            return None

    @staticmethod
    def _to_weather_signal(
        zone_name: str,
        normalized: Dict[str, Any],
        payload: Dict[str, Any],
        bulletin: str,
    ) -> WeatherSignal:
        current = payload.get("current", {})
        observed_at = datetime.now(timezone.utc)
        trace_seed = f"{zone_name}|{observed_at.isoformat()}|{normalized['latitude']}|{normalized['longitude']}"
        trace_id = hashlib.sha1(trace_seed.encode("utf-8")).hexdigest()
        return WeatherSignal(
            zone_name=zone_name,
            latitude=normalized["latitude"],
            longitude=normalized["longitude"],
            opencage_place_id=normalized.get("opencage_place_id"),
            opencage_formatted=normalized.get("opencage_formatted"),
            observed_at=observed_at,
            forecast_date=WeatherFetcher._parse_forecast_date(payload),
            temperature_c=current.get("temperature_2m"),
            precipitation_mm=current.get("precipitation"),
            humidity_pct=current.get("relative_humidity_2m"),
            source="api_openmeteo",
            source_type="api_openmeteo",
            confidence_score=0.85,
            trace_id=trace_id,
            upload_status="pending_upload",
            raw_payload_link=None,
            bulletin_excerpt=bulletin,
        )

    def fetch_bulletin_summary(self, zone_name: str) -> str:
        # Returns the latest available bulletin excerpt for `zone_name`.
        cached = self._bulletin_cache.get(zone_name)
        if cached is not None:
            return cached

        content = ""
        # Prefer DB-backed documents stored by the orchestrator (agri_vector.document_chunks)
        if self.storage:
            try:
                content = (self.storage.get_latest_bulletin(zone_name) or "")
            except Exception as e:
                logger.warning("Failed to read latest bulletin from DB for %s: %s", zone_name, e)

        # Do NOT perform live scraping from the data_collection layer.
        # The scraper orchestrator is responsible for running scrapers and
        # persisting their outputs into the DB (agri_vector.document_chunks).
        # If no DB-backed bulletin is available here, return empty and let
        # the orchestrator or an external workflow trigger a harvest.
        if not content:
            logger.debug(
                "No DB-backed bulletin found for %s; not performing live scraping from data_collection layer",
                zone_name,
            )

        # Sanitize and truncate
        summary = self._sanitize_bulletin_text(content.replace("\n", " "))[:700]
        if not summary:
            logger.debug("No bulletin summary for zone=%s", zone_name)
        self._bulletin_cache[zone_name] = summary
        return summary

    def fetch_forecast(self, zone_name: str, lat: float, lon: float) -> Optional[WeatherSignal]:
        normalized = self.location_service.normalize(zone_name, lat, lon)
        bulletin = self.fetch_bulletin_summary(zone_name)
        payload = self._request_forecast_payload(normalized, zone_name)
        if payload is None:
            return None
        return self._to_weather_signal(zone_name, normalized, payload, bulletin)


class WeatherQuality:
    """Calculates confidence scores based on peer comparison and statistical anomalies."""

    @staticmethod
    def _range_penalty(signal: WeatherSignal) -> float:
        penalty = 0.0
        if signal.humidity_pct is not None and not (0 <= signal.humidity_pct <= 100):
            penalty += 0.35
        if signal.precipitation_mm is not None and signal.precipitation_mm < 0:
            penalty += 0.35
        return penalty

    @staticmethod
    def _statistical_penalty(signal: WeatherSignal, stats: Dict[str, float]) -> float:
        mean = stats.get("mean_temp")
        std = stats.get("std_temp")
        if mean is None or std is None or std <= 0 or signal.temperature_c is None:
            return 0.0
        z_score = abs(signal.temperature_c - mean) / std
        return 0.3 if z_score > 3 else 0.0

    @staticmethod
    def _peer_penalty(signal: WeatherSignal, stats: Dict[str, float]) -> float:
        peer_temp = stats.get("peer_temp")
        if peer_temp is None or signal.temperature_c is None:
            return 0.0
        return 0.2 if abs(signal.temperature_c - peer_temp) >= 7 else 0.0

    @staticmethod
    def compute_score(signal: WeatherSignal, stats: Dict[str, float]) -> float:
        score = 0.9
        score -= WeatherQuality._range_penalty(signal)
        score -= WeatherQuality._statistical_penalty(signal, stats)
        score -= WeatherQuality._peer_penalty(signal, stats)

        # Source bonus
        if signal.source_type.startswith("scraping_"):
            score += 0.05

        return max(0.0, min(1.0, round(score, 3)))


class WeatherStorage:
    """Handles S3 archiving and Postgres persistence."""

    def __init__(self, db_url: str, advisories_dir: Optional[Path] = None):
        self.engine = get_engine(db_url)
        self.advisories_dir = advisories_dir
        self.s3_client = None
        if getattr(settings, "S3_BUCKET", "") and boto3:
            # Fallback to us-east-1 if region is empty to avoid Invalid Endpoint error
            region = settings.S3_REGION or "us-east-1"
            self.s3_client = boto3.client("s3", region_name=region)
        if not self.s3_client:
            raise RuntimeError("S3_BUCKET must be configured for weather storage. Local advisory storage is disabled.")

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
                trace_id TEXT,
                upload_status TEXT NOT NULL DEFAULT 'pending_upload',
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
            "ALTER TABLE agri_weather.observations ADD COLUMN IF NOT EXISTS trace_id TEXT",
            "ALTER TABLE agri_weather.observations ADD COLUMN IF NOT EXISTS upload_status TEXT DEFAULT 'pending_upload'",
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
            "CREATE INDEX IF NOT EXISTS idx_weather_obs_trace_id ON agri_weather.observations (trace_id)",
            "CREATE INDEX IF NOT EXISTS idx_weather_obs_upload_status ON agri_weather.observations (upload_status)",
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
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_weather_alert_dedup
            ON agri_weather.alerts (zone_name, alert_type, forecast_date, details)
            """,
        ]
        with self.engine.begin() as conn:
            for statement in ddl:
                try:
                    conn.execute(text(statement))
                except Exception as e:
                    # Ignore "relation already exists" etc if IF NOT EXISTS fails for constraints
                    logger.debug("DDL warning (safe to ignore): %s", e)

    def get_latest_bulletin(self, zone_name: str) -> str:
        """Return the most recent bulletin content for `zone_name` from
        agri_vector.document_chunks (stored by the orchestrator).
        """
        try:
            sql = text(
                """
                SELECT content, metadata
                FROM agri_vector.document_chunks
                WHERE zone_name = :zone_name
                  AND (
                    doc_type = 'bulletin'
                    OR (metadata->>'doc_type') = 'bulletin'
                    OR (metadata->>'doc_type') = 'weather_bulletin'
                  )
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
            with self.engine.connect() as conn:
                row = conn.execute(sql, {"zone_name": zone_name}).fetchone()
            if not row:
                return ""
            content = row[0] or ""
            meta = row[1] or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            title = (meta.get("title") or meta.get("headline") or "").strip()
            if title and content:
                return f"{title}: {content}"
            return content
        except Exception as e:
            logger.warning("get_latest_bulletin db query failed for %s: %s", zone_name, e)
            return ""

    def upload_advisory(self, chunk: Dict[str, Any], idx: int, trace_id: str = "") -> Dict[str, Optional[str]]:
        """Uploads advisory JSON directly to S3."""
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        raw_zone = str(chunk.get("metadata", {}).get("zone", "zone"))
        safe_zone = "".join(c if c.isalnum() else "_" for c in raw_zone)
        trace_suffix = (trace_id or "")[:10]
        filename = f"weather_{safe_zone}_{ts}_{idx}_{trace_suffix}.json" if trace_suffix else f"weather_{safe_zone}_{ts}_{idx}.json"

        try:
            key_prefix = (settings.S3_KEY_PREFIX or "").strip("/")
            key = f"{key_prefix}/raw_data/weather_advisories/{filename}" if key_prefix else f"raw_data/weather_advisories/{filename}"
            payload = json.dumps(chunk, ensure_ascii=False).encode("utf-8")
            response = self.s3_client.put_object(
                Bucket=settings.S3_BUCKET,
                Key=key,
                Body=payload,
                ContentType="application/json",
            )
            return {
                "s3_uri": f"s3://{settings.S3_BUCKET}/{key}",
                "s3_key": key,
                "etag": str(response.get("ETag") or "").strip('"') or None,
            }
        except Exception as e:
            logger.error("S3 upload failed for %s: %s", filename, e)
            raise

    def delete_advisory(self, s3_key: str) -> None:
        """Compensation step for S3/DB consistency if DB persistence fails."""
        if not s3_key:
            return
        try:
            self.s3_client.delete_object(Bucket=settings.S3_BUCKET, Key=s3_key)
        except Exception as e:
            logger.warning("Rollback delete failed for advisory key=%s: %s", s3_key, e)

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
                confidence_score, trace_id, upload_status, raw_payload_link, bulletin_excerpt, source, source_type
            ) VALUES (
                :zone_name, :latitude, :longitude, :opencage_place_id, :opencage_formatted,
                :observed_at, :forecast_date, :temperature_c, :precipitation_mm, :humidity_pct,
                :confidence_score, :trace_id, :upload_status, :raw_payload_link, :bulletin_excerpt, :source, :source_type
            )
            ON CONFLICT (zone_name, observed_at, source) DO UPDATE SET
                forecast_date = EXCLUDED.forecast_date,
                temperature_c = EXCLUDED.temperature_c,
                precipitation_mm = EXCLUDED.precipitation_mm,
                humidity_pct = EXCLUDED.humidity_pct,
                confidence_score = EXCLUDED.confidence_score,
                trace_id = EXCLUDED.trace_id,
                upload_status = EXCLUDED.upload_status,
                raw_payload_link = EXCLUDED.raw_payload_link,
                bulletin_excerpt = EXCLUDED.bulletin_excerpt,
                opencage_place_id = EXCLUDED.opencage_place_id,
                opencage_formatted = EXCLUDED.opencage_formatted,
                source_type = EXCLUDED.source_type
        """)
        
        with self.engine.begin() as conn:
            conn.execute(upsert, asdict(signal))

    def update_upload_state(self, signal: WeatherSignal, status: str, s3_uri: Optional[str] = None):
        query = text(
            """
            UPDATE agri_weather.observations
            SET upload_status = :status,
                raw_payload_link = COALESCE(:raw_payload_link, raw_payload_link)
            WHERE zone_name = :zone_name
              AND observed_at = :observed_at
              AND source = :source
            """
        )
        with self.engine.begin() as conn:
            conn.execute(
                query,
                {
                    "status": status,
                    "raw_payload_link": s3_uri,
                    "zone_name": signal.zone_name,
                    "observed_at": signal.observed_at,
                    "source": signal.source,
                },
            )

    def fetch_recent_precip_sum(self, zone_name: str, days: int = 15) -> float:
        query = text(
            """
            SELECT COALESCE(SUM(precipitation_mm), 0.0) AS precip_sum
            FROM agri_weather.observations
            WHERE zone_name = :zone_name
                            AND observed_at >= NOW() - make_interval(days => :days)
            """
        )
        with self.engine.connect() as conn:
            row = conn.execute(query, {"zone_name": zone_name, "days": days}).mappings().first()
        return float((row or {}).get("precip_sum") or 0.0)

    def persist_alerts(self, alerts: List[Dict[str, Any]]):
        if not alerts:
            return
        insert = text("""
            INSERT INTO agri_weather.alerts (zone_name, alert_type, severity, details, forecast_date)
            VALUES (:zone_name, :alert_type, :severity, :details, :forecast_date)
            ON CONFLICT DO NOTHING
        """)
        with self.engine.begin() as conn:
            for alert in alerts:
                conn.execute(insert, alert)


class WeatherCollector:
    """Orchestrator for the weather collection pipeline."""

    def __init__(self, zones: Optional[Dict[str, Tuple[float, float]]] = None):
        self.zones = zones or DEFAULT_ZONES
        
        self.location_service = LocationService()
        # create storage first so WeatherFetcher can read previously-scraped bulletins
        self.storage = WeatherStorage(settings.DATABASE_URL)
        self.fetcher = WeatherFetcher(self.location_service, storage=self.storage)

    def run(self) -> Dict[str, Any]:
        self.storage.ensure_schema()
        
        counts = {
            "signals": 0,
            "persisted": 0,
            "alerts": 0,
            "advisory_docs": 0,
            "rolled_back": 0,
            "missing_bulletin": 0,
            "upload_failed": 0,
        }
        advisory_paths = []

        for zone_name, (lat, lon) in self.zones.items():
            # 1. Fetch
            signal = self.fetcher.fetch_forecast(zone_name, lat, lon)
            if not signal:
                continue
            counts["signals"] += 1
            if not signal.bulletin_excerpt:
                counts["missing_bulletin"] += 1

            # 2. Score
            stats = self.storage.fetch_stats(signal.zone_name, signal.source_type)
            signal.confidence_score = WeatherQuality.compute_score(signal, stats)

            # Persist first to keep DB as authoritative state machine.
            self.storage.persist(signal)
            counts["persisted"] += 1

            # 3. Enrich (Advisory & Upload)
            # We do this AFTER scoring so the advisory metadata includes the score if we wanted
            chunk = self._build_advisory(signal)
            try:
                upload_info = self.storage.upload_advisory(chunk, counts["signals"], signal.trace_id)
                signal.raw_payload_link = upload_info.get("s3_uri")
                signal.upload_status = "completed"
                self.storage.update_upload_state(signal, "completed", signal.raw_payload_link)
                advisory_paths.append(upload_info.get("s3_uri") or "")
                counts["advisory_docs"] += 1

                # 5. Alerts
                alerts = self._detect_alerts(signal)
                self.storage.persist_alerts(alerts)
                counts["alerts"] += len(alerts)
            except Exception:
                self.storage.update_upload_state(signal, "failed", None)
                counts["upload_failed"] += 1
                logger.exception("Advisory upload failed for zone=%s trace_id=%s", signal.zone_name, signal.trace_id)
                continue

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
                "trace_id": signal.trace_id,
                "zone": signal.zone_name,
                "forecast_date": signal.forecast_date.date().isoformat(),
                "observed_at": signal.observed_at.isoformat(),
                "source": signal.source,
                "confidence": signal.confidence_score,
                "bulletin_available": bool(signal.bulletin_excerpt),
                "bulletin_excerpt": signal.bulletin_excerpt[:1000]
            }
        }

    def _detect_alerts(self, signal: WeatherSignal) -> List[Dict[str, Any]]:
        alerts = []
        precip_15d = self.storage.fetch_recent_precip_sum(signal.zone_name, days=15)
        if (
            signal.precipitation_mm is not None
            and signal.precipitation_mm <= 0.2
            and precip_15d <= 5.0
            and (signal.temperature_c or 0) >= 34
        ):
            alerts.append({
                "zone_name": signal.zone_name,
                "alert_type": "drought_risk",
                "severity": "HIGH",
                "details": f"Risque de sécheresse (cumul pluie 15j={precip_15d:.1f}mm)",
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
