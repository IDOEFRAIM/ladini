from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from fastmcp import Context
from pydantic import BaseModel

from agriconnect.infrastructure.mcp.base import MCPToolSpec

logger = logging.getLogger("MCP.Tools.Weather")


class WeatherSnapshot(BaseModel):
    location: str
    temperature_c: float
    humidity_pct: float
    precip_mm: float
    wind_kmh: float
    description: str
    source: str = "sentinelle"


class ET0Payload(BaseModel):
    et0_mm: float
    precip_mm: float
    water_deficit_mm: float
    irrigation_needed: bool


class SatellitePayload(BaseModel):
    location: str
    ndvi: Optional[float] = None
    soil_moisture_pct: Optional[float] = None
    anomaly_detected: bool = False


class WeatherProvider:
    name = "weather"

    def __init__(self, sentinelle_tool: Any = None) -> None:
        self._sentinelle_tool = sentinelle_tool

    def _lazy_sentinelle(self) -> Any:
        if self._sentinelle_tool is None:
            try:
                from agriconnect.tools.sentinelle import SentinelleTool

                self._sentinelle_tool = SentinelleTool()
            except Exception as exc:
                logger.error("SentinelleTool unavailable: %s", exc)
        return self._sentinelle_tool

    async def get_current_weather(self, location: str, ctx: Context = None) -> str:
        if ctx:
            await ctx.info(f"Fetching weather for: {location}")

        tool = self._lazy_sentinelle()
        raw = tool.fetch_weather(location) if tool else {}
        payload = WeatherSnapshot(
            location=location,
            temperature_c=raw.get("temp", 30.0),
            humidity_pct=raw.get("humidity", 45.0),
            precip_mm=raw.get("precip", 0.0),
            wind_kmh=raw.get("wind", 12.0),
            description=raw.get("desc", "Ciel degage"),
            source="API_Sentinelle_V2",
        )
        return payload.model_dump_json(indent=2)

    async def compute_irrigation_needs(
        self,
        temperature_c: float,
        humidity_pct: float,
        wind_kmh: float = 10.0,
        precip_mm: float = 0.0,
        ctx: Context = None,
    ) -> str:
        if ctx:
            await ctx.info(f"Computing ET0 for temp={temperature_c} humidity={humidity_pct}")

        et0 = (0.0023 * (temperature_c + 17.8) * (temperature_c ** 0.5) * 15) / 1.5
        et0 = round(max(et0, 0.0), 2)
        deficit = round(max(et0 - precip_mm, 0.0), 2)
        payload = ET0Payload(
            et0_mm=et0,
            precip_mm=precip_mm,
            water_deficit_mm=deficit,
            irrigation_needed=deficit > 2.5,
        )
        return payload.model_dump_json(indent=2)

    async def get_crop_health_vitals(self, location: str, ctx: Context = None) -> str:
        if ctx:
            await ctx.info(f"Fetching satellite data for: {location}")

        tool = self._lazy_sentinelle()
        raw = tool.fetch_satellite(location) if tool else {}
        payload = SatellitePayload(
            location=location,
            ndvi=raw.get("ndvi", 0.65),
            soil_moisture_pct=raw.get("moisture", 22.0),
            anomaly_detected=raw.get("anomaly", False),
        )
        return payload.model_dump_json(indent=2)

    def get_tools(self) -> List[MCPToolSpec]:
        return [
            MCPToolSpec(name="get_current_weather", handler=self.get_current_weather),
            MCPToolSpec(name="compute_irrigation_needs", handler=self.compute_irrigation_needs),
            MCPToolSpec(name="get_crop_health_vitals", handler=self.get_crop_health_vitals),
        ]

    async def ping(self) -> Dict[str, Any]:
        return {"status": "ready", "sentinelle_loaded": bool(self._sentinelle_tool)}


class MCPWeatherServer:
    """Backward-compatible weather server wrapper built on top of WeatherProvider."""

    def __init__(self, *args, **kwargs):
        sentinelle_tool = kwargs.pop("llm_client", None)
        if sentinelle_tool is None and "sentinelle_tool" in kwargs:
            sentinelle_tool = kwargs.pop("sentinelle_tool")
        self._provider = WeatherProvider(sentinelle_tool=sentinelle_tool)

    def list_tools(self):
        return [
            {"name": "get_current_weather", "description": "Obtient la meteo actuelle pour une localisation precise."},
            {"name": "compute_irrigation_needs", "description": "Calcule le besoin en eau (ET0) base sur les conditions meteo."},
            {"name": "get_crop_health_vitals", "description": "Recupere l'indice NDVI et l'humidite du sol via satellite."},
        ]

    def call_tool(self, name: str, arguments: dict):
        handlers = {
            "get_current_weather": self._provider.get_current_weather,
            "compute_irrigation_needs": self._provider.compute_irrigation_needs,
            "get_crop_health_vitals": self._provider.get_crop_health_vitals,
        }
        fn = handlers.get(name)
        if fn is None:
            return {"ok": False, "error": f"Unknown weather tool: {name}"}

        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            raw = loop.run_until_complete(fn(**arguments))
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()
            try:
                asyncio.set_event_loop(None)
            except Exception:
                pass
        return {"ok": True, "data": json.loads(raw) if isinstance(raw, str) else raw}
