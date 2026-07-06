from __future__ import annotations

"""Buyer domain shared helpers (support footer, safe MCP calls)."""

import logging
from typing import Any, Dict, Optional

from agriconnect.graphs.agents.market_coach.utils import ensure_dict

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


async def safe_call_tool(mc_runtime: Any, tool_name: str, **kwargs: Any) -> Dict[str, Any]:
    """Wrapper unique et sécurisé pour tout appel MCP du buyer_flow."""
    try:
        raw = await mc_runtime.call_db(
            tool_name, **{k: v for k, v in kwargs.items() if v is not None}
        )
        result = ensure_dict(raw)
        logger.info("safe_call_tool | tool=%s | status=%s", tool_name, result.get("status"))
        return result
    except Exception as exc:  # noqa: BLE001 — résilience volontaire
        logger.exception("safe_call_tool | tool=%s | échec: %s", tool_name, exc)
        return {
            "status": "error",
            "tool": tool_name,
            "message": with_support_footer(
                "Service temporairement indisponible, veuillez réessayer."
            ),
            "_exception": str(exc),
        }


__all__ = ["SUPPORT_FOOTER", "with_support_footer", "safe_call_tool"]
