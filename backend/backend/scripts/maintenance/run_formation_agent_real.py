import sys
import os
import asyncio
import json
import logging
from dotenv import load_dotenv

# Load env vars
env_path = os.path.join(os.path.dirname(__file__), '../.env')
load_dotenv(env_path, override=True)

# Debug keys
print(f"DEBUG: GROQ_API_KEY present? {bool(os.getenv('GROQ_API_KEY'))}", flush=True)
print(f"DEBUG: AGRICONNECT_APIKEY present? {bool(os.getenv('AGRICONNECT_APIKEY'))}", flush=True)
if os.getenv("AGRICONNECT_APIKEY"):
   print(f"DEBUG: AGRICONNECT_APIKEY starts with: {os.getenv('AGRICONNECT_APIKEY')[:4]}...", flush=True)

# Removed forced legacy key override


# Add src to path
sys.path.append(os.path.join(os.path.dirname(__file__), '../src'))

from agriconnect.graphs.nodes.formation import FormationCoach, FormationConfig, FormationAgentState
from agriconnect.protocols.mcp.security.shield_hub import ShieldHub
from agriconnect.core.get_llm import get_llm

# Setup logging
log_file = os.path.join(os.path.dirname(__file__), '../agent_real_run_internal.log')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(log_file, mode='w', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("RealDataTest")

async def main():
    print("STARTING REAL DATA SCRIPT...", flush=True)
    
    # Check for critical env vars
    if not os.getenv("GROQ_API_KEY"):
        print("WARNING: GROQ_API_KEY not found in environment. LLM calls will fail.", flush=True)
    
    print("Initializing ShieldHub...", flush=True)
    try:
        real_shield = ShieldHub(session_id="test_formation_real")
    except Exception as e:
        print(f"Failed to init ShieldHub: {e}", flush=True)
        return

    print("Initializing FormationCoach...", flush=True)
    try:
        # We don't need to pass llm_client explicitly if get_llm finds the env vars
        # But let's be explicit if we can. 
        # Actually FormationCoach calls get_llm internally if config.llm_client is None?
        # Let's check FormationCoach source... 
        # In FormationCoach.__init__: self.tool = FormationTool(llm=cfg.llm_client)
        # FormationTool likely calls get_llm(llm) inside.
        
        config = FormationConfig(
            shield=real_shield
        )
        agent = FormationCoach(config)
    except Exception as e:
        print(f"Failed to init agent: {e}", flush=True)
        return
    
    # Define state
    state = FormationAgentState(
        user_query="Quelles sont les étapes pour cultiver le maïs Barka ?",
        learner_profile={"niveau": "debutant", "zone": "Bobo-Dioulasso", "culture_actuelle": "Maïs"},
        warnings=[]
    )
    
    print(f"\nRunning agent with query: '{state['user_query']}'")
    print("-" * 50)
    
    # Step 1: Analyze
    print("\n[Node] ANALYZE")
    try:
        res_analyze = agent.analyze_node(state)
        state.update(res_analyze)
        print(f"  > Intent: {state.get('intent')}")
        print(f"  > Focus Topics: {state.get('focus_topics')}")
    except Exception as e:
        print(f"Error in ANALYZE: {e}")
        import traceback
        traceback.print_exc()

    # Step 2: Retrieve (async)
    print("\n[Node] RETRIEVE")
    try:
        if asyncio.iscoroutinefunction(agent.retrieve_node):
            res_retrieve = await agent.retrieve_node(state)
        else:
            res_retrieve = agent.retrieve_node(state)
        state.update(res_retrieve)
        print(f"  > Sources found: {len(state.get('sources', []))}")
        print(f"  > Context length: {len(state.get('retrieved_context', ''))} chars")
    except Exception as e:
        print(f"Error in RETRIEVE: {e}")
        import traceback
        traceback.print_exc()

    # Step 3: Grade
    print("\n[Node] GRADE")
    try:
        res_grade = agent.grade_sources_node(state)
        state.update(res_grade)
        print(f"  > Document Grade: {state.get('document_grade')}")
    except Exception as e:
        print(f"Error in GRADE: {e}")

    # Step 4: Compose (async)
    print("\n[Node] COMPOSE")
    try:
        if asyncio.iscoroutinefunction(agent.compose_node):
            res_compose = await agent.compose_node(state)
        else:
            res_compose = agent.compose_node(state)
        state.update(res_compose)
    except Exception as e:
        print(f"Error in COMPOSE: {e}")
        import traceback
        traceback.print_exc()
    
    print("-" * 50)
    print("\n=== FINAL RESPONSE ===")
    print(state.get("final_response"))
    
    print("\n=== DIAGNOSTIC STRUCTURED ===")
    try:
        print(json.dumps(state.get("structured", {}), indent=2, ensure_ascii=False))
    except:
        print(state.get("structured"))

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"CRITICAL ERROR: {e}", flush=True)
