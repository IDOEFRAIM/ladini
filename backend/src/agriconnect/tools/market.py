import logging
from typing import Any, Dict, Optional

from agriconnect.tools.marketplace_legacy import MarketplaceTool

logger = logging.getLogger("Tool.AgrimarketCompat")


class AgrimarketTool:
    """Backward-compatible adapter for legacy MarketCoach expectations.

    The modern flow should use MCP tools first. This adapter only provides a
    conservative fallback when MCP is unavailable.
    """

    def __init__(self):
        self._legacy = MarketplaceTool()

    def get_commodity_price(self, product_name: str, zone_id: Optional[str] = None) -> Dict[str, Any]:
        try:
            avg = self._legacy.get_average_price(product_name, zone_id=zone_id)
            return {
                "product": product_name,
                "average_price_fcfa_per_kg": avg,
                "currency": "FCFA",
            }
        except Exception as exc:
            logger.warning("Unable to compute commodity price for %s: %s", product_name, exc)
            return {
                "product": product_name,
                "average_price_fcfa_per_kg": None,
                "currency": "FCFA",
            }

    def analyze_market_trends(self, product_name: str, zone_id: Optional[str] = None) -> Dict[str, Any]:
        price_info = self.get_commodity_price(product_name, zone_id=zone_id)
        avg = price_info.get("average_price_fcfa_per_kg")
        trend = "UNKNOWN"
        if isinstance(avg, (int, float)):
            trend = "STABLE"

        return {
            "product": product_name,
            "trend": trend,
            "reference_price_fcfa_per_kg": avg,
            "method": "legacy_avg_price_snapshot",
        }

    @staticmethod
    def get_logistics_info(location: Optional[str]) -> Dict[str, Any]:
        if not location:
            return {
                "location": None,
                "routing_hint": "unknown",
                "notes": "Aucune localisation fournie.",
            }
        return {
            "location": location,
            "routing_hint": "manual_confirmation_required",
            "notes": "Verifier les couts de transport localement avant validation.",
        }

    def register_surplus_offer(self, product_name: str, quantity_kg: float, location: Optional[str] = None) -> bool:
        """Fallback-only local registration.

        Without producer identity and explicit price, local persistence cannot be
        trusted. Return False to force explicit MCP-backed flow.
        """
        logger.warning(
            "register_surplus_offer fallback skipped for product=%s quantity=%s location=%s; MCP flow required",
            product_name,
            quantity_kg,
            location,
        )
        return False
