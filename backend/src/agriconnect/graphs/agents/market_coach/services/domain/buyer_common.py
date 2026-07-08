from __future__ import annotations

"""Buyer domain shared helpers (support footer, safe MCP calls)."""

import logging
from typing import Any, Dict, Optional

from agriconnect.graphs.agents.market_coach.services.mcp.gateway import _BaseGateway

logger = logging.getLogger("AgriConnect.Market.BuyerCommon")

SUPPORT_FOOTER = (
    "🙏 Toute l'équipe Ladini s'excuse pour la gêne occasionnée.\n"
    "📞 Contactez-nous au +226 68 81 52 99 pour une assistance immédiate."
)


def with_support_footer(message: Optional[str]) -> Optional[str]:
    if not isinstance(message, str) or not message.strip():
        return SUPPORT_FOOTER
    if SUPPORT_FOOTER in message:
        return message
    return f"{message.rstrip()}\n\n{SUPPORT_FOOTER}"


class _BuyerGateway(_BaseGateway):
    """Thin buyer-specific wrapper that adds SUPPORT_FOOTER on errors."""

    async def safe_call(self, tool: str, **kwargs: Any) -> Dict[str, Any]:
        try:
            result = await self._call(tool, **kwargs)
            logger.info("safe_call | tool=%s | status=%s", tool, result.get("status"))
            return result
        except Exception as exc:  # noqa: BLE001
            logger.exception("safe_call | tool=%s | échec: %s", tool, exc)
            return {
                "status": "error",
                "tool": tool,
                "message": with_support_footer(
                    "Service temporairement indisponible, veuillez réessayer."
                ),
                "_exception": str(exc),
            }


async def safe_call_tool(mc_runtime: Any, tool_name: str, **kwargs: Any) -> Dict[str, Any]:
    """Legacy wrapper — delegates to _BuyerGateway.safe_call."""
    gw = _BuyerGateway(mc_runtime)
    return await gw.safe_call(tool_name, **kwargs)


__all__ = ["SUPPORT_FOOTER", "with_support_footer", "safe_call_tool", "_BuyerGateway"]
