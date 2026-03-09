"""
Market MCP Server (FastMCP).
Domain: stock management, pricing, and transaction payloads.

Safety:
  - ``prepare_transaction`` is read-only (staging, no write).
  - ``commit_transaction`` writes to DB only after user confirmation.
  - Transactions > 100 000 FCFA flagged for human review.

Tools:
  - get_products(zone, crop, limit)
  - get_stock(farm_id)
  - get_average_price(crop, zone)
  - prepare_transaction(product_id, quantity_kg, price_fcfa_per_unit, buyer_phone, zone_id)
  - commit_transaction(transaction_id, approved)
  - find_buyers(crop, zone)
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator
from fastmcp import FastMCP, Context

logger = logging.getLogger("MCP.MarketServer")

# ────────────────────── FastMCP instance ──────────────────────────────────

mcp = FastMCP("AgriConnect Market MCP Server")

# ────────────────────── Constants ─────────────────────────────────────────

FINANCIAL_REVIEW_THRESHOLD_FCFA: int = 100_000

# ────────────────────── Lazy singletons ───────────────────────────────────

_marketplace_tool = None


def _lazy_tool():
    global _marketplace_tool
    if _marketplace_tool is None:
        try:
            from agriconnect.tools.marketplace import MarketplaceTool
            _marketplace_tool = MarketplaceTool()
        except Exception as exc:
            logger.error("MarketplaceTool unavailable: %s", exc)
    return _marketplace_tool


# DB service is provided by the MCP runtime; callers should use `runtime.db`.


# ────────────────────── Pydantic models ───────────────────────────────────

class ProductItem(BaseModel):
    product_id: str
    name: str
    price_fcfa: float
    quantity_kg: float
    unit: str = "kg"
    zone_id: Optional[str] = None
    seller_phone: Optional[str] = None


class ProductListPayload(BaseModel):
    zone: Optional[str] = None
    crop: Optional[str] = None
    products: List[ProductItem] = Field(default_factory=list)


class StockEntry(BaseModel):
    item_name: str
    quantity_kg: float
    unit: str = "kg"
    last_updated: Optional[str] = None


class StockPayload(BaseModel):
    farm_id: str
    entries: List[StockEntry] = Field(default_factory=list)
    total_value_fcfa: float = 0.0


class PricePayload(BaseModel):
    crop: str
    zone: Optional[str] = None
    average_fcfa_per_kg: float
    min_fcfa: Optional[float] = None
    max_fcfa: Optional[float] = None
    data_points: int = 0


class TransactionPayload(BaseModel):
    transaction_id: str = Field(default_factory=lambda: str(uuid.uuid4())[:12])
    product_id: str
    quantity_kg: float
    price_fcfa_per_unit: float
    total_fcfa: float
    buyer_phone: str
    seller_phone: Optional[str] = None
    zone_id: Optional[str] = None
    requires_human_review: bool = False
    review_reason: Optional[str] = None
    status: str = "PENDING"

    @field_validator("total_fcfa")
    @classmethod
    def check_positive(cls, v: float) -> float:
        if v < 0:
            raise ValueError("total_fcfa must be >= 0")
        return v


class CommitResult(BaseModel):
    transaction_id: str
    status: str
    message: str


class BuyerEntry(BaseModel):
    buyer_phone: str
    zone_name: Optional[str] = None
    desired_quantity_kg: Optional[float] = None


class BuyerListPayload(BaseModel):
    crop: str
    zone: Optional[str] = None
    buyers: List[BuyerEntry] = Field(default_factory=list)


# ────────────────────── Tools ─────────────────────────────────────────────

@mcp.tool()
async def get_products(
    zone: str = "",
    crop: str = "",
    limit: int = 20,
    ctx: Context = None,
) -> str:
    """Liste les produits disponibles dans une zone (données brutes).

    Args:
        zone: Zone géographique (optionnel)
        crop: Culture filtrée (optionnel)
        limit: Nombre max de produits
        ctx: MCP context

    Returns:
        JSON ProductListPayload
    """
    if ctx:
        await ctx.info(f"get_products zone={zone} crop={crop}")

    tool = _lazy_tool()
    products: List[ProductItem] = []
    if tool:
        try:
            raw = tool.find_products_for_buyer(crop or "", zone)
            for p in (raw or [])[:limit]:
                products.append(ProductItem(
                    product_id=str(p.get("id", "")),
                    name=p.get("name", ""),
                    price_fcfa=float(p.get("price", 0)),
                    quantity_kg=float(p.get("quantity", 0)),
                    unit=p.get("unit", "kg"),
                    zone_id=zone or None,
                    seller_phone=p.get("seller_phone"),
                ))
        except Exception as exc:
            logger.warning("get_products failed: %s", exc)

    payload = ProductListPayload(zone=zone or None, crop=crop or None, products=products)
    return payload.model_dump_json(indent=2)


@mcp.tool()
async def get_stock(farm_id: str, ctx: Context = None) -> str:
    """Stock actuel d'une ferme.

    Args:
        farm_id: Identifiant de la ferme
        ctx: MCP context

    Returns:
        JSON StockPayload
    """
    if ctx:
        await ctx.info(f"get_stock farm_id={farm_id}")

    tool = _lazy_tool()
    entries: List[StockEntry] = []
    total_value = 0.0

    if tool:
        try:
            stocks = tool.get_stocks(farm_id)
            for s in (stocks or []):
                kg = float(s.get("quantity", 0))
                price = float(s.get("unit_price_fcfa", 0))
                entries.append(StockEntry(
                    item_name=s.get("item_name", ""),
                    quantity_kg=kg,
                    unit=s.get("unit", "kg"),
                    last_updated=s.get("updated_at"),
                ))
                total_value += kg * price
        except Exception as exc:
            logger.warning("get_stock failed: %s", exc)

    payload = StockPayload(farm_id=farm_id, entries=entries, total_value_fcfa=total_value)
    return payload.model_dump_json(indent=2)


@mcp.tool()
async def get_average_price(crop: str, zone: str = "", ctx: Context = None) -> str:
    """Prix moyen au kg pour une culture dans une zone.

    Args:
        crop: Nom de la culture
        zone: Zone géographique (optionnel)
        ctx: MCP context

    Returns:
        JSON PricePayload
    """
    if ctx:
        await ctx.info(f"get_average_price crop={crop} zone={zone}")

    tool = _lazy_tool()
    avg = 0.0
    if tool:
        try:
            avg = float(tool.get_average_price(crop, zone) or 0)
        except Exception as exc:
            logger.warning("get_average_price failed: %s", exc)

    payload = PricePayload(crop=crop, zone=zone or None, average_fcfa_per_kg=avg)
    return payload.model_dump_json(indent=2)


@mcp.tool()
async def prepare_transaction(
    product_id: str,
    quantity_kg: float,
    price_fcfa_per_unit: float,
    buyer_phone: str,
    zone_id: str = "",
    ctx: Context = None,
) -> str:
    """Prépare un payload de transaction SANS écrire en base.

    Le payload retourné contient un ``transaction_id`` à passer à ``commit_transaction``
    après confirmation utilisateur.

    Args:
        product_id: ID du produit
        quantity_kg: Quantité en kg
        price_fcfa_per_unit: Prix unitaire en FCFA
        buyer_phone: Téléphone de l'acheteur
        zone_id: Zone (optionnel)
        ctx: MCP context

    Returns:
        JSON TransactionPayload
    """
    if ctx:
        await ctx.info(f"prepare_transaction product={product_id} qty={quantity_kg}")

    total = round(quantity_kg * price_fcfa_per_unit, 2)
    requires_review = total > FINANCIAL_REVIEW_THRESHOLD_FCFA
    review_reason = (
        f"Transaction de {total:,.0f} FCFA > seuil de vérification {FINANCIAL_REVIEW_THRESHOLD_FCFA:,} FCFA"
        if requires_review else None
    )

    from agriconnect.protocols.mcp.infrastructure import runtime
    db = runtime.db
    staging = {}
    if db:
        try:
            staging = await db.prepare_transaction_staging({
                "product_id": product_id,
                "quantity_kg": quantity_kg,
                "price_fcfa_per_unit": price_fcfa_per_unit,
                "buyer_phone": buyer_phone,
                "zone_id": zone_id or None,
                "total_fcfa": total,
                "requires_human_review": requires_review,
                "review_reason": review_reason,
                "source": "MCP",
            })
        except Exception as exc:
            logger.warning("prepare_transaction staging failed: %s", exc)

    if ctx and requires_review:
        await ctx.warning(f"Transaction > seuil: {total:,.0f} FCFA — review humaine requise")

    payload = TransactionPayload(
        product_id=product_id,
        quantity_kg=quantity_kg,
        price_fcfa_per_unit=price_fcfa_per_unit,
        total_fcfa=total,
        buyer_phone=buyer_phone,
        zone_id=zone_id or None,
        requires_human_review=requires_review,
        review_reason=review_reason,
        status="PENDING",
        transaction_id=staging.get("transaction_id", str(uuid.uuid4())[:12]),
    )
    return payload.model_dump_json(indent=2)


@mcp.tool()
async def commit_transaction(
    transaction_id: str,
    approved: bool = False,
    ctx: Context = None,
) -> str:
    """Écrit la transaction en base après confirmation utilisateur.

    Args:
        transaction_id: ID de la transaction staging
        approved: True = committer, False = annuler
        ctx: MCP context

    Returns:
        JSON CommitResult
    """
    if ctx:
        await ctx.info(f"commit_transaction id={transaction_id} approved={approved}")

    if not approved:
        result = CommitResult(
            transaction_id=transaction_id,
            status="REJECTED",
            message="Transaction annulée par l'utilisateur.",
        )
        return result.model_dump_json(indent=2)

    from agriconnect.protocols.mcp.infrastructure import runtime
    db = runtime.db
    if not db:
        result = CommitResult(transaction_id=transaction_id, status="REJECTED", message="DB service unavailable")
        return result.model_dump_json(indent=2)

    try:
        res = await db.commit_staged_transaction(transaction_id, approved=True)
        status = res.get("status")
        if status == "COMMITTED":
            result = CommitResult(transaction_id=transaction_id, status="COMMITTED", message=res.get("message", "OK"))
        else:
            result = CommitResult(transaction_id=transaction_id, status="REJECTED", message=res.get("message", "Failed"))
    except Exception as exc:
        logger.error("Commit transaction failed: %s", exc)
        result = CommitResult(transaction_id=transaction_id, status="REJECTED", message=str(exc))

    return result.model_dump_json(indent=2)


@mcp.tool()
async def find_buyers(crop: str, zone: str = "", ctx: Context = None) -> str:
    """Trouve les acheteurs intéressés par une culture dans une zone.

    Args:
        crop: Nom de la culture
        zone: Zone géographique (optionnel)
        ctx: MCP context

    Returns:
        JSON BuyerListPayload
    """
    if ctx:
        await ctx.info(f"find_buyers crop={crop} zone={zone}")

    tool = _lazy_tool()
    buyers: List[BuyerEntry] = []
    if tool:
        try:
            raw = tool.find_buyers_for_product(crop, zone)
            for b in (raw or []):
                buyers.append(BuyerEntry(
                    buyer_phone=b.get("buyer_phone", ""),
                    zone_name=b.get("zone_name"),
                    desired_quantity_kg=b.get("quantity"),
                ))
        except Exception as exc:
            logger.warning("find_buyers failed: %s", exc)

    payload = BuyerListPayload(crop=crop, zone=zone or None, buyers=buyers)
    return payload.model_dump_json(indent=2)


# ────────────────────── Backward-compatible class wrapper ─────────────────

class MarketMCPServer:
    """Compat wrapper for in-process callers."""

    name = "market"

    def __init__(self, marketplace_tool=None, session_factory=None):
        global _marketplace_tool
        if marketplace_tool is not None:
            _marketplace_tool = marketplace_tool

    @staticmethod
    def list_tools():
        return [
            {"name": "get_products", "description": "Liste les produits disponibles dans une zone"},
            {"name": "get_stock", "description": "Stock actuel d'une ferme"},
            {"name": "get_average_price", "description": "Prix moyen au kg pour une culture"},
            {"name": "prepare_transaction", "description": "Prépare un payload de transaction (staging)"},
            {"name": "commit_transaction", "description": "Écrit la transaction en base"},
            {"name": "find_buyers", "description": "Trouve les acheteurs pour une culture"},
        ]

    @staticmethod
    async def _dispatch(name: str, args: Dict[str, Any]) -> str:
        handlers = {
            "get_products": get_products,
            "get_stock": get_stock,
            "get_average_price": get_average_price,
            "prepare_transaction": prepare_transaction,
            "commit_transaction": commit_transaction,
            "find_buyers": find_buyers,
        }
        fn = handlers.get(name)
        if not fn:
            raise ValueError(f"Unknown market tool: {name}")
        return await fn(**args)

    def call_tool_sync(self, name: str, arguments: dict) -> dict:
        import asyncio
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        raw = loop.run_until_complete(self._dispatch(name, arguments))
        return {"ok": True, "data": json.loads(raw) if isinstance(raw, str) else raw}


# ────────────────────── Entry point ───────────────────────────────────────

if __name__ == "__main__":
    logger.info("Starting AgriConnect Market MCP Server")
    mcp.run()
