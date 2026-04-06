"""Lightweight Sentinelle graph end-to-end test using a mocked LLM.

This script stubs `langgraph` to avoid heavy third-party imports, loads the
`nodes.py` module directly, builds a runtime with a mock LLM, and runs the
nodes sequentially to simulate a full workflow.

Run with:

    python scripts/test_sentinelle_mock.py

"""
import asyncio
import importlib.util
import sys
import types
import json
from pathlib import Path

# Step 1: stub minimal langgraph package to avoid heavy imports
langgraph_mod = types.ModuleType("langgraph")
langgraph_graph = types.ModuleType("langgraph.graph")

class DummyStateGraph:
    def __init__(self, *args, **kwargs):
        pass
    def add_node(self, *a, **k):
        pass
    def add_edge(self, *a, **k):
        pass
    def set_entry_point(self, *a, **k):
        pass
    def compile(self, *a, **k):
        # Return a lightweight object with invoke/ainvoke methods
        class Compiled:
            def invoke(self, state):
                return state
            async def ainvoke(self, state):
                return state
        return Compiled()

END = object()
StateGraph = DummyStateGraph
langgraph_graph.StateGraph = StateGraph
langgraph_graph.END = END
langgraph_mod.graph = langgraph_graph
sys.modules["langgraph"] = langgraph_mod
sys.modules["langgraph.graph"] = langgraph_graph

# Step 1.5: stub heavy agriconnect and ML dependencies used by sentinelle nodes
def _make_stub_module(name):
    m = types.ModuleType(name)
    sys.modules[name] = m
    return m

# agriconnect.rag.components.get_groq_sdk
comp = _make_stub_module("agriconnect.rag.components")
comp.get_groq_sdk = lambda: None

# agriconnect.rag.metric.RAGEvaluator
metric = _make_stub_module("agriconnect.rag.metric")
class _RAGEvaluator:
    pass
metric.RAGEvaluator = _RAGEvaluator

# agriconnect.tools.sentinelle.SentinelleTool
tools_s = _make_stub_module("agriconnect.tools.sentinelle")
class _SentinelleTool:
    def __init__(self, llm_client=None):
        pass
    def _fetch_real_weather(self, location):
        return {}
    def _compute_metrics(self, weather, satellite):
        return {"et0_mm": 0}
    def _assess_flood_risk(self, weather, satellite, location):
        return {}
    def _derive_hazards(self, metrics, flood):
        return []
    def synthesize_agronomic_advice(self, metrics, crop_profile):
        return {}
tools_s.SentinelleTool = _SentinelleTool

# agriconnect.tools.refine.RefineTool
tools_r = _make_stub_module("agriconnect.tools.refine")
class _RefineTool:
    def __init__(self, llm=None):
        pass
tools_r.RefineTool = _RefineTool

# agriconnect.tools.crop.BurkinaCropTool
tools_c = _make_stub_module("agriconnect.tools.crop")
class _BurkinaCropTool:
    def get_math_profile(self, name):
        return {}
tools_c.BurkinaCropTool = _BurkinaCropTool

# agriconnect.services.persistence.AgriPersister
pers = _make_stub_module("agriconnect.services.persistence")
class _AgriPersister:
    def __init__(self, db, memory=None):
        self.db = db
pers.AgriPersister = _AgriPersister

# agriconnect.agent_test.base.BaseAgent
ab = _make_stub_module("agriconnect.agent_test.base")
class _BaseAgent:
    pass
ab.BaseAgent = _BaseAgent

# Step 2: load the sentinelle nodes module from file path
# Make backend/src available so we can import the package with relative imports
backend_src = str(Path("backend/src").resolve())
if backend_src not in sys.path:
    sys.path.insert(0, backend_src)

module = importlib.import_module("agriconnect.graphs.agents.sentinelle.nodes")

# Step 3: create a Mock LLM similar to other tests
class _MockChoices:
    def __init__(self, content: str):
        self.message = types.SimpleNamespace(content=content)

class _MockResp:
    def __init__(self, content: str):
        self.choices = [_MockChoices(content)]

class _MockCompletions:
    def create(self, model=None, messages=None, temperature=0.0):
        content = "Réponse sentinelle simulée: surveillez l'état sanitaire et suivez le protocole local."
        return _MockResp(content)

class _MockChat:
    def __init__(self):
        self.completions = _MockCompletions()

class _MockLlm:
    def __init__(self):
        self.chat = _MockChat()

# Step 4: build runtime with mock LLM
SentinelConfig = getattr(module, "SentinelConfig")
build_runtime = getattr(module, "build_runtime")

cfg = SentinelConfig()
cfg.llm_client = _MockLlm()
cfg.shield = object()
runtime = build_runtime(config=cfg)

# Step 5: run nodes sequentially: analyze -> retrieve -> generate -> finalize -> audit
state = {
    "user_query": "Mon champ de maïs montre des taches brunes, que faire ?",
    "location_profile": {"zone": "TestZone", "user_id": "test_user"},
    "weather_snapshot": {},
    "satellite_signals": {},
}

async def run_workflow():
    # analyze
    analysis = await module.analyze_node(state, runtime)
    state.update(analysis or {})

    # retrieve
    ret = await module.retrieve_node(state, runtime)
    state.update(ret or {})

    # generate
    gen = await module.generate_node(state, runtime)
    state.update(gen or {})

    # finalize
    fin = await module.finalize_node(state, runtime)
    state.update(fin or {})

    # audit (may be noop)
    aud = await module.audit_node(state, runtime)
    state.update(aud or {})

    return state

final = asyncio.run(run_workflow())
print("Final state:")
print(json.dumps(final, ensure_ascii=False, indent=2))

# Try to pretty print standardized AgriAgentOutput if available
try:
    from agriconnect.graphs.agents.common.output import AgriAgentOutput, ExpertMetadata
    out = AgriAgentOutput(
        full_text=final.get("final_response", ""),
        structured_data=final.get("agri_response") or {},
        handoff=final.get("handoff_to"),
        expert_metadata=ExpertMetadata(
            name="sentinelle",
            confidence=float(final.get("confidence_score") or 0.0),
            sources=final.get("sources") or [],
        ),
    )
    print("\nStandardized output:\n", json.dumps(out.model_dump(), ensure_ascii=False, indent=2))
except Exception:
    pass
