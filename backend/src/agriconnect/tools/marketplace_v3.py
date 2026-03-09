"""
Marketplace Tool v3 — Async, multi-schema, rich features.

Couche d'abstraction entre l'agent MarketplaceAgent et le service BD.
Toutes les méthodes sont async et exploitent database_service.py.
Gère : identification, fermes, stocks, produits, commandes, enchères,
       cycles de culture, clients, dépenses, matching, dashboard.

Monnaie : FCFA.  Unités locales : sac (100 kg), plat (~2.5 kg), tine (~18 kg).
"""

import logging
from typing import Any, Dict, List, Optional
from datetime import datetime

from agriconnect.services.database.database_service import AgriDatabaseService
from agriconnect.protocols.mcp.client import AgriMCPClient

logger = logging.getLogger("Tool.Marketplace.v3")

"""
Le Tool délègue les conversions et règles lourdes au Service BD (Server MCP).
Toutes les conversions, catégorisations et vérifications métier doivent être
effectuées côté `AgriDatabaseService` pour garantir la cohérence.
"""


class MarketplaceToolV3:
    """
    Outil métier async pour l'agent MarketplaceAgent v3.
    Délègue tout au AgriDatabaseService (async sessions) OU à un client MCP.
    """

    def __init__(self, db_service: AgriDatabaseService = None, mcp_client: AgriMCPClient = None):
        self.db = db_service
        self.mcp_client = mcp_client
        
        # If neither provided, try to use default local service
        if not self.db and not self.mcp_client:
            self.db = AgriDatabaseService()

    async def _delegate(self, method_name: str, **kwargs) -> Any:
        """Dispatcher: use MCP client if available, else local DB service."""
        if self.mcp_client:
            # Map method name to MCP tool name (usually identical in our design)
            tool_name = method_name
            # Call via MCP
            return await self.mcp_client.call_tool(tool_name, kwargs)
        
        # Local service call
        if not self.db:
            raise RuntimeError("No DB service or MCP client available")
            
        method = getattr(self.db, method_name)
        return await method(**kwargs)

    # ══════════════════════════════════════════════════════════════
    # 1. IDENTIFICATION
    # ══════════════════════════════════════════════════════════════

    async def identify_or_create_user(
        self, phone: str, name: str = None, zone_id: str = None,
    ) -> Dict[str, Any]:
        """Identifie par téléphone ou crée User + Producer."""
        return await self._delegate("identify_or_create_user", phone=phone, name=name, zone_id=zone_id)

    # ══════════════════════════════════════════════════════════════
    # 2. FERMES
    # ══════════════════════════════════════════════════════════════

    async def get_or_create_farm(
        self, producer_id: str, farm_name: str = "Ma ferme", zone_id: str = None,
    ) -> Dict[str, Any]:
        return await self.db.get_or_create_farm(producer_id, farm_name, zone_id)

    async def get_farms(self, producer_id: str) -> List[Dict[str, Any]]:
        return await self.db.get_farms(producer_id)

    async def update_farm(self, farm_id: str, **kwargs) -> Optional[Dict[str, Any]]:
        return await self.db.update_farm(farm_id, **kwargs)

    # ══════════════════════════════════════════════════════════════
    # 3. STOCKS (avec conversion d'unités locales)
    # ══════════════════════════════════════════════════════════════

    async def add_stock(
        self, farm_id: str, item_name: str, quantity: float,
        unit: str = "kg", stock_type: str = "HARVEST",
        reason: str = "Ajout via agent",
    ) -> Dict[str, Any]:
        """Ajoute du stock avec conversion automatique des unités locales."""
        multiplier, unit_final, qty_kg = await self.db.normalize_unit(quantity, unit)

        result = await self.db.add_stock(
            farm_id=farm_id, item_name=item_name,
            quantity=qty_kg, unit=unit_final,
            stock_type=stock_type, reason=reason,
        )

        # Enrichir la réponse avec les infos de conversion
        if multiplier != 1.0:
            result["original_quantity"] = quantity
            result["original_unit"] = unit
            result["converted_note"] = f"{quantity} {unit} = {qty_kg:.0f} kg"

        return result

    async def remove_stock(
        self, farm_id: str, item_name: str, quantity: float,
        unit: str = "kg", reason: str = "Retrait",
        movement_type: str = "OUT",
    ) -> Dict[str, Any]:
        """Retire du stock (vente, perte, transfert)."""
        multiplier, unit_final, qty_kg = await self.db.normalize_unit(quantity, unit)
        return await self.db.remove_stock(
            farm_id=farm_id, item_name=item_name,
            quantity=qty_kg, reason=reason, movement_type=movement_type,
        )

    async def get_stocks(self, farm_id: str) -> List[Dict[str, Any]]:
        return await self.db.get_stocks(farm_id)

    async def get_stock_history(self, stock_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Historique des mouvements d'un stock."""
        return await self.db.get_stock_movements(stock_id, limit)

    # ══════════════════════════════════════════════════════════════
    # 4. PRODUITS (mise en vente)
    # ══════════════════════════════════════════════════════════════

    async def create_product(
        self, producer_id: str, name: str, price: float,
        quantity_for_sale: float, unit: str = "kg",
        category_label: str = None, description: str = None,
    ) -> Dict[str, Any]:
        """Crée un produit en vente. Catégorie auto-détectée si non fournie."""
        multiplier, unit_final, qty_kg = await self.db.normalize_unit(quantity_for_sale, unit)
        cat = category_label or await self.db.guess_category(name)

        return await self.db.create_product(
            producer_id=producer_id, name=name, price=price,
            quantity_for_sale=qty_kg, unit=unit_final,
            category_label=cat, description=description,
        )

    async def list_products(self, producer_id: str) -> List[Dict[str, Any]]:
        return await self.db.list_products(producer_id)

    async def search_products(
        self, product_name: str, zone_id: str = None, limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Recherche produits disponibles pour acheteurs."""
        return await self.db.search_products(product_name, zone_id, limit)

    # ══════════════════════════════════════════════════════════════
    # 5. COMMANDES
    # ══════════════════════════════════════════════════════════════

    async def create_order(
        self, product_id: str, quantity: float,
        buyer_phone: str, buyer_name: str = None,
        zone_id: str = None,
    ) -> Dict[str, Any]:
        return await self.db.create_order(
            product_id=product_id, quantity=quantity,
            buyer_phone=buyer_phone, buyer_name=buyer_name,
            zone_id=zone_id,
        )

    async def get_orders(
        self, buyer_phone: str = None, status: str = None, limit: int = 20,
    ) -> List[Dict[str, Any]]:
        return await self.db.get_orders(buyer_phone=buyer_phone, status=status, limit=limit)

    async def update_order_status(
        self, order_id: str, new_status: str, payment_status: str = None,
    ) -> Optional[Dict[str, Any]]:
        return await self.db.update_order_status(order_id, new_status, payment_status)

    # ══════════════════════════════════════════════════════════════
    # 6. ENCHÈRES (nouveau !)
    # ══════════════════════════════════════════════════════════════

    async def create_auction(
        self, buyer_id: str, sub_category_id: str, quantity: float,
        max_price_per_unit: float, deadline: datetime,
        unit: str = "TONNE", target_zone_id: str = None,
    ) -> Dict[str, Any]:
        """Crée une enchère d'achat (appel d'offres)."""
        return await self.db.create_auction(
            buyer_id=buyer_id, sub_category_id=sub_category_id,
            quantity=quantity, max_price_per_unit=max_price_per_unit,
            deadline=deadline, unit=unit, target_zone_id=target_zone_id,
        )

    async def get_open_auctions(
        self, zone_id: str = None, limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Liste les enchères ouvertes (appels d'offres) dans la zone."""
        return await self.db.get_open_auctions(zone_id, limit)

    async def place_bid(
        self, auction_id: str, producer_id: str, offered_price: float,
    ) -> Dict[str, Any]:
        """Soumettre une offre sur une enchère."""
        return await self.db.place_bid(auction_id, producer_id, offered_price)

    # ══════════════════════════════════════════════════════════════
    # 7. CLIENTS
    # ══════════════════════════════════════════════════════════════

    async def get_or_create_client(
        self, producer_id: str, name: str, phone: str,
    ) -> Dict[str, Any]:
        return await self.db.get_or_create_client(producer_id, name, phone)

    async def get_clients(self, producer_id: str) -> List[Dict[str, Any]]:
        return await self.db.get_clients(producer_id)

    # ══════════════════════════════════════════════════════════════
    # 8. DÉPENSES (nouveau !)
    # ══════════════════════════════════════════════════════════════

    async def add_expense(
        self, farm_id: str, label: str, amount: float,
        category: str = "OTHER",
    ) -> Dict[str, Any]:
        """Enregistre une dépense pour la ferme."""
        return await self.db.add_expense(farm_id, label, amount, category)

    async def get_expenses(
        self, farm_id: str, category: str = None, limit: int = 50,
    ) -> List[Dict[str, Any]]:
        return await self.db.get_expenses(farm_id, category, limit)

    async def get_expense_summary(self, farm_id: str) -> Dict[str, Any]:
        """Résumé des dépenses par catégorie."""
        return await self.db.get_expense_summary(farm_id)

    # ══════════════════════════════════════════════════════════════
    # 9. CYCLES DE CULTURE (nouveau !)
    # ══════════════════════════════════════════════════════════════

    async def create_crop_cycle(
        self, farm_id: str, crop_type: str, area_size: float,
        planted_at: datetime, expected_harvest_date: datetime,
        expected_yield: float,
    ) -> Dict[str, Any]:
        """Enregistre un cycle de culture."""
        return await self.db.create_crop_cycle(
            farm_id=farm_id, crop_type=crop_type, area_size=area_size,
            planted_at=planted_at, expected_harvest_date=expected_harvest_date,
            expected_yield=expected_yield,
        )

    async def get_crop_cycles(self, farm_id: str) -> List[Dict[str, Any]]:
        return await self.db.get_crop_cycles(farm_id)

    # ══════════════════════════════════════════════════════════════
    # 10. MATCHING INTELLIGENT (amélioré)
    # ══════════════════════════════════════════════════════════════

    async def find_buyers_for_product(
        self, product_name: str, zone_id: str = None, limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Cherche les enchères ouvertes correspondant au produit du vendeur."""
        auctions = await self.db.get_open_auctions(zone_id, limit)
        # Filtrer par nom de produit (fuzzy)
        matches = []
        product_lower = product_name.lower()
        for a in auctions:
            # L'enchère contient sub_category_id; matching léger par nom
            if product_lower in str(a).lower():
                matches.append(a)
        return matches

    async def find_products_for_buyer(
        self, product_name: str, zone_id: str = None, limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Cherche les produits disponibles pour un acheteur."""
        return await self.db.search_products(product_name, zone_id, limit)

    async def auto_match(
        self, product_name: str, zone_id: str = None,
        seller_phone: str = None, product_id: str = None,
    ) -> List[Dict[str, Any]]:
        """
        Matching automatique : cherche des acheteurs potentiels
        (enchères + alertes) pour un produit mis en vente.
        """
        matches = []

        # 1. Enchères ouvertes dans la zone
        auctions = await self.find_buyers_for_product(product_name, zone_id)
        for a in auctions:
            matches.append({
                "type": "auction",
                "auction_id": a.get("id"),
                "quantity": a.get("quantity"),
                "max_price": a.get("max_price_per_unit"),
                "deadline": str(a.get("deadline", "")),
            })

        # 2. Commandes en attente dans la zone
        pending_orders = await self.db.get_orders(status="PENDING")
        for o in pending_orders:
            if product_name.lower() in str(o).lower():
                matches.append({
                    "type": "pending_order",
                    "order_id": o.get("id"),
                    "buyer_phone": o.get("customer_phone"),
                })

        return matches

    # ══════════════════════════════════════════════════════════════
    # 11. DASHBOARD & ANALYTICS
    # ══════════════════════════════════════════════════════════════

    async def get_dashboard(self, producer_id: str) -> Dict[str, Any]:
        """Dashboard complet du producteur : stock, revenus, dépenses, score."""
        return await self.db.get_producer_dashboard(producer_id)

    async def get_zone_market(self, zone_id: str) -> Dict[str, Any]:
        """Vue marché zone : produits, prix moyens, enchères, anomalies."""
        return await self.db.get_zone_market_overview(zone_id)

    # ══════════════════════════════════════════════════════════════
    # 12. PRIX DE RÉFÉRENCE (DRDR)
    # ══════════════════════════════════════════════════════════════

    async def get_reference_price(
        self, product_name: str, zone_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Prix de référence DRDR pour un produit dans une zone."""
        return await self.db.get_standard_price(product_name, zone_id)

    async def check_price_anomaly(
        self, product_name: str, proposed_price: float, zone_id: str,
    ) -> Dict[str, Any]:
        """Vérifie si un prix est aberrant par rapport au prix de référence."""
        return await self.db.check_price_anomaly(product_name, proposed_price, zone_id)

    # ══════════════════════════════════════════════════════════════
    # 13. GOUVERNANCE — Zones
    # ══════════════════════════════════════════════════════════════

    async def get_zone_info(self, zone_id: str) -> Optional[Dict[str, Any]]:
        return await self.db.get_zone(zone_id)

    async def search_zone(self, name: str) -> Optional[Dict[str, Any]]:
        return await self.db.get_zone_by_name(name)

    async def get_child_zones(self, parent_id: str) -> List[Dict[str, Any]]:
        return await self.db.get_child_zones(parent_id)

    # ══════════════════════════════════════════════════════════════
    # 14. CATÉGORIES & SOUS-CATÉGORIES
    # ══════════════════════════════════════════════════════════════

    async def get_categories(self) -> List[Dict[str, Any]]:
        return await self.db.get_categories()

    async def get_sub_categories(self, category_id: str) -> List[Dict[str, Any]]:
        return await self.db.get_sub_categories(category_id)
