
import sys
from pathlib import Path
# Add project source to path (3 levels up from <category>/script.py -> backend/src)
sys.path.append(str(Path(__file__).resolve().parents[2] / "src"))

"""Validate SQLAlchemy <-> Prisma schema alignment."""
from agriconnect.services.models import (
    Base, Zone, User, Alert, MarketItem, WeatherData,
    Conversation, ConversationMessage,
    UserCrop, SurplusOffer, SoilDiagnosis, PlantDiagnosis, Reminder,
    ClimaticRegion, Producer, Farm, Stock, StockMovement,
    Expense, Product, Order, OrderItem, MarketAlert,
    AgentAction, ExternalContext,
)

# 1. Check table names match Prisma @@map
tables = {m.__tablename__ for m in [
    Zone, User, ClimaticRegion, Producer, Farm,
    Stock, StockMovement, Expense, Product, Order, OrderItem,
    Conversation, AgentAction, ExternalContext,
]}
prisma_maps = {
    'zones', 'users', 'climatic_regions', 'producers', 'farms',
    'stocks', 'stock_movements', 'expenses', 'products', 'orders', 'order_items',
    'conversations', 'agent_actions', 'external_context_files',
}
match = tables == prisma_maps
print(f"Table names match Prisma: {match}")
if not match:
    print(f"  Missing: {prisma_maps - tables}")
    print(f"  Extra:   {tables - prisma_maps}")

# 2. Verify Conversation monitoring fields
conv_cols = [c.name for c in Conversation.__table__.columns]
expected = ['query', 'response', 'agent_type', 'crop', 'zone_id', 'mode',
            'is_waiting_for_input', 'missing_slots', 'execution_path',
            'confidence_score', 'total_tokens_used', 'response_time_ms', 'audit_trail_id']
missing = [c for c in expected if c not in conv_cols]
print(f"Conversation monitoring: {'ALL OK' if not missing else f'MISSING: {missing}'}")

# 3. Verify AgentAction HITL fields
aa_cols = [c.name for c in AgentAction.__table__.columns]
expected_aa = ['agent_name', 'action_type', 'payload', 'status', 'priority',
               'order_id', 'user_id', 'audit_trail_id', 'ai_reasoning',
               'admin_notes', 'validated_by_id']
missing_aa = [c for c in expected_aa if c not in aa_cols]
print(f"AgentAction HITL:        {'ALL OK' if not missing_aa else f'MISSING: {missing_aa}'}")

# 4. Verify Order.is_agent_order
order_cols = [c.name for c in Order.__table__.columns]
print(f"Order.is_agent_order:    {'OK' if 'is_agent_order' in order_cols else 'MISSING'}")

# 5. Verify Zone Prisma fields
zone_cols = [c.name for c in Zone.__table__.columns]
expected_zone = ['code', 'climatic_region_id', 'latitude', 'longitude', 'is_active']
missing_zone = [c for c in expected_zone if c not in zone_cols]
print(f"Zone Prisma fields:      {'ALL OK' if not missing_zone else f'MISSING: {missing_zone}'}")

# 6. Verify User Prisma fields
user_cols = [c.name for c in User.__table__.columns]
expected_user = ['email', 'role', 'password', 'image']
missing_user = [c for c in expected_user if c not in user_cols]
print(f"User Prisma fields:      {'ALL OK' if not missing_user else f'MISSING: {missing_user}'}")

# 7. Verify ExternalContext
ec_cols = [c.name for c in ExternalContext.__table__.columns]
expected_ec = ['file_name', 'file_type', 'file_url', 'category', 'zone_id', 'is_vectorized', 'mcp_server_id']
missing_ec = [c for c in expected_ec if c not in ec_cols]
print(f"ExternalContext fields:  {'ALL OK' if not missing_ec else f'MISSING: {missing_ec}'}")

print()
all_ok = match and not missing and not missing_aa and not missing_zone and not missing_user and not missing_ec
print(f"{'✅ ALL MODELS ALIGNED — Prisma <-> SQLAlchemy coexistence safe' if all_ok else '❌ ALIGNMENT ISSUES FOUND'}")
