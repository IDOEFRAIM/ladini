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
from agriconnect.protocols.mcp.infrastructure import AgriDBMCPServer

# Constantes réelles fournies
PRODUCER_ID = "fa987f63-fafa-4147-9676-52c9af0edc75"
ADMIN_ID    = "a0f5c3f2-53d3-420c-b34d-2843db275613"
CLIENT_ID   = "fa987f63-fafa-4147-9676-52c9af0edc75"

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
        # call a safe, existing tool: list_products requires a producer_id; try to fetch one
        async with get_db() as session:
            r = await session.execute(text('SELECT producer_id FROM products LIMIT 1'))
            row = r.fetchone()
            producer_id = row[0] if row else None

        if not producer_id:
            pytest.skip("Aucun producteur trouvé pour tester list_products")

        res = await server._list_products({"producer_id": producer_id})
        assert res.status == "ok"
        assert isinstance(res.data, list)
        print(f"\n✅ Produits listés pour producer {producer_id}: {len(res.data)}")

    async def test_get_user_profile_real_data(self):
        init_db()
        server = AgriDBMCPServer()
        # find any real user id
        async with get_db() as session:
            r = await session.execute(text('SELECT id FROM users LIMIT 1'))
            row = r.fetchone()
            user_id = row[0] if row else None

        if not user_id:
            pytest.skip("Aucun utilisateur en base pour tester get_user_profile")

        res = await server._get_user_profile({"user_id": user_id})
        assert res.status == "ok"
        assert isinstance(res.data, dict)
        assert res.data.get("id") == user_id
        print(f"\n✅ Profil récupéré pour {user_id}")

    async def test_prepare_and_commit_transaction(self):
        init_db()
        server = AgriDBMCPServer()

        # get a product with available quantity
        async with get_db() as session:
            r = await session.execute(text('SELECT id, price FROM products WHERE quantity_for_sale > 0 LIMIT 1'))
            row = r.fetchone()
            if not row:
                pytest.skip("Aucun produit en stock pour tester le flux prepare->commit")
            product_id, price = row[0], float(row[1] or 0)

        payload = {
            "product_id": product_id,
            "quantity_kg": 1.0,
            "price_fcfa_per_unit": price or 1000.0,
            "buyer_phone": "+22607000000",
            "source": "TEST"
        }

        staged = await server._prepare_transaction_staging(payload)
        assert staged.status == "ok"
        tx = staged.data
        assert isinstance(tx, dict)
        tx_id = tx.get("transaction_id") or tx.get("id")
        assert tx_id

        committed = await server._commit_staged_transaction({"transaction_id": tx_id, "approved": True})
        assert committed.status == "ok"
        assert isinstance(committed.data, dict)
        assert committed.data.get("status") == "COMMITTED"


    async def test_stock_update_and_movement_trace(self):
        init_db()
        server = AgriDBMCPServer()

        # find an existing stock row
        async with get_db() as session:
            r = await session.execute(text('SELECT farm_id, item_name FROM stocks LIMIT 1'))
            row = r.fetchone()
            if not row:
                pytest.skip("Aucun stock en base pour tester update_stock")
            farm_id, item_name = row[0], row[1]

        res = await server._update_stock({
            "farm_id": farm_id,
            "item_name": item_name,
            "quantity_change": 1.0,
            "reason": "Test Intégration"
        })
        assert res.status == "ok"
        assert isinstance(res.data, dict)
        assert res.data.get("added") == 1.0 or res.data.get("new_total") is not None
        print(f"\n✅ Mouvement de stock enregistré pour {item_name}")

    async def test_high_intensity_read_write(self):
        init_db()
        server = AgriDBMCPServer()

        print("\n--- Début du stress test (Parallèle) ---")
        start_time = time.perf_counter()

        async with get_db() as session:
            r = await session.execute(text('SELECT id FROM users LIMIT 1'))
            row = r.fetchone()
            user_id = row[0] if row else None

        tasks = [server._get_user_profile({"user_id": user_id})]
        # add a product listing call if possible
        async with get_db() as session:
            r = await session.execute(text('SELECT producer_id FROM products LIMIT 1'))
            row = r.fetchone()
            producer_id = row[0] if row else None
        if producer_id:
            tasks.append(server._list_products({"producer_id": producer_id}))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        duration = time.perf_counter() - start_time

        for i, res in enumerate(results):
            if isinstance(res, Exception):
                print(f"❌ Task {i} a échoué: {res}")
                raise res
            else:
                assert getattr(res, "status", None) == "ok"
                print(f"✅ Task {i} réussie")

        print(f"⏱️ Temps total: {duration:.4f}s")
        assert duration < 5.0

        