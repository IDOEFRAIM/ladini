from __future__ import annotations

from fastmcp import FastMCP

from agriconnect.protocols.mcp.tools.market import MarketProvider

_PROVIDER = MarketProvider()
_MCP = FastMCP("AgriConnect Market Server")


@_MCP.tool(name="get_products")
async def get_products(zone: str = "", crop: str = "", limit: int = 20) -> str:
    return await _PROVIDER.get_products(zone=zone, crop=crop, limit=limit)


@_MCP.tool(name="prepare_transaction")
async def prepare_transaction(
    product_id: str,
    quantity_kg: float,
    price_fcfa_per_unit: float,
    buyer_phone: str,
    zone_id: str = "",
) -> str:
    return await _PROVIDER.prepare_transaction(
        product_id=product_id,
        quantity_kg=quantity_kg,
        price_fcfa_per_unit=price_fcfa_per_unit,
        buyer_phone=buyer_phone,
        zone_id=zone_id,
    )


@_MCP.tool(name="commit_transaction")
async def commit_transaction(transaction_id: str, approved: bool = False) -> str:
    return await _PROVIDER.commit_transaction(transaction_id=transaction_id, approved=approved)


if __name__ == "__main__":
    _MCP.run()
