"""AgriConnect AI Server — Exposition Native AG-UI via FastAPI.

Cette version implémente rigoureusement le protocole de message AG-UI,
en sérialisant correctement la structure multinœud de LangGraph.

Run with:
    poetry run uvicorn server:app --reload --port 8000
"""

from __future__ import annotations

import logging
import json
import warnings
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Dict, Optional

# Neutralisation des alertes de métadonnées pour assurer des logs clairs
warnings.filterwarnings("ignore", category=UserWarning, module="pydantic")
warnings.filterwarnings("ignore", category=DeprecationWarning, module="langchain_core")

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agriconnect.graphs.agents.market_coach.nodes import build_graph, build_runtime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("AgriConnect.AGUI.Server")

# Initialisation globale des workflows pour le typage des endpoints
producer_workflow = None
buyer_workflow = None
mc_runtime = build_runtime()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Gestionnaire moderne du cycle de vie des runtimes et graphes AgriConnect."""
    global producer_workflow, buyer_workflow
    
    logger.info("⚡ Connexion au Runtime Market Coach (MCP)...")
    await mc_runtime.__aenter__()
    logger.info("🚀 Runtime MCP connecté avec succès.")
    
    logger.info("📦 Compilation des StateGraphs LangGraph...")
    producer_workflow = build_graph(role="PRODUCER", mc_runtime=mc_runtime)
    buyer_workflow = build_graph(role="BUYER", mc_runtime=mc_runtime)
    
    # Injection dynamique des routes une fois les graphes compilés en mémoire
    add_langgraph_fastapi_endpoint(app, producer_workflow, "/api/agents/producer")
    add_langgraph_fastapi_endpoint(app, buyer_workflow, "/api/agents/buyer")
    
    yield
    
    logger.info("🛑 Déconnexion du Runtime MCP...")
    await mc_runtime.__exit__(None, None, None)
    logger.info("🛑 Serveur AgriConnect éteint proprement.")


app = FastAPI(
    title="AgriConnect MarketCoach API",
    description="Backend d'exécution LangGraph standardisé avec le protocole AG-UI",
    version="1.1.0",
    lifespan=lifespan
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class AgentRequest(BaseModel):
    message: str = Field(..., description="Message textuel de l'utilisateur")
    thread_id: str = Field(..., description="Identifiant unique de session de chat")
    state_updates: Dict[str, Any] = Field(default_factory=dict, description="Variables d'injection directe dans le State")


def format_agui_event(node_name: str, final_response: Optional[str], component: Optional[Dict[str, Any]]) -> str:
    """Standardise l'envoi de données au format Server-Sent Events (SSE) d'AG-UI."""
    payload = {
        "event": "node_update",
        "node": node_name,
        "final_response": final_response,
        "ag_ui_component": None
    }
    
    # Validation et alignement structure l'interface AG-UI s'il existe
    if component and isinstance(component, dict):
        payload["ag_ui_component"] = {
            "lc_type": component.get("lc_type", "constructor"),
            "id": component.get("id", ["ag_ui", "CustomComponent"]),
            "kwargs": component.get("kwargs", {})
        }
        
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def add_langgraph_fastapi_endpoint(app: FastAPI, workflow: Any, path: str) -> None:
    """Injecte un endpoint de streaming de composants graphiques dynamiques AG-UI."""
    
    @app.post(path, summary="Stream l'exécution de l'agent avec rendu AG-UI")
    async def stream_agent_endpoint(request: AgentRequest):
        
        async def event_generator() -> AsyncGenerator[str, None]:
            config = {"configurable": {"thread_id": request.thread_id}}
            inputs = {"user_query": request.message}
            if request.state_updates:
                inputs.update(request.state_updates)
                
            # Variables de conservation d'état le temps de la complétion du graphe
            accumulated_text = None
            accumulated_component = None
                
            # Boucle asynchrone sur le stream LangGraph
            async for range_streamer in workflow.astream(inputs, config=config, stream_mode="updates"):
                # Protection si l'événement retourné par LangGraph est vide
                if not range_streamer or not isinstance(range_streamer, dict):
                    continue
                
                # Extraction sécurisée des clés de nœuds actifs
                node_keys = list(range_streamer.keys())
                if not node_keys:
                    continue
                    
                node_name = node_keys[0]
                node_state = range_streamer[node_name]
                
                if isinstance(node_state, dict):
                    # Extraction et mise en mémoire tampon des payloads d'interface graphique
                    # NOTE: use key presence (not `is not None`) so nodes can explicitly clear
                    # previously accumulated values by setting them to None.
                    if "final_response" in node_state:
                        accumulated_text = node_state["final_response"]
                    if "ag_ui_component" in node_state:
                        accumulated_component = node_state["ag_ui_component"]
                
                # Émission de l'événement SSE : l'UI finale n'est poussée 
                # que sur le nœud "final_response" pour éviter les sauts d'affichage côté client.
                if node_name == "final_response":
                    yield format_agui_event(node_name, accumulated_text, accumulated_component)
                else:
                    # Pour les nœuds intermédiaires, notification d'avancement pour activer les loaders UI
                    yield format_agui_event(node_name, None, None)
                    
        return StreamingResponse(
            event_generator(), 
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no"  # Désactive le buffering sous Nginx
            }
        )


@app.get("/health", summary="Vérification de la santé des runtimes de négociation")
def health_check():
    return {
        "status": "healthy", 
        "framework": "FastAPI + LangGraph v0.3",
        "protocol": "AG-UI compliant",
        "modes": ["PRODUCER", "BUYER"]
    }