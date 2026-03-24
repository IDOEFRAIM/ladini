"""
Weather Data Reconciliation Service.
Validates and merges observations from multiple sources (API vs Scraping).
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import bindparam, text

from agriconnect.services.data_collection.weather.weather_collector import WeatherStorage

logger = logging.getLogger(__name__)

BASELINE_SOURCES = ("api_openmeteo", "api_openweather")
LOCAL_SOURCE = "scraping_anam"
RECONCILED_SOURCE = "reconciled"


class WeatherReconciler:
    """Service to reconcile weather data from disparate sources."""

    def __init__(self, storage: WeatherStorage):
        self.storage = storage

    def _backfill_legacy_data(self, conn) -> None:
        """Apply data fixups for legacy rows."""
        statements = [
            "UPDATE agri_weather.observations SET confidence_score = 0.85 WHERE confidence_score IS NULL",
            "UPDATE agri_weather.observations SET source_type = source WHERE source_type IS NULL",
        ]
        for statement in statements:
            try:
                conn.execute(text(statement))
            except Exception:
                pass

    def _score_with_gap(self, temp_gap: Optional[float], rain_gap: Optional[float]) -> float:
        # Penalty logic consistent with WeatherQuality
        score = 0.9
        if temp_gap is not None:
            if temp_gap >= 7:
                score -= 0.25 
            elif temp_gap >= 4:
                score -= 0.1
        if rain_gap is not None:
            if rain_gap >= 10:
                score -= 0.2
            elif rain_gap >= 5:
                score -= 0.1
        return max(0.0, min(1.0, round(score, 3)))

    def _latest_by_source_sql(self) -> str:
        return """
        WITH ranked AS (
            SELECT
                zone_name,
                latitude,
                longitude,
                opencage_place_id,
                opencage_formatted,
                observed_at,
                forecast_date,
                temperature_c,
                precipitation_mm,
                humidity_pct,
                source,
                source_type,
                confidence_score,
                raw_payload_link,
                ROW_NUMBER() OVER (
                    PARTITION BY zone_name, source_type
                    ORDER BY observed_at DESC
                ) AS rn
            FROM agri_weather.observations
            WHERE observed_at >= NOW() - INTERVAL '2 days'
              AND source_type IN :sources
        )
        SELECT
            zone_name,
            latitude,
            longitude,
            opencage_place_id,
            opencage_formatted,
            observed_at,
            forecast_date,
            temperature_c,
            precipitation_mm,
            humidity_pct,
            source,
            source_type,
            confidence_score,
            raw_payload_link
        FROM ranked
        WHERE rn = 1
        ORDER BY zone_name, source_type
        """

    def _pick_baseline(self, rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        for row in rows:
            if row["source_type"] in BASELINE_SOURCES:
                return row
        return None

    def reconcile(self, write_reconciled: bool = True, temp_threshold: float = 5.0, rain_threshold: float = 6.0) -> Dict[str, Any]:
        self.storage.ensure_schema()
        
        by_zone: Dict[str, List[Dict[str, Any]]] = {}
        with self.storage.engine.begin() as conn:
            self._backfill_legacy_data(conn)
            
            result = conn.execute(
                text(self._latest_by_source_sql()).bindparams(bindparam("sources", expanding=True)),
                {"sources": [*BASELINE_SOURCES, LOCAL_SOURCE]},
            ).mappings().all()
            for row in result:
                by_zone.setdefault(row["zone_name"], []).append(dict(row))

        rows_to_write: List[Dict[str, Any]] = []
        report: List[Dict[str, Any]] = []

        for zone_name, rows in by_zone.items():
            baseline = self._pick_baseline(rows)
            local = next((r for r in rows if r["source_type"] == LOCAL_SOURCE), None)

            if not baseline:
                continue

            chosen = baseline
            preferred_source_type = baseline["source_type"]

            temp_gap = None
            rain_gap = None
            if local:
                if baseline.get("temperature_c") is not None and local.get("temperature_c") is not None:
                    temp_gap = abs(float(local["temperature_c"]) - float(baseline["temperature_c"]))
                if baseline.get("precipitation_mm") is not None and local.get("precipitation_mm") is not None:
                    rain_gap = abs(float(local["precipitation_mm"]) - float(baseline["precipitation_mm"]))

                if (temp_gap is not None and temp_gap >= temp_threshold) or (
                    rain_gap is not None and rain_gap >= rain_threshold
                ):
                    chosen = local
                    preferred_source_type = LOCAL_SOURCE

            confidence = self._score_with_gap(temp_gap, rain_gap)
            report.append(
                {
                    "zone_name": zone_name,
                    "preferred_source_type": preferred_source_type,
                    "temp_gap_c": temp_gap,
                    "rain_gap_mm": rain_gap,
                    "confidence_score": confidence,
                }
            )

            if write_reconciled:
                rows_to_write.append(
                    {
                        "zone_name": zone_name,
                        "latitude": chosen["latitude"],
                        "longitude": chosen["longitude"],
                        "opencage_place_id": chosen.get("opencage_place_id"),
                        "opencage_formatted": chosen.get("opencage_formatted"),
                        "observed_at": datetime.now(timezone.utc),
                        "forecast_date": chosen["forecast_date"],
                        "temperature_c": chosen.get("temperature_c"),
                        "precipitation_mm": chosen.get("precipitation_mm"),
                        "humidity_pct": chosen.get("humidity_pct"),
                        "confidence_score": confidence,
                        "raw_payload_link": chosen.get("raw_payload_link"),
                        "bulletin_excerpt": f"reconciled from baseline/local; temp_gap={temp_gap}, rain_gap={rain_gap}",
                        "source": RECONCILED_SOURCE,
                        "source_type": RECONCILED_SOURCE,
                    }
                )

        if write_reconciled and rows_to_write:
            upsert_sql = text(
                """
                INSERT INTO agri_weather.observations (
                    zone_name, latitude, longitude, opencage_place_id, opencage_formatted,
                    observed_at, forecast_date, temperature_c, precipitation_mm, humidity_pct,
                    confidence_score, raw_payload_link, bulletin_excerpt, source, source_type
                ) VALUES (
                    :zone_name, :latitude, :longitude, :opencage_place_id, :opencage_formatted,
                    :observed_at, :forecast_date, :temperature_c, :precipitation_mm, :humidity_pct,
                    :confidence_score, :raw_payload_link, :bulletin_excerpt, :source, :source_type
                )
                ON CONFLICT (zone_name, observed_at, source)
                DO UPDATE SET
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
                """
            )
            with self.storage.engine.begin() as conn:
                for row in rows_to_write:
                    conn.execute(upsert_sql, row)

        return {
            "zones_total": len(by_zone),
            "zones_compared": len(report),
            "reconciled_written": len(rows_to_write) if write_reconciled else 0,
            "report_summary": f"Compared {len(report)} zones, reconciled {len(rows_to_write)} rows",
        }
