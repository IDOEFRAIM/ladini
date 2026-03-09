from typing import Dict, Any
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from .common import Producer, Stock, Farm, Order, OrderItem, Product, Expense, Client, TrustScore, Auction, Anomaly


class DashboardsMixin:
    async def get_producer_dashboard(self, session: AsyncSession, producer_id: str) -> Dict[str, Any]:
        prod_stmt = select(Producer).where(Producer.id == producer_id)
        prod_result = await session.execute(prod_stmt)
        producer = prod_result.scalar_one_or_none()
        if not producer:
            return {"error": "Producteur introuvable."}

        stocks_stmt = select(func.count(Stock.id).label("stock_count"), func.coalesce(func.sum(Stock.quantity), 0).label("total_kg")).join(Farm, Farm.id == Stock.farm_id).where(Farm.producer_id == producer_id)
        stocks_result = await session.execute(stocks_stmt)
        stocks_agg = stocks_result.first()

        revenue_stmt = select(func.coalesce(func.sum(Order.total_amount), 0)).join(OrderItem, OrderItem.order_id == Order.id).join(Product, Product.id == OrderItem.product_id).where(Product.producer_id == producer_id, Order.status == "DELIVERED")
        revenue_result = await session.execute(revenue_stmt)
        total_revenue = revenue_result.scalar() or 0

        expenses_stmt = select(func.coalesce(func.sum(Expense.amount), 0)).join(Farm, Farm.id == Expense.farm_id).where(Farm.producer_id == producer_id)
        expenses_result = await session.execute(expenses_stmt)
        total_expenses = expenses_result.scalar() or 0

        clients_stmt = select(func.count(Client.id)).where(Client.producer_id == producer_id)
        clients_result = await session.execute(clients_stmt)
        client_count = clients_result.scalar() or 0

        trust_stmt = select(TrustScore).where(TrustScore.user_id == producer.user_id)
        trust_result = await session.execute(trust_stmt)
        trust = trust_result.scalar_one_or_none()

        return {
            "producer": producer.to_dict(),
            "stocks": {
                "count": stocks_agg.stock_count if stocks_agg else 0,
                "total_kg": float(stocks_agg.total_kg) if stocks_agg else 0,
            },
            "financials": {
                "total_revenue_fcfa": float(total_revenue),
                "total_expenses_fcfa": float(total_expenses),
                "profit_fcfa": float(total_revenue) - float(total_expenses),
            },
            "clients_count": client_count,
            "trust_score": trust.to_dict() if trust else None,
        }

    async def get_zone_market_overview(self, session: AsyncSession, zone_id: str) -> Dict[str, Any]:
        products_stmt = select(Product.category_label, func.count(Product.id).label("product_count"), func.avg(Product.price).label("avg_price"), func.sum(Product.quantity_for_sale).label("total_available")).join(Producer, Producer.id == Product.producer_id).where(Producer.zone_id == zone_id, Product.quantity_for_sale > 0).group_by(Product.category_label)
        products_result = await session.execute(products_stmt)
        products_by_cat = [{"category": r.category_label, "count": r.product_count, "avg_price_fcfa": round(float(r.avg_price), 0) if r.avg_price else 0, "total_available_kg": float(r.total_available) if r.total_available else 0} for r in products_result]

        auctions_stmt = select(func.count(Auction.id)).where(Auction.status == "OPEN", or_(Auction.target_zone_id == zone_id, Auction.target_zone_id.is_(None)))
        auctions_result = await session.execute(auctions_stmt)
        open_auctions = auctions_result.scalar() or 0

        anomalies_stmt = select(func.count(Anomaly.id)).where(Anomaly.zone_id == zone_id, Anomaly.is_resolved.is_(False))
        anomalies_result = await session.execute(anomalies_stmt)
        active_anomalies = anomalies_result.scalar() or 0

        return {"zone_id": zone_id, "products_by_category": products_by_cat, "open_auctions": open_auctions, "active_anomalies": active_anomalies}
