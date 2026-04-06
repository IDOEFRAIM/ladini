"""
Marketplace Tool — Opérations CRUD Agribusiness.

Fournit les fonctions d'accès DB pour l'agent MarketplaceAgent.
Gère : identification utilisateur, producteur, fermes, stocks,
       produits en vente, commandes, matching acheteur↔vendeur.

Monnaie : FCFA.  Unités locales : sac (100 kg), plat (~2.5 kg), tine (~18 kg).
"""

import logging
import uuid
from typing import Any, Dict, List, Optional
import os

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker, Session
from contextlib import contextmanager

from agriconnect.core.settings import settings
from agriconnect.core.db import get_engine, resolve_database_url
from agriconnect.core.database import _engine, _SessionLocal

logger = logging.getLogger("Tool.Marketplace")

# ── Conversion unités locales → kg ──────────────────────────
UNIT_TO_KG = {
    "sac": 100,
    "sacs": 100,
    "tine": 18,
    "tines": 18,
    "plat": 2.5,
    "plats": 2.5,
    "kg": 1,
    "tonne": 1000,
    "tonnes": 1000,
}


class MarketplaceTool:
    """
    Outil métier pour la gestion agribusiness.
    Utilisé par l'agent MarketplaceAgent (LangGraph).
    """

    def __init__(self, engine=None, session_factory=None):
        if engine and session_factory:
            self.engine = engine
            self.SessionLocal = session_factory
        elif _engine and _SessionLocal:
            self.engine = _engine
            self.SessionLocal = _SessionLocal
        elif not os.getenv("PYTEST_CURRENT_TEST"):
            # Avoid creating a live engine during pytest runs in developer machines
            self.engine = get_engine(resolve_database_url(required=True))
            self.SessionLocal = sessionmaker(bind=self.engine)
        else:
            self.engine = None
            self.SessionLocal = None
            logger.warning("MarketplaceTool : pas de DB configurée.")

    @contextmanager
    def _session(self):
        if not self.SessionLocal:
            raise RuntimeError("Database non disponible.")
        s = self.SessionLocal()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    # ══════════════════════════════════════════════════════════════
    # 1. IDENTIFICATION UTILISATEUR
    # ══════════════════════════════════════════════════════════════

    def identify_or_create_user(
        self, phone: str, name: str = None, zone_id: str = None
    ) -> Dict[str, Any]:
        """
        Identifie un utilisateur par son téléphone.
        S'il n'existe pas, crée un User + Producer automatiquement.
        """
        with self._session() as s:
            row = s.execute(
                text("SELECT id, name, phone, zone_id, is_onboarded FROM users WHERE phone = :p"),
                {"p": phone},
            ).fetchone()

            if row:
                user = dict(row._mapping)
                # Vérifier s'il a déjà un profil Producer
                prod = s.execute(
                    text("SELECT id, status FROM producers WHERE user_id = :uid"),
                    {"uid": user["id"]},
                ).fetchone()
                user["producer_id"] = prod._mapping["id"] if prod else None
                user["is_new"] = False
                return user

            # Créer User + Producer
            user_id = str(uuid.uuid4())
            s.execute(
                text(
                    "INSERT INTO users (id, phone, name, zone_id, is_onboarded) "
                    "VALUES (:id, :phone, :name, :zone_id, true)"
                ),
                {"id": user_id, "phone": phone, "name": name or phone, "zone_id": zone_id},
            )

            producer_id = str(uuid.uuid4())
            s.execute(
                text(
                    "INSERT INTO producers (id, user_id, zone_id, status) "
                    "VALUES (:id, :uid, :zone_id, 'ACTIVE')"
                ),
                {"id": producer_id, "uid": user_id, "zone_id": zone_id},
            )

            logger.info("Nouveau User+Producer créé : %s (%s)", phone, user_id)
            return {
                "id": user_id,
                "phone": phone,
                "name": name or phone,
                "zone_id": zone_id,
                "producer_id": producer_id,
                "is_new": True,
                "is_onboarded": True,
            }

    # ══════════════════════════════════════════════════════════════
    # 2. GESTION DES FERMES
    # ══════════════════════════════════════════════════════════════

    def get_or_create_farm(
        self, producer_id: str, farm_name: str = "Ma ferme", zone_id: str = None
    ) -> Dict[str, Any]:
        """Récupère la ferme du producteur ou en crée une par défaut."""
        with self._session() as s:
            row = s.execute(
                text("SELECT * FROM farms WHERE producer_id = :pid LIMIT 1"),
                {"pid": producer_id},
            ).fetchone()

            if row:
                return dict(row._mapping)

            farm_id = str(uuid.uuid4())
            s.execute(
                text(
                    "INSERT INTO farms (id, name, producer_id, zone_id) "
                    "VALUES (:id, :name, :pid, :zid)"
                ),
                {"id": farm_id, "name": farm_name, "pid": producer_id, "zid": zone_id},
            )
            logger.info("Ferme créée : %s pour producer %s", farm_name, producer_id)
            return {"id": farm_id, "name": farm_name, "producer_id": producer_id, "zone_id": zone_id}

    # ══════════════════════════════════════════════════════════════
    # 3. GESTION DES STOCKS
    # ══════════════════════════════════════════════════════════════

    def add_stock(
        self,
        farm_id: str,
        item_name: str,
        quantity: float,
        unit: str = "kg",
        stock_type: str = "HARVEST",
        reason: str = "Ajout via agent",
    ) -> Dict[str, Any]:
        """
        Ajoute ou met à jour le stock d'un produit.
        Enregistre un StockMovement de type IN.
        Gère les unités locales (sac, tine, plat) → kg.
        """
        # Conversion unité locale
        qty_kg = quantity * UNIT_TO_KG.get(unit.lower(), 1)
        unit_final = "kg" if unit.lower() in UNIT_TO_KG else unit

        with self._session() as s:
            # Chercher si le stock existe déjà
            row = s.execute(
                text(
                    "SELECT id, quantity FROM stocks "
                    "WHERE farm_id = :fid AND LOWER(item_name) = LOWER(:item)"
                ),
                {"fid": farm_id, "item": item_name},
            ).fetchone()

            if row:
                stock_id = row._mapping["id"]
                new_qty = row._mapping["quantity"] + qty_kg
                s.execute(
                    text("UPDATE stocks SET quantity = :q, updated_at = NOW() WHERE id = :sid"),
                    {"q": new_qty, "sid": stock_id},
                )
            else:
                stock_id = str(uuid.uuid4())
                new_qty = qty_kg
                s.execute(
                    text(
                        "INSERT INTO stocks (id, farm_id, item_name, quantity, unit, type) "
                        "VALUES (:id, :fid, :item, :qty, :unit, :type)"
                    ),
                    {
                        "id": stock_id, "fid": farm_id, "item": item_name,
                        "qty": qty_kg, "unit": unit_final, "type": stock_type,
                    },
                )

            # Enregistrer le mouvement
            mvt_id = str(uuid.uuid4())
            s.execute(
                text(
                    "INSERT INTO stock_movements (id, stock_id, type, quantity, reason) "
                    "VALUES (:id, :sid, 'IN', :qty, :reason)"
                ),
                {"id": mvt_id, "sid": stock_id, "qty": qty_kg, "reason": reason},
            )

        logger.info("Stock ajouté : %s +%.1f kg (stock_id=%s)", item_name, qty_kg, stock_id)
        return {
            "stock_id": stock_id,
            "item_name": item_name,
            "added_kg": qty_kg,
            "new_total_kg": new_qty,
            "original_quantity": quantity,
            "original_unit": unit,
        }

    def get_stocks(self, farm_id: str) -> List[Dict[str, Any]]:
        """Liste tous les stocks d'une ferme."""
        with self._session() as s:
            rows = s.execute(
                text("SELECT * FROM stocks WHERE farm_id = :fid ORDER BY item_name"),
                {"fid": farm_id},
            ).fetchall()
            return [dict(r._mapping) for r in rows]

    # ══════════════════════════════════════════════════════════════
    # 4. MISE EN VENTE (PRODUITS)
    # ══════════════════════════════════════════════════════════════

    def create_product(
        self,
        producer_id: str,
        name: str,
        price: float,
        quantity_for_sale: float,
        unit: str = "kg",
        category_label: str = "Céréales",
        description: str = None,
    ) -> Dict[str, Any]:
        """Crée un produit en vente sur la marketplace."""
        qty_kg = quantity_for_sale * UNIT_TO_KG.get(unit.lower(), 1)
        product_id = str(uuid.uuid4())
        short_code = product_id[:8].upper()

        with self._session() as s:
            s.execute(
                text(
                    "INSERT INTO products "
                    "(id, short_code, name, category_label, description, price, unit, "
                    " quantity_for_sale, producer_id) "
                    "VALUES (:id, :sc, :name, :cat, :desc, :price, :unit, :qty, :pid)"
                ),
                {
                    "id": product_id, "sc": short_code,
                    "name": name, "cat": category_label,
                    "desc": description, "price": price,
                    "unit": "kg", "qty": qty_kg, "pid": producer_id,
                },
            )

        logger.info("Produit créé : %s à %d FCFA/kg (id=%s)", name, price, product_id)
        return {
            "product_id": product_id,
            "short_code": short_code,
            "name": name,
            "price_fcfa": price,
            "quantity_kg": qty_kg,
        }

    def list_products(self, producer_id: str) -> List[Dict[str, Any]]:
        """Liste les produits en vente d'un producteur."""
        with self._session() as s:
            rows = s.execute(
                text(
                    "SELECT id, short_code, name, price, quantity_for_sale, unit "
                    "FROM products WHERE producer_id = :pid ORDER BY created_at DESC"
                ),
                {"pid": producer_id},
            ).fetchall()
            return [dict(r._mapping) for r in rows]

    # ══════════════════════════════════════════════════════════════
    # 5. MATCHING ACHETEUR ↔ VENDEUR
    # ══════════════════════════════════════════════════════════════

    def find_buyers_for_product(
        self, product_name: str, zone_id: str = None
    ) -> List[Dict[str, Any]]:
        """
        Cherche les acheteurs intéressés (MarketAlert SEARCHING)
        dans la même zone ou région climatique.
        """
        with self._session() as s:
            if zone_id:
                rows = s.execute(
                    text(
                        "SELECT ma.*, z.name AS zone_name FROM market_alerts ma "
                        "LEFT JOIN zones z ON z.id = ma.zone_id "
                        "WHERE LOWER(ma.product_name) = LOWER(:pname) "
                        "AND ma.status = 'SEARCHING' "
                        "AND (ma.zone_id = :zid OR ma.zone_id IN ("
                        "  SELECT id FROM zones WHERE id IN ("
                        "    SELECT z2.id FROM zones z2 "
                        "    JOIN zones z1 ON z1.region = z2.region "
                        "    WHERE z1.id = :zid"
                        "  )"
                        ")) "
                        "ORDER BY ma.created_at DESC LIMIT 10"
                    ),
                    {"pname": product_name, "zid": zone_id},
                ).fetchall()
            else:
                rows = s.execute(
                    text(
                        "SELECT ma.*, z.name AS zone_name FROM market_alerts ma "
                        "LEFT JOIN zones z ON z.id = ma.zone_id "
                        "WHERE LOWER(ma.product_name) = LOWER(:pname) "
                        "AND ma.status = 'SEARCHING' "
                        "ORDER BY ma.created_at DESC LIMIT 10"
                    ),
                    {"pname": product_name},
                ).fetchall()

            return [dict(r._mapping) for r in rows]

    def find_products_for_buyer(
        self, product_name: str, zone_id: str = None
    ) -> List[Dict[str, Any]]:
        """
        Cherche les produits disponibles correspondant à une recherche acheteur.
        Retourne les produits avec les infos du producteur.
        """
        with self._session() as s:
            query = (
                "SELECT p.id, p.name, p.price, p.quantity_for_sale, p.unit, "
                "p.short_code, pr.zone_id, u.phone AS producer_phone, u.name AS producer_name, "
                "z.name AS zone_name "
                "FROM products p "
                "JOIN producers pr ON pr.id = p.producer_id "
                "JOIN users u ON u.id = pr.user_id "
                "LEFT JOIN zones z ON z.id = pr.zone_id "
                "WHERE LOWER(p.name) LIKE LOWER(:pname) "
                "AND p.quantity_for_sale > 0 "
            )
            params = {"pname": f"%{product_name}%"}

            if zone_id:
                query += (
                    "AND (pr.zone_id = :zid OR pr.zone_id IN ("
                    "  SELECT z2.id FROM zones z2 "
                    "  JOIN zones z1 ON z1.region = z2.region "
                    "  WHERE z1.id = :zid"
                    ")) "
                )
                params["zid"] = zone_id

            query += "ORDER BY p.price ASC LIMIT 10"

            rows = s.execute(text(query), params).fetchall()
            return [dict(r._mapping) for r in rows]

    def create_market_alert(
        self, product_name: str, buyer_phone: str, zone_id: str = None
    ) -> Dict[str, Any]:
        """Crée une alerte de recherche (acheteur cherche un produit)."""
        alert_id = str(uuid.uuid4())
        with self._session() as s:
            s.execute(
                text(
                    "INSERT INTO market_alerts (id, product_name, zone_id, buyer_phone) "
                    "VALUES (:id, :pname, :zid, :phone)"
                ),
                {"id": alert_id, "pname": product_name, "zid": zone_id, "phone": buyer_phone},
            )
        logger.info("MarketAlert créé : %s cherche %s", buyer_phone, product_name)
        return {"alert_id": alert_id, "product_name": product_name, "status": "SEARCHING"}

    def match_alert_to_product(self, alert_id: str, product_id: str, producer_phone: str) -> bool:
        """Marque une alerte comme matchée avec un produit."""
        with self._session() as s:
            s.execute(
                text(
                    "UPDATE market_alerts SET status = 'MATCHED', "
                    "matched_product_id = :pid, matched_producer_phone = :pp "
                    "WHERE id = :aid"
                ),
                {"pid": product_id, "pp": producer_phone, "aid": alert_id},
            )
        return True

    # ══════════════════════════════════════════════════════════════
    # 6. COMMANDES
    # ══════════════════════════════════════════════════════════════

    def create_order(
        self,
        product_id: str,
        quantity: float,
        buyer_phone: str,
        buyer_name: str = None,
        zone_id: str = None,
        source: str = "WHATSAPP",
    ) -> Dict[str, Any]:
        """Crée une commande pour un produit."""
        order_id = str(uuid.uuid4())
        with self._session() as s:
            # Récupérer le prix du produit
            prod = s.execute(
                text("SELECT price, name FROM products WHERE id = :pid"),
                {"pid": product_id},
            ).fetchone()

            if not prod:
                return {"error": "Produit introuvable"}

            price = prod._mapping["price"]
            total = price * quantity

            s.execute(
                text(
                    "INSERT INTO orders (id, customer_phone, customer_name, zone_id, "
                    "status, source, total_amount) "
                    "VALUES (:id, :phone, :name, :zid, 'PENDING', :src, :total)"
                ),
                {
                    "id": order_id, "phone": buyer_phone, "name": buyer_name,
                    "zid": zone_id, "src": source, "total": total,
                },
            )

            item_id = str(uuid.uuid4())
            s.execute(
                text(
                    "INSERT INTO order_items (id, order_id, product_id, quantity, price_at_sale) "
                    "VALUES (:id, :oid, :pid, :qty, :price)"
                ),
                {"id": item_id, "oid": order_id, "pid": product_id, "qty": quantity, "price": price},
            )

        return {
            "order_id": order_id,
            "product_name": prod._mapping["name"],
            "quantity": quantity,
            "total_fcfa": total,
            "status": "PENDING",
        }

    # ══════════════════════════════════════════════════════════════
    # 7. PRIX MOYENS PAR ZONE
    # ══════════════════════════════════════════════════════════════

    def get_average_price(self, product_name: str, zone_id: str = None) -> Optional[float]:
        """Retourne le prix moyen d'un produit dans une zone."""
        with self._session() as s:
            if zone_id:
                row = s.execute(
                    text(
                        "SELECT AVG(p.price) AS avg_price FROM products p "
                        "JOIN producers pr ON pr.id = p.producer_id "
                        "WHERE LOWER(p.name) LIKE LOWER(:pname) AND pr.zone_id = :zid "
                        "AND p.quantity_for_sale > 0"
                    ),
                    {"pname": f"%{product_name}%", "zid": zone_id},
                ).fetchone()
            else:
                row = s.execute(
                    text(
                        "SELECT AVG(p.price) AS avg_price FROM products p "
                        "WHERE LOWER(p.name) LIKE LOWER(:pname) AND p.quantity_for_sale > 0"
                    ),
                    {"pname": f"%{product_name}%"},
                ).fetchone()

            if row and row._mapping["avg_price"]:
                return round(row._mapping["avg_price"], 0)
            return None

    # ══════════════════════════════════════════════════════════════
    # 8. AUTO-MATCHING (appelé après création d'un Product)
    # ══════════════════════════════════════════════════════════════

    def auto_match(self, product_name: str, zone_id: str, producer_phone: str, product_id: str) -> List[Dict]:
        """
        Vérifie si des acheteurs cherchent ce produit dans cette zone.
        Retourne les matches trouvés pour notification.
        """
        buyers = self.find_buyers_for_product(product_name, zone_id)
        matches = []
        for buyer in buyers:
            self.match_alert_to_product(buyer["id"], product_id, producer_phone)
            matches.append({
                "buyer_phone": buyer["buyer_phone"],
                "product_name": product_name,
                "zone_name": buyer.get("zone_name", ""),
            })
        if matches:
            logger.info("Auto-match : %d acheteurs trouvés pour %s", len(matches), product_name)
        return matches
