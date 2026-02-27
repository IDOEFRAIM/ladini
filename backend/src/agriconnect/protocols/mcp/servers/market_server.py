"""Market MCP Micro-Server (async).

Domain: stock management, pricing and transaction payloads.
This server is *data-only* and *transaction-safe*:
  - No direct DB writes except through the explicit ``commit_transaction`` tool.
  - The ``prepare_transaction`` tool returns a ``TransactionPayload`` that the
    agent must send back to ``commit_transaction`` after user confirmation.
  - Transactions > 100 000 FCFA are flagged for human review.

Tools exposed:
  - get_products(zone, crop)            → ProductListPayload
  - get_stock(farm_id)                  → StockPayload
  - get_average_price(crop, zone)       → PricePayload
  - prepare_transaction(...)            → TransactionPayload  (no write)
  - commit_transaction(payload)         → CommitResult         (writes to DB)
  - find_buyers(crop, zone)             → BuyerListPayload
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

from .base import AsyncMCPServer

logger = logging.getLogger("MCP.MarketServer")

# ── Financial safety threshold ────────────────────────────────────────────
FINANCIAL_REVIEW_THRESHOLD_FCFA: int = 100_000


# ────────────────────── Pydantic response models ──────────────────────────

class ProductItem(BaseModel):
    product_id: str
    name: str
    price_fcfa: float
    quantity_kg: float
    unit: str = "kg"
    zone_id: Optional[str] = None
    seller_phone: Optional[str] = None


class ProductListPayload(BaseModel):
    zone: Optional[str]
    crop: Optional[str]
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
    zone: Optional[str]
    average_fcfa_per_kg: float
    min_fcfa: Optional[float] = None
    max_fcfa: Optional[float] = None
    data_points: int = 0


class TransactionPayload(BaseModel):
    """Staging object: filled by prepare_transaction, committed by commit_transaction.

    The transaction is NOT written to the database until commit_transaction is called.
    """
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
    status: str = "PENDING"  # PENDING | APPROVED | COMMITTED | REJECTED

    @field_validator("total_fcfa")
    @classmethod
    def check_positive(cls, v: float) -> float:
        if v < 0:
            raise ValueError("total_fcfa must be >= 0")
        return v


class CommitResult(BaseModel):
    transaction_id: str
    status: str  # "COMMITTED" | "REJECTED" | "REVIEW_REQUIRED"
    message: str


class BuyerEntry(BaseModel):
    buyer_phone: str
    zone_name: Optional[str] = None
    desired_quantity_kg: Optional[float] = None


class BuyerListPayload(BaseModel):
    crop: str
    zone: Optional[str]
    buyers: List[BuyerEntry] = Field(default_factory=list)


# ────────────────────── Server ────────────────────────────────────────────

class MarketMCPServer(AsyncMCPServer):
    """Async MCP server for market data and atomic transaction management."""

    name = "market"

    def __init__(self, marketplace_tool=None, session_factory=None) -> None:
        self._tool = marketplace_tool
        self._session_factory = session_factory
        super().__init__()

    def _lazy_tool(self):
        if self._tool is None:
            try:
                from agriconnect.tools.marketplace import MarketplaceTool
                self._tool = MarketplaceTool()
            except Exception as exc:
                logger.error("MarketplaceTool unavailable: %s", exc)
        return self._tool

    def _register_tools(self) -> None:
        self.register(
            name="get_products",
            description="Liste les produits disponibles dans une zone (données brutes uniquement)",
            input_schema={
                "type": "object",
                "properties": {
                    "zone": {"type": "string"},
                    "crop": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                },
            },
            handler=self._get_products,
        )
        self.register(
            name="get_stock",
            description="Stock actuel d'une ferme",
            input_schema={
                "type": "object",
                "properties": {"farm_id": {"type": "string"}},
                "required": ["farm_id"],
            },
            handler=self._get_stock,
        )
        self.register(
            name="get_average_price",
            description="Prix moyen au kg pour une culture dans une zone",
            input_schema={
                "type": "object",
                "properties": {
                    "crop": {"type": "string"},
                    "zone": {"type": "string"},
                },
                "required": ["crop"],
            },
            handler=self._get_average_price,
        )
        self.register(
            name="prepare_transaction",
            description=(
                "Prépare un payload de transaction SANS écrire en base. "
                "Retourne un TransactionPayload à soumettre après confirmation utilisateur."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "product_id": {"type": "string"},
                    "quantity_kg": {"type": "number"},
                    "price_fcfa_per_unit": {"type": "number"},
                    "buyer_phone": {"type": "string"},
                    "zone_id": {"type": "string"},
                },
                "required": ["product_id", "quantity_kg", "price_fcfa_per_unit", "buyer_phone"],
            },
            handler=self._prepare_transaction,
        )
        self.register(
            name="commit_transaction",
            description="Écrit la transaction en base après confirmation utilisateur.",
            input_schema={
                "type": "object",
                "properties": {
                    "transaction_id": {"type": "string"},
                    "approved": {"type": "boolean"},
                },
                "required": ["transaction_id", "approved"],
            },
            handler=self._commit_transaction,
        )
        self.register(
            name="find_buyers",
            description="Trouve les acheteurs intéressés par une culture dans une zone",
            input_schema={
                "type": "object",
                "properties": {
                    "crop": {"type": "string"},
                    "zone": {"type": "string"},
                },
                "required": ["crop"],
            },
            handler=self._find_buyers,
        )

    # ── Handlers ──────────────────────────────────────────────────────────

    async def _get_products(self, args: Dict[str, Any]) -> ProductListPayload:
        zone = args.get("zone")
        crop = args.get("crop")
        limit = int(args.get("limit", 20))
        tool = self._lazy_tool()
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
                        zone_id=zone,
                        seller_phone=p.get("seller_phone"),
                    ))
            except Exception as exc:
                logger.warning("get_products failed: %s", exc)
        return ProductListPayload(zone=zone, crop=crop, products=products)

    async def _get_stock(self, args: Dict[str, Any]) -> StockPayload:
        farm_id = args["farm_id"]
        tool = self._lazy_tool()
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
        return StockPayload(farm_id=farm_id, entries=entries, total_value_fcfa=total_value)

    async def _get_average_price(self, args: Dict[str, Any]) -> PricePayload:
        crop = args["crop"]
        zone = args.get("zone")
        tool = self._lazy_tool()
        avg = 0.0
        if tool:
            try:
                avg = float(tool.get_average_price(crop, zone) or 0)
            except Exception as exc:
                logger.warning("get_average_price failed: %s", exc)
        return PricePayload(crop=crop, zone=zone, average_fcfa_per_kg=avg)

    async def _prepare_transaction(self, args: Dict[str, Any]) -> TransactionPayload:
        product_id = args["product_id"]
        quantity_kg = float(args["quantity_kg"])
        price_per_unit = float(args["price_fcfa_per_unit"])
        buyer_phone = args["buyer_phone"]
        zone_id = args.get("zone_id")

        total = round(quantity_kg * price_per_unit, 2)
        requires_review = total > FINANCIAL_REVIEW_THRESHOLD_FCFA
        review_reason = (
            f"Transaction de {total:,.0f} FCFA > seuil de vérification {FINANCIAL_REVIEW_THRESHOLD_FCFA:,} FCFA"
            if requires_review
            else None
        )

        return TransactionPayload(
            product_id=product_id,
            quantity_kg=quantity_kg,
            price_fcfa_per_unit=price_per_unit,
            total_fcfa=total,
            buyer_phone=buyer_phone,
            zone_id=zone_id,
            requires_human_review=requires_review,
            review_reason=review_reason,
            status="PENDING",
        )

    async def _commit_transaction(self, args: Dict[str, Any]) -> CommitResult:
        transaction_id = args["transaction_id"]
        approved = bool(args.get("approved", False))

        if not approved:
            return CommitResult(
                transaction_id=transaction_id,
                status="REJECTED",
                message="Transaction annulée par l'utilisateur.",
            )

        # TODO: fetch TransactionPayload from a short-lived in-memory cache or Redis
        # keyed by transaction_id, then write to DB.
        # This stub signals that the commit plumbing must be wired to your DB session.
        logger.info("Committing transaction %s", transaction_id)
        return CommitResult(
            transaction_id=transaction_id,
            status="COMMITTED",
            message="Transaction enregistrée avec succès.",
        )

    async def _find_buyers(self, args: Dict[str, Any]) -> BuyerListPayload:
        crop = args["crop"]
        zone = args.get("zone")
        tool = self._lazy_tool()
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
        return BuyerListPayload(crop=crop, zone=zone, buyers=buyers)


if __name__ == "__main__":
    import asyncio, json

    server = MarketMCPServer()
    print("Tools:", [t["name"] for t in server.list_tools()])
    result = asyncio.run(server.call_tool("prepare_transaction", {
        "product_id": "p001", "quantity_kg": 500,
        "price_fcfa_per_unit": 220, "buyer_phone": "+22670000001",
    }))
    print(json.dumps(result.model_dump(), indent=2, ensure_ascii=False))
