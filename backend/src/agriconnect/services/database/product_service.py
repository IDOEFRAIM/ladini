"""ProductService — catalogue, offres de marché (prévente) et stock producteur."""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from agriconnect.domain.models import Product, MarketOffer
from agriconnect.domain.catalog.dto import ProductDTO as ProductModel, MarketOfferDTO as MarketOfferModel
from agriconnect.services.database.base_service import BaseService, transactional


class ProductService(BaseService):

    # ── Catalogue produits ──────────────────────────────────────────────────
    @transactional(write=False)
    async def search_products(
        self, session: AsyncSession, *, query: str, limit: int = 20
    ) -> list[ProductModel]:
        stmt = (
            select(Product)
            .where(Product.is_available.is_(True))
            .where(Product.name.op("%")(query))
            .limit(limit)
        )
        rows = (await session.execute(stmt)).scalars().all()
        return [ProductModel.model_validate(p) for p in rows]

    @transactional(write=False)
    async def get_product(self, session: AsyncSession, product_id: str) -> Optional[ProductModel]:
        obj = await session.get(Product, product_id)
        return ProductModel.model_validate(obj) if obj else None

    @transactional(write=False)
    async def list_producer_products(
        self, session: AsyncSession, producer_id: str, *, only_available: bool = True
    ) -> list[ProductModel]:
        stmt = select(Product).where(Product.producer_id == producer_id)
        if only_available:
            stmt = stmt.where(Product.is_available.is_(True))
        rows = (await session.execute(stmt)).scalars().all()
        return [ProductModel.model_validate(p) for p in rows]

    @transactional(write=True)
    async def create_product(self, session: AsyncSession, product: ProductModel) -> str:
        obj = Product(**product.to_db())
        session.add(obj)
        await session.flush()
        return str(obj.id)

    # ── Offres de marché (prévente) ─────────────────────────────────────────
    @transactional(write=False)
    async def list_public_offers(
        self, session: AsyncSession, *, sub_category_id: Optional[str] = None, limit: int = 30
    ) -> list[MarketOfferModel]:
        stmt = (
            select(MarketOffer)
            .where(MarketOffer.is_public.is_(True))
            .where(MarketOffer.status == "PUBLISHED")
        )
        if sub_category_id:
            stmt = stmt.where(MarketOffer.sub_category_id == sub_category_id)
        rows = (await session.execute(stmt.limit(limit))).scalars().all()
        return [MarketOfferModel.model_validate(o) for o in rows]

    @transactional(write=True)
    async def publish_offer(self, session: AsyncSession, offer: MarketOfferModel) -> str:
        payload = offer.to_db()
        payload.setdefault("status", "PUBLISHED")
        payload["is_public"] = True
        obj = MarketOffer(**payload)
        session.add(obj)
        await session.flush()
        return str(obj.id)

    @transactional(write=True)
    async def reserve_offer_quantity(
        self, session: AsyncSession, offer_id: str, quantity: Decimal
    ) -> bool:
        """Réservation atomique : n'aboutit que si le disponible le permet."""
        stmt = (
            update(MarketOffer)
            .where(MarketOffer.id == offer_id)
            .where((MarketOffer.available_quantity - MarketOffer.reserved_quantity) >= quantity)
            .values(reserved_quantity=MarketOffer.reserved_quantity + quantity)
        )
        result = await session.execute(stmt)
        return result.rowcount == 1
