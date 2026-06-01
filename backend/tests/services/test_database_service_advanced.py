import pytest
import pytest_asyncio
import uuid
from datetime import datetime, timedelta
import os
import sys

# Ensure backend src is importable
ROOT = os.path.abspath(os.path.join(os.getcwd(), "backend", "src"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agriconnect.core.database import init_db, check_connection, get_db, close_db
from agriconnect.domain.models import Product, CropCycle, AgentAction, Auction, Bid, TransactionStaging, TrustScore
from agriconnect.services.database.database_service import AgriDatabaseService

# Global session setup
@pytest_asyncio.fixture(scope="function", autouse=True)
async def setup_db():
    init_db()
    ok = await check_connection()
    if not ok:
        pytest.skip("Base de données Postgres non disponible pour ces tests")
    try:
        yield
    finally:
        await close_db()

import os
import sys
import uuid
from datetime import datetime, timedelta

import pytest
import pytest_asyncio

# Ensure backend src is importable
ROOT = os.path.abspath(os.path.join(os.getcwd(), "backend", "src"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agriconnect.core.database import init_db, check_connection, get_db, close_db
from agriconnect.services.database.database_service import AgriDatabaseService


# Use the producer id provided by the user
TEST_PRODUCER_ID = "3cadb350-59e5-4ad8-ae95-5ff7cc1350bd"


@pytest_asyncio.fixture(scope="function", autouse=True)
async def setup_db():
    init_db()
    ok = await check_connection()
    if not ok:
        pytest.skip("Database not available for integration tests")
    try:
        yield
    finally:
        await close_db()


@pytest_asyncio.fixture(scope="function")
async def db_session():
    async with get_db() as session:
        yield session


@pytest.fixture
def db_service():
    return AgriDatabaseService()


pytestmark = pytest.mark.asyncio


async def test_all_database_service_methods_smoke(db_service, db_session):
    """Smoke test that calls every public method on the AgriDatabaseService.

    This is intentionally permissive: we assert basic return shapes and
    avoid hard failure expectations so the test can run against real test DBs.
    """

    svc = db_service

    # --- Auth / user flows
    user = await svc.identify_or_create_user(phone="+221770000001", name="Test User")
    assert user is not None
    _ = await svc.get_user_by_phone("+221770000001")
    _ = await svc.get_user_by_id(user.get("id") or user.get("user_id") or "")

    # --- Utils
    _ = await svc.normalize_unit(1000, "G")
    _ = await svc.guess_category("maize seeds")
    _ = await svc.check_price_anomaly("maize", 200.0, None)

    # --- Marketplace: farms/products/stocks
    # Use a fresh producer id for creations to avoid colliding with existing test data
    local_producer = str(uuid.uuid4())
    farm = await svc.get_or_create_farm(local_producer, farm_name="Test Farm")
    assert farm is not None
    farms = await svc.get_farms(local_producer)
    assert isinstance(farms, (list, dict)) or farms is None
    await svc.update_farm(farm.get("id") or farm.get("farm_id") or "", business_name="Updated")

    await svc.add_stock(farm.get("id") or farm.get("farm_id"), "maize", 50.0)
    await svc.adjust_stock(farm.get("id") or farm.get("farm_id"), "maize", -10.0)
    stocks = await svc.get_stocks(farm.get("id") or farm.get("farm_id"))
    _ = await svc.get_stock_movements(stocks[0].get("id") if stocks else "", limit=5)

    prod = await svc.create_product(local_producer, name="Test Maize", price=300.0, quantity_for_sale=100.0)
    assert prod is not None
    _ = await svc.list_products(TEST_PRODUCER_ID)
    _ = await svc.search_products("Maize", limit=5)

    # create an order and then query/update it
    order = await svc.create_order(product_id=prod.get("id"), quantity=1, buyer_phone="+221770000002")
    assert order is not None
    orders = await svc.get_orders(buyer_phone="+221770000002")
    if orders:
        _ = await svc.update_order_status(orders[0].get("id"), new_status="CONFIRMED")

    client = await svc.get_or_create_client(TEST_PRODUCER_ID, name="Client A", phone="+221770000003")
    _ = await svc.get_clients(TEST_PRODUCER_ID)

    # expenses
    await svc.add_expense(farm.get("id") or farm.get("farm_id"), label="Seeds", amount=1500.0)
    _ = await svc.get_expenses(farm.get("id") or farm.get("farm_id"))
    _ = await svc.get_expense_summary(farm.get("id") or farm.get("farm_id"))

    # crop cycles
    cycle = await svc.create_crop_cycle(farm.get("id") or farm.get("farm_id"), crop_type="MAIZE", area_size=1.0,
                                         planted_at=datetime.utcnow(), expected_harvest_date=datetime.utcnow() + timedelta(days=90), expected_yield=1000.0)
    _ = await svc.get_crop_cycles(farm.get("id") or farm.get("farm_id"))

    # transactions staging (prepare + get + delete expired)
    payload = {"product_id": prod.get("id"), "quantity_kg": 1.0, "price_fcfa_per_unit": 300}
    staging = await svc.prepare_transaction_staging(payload)
    assert "transaction_id" in staging
    _ = await svc.get_staged_transaction(staging["transaction_id"])
    # try commit (may succeed or fail depending on stock) — ensure it returns a shape
    res = await svc.commit_staged_transaction(staging["transaction_id"], approved=True)
    assert isinstance(res, dict)
    await svc.delete_expired_stagings(older_than_seconds=0)

    # intelligence
    await svc.record_zone_metric(zone_id=None, metric_name="temp", value=27.5)
    await svc.log_conversation(user_id=user.get("id") or user.get("user_id"), query="Hi", response="Hello")
    action = await svc.create_agent_action("agent", "NOTIFY", {"msg": "hello"})
    if action and action.get("id"):
        _ = await svc.get_pending_actions(limit=10)
        await svc.update_action_status(action.get("id"), new_status="DONE")

    await svc.log_audit(actor_id=user.get("id") or user.get("user_id"), action="test", entity_type="product", entity_id=prod.get("id"))

    ts = await svc.get_trust_score(user.get("id") or user.get("user_id"))
    _ = await svc.update_trust_score(user.get("id") or user.get("user_id"), agent_name="agent", justification="ok", data_points={}, reliability_delta=0.1)

    await svc.report_anomaly(zone_id=None, level="INFO", title="test", message="anomaly")
    _ = await svc.get_active_anomalies(limit=10)
    await svc.emit_territory_event(zone_id=None, event_type="TEST", payload={"x": 1})

    # dashboards
    _ = await svc.get_producer_dashboard(TEST_PRODUCER_ID)
    _ = await svc.get_zone_market_overview(zone_id=None)

    # If we get this far without unhandled exceptions the smoke test is successful
    assert True
