"""Graph definition for AgriConnect Orchestrator.

This module purely defines the LangGraph structure, wiring the nodes
from `nodes.py` together.
"""

from langgraph.graph import StateGraph, END
from agriconnect.graphs.state import GlobalAgriState
from agriconnect.graphs.orchestrateur.nodes import OrchestratorNodes

def create_graph(nodes: OrchestratorNodes) -> StateGraph:
    """Build the AgriConnect Orchestrator graph."""
    
    workflow = StateGraph(GlobalAgriState)

    # 1. Add Nodes
    workflow.add_node("ANALYZE", nodes.analyze_needs)
    workflow.add_node("EXECUTE_CHAT", nodes.execute_chat)
    workflow.add_node("RUN_EXPERT", nodes.run_expert_node)
    
    # HITL Gate - blocks if requires_validation is True
    workflow.add_node("HITL_GATE", nodes.hitl_gate)
    
    workflow.add_node("SYNTHESIZE", nodes.synthesize_results)
    
    # Helper nodes
    workflow.add_node("GENERATE_AUDIO", nodes.generate_audio)
    workflow.add_node("PERSIST", nodes.persist)
    workflow.add_node("REJECT", nodes.execute_rejection)

    # 2. Define Edges
    workflow.set_entry_point("ANALYZE")

    # Routing from ANALYZE
    # Returns either "EXECUTE_CHAT", "REJECT", or [Send("RUN_EXPERT", ...)]
    workflow.add_conditional_edges(
        "ANALYZE",
        nodes.route_flow,
        ["EXECUTE_CHAT", "RUN_EXPERT", "REJECT"]
    )

    # Expert Fan-in Path
    workflow.add_edge("RUN_EXPERT", "HITL_GATE")
    workflow.add_edge("HITL_GATE", "SYNTHESIZE")
    workflow.add_edge("SYNTHESIZE", "GENERATE_AUDIO")

    # Chat Path
    workflow.add_edge("EXECUTE_CHAT", "GENERATE_AUDIO")

    # Rejection Path
    workflow.add_edge("REJECT", "GENERATE_AUDIO")

    # Finalize
    workflow.add_edge("GENERATE_AUDIO", "PERSIST")
    workflow.add_edge("PERSIST", END)

    return workflow
