from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

from agriconnect.infrastructure.mcp.base import MCPToolSpec

logger = logging.getLogger("MCP.Tools.Market")

FINANCIAL_REVIEW_THRESHOLD_FCFA: int = 100_000


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


class TransactionPayload(BaseModel):
    transaction_id: str = Field(default_factory=lambda: str(uuid.uuid4())[:12])
    product_id: str
    quantity_kg: str
    price_fcfa_per_unit: float
    total_fcfa: float
    buyer_phone: str
    seller_phone: Optional[str] = None
    zone_id: Optional[str] = None
    requires_human_review: bool = False
    review_reason: Optional[str] = None
    status: str = "PENDING"


class MarketProvider:
    """Pure market capability provider with DI for tool and DB services."""

    name = "market"

    def __init__(self, marketplace_tool: Any = None, db_service: Any = None) -> None:
        self._marketplace_tool = marketplace_tool
        self._db_service = db_service

    def _lazy_tool(self) -> Any:
        if self._marketplace_tool is None:
            try:
                from agriconnect.tools.marketplace_legacy import MarketplaceTool

                self._marketplace_tool = MarketplaceTool()
            except Exception as exc:
                logger.error("MarketplaceTool unavailable: %s", exc)
        return self._marketplace_tool

    def _db(self) -> Any:
        if self._db_service is not None:
            return self._db_service
        try:
            from agriconnect.infrastructure.mcp.runtime import runtime

            return runtime.db
        except Exception:
            return None

    async def get_products(self, zone: str = "", crop: str = "", limit: int = 20, ctx: Any = None) -> str:
        if ctx:
            await ctx.info(f"get_products zone={zone} crop={crop}")

        tool = self._lazy_tool()
        products: List[ProductItem] = []
        if tool:
            try:
                raw = tool.find_products_for_buyer(crop or "", zone)
                for p in (raw or [])[:limit]:
                    products.append(
                        ProductItem(
                            product_id=str(p.get("id", "")),
                            name=p.get("name", ""),
                            price_fcfa=float(p.get("price", 0)),
                            quantity_kg=float(p.get("quantity", 0)),
                            unit=p.get("unit", "kg"),
                            zone_id=zone or None,
                            seller_phone=p.get("seller_phone"),
                        )
                    )
            except Exception as exc:
                logger.warning("get_products failed: %s", exc)

        payload = ProductListPayload(zone=zone or None, crop=crop or None, products=products)
        return payload.model_dump_json(indent=2)

    async def get_stock(self, farm_id: str, ctx: Any = None) -> str:
        if ctx:
            await ctx.info(f"get_stock farm_id={farm_id}")

        tool = self._lazy_tool()
        entries: List[StockEntry] = []
        total_value = 0.0
        if tool:
            try:
                stocks = tool.get_stocks(farm_id)
                for s in (stocks or []):
                    kg = float(s.get("quantity", 0))
                    price = float(s.get("unit_price_fcfa", 0))
                    entries.append(
                        StockEntry(
                            item_name=s.get("item_name", ""),
                            quantity_kg=kg,
                            unit=s.get("unit", "kg"),
                            last_updated=s.get("updated_at"),
                        )
                    )
                    total_value += kg * price
            except Exception as exc:
                logger.warning("get_stock failed: %s", exc)

        return StockPayload(farm_id=farm_id, entries=entries, total_value_fcfa=total_value).model_dump_json(indent=2)

    async def get_average_price(self, crop: str, zone: str = "", ctx: Any = None) -> str:
        if ctx:
            await ctx.info(f"get_average_price crop={crop} zone={zone}")

        tool = self._lazy_tool()
        avg = 0.0
        if tool:
            try:
                avg = float(tool.get_average_price(crop, zone) or 0)
            except Exception as exc:
                logger.warning("get_average_price failed: %s", exc)

        return PricePayload(crop=crop, zone=zone or None, average_fcfa_per_kg=avg).model_dump_json(indent=2)

    async def prepare_transaction(
        self,
        product_id: str,
        quantity_kg: float,
        price_fcfa_per_unit: float,
        buyer_phone: str,
        zone_id: str = "",
        ctx: Any = None,
    ) -> str:
        if ctx:
            await ctx.info(f"prepare_transaction product={product_id} qty={quantity_kg}")

        total = round(quantity_kg * price_fcfa_per_unit, 2)
        requires_review = total > FINANCIAL_REVIEW_THRESHOLD_FCFA
        review_reason = (
            f"Transaction de {total:,.0f} FCFA > seuil de verification {FINANCIAL_REVIEW_THRESHOLD_FCFA:,} FCFA"
            if requires_review
            else None
        )

        db = self._db()
        staging: Dict[str, Any] = {}
        if db:
            try:
                staging = await db.prepare_transaction_staging(
                    {
                        "product_id": product_id,
                        "quantity_kg": quantity_kg,
                        "price_fcfa_per_unit": price_fcfa_per_unit,
                        "buyer_phone": buyer_phone,
                        "zone_id": zone_id or None,
                        "total_fcfa": total,
                        "requires_human_review": requires_review,
                        "review_reason": review_reason,
                        "source": "MCP",
                    }
                )
            except Exception as exc:
                logger.warning("prepare_transaction staging failed: %s", exc)

        if ctx and requires_review:
            await ctx.warning(f"Transaction > seuil: {total:,.0f} FCFA - review humaine requise")

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

    async def commit_transaction(self, transaction_id: str, approved: bool = False, ctx: Any = None) -> str:
        if ctx:
            await ctx.info(f"commit_transaction id={transaction_id} approved={approved}")

        if not approved:
            return TransactionPayload(
                transaction_id=transaction_id,
                product_id="",
                quantity_kg=0,
                price_fcfa_per_unit=0.0,
                total_fcfa=0.0,
                buyer_phone="",
                status="REJECTED",
            ).model_dump_json(indent=2)

        db = self._db()
        if not db:
            return TransactionPayload(
                transaction_id=transaction_id,
                product_id="",
                quantity_kg=0,
                price_fcfa_per_unit=0.0,
                total_fcfa=0.0,
                buyer_phone="",
                status="REJECTED",
            ).model_dump_json(indent=2)

        try:
            res = await db.commit_staged_transaction(transaction_id, approved=approved)
            status = res.get("status")
            if status == "COMMITTED":
                payload = {"transaction_id": transaction_id, "status": "COMMITTED", "message": res.get("message", "OK")}
            else:
                payload = {"transaction_id": transaction_id, "status": "REJECTED", "message": res.get("message", "Failed")}
        except Exception as exc:
            logger.error("Commit transaction failed: %s", exc)
            payload = {"transaction_id": transaction_id, "status": "REJECTED", "message": str(exc)}

        import json

        return json.dumps(payload, ensure_ascii=False)

    async def find_buyers(self, crop: str, zone: str = "", ctx: Any = None) -> str:
        if ctx:
            await ctx.info(f"find_buyers crop={crop} zone={zone}")

        tool = self._lazy_tool()
        buyers: List[Dict[str, Any]] = []
        if tool:
            try:
                raw = tool.find_buyers_for_product(crop, zone)
                for b in (raw or []):
                    buyers.append({"buyer_phone": b.get("buyer_phone", ""), "zone_name": b.get("zone_name"), "desired_quantity_kg": b.get("quantity")})
            except Exception as exc:
                logger.warning("find_buyers failed: %s", exc)

        import json

        return json.dumps({"crop": crop, "zone": zone or None, "buyers": buyers}, ensure_ascii=False)

    def get_tools(self) -> List[MCPToolSpec]:
        return [
            MCPToolSpec(name="get_products", handler=self.get_products),
            MCPToolSpec(name="get_stock", handler=self.get_stock),
            MCPToolSpec(name="get_average_price", handler=self.get_average_price),
            MCPToolSpec(name="prepare_transaction", handler=self.prepare_transaction),
            MCPToolSpec(name="commit_transaction", handler=self.commit_transaction),
            MCPToolSpec(name="find_buyers", handler=self.find_buyers),
        ]

    async def ping(self) -> Dict[str, Any]:
        return {
            "status": "ready",
            "marketplace_tool_loaded": bool(self._marketplace_tool),
            "db_bound": bool(self._db_service),
        }
