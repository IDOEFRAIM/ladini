import asyncio
import os
import sys
import pytest
import time
import json
from sqlalchemy import text

# Configuration du chemin
ROOT = os.path.abspath(os.path.join(os.getcwd(), "backend", "src"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agriconnect.core.database import get_db, init_db, check_connection, close_db
from agriconnect.protocols.mcp.servers.agri_db_server import AgriDBMCPServer

# Constantes réelles fournies
PRODUCER_ID = "cmltxpvae00002493hy3kgant"
ADMIN_ID    = "cmltwz6c90000tc931jduafp5"
CLIENT_ID   = "cmludrpen00009s93aw0316t2"

@pytest.fixture(scope="session", autouse=True)
def setup_database():
    """Initialise la DB une seule fois pour toute la session de test."""
    init_db() # Appelé ici globalement
    yield
    # Optionnel: close_db() si tu as une fonction de fermeture

@pytest.mark.asyncio
class TestAgriDBMCPServerFullLogic:

    async def test_list_producers_active(self):
        # On s'assure que la DB est prête au début du test au cas où
        init_db() 
        server = AgriDBMCPServer()
        producers = await server._list_producers({"status": "ACTIVE"})
        
        assert isinstance(producers, list)
        print(f"\n✅ Producteurs actifs trouvés: {len(producers)}")

    async def test_get_user_profile_real_data(self):
        init_db()
        server = AgriDBMCPServer()
        profile = await server._get_user_profile({"user_id": PRODUCER_ID})
        
        assert profile is not None
        assert profile.user_id == PRODUCER_ID
        print(f"\n✅ Profil récupéré pour {PRODUCER_ID}")

    async def test_create_order_full_transaction(self):
            init_db()
            server = AgriDBMCPServer()
            
            # Astuce : On récupère un produit valide en base d'abord pour éviter l'IntegrityError
            async with get_db() as session:
                res = await session.execute(text('SELECT id FROM products LIMIT 1'))
                row = res.fetchone()
                real_product_id = row[0] if row else "cuid_fictif_si_vide"

            order_data = {
                "buyer_id": ADMIN_ID,
                "client_id": CLIENT_ID,
                "customer_phone": "+22607000000",
                "items": [
                    {"product_id": real_product_id, "quantity": 1.0, "price": 1000.0}
                ]
            }
            
            result = await server._create_order(order_data)
            assert result["status"] == "success"


    async def test_stock_update_and_movement_trace(self):
        init_db()
        server = AgriDBMCPServer()
        
        # On cherche un stock pour ce producteur
        stocks = await server._get_farm_stocks({"farm_id": PRODUCER_ID})
        if not stocks:
            pytest.skip("Aucun stock pour tester le mouvement.")
            
        target = stocks[0]
        res = await server._update_stock({
            "stock_id": target.id,
            "quantity_change": 5.0,
            "reason": "Test Intégration"
        })
        assert res["status"] == "updated"
        print(f"\n✅ Mouvement de stock enregistré pour {target.item_name}")

    async def test_high_intensity_read_write(self):
        init_db()
        server = AgriDBMCPServer()
        
        print("\n--- Début du stress test (Parallèle) ---")
        start_time = time.perf_counter()
        
        # On utilise return_exceptions=True pour voir si un seul tool échoue sans stopper tout le test
        results = await asyncio.gather(
            server._get_user_profile({"user_id": PRODUCER_ID}),
            server._get_agent_audit({"user_id": ADMIN_ID, "limit": 2}),
            server._list_producers({"zone_id": None, "status": "ACTIVE"}),
            return_exceptions=True
        )
        
        duration = time.perf_counter() - start_time

        for i, res in enumerate(results):
            if isinstance(res, Exception):
                print(f"❌ Task {i} a échoué: {res}")
                raise res # On relance l'erreur pour faire échouer le test proprement
            else:
                print(f"✅ Task {i} réussie")

        print(f"⏱️ Temps total: {duration:.4f}s")
        assert duration < 2.5

        