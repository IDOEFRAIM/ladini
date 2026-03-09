import pytest
import pytest_asyncio
import uuid
import os
import sys

# Ensure backend src is importable
ROOT = os.path.abspath(os.path.join(os.getcwd(), "backend", "src"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agriconnect.core.database import init_db, check_connection, get_db, close_db
from agriconnect.services.database.database_service import AgriDatabaseService


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


@pytest_asyncio.fixture(scope="function")
async def db_session():
    async with get_db() as session:
        yield session


@pytest.fixture
def db_service():
    return AgriDatabaseService()


pytestmark = pytest.mark.asyncio


async def test_normalize_guess_and_check_price_anomaly(monkeypatch):
    svc = AgriDatabaseService()

    m, u, kg = await svc.normalize_unit(2, "sac")
    assert m == 100
    assert u == "KG"
    assert kg == 200

    assert await svc.guess_category("maïs") == "Céréales"
    assert await svc.guess_category("inconnu") == "Autres"

    async def fake_ref(self, product_name, zone_id):
        return {"price_per_unit": 100}

    monkeypatch.setattr(AgriDatabaseService, "get_standard_price", fake_ref)
    res = await svc.check_price_anomaly("maïs", 350, "zone1")
    assert res["is_anomaly"] is True and res["level"] == "HIGH"

    res2 = await svc.check_price_anomaly("maïs", 20, "zone1")
    assert res2["is_anomaly"] is True and res2["level"] == "LOW"

    async def fake_none(self, product_name, zone_id):
        return None

    monkeypatch.setattr(AgriDatabaseService, "get_standard_price", fake_none)
    res3 = await svc.check_price_anomaly("maïs", 50, "zone1")
    assert res3["is_anomaly"] is False


async def test_agent_action_and_audit_and_metrics(db_service):
    # Agent action lifecycle
    action = await db_service.create_agent_action("TestAgent", "TYPE", {"k": "v"}, user_id="u1")
    assert action["agent_name"] == "TestAgent"

    pendings = await db_service.get_pending_actions(agent_name="TestAgent")
    assert any(p["id"] == action["id"] for p in pendings)

    updated = await db_service.update_action_status(action["id"], "DONE", admin_notes="ok", validated_by_id="admin")
    assert updated is not None and updated["status"] == "DONE"

    # Metrics + audit + events
    metric = await db_service.record_zone_metric("zone-x", "rain_mm", 12.3)
    assert metric["metric_name"] == "rain_mm"

    aid = await db_service.log_audit(actor_id="actor1", action="TEST", entity_type="entity", entity_id="e1", old_value={"a":1}, new_value={"b":2})
    assert isinstance(aid, str) and len(aid) > 0

    ev = await db_service.emit_territory_event("zone-x", "ALERT", payload={"p":1})
    assert isinstance(ev, str) and len(ev) > 0

    an = await db_service.report_anomaly("zone-x", "HIGH", "TestAnom", message="msg", source="sys", details={"d":1})
    assert an["title"] == "TestAnom"

    actives = await db_service.get_active_anomalies("zone-x")
    assert any(a["id"] == an["id"] for a in actives)
