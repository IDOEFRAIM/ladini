"""MCP domain gateways — typed wrappers around ``mc_runtime.call_db``.

Each gateway encapsulates a single MCP domain (profile, farm, auction, …),
providing explicit method signatures instead of raw ``call_db(tool_name, **kw)``
calls scattered across the codebase.

Usage::

    gw = ProfileGateway(mc_runtime)
    profile = await gw.get_user_by_phone("+226…")
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from agriconnect.graphs.agents.market_coach.utils import ensure_dict

logger = logging.getLogger("AgriConnect.Market.MCPGateway")


class _BaseGateway:
    __slots__ = ("_rt",)

    def __init__(self, mc_runtime: Any) -> None:
        self._rt = mc_runtime

    async def _call(self, tool: str, **kwargs: Any) -> Dict[str, Any]:
        raw = await self._rt.call_db(tool, **{k: v for k, v in kwargs.items() if v is not None})
        return ensure_dict(raw)


# ── Profile ────────────────────────────────────────────────────────

class ProfileGateway(_BaseGateway):
    async def get_user_by_phone(self, phone: str) -> Dict[str, Any]:
        result = await self._call("get_user_by_phone", phone=phone.strip())
        logger.debug("ProfileGateway.get_user_by_phone status=%s", result.get("status"))
        return result

    async def identify_or_create_user(self, phone: str) -> Dict[str, Any]:
        return await self._call("identify_or_create_user", phone=phone.strip())


# ── Farm ───────────────────────────────────────────────────────────

class FarmGateway(_BaseGateway):
    async def list_farms(self, phone: str) -> List[Dict[str, Any]]:
        result = await self._call("get_producer_farm", phone=phone.strip())
        farms = result.get("data") or result.get("farms") or result.get("results") or []
        return farms if isinstance(farms, list) else []

    async def list_farms_alt(self, phone: str) -> List[Dict[str, Any]]:
        """Alternative endpoint used by producer flow (``get_farms``)."""
        result = await self._call("get_farms", phone=phone.strip())
        farms = result.get("data") or []
        return farms if isinstance(farms, list) else []

    async def create_farm(self, **kwargs: Any) -> Dict[str, Any]:
        result = await self._call("create_farm", **kwargs)
        return result.get("data") or result


# ── Auctions & Bids ───────────────────────────────────────────────

class AuctionGateway(_BaseGateway):
    async def search_open_auctions(self, **kwargs: Any) -> Dict[str, Any]:
        return await self._call("get_auctions", **kwargs)

    async def get_my_active_bids(self, phone: str) -> Dict[str, Any]:
        return await self._call("get_my_active_bids", phone=phone.strip())

    async def get_auctions_bids(self, **kwargs: Any) -> Dict[str, Any]:
        return await self._call("get_auctions_bids", **kwargs)

    async def get_auction_bids(self, auction_id: str) -> Dict[str, Any]:
        return await self._call("get_auction_bids", auction_id=auction_id)

    async def select_winning_bid(self, bid_id: str) -> Dict[str, Any]:
        return await self._call("select_winning_bid", bid_id=bid_id)


# ── Stock ──────────────────────────────────────────────────────────

class StockGateway(_BaseGateway):
    async def get_producer_stocks(self, phone: str) -> Dict[str, Any]:
        return await self._call("get_producer_stocks", phone=phone.strip())

    async def validate_stock_availability(
        self, product_id: str, quantity: float, unit: str, buyer_phone: str,
    ) -> Dict[str, Any]:
        return await self._call(
            "validate_stock_availability_atomic",
            product_id=product_id,
            quantity=quantity,
            unit=unit,
            buyer_phone=buyer_phone,
        )


# ── Product Search ─────────────────────────────────────────────────

class ProductGateway(_BaseGateway):
    async def search_products(self, product: str, phone: str) -> Dict[str, Any]:
        return await self._call("search_products", product=product, phone=phone)


# ── Negotiation ────────────────────────────────────────────────────

class NegotiationGateway(_BaseGateway):
    async def update_offer(self, buyer_phone: str, negotiation_id: str, new_price: Any) -> Dict[str, Any]:
        return await self._call(
            "update_negotiation_offer",
            buyer_phone=buyer_phone,
            negotiation_id=negotiation_id,
            new_price=new_price,
        )

    async def close_session(self, buyer_phone: str, negotiation_id: str, reason: str = "buyer_abandoned") -> Dict[str, Any]:
        return await self._call(
            "close_negotiation_session",
            buyer_phone=buyer_phone,
            negotiation_id=negotiation_id,
            reason=reason,
        )


# ── Preorder ───────────────────────────────────────────────────────

class PreorderGateway(_BaseGateway):
    async def create_draft(self, buyer_phone: str, cart_items: Any, payment_method: str = "CASH", delivery_zone_id: Any = None) -> Dict[str, Any]:
        return await self._call(
            "create_preorder_draft",
            buyer_phone=buyer_phone,
            cart_items=cart_items,
            payment_method=payment_method,
            delivery_zone_id=delivery_zone_id,
        )

    async def confirm_draft(self, buyer_phone: str, preorder_id: str) -> Dict[str, Any]:
        return await self._call("confirm_preorder_draft", buyer_phone=buyer_phone, preorder_id=preorder_id)


# ── Order Tracking ─────────────────────────────────────────────────

class OrderTrackingGateway(_BaseGateway):
    async def get_transaction_summary(self, **kwargs: Any) -> Dict[str, Any]:
        return await self._call("get_transaction_summary", **kwargs)

    async def get_buyer_orders_dashboard(self, phone: str) -> Dict[str, Any]:
        return await self._call("get_buyer_orders_dashboard", phone=phone.strip())


# ── Agent Actions ──────────────────────────────────────────────────

class AgentActionGateway(_BaseGateway):
    async def create_action(self, agent_name: str, action_type: str, payload: Any) -> Dict[str, Any]:
        return await self._call(
            "create_agent_action",
            agent_name=agent_name,
            action_type=action_type,
            payload=payload,
        )


__all__ = [
    "ProfileGateway",
    "FarmGateway",
    "AuctionGateway",
    "StockGateway",
    "ProductGateway",
    "NegotiationGateway",
    "PreorderGateway",
    "OrderTrackingGateway",
    "AgentActionGateway",
]
