import sys
from pathlib import Path
import json

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "backend" / "src"
sys.path.insert(0, str(SRC))

from agriconnect.graphs.agents.formation.graph import FormationConfig, get_agent_graph
from agriconnect.infrastructure.mcp.security import ShieldHub

cfg = FormationConfig()
print("Initializing ShieldHub (may attempt MCP registry/manager setup)...")
shield = ShieldHub(session_id="test_session")
cfg.shield = shield

print("Compiling graph with real ShieldHub...")
app = get_agent_graph(config=cfg)

sample_state = {"user_query": "Bonjour, je veux des conseils sur le semis du maïs.", "learner_profile": {"user_id": "test_user"}}
print("Invoking graph (may call LLM and RAG)...")
try:
    final = app.invoke(sample_state)
except Exception:
    import asyncio
    final = asyncio.run(app.ainvoke(sample_state))

print(json.dumps(final, ensure_ascii=False, indent=2))
