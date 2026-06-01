from typing import Dict, Any
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload
from agriconnect.domain.models import (
    Anomaly,
    Auction,
    Client,
    Expense,
    Farm,
    Order,
    OrderItem,
    Producer,
    Product,
    Stock,
    TrustScore,
    Zone
)
from .common import clean_text

class DashboardsMixin:
    async def get_all_producer_dashboard(self, session: AsyncSession) -> Dict[str, Any]:
        """
        Récupère les statistiques complètes d'un producteur.
        Correction : Cast UUID pour PostgreSQL et calcul précis des revenus.
        """
        stmt = (
        select(Producer)
        .options(selectinload(Producer.user))
        .order_by(Producer.created_at.desc())
        )
        
        result = await session.execute(stmt)
        producers = result.scalars().all()

        return [
            {
                "id": p.id,
                "business_name": p.business_name,
                "status": p.status,
                "is_certified": p.is_certified,
                "location": {
                    "region": p.region,
                    "province": p.province,
                    "commune": p.commune
                },
                "user_info": {
                    "full_name": p.user.name if p.user else "Inconnu",
                    "phone": p.user.phone if p.user else None,
                    "email": p.user.email if p.user else None
                },
                "created_at": p.created_at.isoformat() if p.created_at else None
            }
            for p in producers
        ]

    async def get_producer_dashboard(self, session: AsyncSession, producer_id: str) -> Dict[str, Any]:
        """
        Récupère les statistiques complètes d'un producteur spécifique.
        Inclut les infos utilisateur, les stocks, les finances et le TrustScore.
        """
        # 1. Récupération du Producteur avec son Utilisateur (via selectinload pour l'async)
        prod_stmt = (
            select(Producer)
            .options(selectinload(Producer.user))
            .where(Producer.id == producer_id)
        )
        prod_result = await session.execute(prod_stmt)
        producer = prod_result.scalar_one_or_none()
        
        if not producer:
            return {"error": "Producteur introuvable."}

        # 2. Agrégation des stocks (via Farm)
        stocks_stmt = (
            select(
                func.count(Stock.id).label("stock_count"), 
                func.coalesce(func.sum(Stock.quantity), 0).label("total_kg")
            )
            .join(Farm, Farm.id == Stock.farm_id)
            .where(Farm.producer_id == producer_id)
        )
        stocks_agg = (await session.execute(stocks_stmt)).first()

        # 3. Revenu Total (Somme des OrderItems pour les commandes livrées)
        revenue_stmt = (
            select(func.coalesce(func.sum(OrderItem.price_at_sale * OrderItem.quantity), 0))
            .join(Order, Order.id == OrderItem.order_id)
            .join(Product, Product.id == OrderItem.product_id)
            .where(Product.producer_id == producer_id, Order.status == "DELIVERED")
        )
        total_revenue = (await session.execute(revenue_stmt)).scalar() or 0

        # 4. Dépenses totales (via Farm)
        expenses_stmt = (
            select(func.coalesce(func.sum(Expense.amount), 0))
            .join(Farm, Farm.id == Expense.farm_id)
            .where(Farm.producer_id == producer_id)
        )
        total_expenses = (await session.execute(expenses_stmt)).scalar() or 0

        # 5. Nombre de clients uniques rattachés
        clients_stmt = select(func.count(Client.id)).where(Client.producer_id == producer_id)
        client_count = (await session.execute(clients_stmt)).scalar() or 0

        # 6. Trust Score (Basé sur l'user_id du producteur)
        trust_stmt = select(TrustScore).where(TrustScore.user_id == producer.user_id)
        trust = (await session.execute(trust_stmt)).scalar_one_or_none()

        # Construction de la réponse
        return {
            "producer": {
                "id": producer.id,
                "business_name": producer.business_name,
                "status": producer.status,
                "user_info": {
                    "full_name": producer.user.name if producer.user else "Inconnu",
                    "phone": producer.user.phone if producer.user else None
                }
            },
            "stocks": {
                "count": stocks_agg.stock_count if stocks_agg else 0,
                "total_kg": float(stocks_agg.total_kg) if stocks_agg else 0.0,
            },
            "financials": {
                "total_revenue_fcfa": float(total_revenue),
                "total_expenses_fcfa": float(total_expenses),
                "profit_fcfa": float(total_revenue) - float(total_expenses),
            },
            "metrics": {
                "clients_count": client_count,
                "trust_score": trust.score if trust else 0.0,
                "trust_level": trust.level if trust else "N/A"
            }
        }
    
    async def get_all_zone_market_overview(self, session: AsyncSession) -> Dict[str, Any]:
        """
        Récupère les zones avec les métadonnées nécessaires pour le Frontend.
        Inclut la hiérarchie et les coordonnées.
        """
        # On sélectionne uniquement les colonnes utiles pour éviter de charger le 'path' complet
        stmt = select(
            Zone.id,
            Zone.name,
            Zone.code,
            Zone.parent_id,
            Zone.latitude,
            Zone.longitude,
            Zone.depth
        ).where(Zone.is_active.is_(True)).order_by(Zone.depth, Zone.name)
        
        result = await session.execute(stmt)
        
        return [
            {
                "id": r.id,
                "label": f"{r.name} ({r.code})", # Utile pour les Selects
                "parent_id": r.parent_id,
                "coords": {"lat": r.latitude, "lng": r.longitude} if r.latitude else None,
                "is_root": r.depth == 0
            } for r in result
        ]

    async def get_zone_market_overview(self, session: AsyncSession, zone_id: str) -> Dict[str, Any]:
        """
        Récupère toutes les métadonnées d'une zone spécifique par son ID.
        Inclut les informations de hiérarchie et de localisation.
        """
        # 1. Nettoyage de l'ID (Sécurité)
        zone_id = clean_text(zone_id, "zone_id", required=True)

        # 2. Construction de la requête avec jointures optionnelles
        # On récupère les infos de la zone et éventuellement le nom de la zone parente
        parent_alias = aliased(Zone)
        stmt = (
            select(
                Zone,
                parent_alias.name.label("parent_name")
            )
            .outerjoin(parent_alias, Zone.parent_id == parent_alias.id)
            .where(Zone.id == zone_id)
        )

        result = await session.execute(stmt)
        row = result.first()

        if not row:
            raise ValueError(f"Zone avec l'ID {zone_id} introuvable.")

        zone = row.Zone
        parent_name = row.parent_name

        # 3. Formatage de la réponse pour le Frontend
        return {
            "metadata": {
                "id": zone.id,
                "name": zone.name,
                "code": zone.code,
                "is_active": zone.is_active,
                "created_at": zone.created_at.isoformat() if zone.created_at else None
            },
            "hierarchy": {
                "parent_id": zone.parent_id,
                "parent_name": parent_name,
                "depth": zone.depth,
                "path": zone.path
            },
            "geo": {
                "latitude": zone.latitude,
                "longitude": zone.longitude,
                "region_id": zone.climatic_region_id
            },
            "organization_id": zone.organization_id
        }