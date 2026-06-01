import json
from typing import Any, AsyncGenerator, Dict, Union
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

class AgentRequest(BaseModel):
    message: str
    thread_id: str
    state_updates: Dict[str, Any] = {}

def add_langgraph_fastapi_endpoint(app: FastAPI, workflow: Any, path: str) -> None:
    """Injecte un endpoint de streaming compatible avec le protocole AG-UI."""
    
    @app.post(path)
    async def stream_agent_endpoint(request: AgentRequest):
        
        async def event_generator() -> AsyncGenerator[str, None]:
            config = {"configurable": {"thread_id": request.thread_id}}
            
            # Initialisation de l'input pour LangGraph
            inputs = {"user_query": request.message}
            if request.state_updates:
                inputs.update(request.state_updates)
                
            # Stream des événements du graphe (Node par Node)
            async for event in workflow.astream(inputs, config=config, stream_mode="updates"):
                # Récupération du nœud actif et de son état produit
                node_name = list(event.keys())[0]
                node_state = event[node_name]
                
                # Payload standard d'événement AG-UI
                ag_event = {
                    "event": "node_update",
                    "node": node_name,
                    "final_response": node_state.get("final_response") if isinstance(node_state, dict) else None,
                    "ag_ui_component": node_state.get("ag_ui_component") if isinstance(node_state, dict) else None
                }
                
                yield f"data: {json.dumps(ag_event)}\n\n"
                
        return StreamingResponse(event_generator(), media_type="text/event-stream")