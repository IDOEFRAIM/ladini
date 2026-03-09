"""
Agri-Weather MCP Server (FastMCP).
Domain: Meteorological data, ET0 calculation, and Satellite NDVI signals.

Tools:
  - get_current_weather(location)                              → WeatherSnapshot
  - compute_irrigation_needs(temperature_c, humidity_pct, …)   → ET0Payload
  - get_crop_health_vitals(location)                           → SatellitePayload
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field
from fastmcp import FastMCP, Context

logger = logging.getLogger("MCP.WeatherServer")

# ────────────────────── FastMCP instance ──────────────────────────────────

mcp = FastMCP("AgriConnect Weather MCP Server")

# ────────────────────── Lazy singleton ────────────────────────────────────

_sentinelle_tool = None


def _lazy_sentinelle():
    """Load the SentinelleTool only when first needed (RAM saver)."""
    global _sentinelle_tool
    if _sentinelle_tool is None:
        try:
            from agriconnect.tools.sentinelle import SentinelleTool
            _sentinelle_tool = SentinelleTool()
            logger.info("SentinelleTool initialisé pour le serveur Weather.")
        except Exception as exc:
            logger.error("Échec de chargement de SentinelleTool: %s", exc)
    return _sentinelle_tool


# ────────────────────── Pydantic response models ──────────────────────────

class WeatherSnapshot(BaseModel):
    location: str
    temperature_c: float
    humidity_pct: float
    precip_mm: float
    wind_kmh: float
    description: str
    source: str = "sentinelle"


class ET0Payload(BaseModel):
    """Estimation de l'évapotranspiration de référence (FAO-56 simplifié)."""
    et0_mm: float
    precip_mm: float
    water_deficit_mm: float
    irrigation_needed: bool


class SatellitePayload(BaseModel):
    location: str
    ndvi: Optional[float] = None
    soil_moisture_pct: Optional[float] = None
    anomaly_detected: bool = False


# ────────────────────── Tools ─────────────────────────────────────────────

@mcp.tool()
async def get_current_weather(location: str, ctx: Context) -> str:
    """Obtient la météo actuelle pour une localisation précise.

    Args:
        location: Nom du village ou de la zone
        ctx: MCP context for logging

    Returns:
        JSON string of the WeatherSnapshot
    """
    await ctx.info(f"Fetching weather for: {location}")
    tool = _lazy_sentinelle()
    raw = tool.fetch_weather(location) if tool else {}

    snapshot = WeatherSnapshot(
        location=location,
        temperature_c=raw.get("temp", 30.0),
        humidity_pct=raw.get("humidity", 45.0),
        precip_mm=raw.get("precip", 0.0),
        wind_kmh=raw.get("wind", 12.0),
        description=raw.get("desc", "Ciel dégagé"),
        source="API_Sentinelle_V2",
    )
    return snapshot.model_dump_json(indent=2)


@mcp.tool()
async def compute_irrigation_needs(
    temperature_c: float,
    humidity_pct: float,
    wind_kmh: float = 10.0,
    precip_mm: float = 0.0,
    ctx: Context = None,
) -> str:
    """Calcule le besoin en eau (ET0) basé sur les conditions météo.

    Utilise une version simplifiée de la formule de Hargreaves.

    Args:
        temperature_c: Température en degrés Celsius
        humidity_pct: Humidité relative en pourcentage
        wind_kmh: Vent en km/h (défaut 10)
        precip_mm: Précipitations en mm (défaut 0)
        ctx: MCP context for logging

    Returns:
        JSON string of the ET0Payload
    """
    if ctx:
        await ctx.info(f"Computing ET0: temp={temperature_c}°C, humidity={humidity_pct}%")

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


@mcp.tool()
async def get_crop_health_vitals(location: str, ctx: Context) -> str:
    """Récupère l'indice NDVI et l'humidité du sol via satellite.

    Args:
        location: Nom du village ou de la zone
        ctx: MCP context for logging

    Returns:
        JSON string of the SatellitePayload
    """
    await ctx.info(f"Fetching satellite data for: {location}")
    tool = _lazy_sentinelle()
    raw = tool.fetch_satellite(location) if tool else {}

    payload = SatellitePayload(
        location=location,
        ndvi=raw.get("ndvi", 0.65),
        soil_moisture_pct=raw.get("moisture", 22.0),
        anomaly_detected=raw.get("anomaly", False),
    )
    return payload.model_dump_json(indent=2)


# ────────────────────── Resources ─────────────────────────────────────────

@mcp.resource("weather://status")
async def weather_server_status() -> str:
    """Health-check resource for the weather server."""
    tool = _lazy_sentinelle()
    return f'{{"status": "ok", "sentinelle_loaded": {str(bool(tool)).lower()}}}'


# ────────────────────── Backward-compatible class wrapper ─────────────────

class WeatherMCPServer:
    """Thin compatibility wrapper so existing callers can use call_tool(name, args).

    For new code prefer calling the FastMCP tool functions directly or
    running the server via ``mcp.run()``.
    """

    name = "weather"

    def __init__(self, sentinelle_tool=None, **kwargs):
        global _sentinelle_tool
        if sentinelle_tool is not None:
            _sentinelle_tool = sentinelle_tool

    @staticmethod
    def list_tools():
        return [
            {"name": "get_current_weather", "description": "Obtient la météo actuelle pour une localisation précise."},
            {"name": "compute_irrigation_needs", "description": "Calcule le besoin en eau (ET0) basé sur les conditions météo."},
            {"name": "get_crop_health_vitals", "description": "Récupère l'indice NDVI et l'humidité du sol via satellite."},
        ]

    @staticmethod
    async def _dispatch(name: str, args: Dict[str, Any]) -> str:
        handlers = {
            "get_current_weather": get_current_weather,
            "compute_irrigation_needs": compute_irrigation_needs,
            "get_crop_health_vitals": get_crop_health_vitals,
        }
        fn = handlers.get(name)
        if not fn:
            raise ValueError(f"Unknown weather tool: {name}")
        return await fn(**args)

    def call_tool_sync(self, name: str, arguments: dict) -> dict:
        import asyncio, json
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        raw = loop.run_until_complete(self._dispatch(name, arguments))
        return {"ok": True, "data": json.loads(raw) if isinstance(raw, str) else raw}


# ────────────────────── Entry point ───────────────────────────────────────

if __name__ == "__main__":
    logger.info("Starting AgriConnect Weather MCP Server")
    mcp.run()
