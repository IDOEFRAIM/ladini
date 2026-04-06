from __future__ import annotations

import json

from fastmcp import FastMCP

from agriconnect.protocols.mcp.tools.weather import WeatherProvider

_PROVIDER = WeatherProvider()
_MCP = FastMCP("AgriConnect Weather Server")


@_MCP.tool(name="get_current_weather")
async def get_current_weather(location: str) -> str:
    return await _PROVIDER.get_current_weather(location=location)


@_MCP.tool(name="compute_irrigation_needs")
async def compute_irrigation_needs(
    temperature_c: float,
    humidity_pct: float,
    wind_kmh: float = 10.0,
    precip_mm: float = 0.0,
) -> str:
    return await _PROVIDER.compute_irrigation_needs(
        temperature_c=temperature_c,
        humidity_pct=humidity_pct,
        wind_kmh=wind_kmh,
        precip_mm=precip_mm,
    )


@_MCP.tool(name="get_crop_health_vitals")
async def get_crop_health_vitals(location: str) -> str:
    return await _PROVIDER.get_crop_health_vitals(location=location)


@_MCP.resource("weather://status")
async def weather_status() -> str:
    return json.dumps(await _PROVIDER.ping(), ensure_ascii=False)


if __name__ == "__main__":
    _MCP.run()
