import os
import sys
import pytest
from sqlalchemy import text

# Ensure project src is importable when running tests directly
ROOT = os.path.abspath(os.path.join(os.getcwd(), "backend", "src"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agriconnect.core.database import get_db, init_db
from agriconnect.protocols.mcp.infrastructure import AgriDBMCPServer


PRODUCER_ID = "fa987f63-fafa-4147-9676-52c9af0edc75"


@pytest.mark.asyncio
async def test_fetch_full_producer_info():
    """Fetch and assert all available info for a producer: profile, products, farms, dashboard."""
    init_db()
    server = AgriDBMCPServer()

    # Ensure producer exists and fetch linked user_id
    async with get_db() as session:
        r = await session.execute(text(f"SELECT user_id FROM marketplace.producers WHERE id = '{PRODUCER_ID}' LIMIT 1"))
        row = r.fetchone()
        if not row:
            pytest.skip(f"Producer {PRODUCER_ID} not present in DB")
        user_id = row[0]

    # 1) user profile
    profile_res = await server._get_user_profile({"user_id": user_id})
    assert profile_res.status == "ok", f"get_user_profile failed: {profile_res}"
    assert isinstance(profile_res.data, dict)

    # 2) products list
    products_res = await server._list_products({"producer_id": PRODUCER_ID})
    assert products_res.status == "ok", f"list_products failed: {products_res}"
    assert isinstance(products_res.data, list)

    # 3) farms for producer (may be empty)
    farms_res = await server._get_farms({"producer_id": PRODUCER_ID})
    assert farms_res.status == "ok", f"get_farms failed: {farms_res}"

    # 4) dashboard / aggregated metrics
    dash_res = await server._get_producer_dashboard({"producer_id": PRODUCER_ID})
    assert dash_res.status == "ok", f"get_producer_dashboard failed: {dash_res}"

    # Print for developer visibility
    print("\n--- Producer full info ---")
    print("user_id:", user_id)
    print("profile:", profile_res.data)
    print("products_count:", len(products_res.data))
    print("farms:", farms_res.data)
    print("dashboard:", dash_res.data)
