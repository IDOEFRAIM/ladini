import logging
import asyncio
from typing import Any, Dict, List, Optional
from datetime import datetime
try:
    import cuid  # Nécessite 'pip install cuid'
    def generate_cuid():
        return cuid.cuid()
except Exception:
    # Fallback simple CUID-like generator when package missing (tests/dev)
    import secrets
    def generate_cuid():
        return secrets.token_urlsafe(18).replace('-', '')[:25]
from pydantic import BaseModel
from sqlalchemy import text,String, bindparam
from .base import AsyncMCPServer
from agriconnect.core.database import get_db

logger = logging.getLogger("MCP.AgriDBServer")

# --- Helpers de Synchro Prisma ---

class AgriDBMCPServer(AsyncMCPServer):
    name = "agri_db_full_access"

    def _register_tools(self) -> None:
        # --- OUTILS DE LECTURE (Profil, Stock, Audit) ---
        self.register(
            name="get_user_profile",
            description="Récupère le profil détaillé d'un producteur (localisation, etc.).",
            input_schema={
                "type": "object",
                "properties": {"user_id": {"type": "string"}},
                "required": ["user_id"]
            },
            handler=self._get_user_profile,
        )

        self.register(
            name="get_farm_stocks",
            description="Consulte l'état des stocks d'une ferme via l'ID utilisateur.",
            input_schema={
                "type": "object",
                "properties": {"farm_id": {"type": "string"}},
                "required": ["farm_id"]
            },
            handler=self._get_farm_stocks,
        )

        self.register(
            name="list_producers",
            description="Liste les producteurs actifs filtrés par zone géographique.",
            input_schema={
                "type": "object",
                "properties": {
                    "zone_id": {"type": "string"},
                    "status": {"type": "string", "default": "ACTIVE"}
                }
            },
            handler=self._list_producers,
        )

        # --- OUTILS D'ACTION (Commandes, Mouvements) ---
        self.register(
            name="create_marketplace_order",
            description="Gère une transaction : crée la commande, les items et lie le client.",
            input_schema={
                "type": "object",
                "properties": {
                    "buyer_id": {"type": "string"},
                    "client_id": {"type": "string"},
                    "customer_phone": {"type": "string"},
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "product_id": {"type": "string"},
                                "quantity": {"type": "number"},
                                "price": {"type": "number"}
                            }
                        }
                    },
                    "agent_action_id": {"type": "string"}
                },
                "required": ["items", "customer_phone"]
            },
            handler=self._create_order,
        )

        self.register(
            name="update_stock_with_movement",
            description="Ajuste un stock et génère automatiquement un mouvement historique.",
            input_schema={
                "type": "object",
                "properties": {
                    "stock_id": {"type": "string"},
                    "quantity_change": {"type": "number"},
                    "reason": {"type": "string"}
                },
                "required": ["stock_id", "quantity_change", "reason"]
            },
            handler=self._update_stock,
        )

    # ───────────────── IMPLEMENTATIONS ─────────────────


    async def _get_user_profile(self, args: Dict[str, Any]):
        """Récupère le profil du producteur (Nécessaire pour le test)"""
        user_id = args.get("user_id")
        async with get_db() as session:
            query = text('SELECT "userId", region, province, commune FROM producers WHERE "userId" = :uid')
            res = await session.execute(query, {"uid": user_id})
            r = res.fetchone()
            if r:
                # On retourne un objet simple ou une classe Pydantic
                from pydantic import BaseModel
                class Profile(BaseModel):
                    user_id: str
                    profile_data: dict
                    version: int = 1
                return Profile(user_id=r[0], profile_data={"region": r[1], "province": r[2], "commune": r[3]})
            return None

    async def _get_farm_stocks(self, args: Dict[str, Any]):
        """Consulte les stocks (Nécessaire pour le test)"""
        farm_id = args.get("farm_id") # Dans ton cas, c'est souvent le PRODUCER_ID
        async with get_db() as session:
            query = text("""
                SELECT s.id, s."itemName", s.quantity, s.unit, s.type 
                FROM stocks s
                JOIN farms f ON s."farmId" = f.id
                JOIN producers p ON f."producerId" = p.id
                WHERE p."userId" = :fid
            """)
            res = await session.execute(query, {"fid": farm_id})
            # On simule le retour d'objets pour le test
            from pydantic import BaseModel
            class Stock(BaseModel):
                id: str
                item_name: str
                quantity: float
                unit: str
                item_type: str

            return [Stock(id=r[0], item_name=r[1], quantity=r[2], unit=r[3], item_type=r[4]) for r in res.fetchall()]

    async def _get_agent_audit(self, args: Dict[str, Any]):
        user_id = args.get("user_id")
        async with get_db() as session:
            query = text("""
                SELECT "createdAt", "agentName", "actionType", payload, "confidenceScore" 
                FROM agent_actions 
                WHERE "userId" = :uid 
                ORDER BY "createdAt" DESC LIMIT :lim
            """)
            res = await session.execute(query, {"uid": user_id, "lim": args.get("limit", 5)})
            
            # Utilisation de dict(r._mapping) pour éviter les erreurs de tuple
            return [dict(r._mapping) for r in res.fetchall()]
    async def _list_producers(self, args: Dict[str, Any]):
        async with get_db() as session:
            zid = args.get("zone_id")
            status = args.get("status", "ACTIVE")

            # Construction dynamique de la clause WHERE
            # On évite le ":zid IS NULL" qui fait planter asyncpg
            query_str = """
                SELECT p.id, p."businessName", p.status, u.name, u.phone, z.name as zone_name
                FROM producers p
                JOIN users u ON p."userId" = u.id
                LEFT JOIN zones z ON p."zoneId" = z.id
                WHERE p.status = :status
            """
            params = {"status": status}

            if zid is not None:
                query_str += ' AND p."zoneId" = :zid'
                params["zid"] = zid

            res = await session.execute(text(query_str), params)
            return [dict(r._mapping) for r in res.fetchall()]  
   
    async def _create_order(self, args: Dict[str, Any]):
        order_id = generate_cuid()
        total_amount = sum(item['quantity'] * item['price'] for item in args['items'])
        
        async with get_db() as session:
            async with session.begin():
                # On s'assure que les colonnes matchent exactement ton schéma Prisma
                order_stmt = text("""
                    INSERT INTO orders (
                        id, "buyerId", "clientId", "customerPhone", "totalAmount", 
                        status, "isAgentOrder", "createdAt", "updatedAt"
                    ) VALUES (
                        :id, :bid, :cid, :phone, :amount, 'PENDING', true, NOW(), NOW()
                    )
                """)
                await session.execute(order_stmt, {
                    "id": order_id, 
                    "bid": args.get("buyer_id"), 
                    "cid": args.get("client_id"), # Doit être un ID valide cmlu...
                    "phone": args.get("customer_phone"), 
                    "amount": total_amount
                })

                for item in args['items']:
                    item_stmt = text("""
                        INSERT INTO order_items (id, "orderId", "productId", quantity, "priceAtSale")
                        VALUES (:iid, :oid, :pid, :qty, :price)
                    """)
                    await session.execute(item_stmt, {
                        "iid": generate_cuid(), 
                        "oid": order_id, 
                        "pid": item["product_id"], # ATTENTION: Doit exister en base
                        "qty": item["quantity"], 
                        "price": item["price"]
                    })
                return {"status": "success", "order_id": order_id}

    async def _update_stock(self, args: Dict[str, Any]):
        async with get_db() as session:
            async with session.begin():
                # 1. Update Stock
                upd_stmt = text('UPDATE stocks SET quantity = quantity + :change, "updatedAt" = NOW() WHERE id = :sid')
                await session.execute(upd_stmt, {"change": args["quantity_change"], "sid": args["stock_id"]})

                # 2. Create Movement (Table 'stock_movements')
                mov_type = "IN" if args["quantity_change"] > 0 else "OUT"
                mov_stmt = text("""
                    INSERT INTO stock_movements (id, "stockId", type, quantity, reason, "createdAt")
                    VALUES (:mid, :sid, :type, :qty, :reason, NOW())
                """)
                await session.execute(mov_stmt, {
                    "mid": generate_cuid(), "sid": args["stock_id"],
                    "type": mov_type, "qty": abs(args["quantity_change"]), "reason": args["reason"]
                })
                
                return {"status": "updated", "type": mov_type}