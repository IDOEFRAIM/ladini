from __future__ import annotations

from agriconnect.infrastructure.mcp.base import MCPServerApp
from agriconnect.infrastructure.mcp.runtime import runtime
from agriconnect.protocols.mcp.tools.market import MarketProvider
from agriconnect.protocols.mcp.tools.weather import WeatherProvider
from agriconnect.protocols.mcp.tools.agronomy import AgronomyTools
from agriconnect.protocols.mcp.tools.units import UnitsTools


def build_mcp_app() -> MCPServerApp:
    # Import for side effects: registers DB resource/tools on the shared runtime MCP.
    import agriconnect.protocols.mcp.tools.db_handler  # noqa: F401

    providers = [
        AgronomyTools(),
        WeatherProvider(),
        MarketProvider(),
        UnitsTools(),
    ]
    return MCPServerApp(
        "AgriConnect Unified MCP Server",
        providers=providers,
        startup_callbacks=[runtime.start],
        shutdown_callbacks=[runtime.stop],
    )
