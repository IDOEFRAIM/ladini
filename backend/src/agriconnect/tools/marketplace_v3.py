"""
Marketplace Tool v3 — Async, multi-schema, rich features.
Aligné pour l'architecture AgriConnect (MCP + Local DB).
"""

import logging
import json
from typing import Any, Dict, List, Optional, Union,Tuple
from datetime import datetime

from agriconnect.services.database.database_service import AgriDatabaseService
from agriconnect.infrastructure.mcp.client import AgriMCPClient

logger = logging.getLogger("Tool.Marketplace.v3")

class MarketplaceToolV3:
    """
    Outil métier async pour l'agent MarketplaceAgent.
    Fonctionne en mode hybride : délègue soit à un serveur MCP (DigitalOcean),
    soit au service de base de données local.
    """

    def __init__(self, db_service: AgriDatabaseService = None, mcp_client: AgriMCPClient = None):
        self.db = db_service
        self.mcp_client = mcp_client
        
        # Initialisation par défaut si rien n'est fourni
        if not self.db and not self.mcp_client:
            self.db = AgriDatabaseService()

    async def call_tool(self, method_name: str, arguments: Dict[str, Any]) -> Any:
        """
        Point d'entrée universel pour les nœuds LangGraph.
        Permet d'appeler n'importe quelle méthode de cette classe par son nom.
        """
        if not hasattr(self, method_name):
            raise AttributeError(f"[MarketplaceToolV3] L'outil '{method_name}' est introuvable.")
        
        method = getattr(self, method_name)
        return await method(**arguments)


    def positive_float(value: Any, field: str, allow_zero: bool = False) -> float:
        """Valide et convertit une valeur en float positif."""
        try:
            # On convertit en float (accepte str "50.0" ou int 50)
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be numeric") from exc
        
        if number < 0 or (number == 0 and not allow_zero):
            raise ValueError(f"{field} must be positive")
        return number
    
    def normalize_unit(self, quantity: Any, unit: str) -> Dict[str, Any]:
        """
        Normalise les unités et retourne un dictionnaire compatible avec le dispatcher.
        """
        # 1. Validation et conversion locale
        quantity_float = float(quantity) if quantity is not None else quantity
        
        # 2. Logique de conversion
        key = str(unit).lower().strip()
        UNIT_TO_KG = {
            "sac": 100.0, "sacs": 100.0,
            "tine": 18.0, "tines": 18.0,
            "plat": 2.5, "plats": 2.5,
            "kg": 1.0, "kilo": 1.0, "kilos": 1.0,
            "tonne": 1000.0, "tonnes": 1000.0,
        }
        
        multiplier = UNIT_TO_KG.get(key, 1.0)
        unit_final = "KG" if key in UNIT_TO_KG else (unit or "").upper()
        qty_kg = quantity_float * multiplier

        # 3. Retour aligné (Dictionnaire pour éviter les erreurs d'unpacking)
        return {
            "multiplier": multiplier,
            "unit": unit_final,
            "quantity_kg": qty_kg
        }

    async def _delegate(self, method_name: str, **kwargs) -> Any:
        """
        Dispatcher centralisé. 
        Gère la communication avec le client MCP ou le service local.
        """
        if self.mcp_client:
            # 1. Appel via MCP
            result = await self.mcp_client.call_tool(method_name, kwargs)
            
            # Gestion du retour : le serveur MCP peut renvoyer du JSON string
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except (json.JSONDecodeError, TypeError):
                    pass # On garde le résultat brut si ce n'est pas du JSON

            # Extraction standardisée du format {'status': 'ok', 'data': ...}
            if isinstance(result, dict):
                if result.get("status") == "error":
                    logger.error(f"Erreur MCP ({method_name}): {result.get('message')}")
                    return result # On laisse l'appelant gérer l'erreur
                
                # Si le serveur renvoie 'data', on déballe pour simplifier la vie de l'agent
                if "data" in result:
                    return result["data"]
            
            return result
        
        # 2. Appel local (Fallback)
        if not self.db:
            raise RuntimeError("Aucun service de données disponible (DB ou MCP).")
            
        if not hasattr(self.db, method_name):
            raise AttributeError(f"Le service DB local n'implémente pas '{method_name}'")
            
        method = getattr(self.db, method_name)
        return await method(**kwargs)

    # --- 1. UTILITAIRES & NORMALISATION ---



    # --- 2. IDENTIFICATION ---

    async def identify_or_create_user(self, phone: str, name: str = None, zone_id: str = None) -> Dict[str, Any]:
        return await self._delegate("identify_or_create_user", phone=phone, name=name, zone_id=zone_id)

    # --- 3. FERMES ---

    async def get_or_create_farm(self, producer_id: str, farm_name: str = "Ma ferme", zone_id: str = None) -> Dict[str, Any]:
        return await self._delegate("get_or_create_farm", producer_id=producer_id, farm_name=farm_name, zone_id=zone_id)

    async def get_farms(self, producer_id: str) -> List[Dict[str, Any]]:
        return await self._delegate("get_farms", producer_id=producer_id)

    # --- 4. STOCKS ---

    async def add_stock(self, farm_id: str, item_name: str, quantity: float, unit: str = "kg", **kwargs) -> Dict[str, Any]:
        norm = await self.normalize_unit(quantity, unit)
        return await self._delegate(
            "add_stock",
            farm_id=farm_id,
            item_name=item_name,
            quantity=norm.get("quantity_kg", quantity),
            unit=norm.get("unit", "KG"),
            **kwargs
        )

    # --- 5. PRODUITS (MARCHÉ) ---

    async def create_product(self, producer_id: str, name: str, price: float, quantity_for_sale: float, unit: str = "kg", **kwargs) -> Dict[str, Any]:
        norm = await self.normalize_unit(quantity_for_sale, unit)
        return await self._delegate(
            "create_product",
            producer_id=producer_id,
            name=name,
            price=price,
            quantity_for_sale=norm.get("quantity_kg", quantity_for_sale),
            unit=norm.get("unit", "KG"),
            **kwargs
        )

    async def search_products(self, product_name: str, zone_id: str = None, limit: int = 10) -> List[Dict[str, Any]]:
        return await self._delegate("search_products", product_name=product_name, zone_id=zone_id, limit=limit)

    # --- 6. COMMANDES ---

    async def create_order(self, product_id: str, quantity: float, buyer_phone: str, **kwargs) -> Dict[str, Any]:
        return await self._delegate("create_order", product_id=product_id, quantity=quantity, buyer_phone=buyer_phone, **kwargs)

    async def update_order_status(self, order_id: str, new_status: str, **kwargs) -> Optional[Dict[str, Any]]:
        return await self._delegate("update_order_status", order_id=order_id, new_status=new_status, **kwargs)

    # --- 7. ENCHÈRES & MATCHING ---

    async def create_auction(self, **kwargs) -> Dict[str, Any]:
        return await self._delegate("create_auction", **kwargs)

    async def auto_match(self, product_name: str, zone_id: str = None, **kwargs) -> List[Dict[str, Any]]:
        return await self._delegate("auto_match", product_name=product_name, zone_id=zone_id, **kwargs)

    # --- 8. ANALYTICS ---

    async def get_dashboard(self, producer_id: str) -> Dict[str, Any]:
        return await self._delegate("get_producer_dashboard", producer_id=producer_id)

    async def check_price_anomaly(self, product_name: str, proposed_price: float, zone_id: str) -> Dict[str, Any]:
        return await self._delegate("check_price_anomaly", product_name=product_name, proposed_price=proposed_price, zone_id=zone_id)