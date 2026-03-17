import os
import sys
import pytest
import uuid
from sqlalchemy import text

# Ensure project src is importable when running tests directly
ROOT = os.path.abspath(os.path.join(os.getcwd(), "backend", "src"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agriconnect.core.database import get_db, init_db
from agriconnect.protocols.mcp.infrastructure import AgriDBMCPServer
from agriconnect.protocols.mcp.security import (
    MCPPermissionClient,
    MCPPermissionHostApp,
    MCPSessionManager,
)


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
    profile_res = await server.call_tool("get_user_profile", {"user_id": str(user_id)})
    assert profile_res.get("status") == "ok", f"get_user_profile failed: {profile_res}"
    assert isinstance(profile_res.get("data"), dict)

    # 2) products list
    products_res = await server.call_tool("list_products", {"producer_id": PRODUCER_ID})
    assert products_res.get("status") == "ok", f"list_products failed: {products_res}"
    assert isinstance(products_res.get("data"), list)

    # 3) farms for producer (may be empty)
    farms_res = await server.call_tool("get_farms", {"producer_id": PRODUCER_ID})
    assert farms_res.get("status") == "ok", f"get_farms failed: {farms_res}"

    # 4) dashboard / aggregated metrics
    dash_res = await server.call_tool("get_producer_dashboard", {"producer_id": PRODUCER_ID})
    assert dash_res.get("status") == "ok", f"get_producer_dashboard failed: {dash_res}"

    # Print for developer visibility
    print("\n--- Producer full info ---")
    print("user_id:", user_id)
    print("profile:", profile_res.get("data"))
    print("products_count:", len(products_res.get("data", [])))
    print("farms:", farms_res.get("data"))
    print("dashboard:", dash_res.get("data"))


@pytest.mark.asyncio
async def test_create_product_and_retrieve_for_producer():
    """Ensure marketplace insert + retrieval works via MCP DB tools for the producer."""
    init_db()
    server = AgriDBMCPServer()

    product_name = f"pytest_maize_{uuid.uuid4().hex[:8]}"

    create_res = await server.call_tool(
        "create_product",
        {
            "producer_id": PRODUCER_ID,
            "name": product_name,
            "price": 275.0,
            "quantity_for_sale": 120.0,
            "unit": "KG",
            "category_label": "Céréales",
            "description": "Produit test inséré par MCP",
        },
    )
    assert create_res.get("status") == "ok", f"create_product failed: {create_res}"

    list_res = await server.call_tool("list_products", {"producer_id": PRODUCER_ID})
    assert list_res.get("status") == "ok", f"list_products failed after insert: {list_res}"

    products = list_res.get("data", [])
    assert any((p.get("name") or "").lower() == product_name.lower() for p in products), (
        f"Inserted product '{product_name}' not found in producer product list"
    )


@pytest.mark.asyncio
async def test_market_and_marketplace_tools_via_shield_session():
    """Ensure market write + marketplace read work through Shield session manager."""
    init_db()
    backend = AgriDBMCPServer()
    shield_client = MCPPermissionClient(backend=backend, session_id="pytest-producer")
    shield_host = MCPPermissionHostApp(client=shield_client)
    session = MCPSessionManager(host=shield_host, session_id="pytest-producer")

    # Market flow write tool (secured)
    surplus = await session.execute(
        "register_surplus_offer",
        {
            "commodity": "maïs",
            "quantity": 50.0,
            "location": "Kaya",
            "contact": "+22670000000",
        },
    )
    assert surplus.get("status") == "ok", f"register_surplus_offer failed: {surplus}"

    # Marketplace flow read tool (secured)
    products = await session.execute("list_products", {"producer_id": PRODUCER_ID})
    assert products.get("status") == "ok", f"list_products via shield failed: {products}"
    assert isinstance(products.get("data"), list)
