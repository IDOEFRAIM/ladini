"""AgriConnect Orchestrator — Main Entry Point.

This class initializes the context (DB, Memory, Services, Protocols)
and builds the LangGraph using the `OrchestratorNodes` and `create_graph` logic.

Refactored for robustness and clean separation of concerns.
"""

import logging
import json
from typing import Dict, Any, Optional

from agriconnect.core.llm import get_groq_sdk
from agriconnect.graphs.state import GlobalAgriState
from agriconnect.graphs.orchestrateur.nodes import OrchestratorNodes
from agriconnect.graphs.orchestrateur.graph import create_graph

# Setup helpers
from agriconnect.graphs.orchestrateur.message_flow_setup import (
    init_db_and_memory,
    init_protocols,
    init_experts,
    init_services,
    init_tracing,
    AgriContext
)

logger = logging.getLogger(__name__)


class MessageResponseFlow:
    """
    AgriConnect Orchestrator (Refactored).

    Delegates logic to `OrchestratorNodes` and structure to `graph.py`.
    """

    def __init__(self, llm_client=None):
        self.llm = llm_client if llm_client is not None else get_groq_sdk()
        
        # 1. Bootstrap Context
        self.ctx = AgriContext(llm_client=self.llm).bootstrap()
        
        # 2. Run Setup Pipelines (Side-effects on self)
        self._init_db_and_memory()
        self._init_protocols()
        self._init_experts()
        self._init_services()
        self._init_tracing()

        # 3. Initialize Nodes
        self.nodes = OrchestratorNodes(
            llm=self.llm,
            ctx=self.ctx,
            mcp_shield=getattr(self, "mcp_shield", None),
            mcp_session=getattr(self, "mcp_session", None),
            router=None  # Built-in LLM routing used
        )

        # 5. Build Graph
        # Checkpointer could be added here if needed (e.g. MemorySaver)
        self.graph = create_graph(self.nodes).compile()

    def _init_db_and_memory(self):
        return init_db_and_memory(self)

    def _init_protocols(self):
        init_protocols(self)
        # Verify injection
        if getattr(self, "mcp_shield", None):
            try:
                # Injection dependency back-link
                setattr(self.mcp_shield, "authority", self)
            except Exception:
                pass
        if getattr(self, "mcp_session", None):
            try:
                setattr(self.ctx, "mcp_session", self.mcp_session)
                setattr(self.ctx, "mcp_shield", getattr(self, "mcp_shield", None))
            except Exception:
                pass

    def _init_experts(self):
        return init_experts(self)

    def _init_services(self):
        return init_services(self)

    def _init_tracing(self):
        return init_tracing(self)

    def invoke(self, inputs: Dict[str, Any], config: Optional[Dict[str, Any]] = None):
        """Invoke the graph."""
        return self.graph.invoke(inputs, config)
    
    def stream(self, inputs: Dict[str, Any], config: Optional[Dict[str, Any]] = None):
        """Stream the graph options."""
        return self.graph.stream(inputs, config)

    # --- Backward Compatibility / Lightweight Runner ---
    
    def analyze_needs(self, state: GlobalAgriState) -> Dict[str, Any]:
        return self.nodes.analyze_needs(state)

    def run(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Lightweight runner that mimics the graph execution for tests 
        that cannot run the full compiled graph.
        
        Note: This simplifies parallel execution to sequential.
        """
        s = dict(state)
        
        # 1. ANALYZE
        out = self.nodes.analyze_needs(s)
        if "needs" in out:
            s["needs"] = out["needs"]
        
        # 2. ROUTE
        route_decision = self.nodes.route_flow(s) # Returns "EXECUTE_CHAT" or [Send...] or "REJECT"

        # 3. DISPATCH & EXECUTE
        expert_responses = []

        if route_decision == "EXECUTE_CHAT":
            res = self.nodes.execute_chat(s)
            s.update(res)
        elif route_decision == "REJECT":
            res = self.nodes.execute_rejection(s)
            s.update(res)
        elif isinstance(route_decision, list):
            # It's a list of Send objects
            # fan-out / fan-in simulation
            for send_obj in route_decision:
                # Manually invoke run_expert_node with the Send payload
                # Send(node, args) - args are usually the state tailored for the node
                if hasattr(send_obj, "args"):
                    send_args = send_obj.args
                else:
                    # Fallback for older LangGraph versions or mock types
                    send_args = s 

                expert_res = self.nodes.run_expert_node(send_args)
                if "expert_responses" in expert_res:
                    expert_responses.extend(expert_res["expert_responses"])
            
            s["expert_responses"] = expert_responses
            
            # HITL Gate (Skipped in simple run unless mocked)
            # Synthesize
            syn_res = self.nodes.synthesize_results(s)
            s.update(syn_res)
        
        # 4. FINALIZE (Audio + Persist)
        try:
            audio = self.nodes.generate_audio(s)
            s.update(audio or {})
        except Exception:
            s["audio_url"] = None
            
        try:
            self.nodes.persist(s)
        except Exception:
            pass
            
        return s

