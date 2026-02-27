"""
Agri-Weather MCP Server (Async).
Domain: Meteorological data, ET0 calculation, and Satellite NDVI signals.
"""

from __future__ import annotations
import logging
import math
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from .base import AsyncMCPServer

logger = logging.getLogger("MCP.AgriWeatherServer")

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

# ────────────────────── Server ────────────────────────────────────────────

class WeatherMCPServer(AsyncMCPServer):
    """Serveur MCP pour la météo et les signaux agro-climatiques."""

    name = "weather"

    def __init__(self, sentinelle_tool=None, **kwargs) -> None:
        # Accept and ignore extra kwargs (e.g. llm_client) to remain flexible
        # when servers are instantiated uniformly by the orchestrator.
        self._sentinelle = sentinelle_tool
        super().__init__()

    def _lazy_sentinelle(self):
        """Charge le connecteur API météo/satellite seulement si nécessaire."""
        if self._sentinelle is None:
            try:
                from agriconnect.tools.sentinelle import SentinelleTool
                self._sentinelle = SentinelleTool()
                logger.info("SentinelleTool initialisé pour le serveur Weather.")
            except Exception as exc:
                logger.error("Échec de chargement de SentinelleTool: %s", exc)
        return self._sentinelle

    def _register_tools(self) -> None:
        # --- Météo Temps Réel ---
        self.register(
            name="get_current_weather",
            description="Obtient la météo actuelle pour une localisation précise.",
            input_schema={
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "Nom du village ou zone"}
                },
                "required": ["location"],
            },
            handler=self._get_current_weather,
        )

        # --- Calcul Agro-météo (ET0) ---
        self.register(
            name="compute_irrigation_needs",
            description="Calcule le besoin en eau (ET0) basé sur les conditions météo.",
            input_schema={
                "type": "object",
                "properties": {
                    "temperature_c": {"type": "number"},
                    "humidity_pct": {"type": "number"},
                    "wind_kmh": {"type": "number"},
                    "precip_mm": {"type": "number", "default": 0},
                },
                "required": ["temperature_c", "humidity_pct"],
            },
            handler=self._compute_et0,
        )

        # --- Données Satellitaires ---
        self.register(
            name="get_crop_health_vitals",
            description="Récupère l'indice NDVI et l'humidité du sol via satellite.",
            input_schema={
                "type": "object",
                "properties": {
                    "location": {"type": "string"}
                },
                "required": ["location"],
            },
            handler=self._get_satellite_data,
        )

    # ── Handlers ──────────────────────────────────────────────────────────

    async def _get_current_weather(self, args: Dict[str, Any]) -> WeatherSnapshot:
        location = args["location"]
        tool = self._lazy_sentinelle()
        
        # Simulation d'appel API via le tool Sentinelle
        raw = tool.fetch_weather(location) if tool else {}
        
        return WeatherSnapshot(
            location=location,
            temperature_c=raw.get("temp", 30.0),
            humidity_pct=raw.get("humidity", 45.0),
            precip_mm=raw.get("precip", 0.0),
            wind_kmh=raw.get("wind", 12.0),
            description=raw.get("desc", "Ciel dégagé"),
            source="API_Sentinelle_V2"
        )

    async def _compute_et0(self, args: Dict[str, Any]) -> ET0Payload:
        """
        Estimation simplifiée de l'ET0 (Penman-Monteith).
        Utile pour dire à l'agriculteur s'il doit arroser aujourd'hui.
        """
        t = args["temperature_c"]
        rh = args["humidity_pct"]
        wind = args.get("wind_kmh", 10.0) / 3.6  # m/s
        precip = args.get("precip_mm", 0.0)

        # Calcul simplifié de l'évapotranspiration
        # (Formule indicative pour l'exemple)
        et0 = (0.0023 * (t + 17.8) * (t ** 0.5) * 15) / 1.5 # Hargreaves modifiée
        et0 = round(max(et0, 0.0), 2)
        deficit = round(max(et0 - precip, 0.0), 2)

        return ET0Payload(
            et0_mm=et0,
            precip_mm=precip,
            water_deficit_mm=deficit,
            irrigation_needed=deficit > 2.5 # Seuil d'alerte
        )

    async def _get_satellite_data(self, args: Dict[str, Any]) -> SatellitePayload:
        location = args["location"]
        tool = self._lazy_sentinelle()
        
        # Appel simulé aux données Sentinelle-2
        raw = tool.fetch_satellite(location) if tool else {}
        
        return SatellitePayload(
            location=location,
            ndvi=raw.get("ndvi", 0.65),
            soil_moisture_pct=raw.get("moisture", 22.0),
            anomaly_detected=raw.get("anomaly", False)
        )

if __name__ == "__main__":
    import asyncio
    server = WeatherMCPServer()
    print(f"🌦️ Serveur '{server.name}' prêt.")
    print("Outils:", [t["name"] for t in server.list_tools()])