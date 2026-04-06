import sys
import os
import asyncio
import json
print("STARTING SCRIPT...", flush=True)
from unittest.mock import MagicMock

# Add src to path
# Assuming we run from project root or backend/
sys.path.append(os.path.join(os.path.dirname(__file__), '../src'))

from agriconnect.graphs.nodes.formation import FormationCoach, FormationConfig, FormationAgentState

async def main():
    print("Initializing FormationCoach...", flush=True)
    
    # Mock Shield
    mock_shield = MagicMock()
    # Mock call and call_tool to return a valid MCP response structure
    # Structure expected by _parse_mcp_response: {"status": "ok", "content": [{"text": json.dumps(...)}]}
    
    mock_docs = {
        "documents": [
            {
                "title": "Fiche Technique Maïs Barka", 
                "text": "Le Maïs Barka est une variété améliorée adaptée au climat soudano-sahélien. Cycle de 90 jours. Rendement potentiel 4-5 tonnes/ha. Nécessite un apport de NPK au semis et Urée à 30 jours."
            },
            {
                "title": "Itinéraire technique Maïs",
                "text": "Préparation du sol: Labour profond recommandé. Semis: 2 graines par poquet à 3-5 cm de profondeur. Ecartement: 80cm entre lignes, 40cm entre poquets."
            }
        ]
    }
    
    mock_response = {
        "status": "ok", 
        "content": [{"text": json.dumps(mock_docs)}]
    }

    mock_shield.call.return_value = mock_response
    mock_shield.call_tool.return_value = mock_response

    # Config
    config = FormationConfig(
        shield=mock_shield
    )
    
    try:
        agent = FormationCoach(config)
    except Exception as e:
        print(f"Failed to init agent: {e}")
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
    res_analyze = agent.analyze_node(state)
    state.update(res_analyze)
    print(f"  > Intent: {state.get('intent')}")
    print(f"  > Focus Topics: {state.get('focus_topics')}")
    
    # Step 2: Retrieve (async)
    print("\n[Node] RETRIEVE")
    # Retrieve needs to be awaited
    if asyncio.iscoroutinefunction(agent.retrieve_node):
        res_retrieve = await agent.retrieve_node(state)
    else:
        res_retrieve = agent.retrieve_node(state)
    state.update(res_retrieve)
    print(f"  > Sources found: {len(state.get('sources', []))}")
    print(f"  > Context length: {len(state.get('retrieved_context', ''))} chars")

    # Step 3: Grade
    print("\n[Node] GRADE")
    res_grade = agent.grade_sources_node(state)
    state.update(res_grade)
    print(f"  > Document Grade: {state.get('document_grade')}")
    
    # Step 4: Compose (async)
    print("\n[Node] COMPOSE")
    if asyncio.iscoroutinefunction(agent.compose_node):
        res_compose = await agent.compose_node(state)
    else:
        res_compose = agent.compose_node(state)
    state.update(res_compose)
    
    print("-" * 50)
    print("\n=== FINAL RESPONSE ===")
    print(state.get("final_response"))
    
    print("\n=== DIAGNOSTIC STRUCTURED ===")
    print(json.dumps(state.get("structured", {}), indent=2, ensure_ascii=False))

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"CRITICAL ERROR: {e}", flush=True)
