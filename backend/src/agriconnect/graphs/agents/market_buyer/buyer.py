"""Gradio harness to manually test the MarketCoach agents (Producer & Buyer).

Run from repo root:
    python -m agriconnect.graphs.agents.market_coach.market
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Allow running this file directly (where `backend/src` isn't on sys.path).
_THIS_FILE = Path(__file__).resolve()
_BACKEND_SRC = _THIS_FILE.parents[4]  # .../backend/src
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

# Noise reduction
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
os.environ.setdefault("TF_ENABLE_ONEDNN_OPTS", "0")
os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")

import gradio as gr

# IMPORT CRUCIAL : On passe par la façade officielle
from agriconnect.graphs.agents.market_coach.nodes import build, build_graph, build_runtime

# Identifiant pivot universel basé sur le canal de communication (WhatsApp)
TEST_PHONE_NUMBER = "+22601479808"


class AgriConnectLiveTester:
    def __init__(self):
        self.mc_runtime = None
        self.producer_app = None
        self.buyer_app = None
        self.current_state = None
        self.current_role = "PRODUCER"  # 'PRODUCER' ou 'BUYER'
        self.init_error: str | None = None

    async def initialize_apps(self):
        """Initialise le runtime MCP partagé et compile les deux graphes réels."""
        if self.mc_runtime is None:
            try:
                self.mc_runtime = build_runtime()
                await self.mc_runtime.__aenter__()
                
                # 🌾 Vrai Graphe Producteur
                self.producer_app = build(mc_runtime=self.mc_runtime)
                
                # 💼 Vrai Graphe Acheteur 
                self.buyer_app = build_graph(role="BUYER", mc_runtime=self.mc_runtime)
                
                self.init_error = None
            except Exception as exc:
                self.init_error = str(exc) or exc.__class__.__name__
                return f"❌ Échec de connexion LLM/MCP : {self.init_error}"
        return "🚀 Connecté aux serveurs AgriConnect"

    def create_initial_state(self, user_query: str) -> dict:
        """Initialise le state sans injecter d'ID. La résolution se fait par le numéro."""
        return {
            "user_phone": TEST_PHONE_NUMBER,
            "session_id": f"whatsapp_session_{self.current_role.lower()}_live",
            "user_profile": {"zone_id": ""},  # Résolu dynamiquement par le service via le téléphone
            "status": "START",
            "current_goal": None,
            "transaction_payload": {},
            "extracted_entities": {},
            "available_mapping": {},  # Conserve les index courts ("1", "2") entre les tours
            "missing_fields": [],
            "validation_errors": [],
            "execution_authorized": False,
            "retry_count": 0,
            "response_strategy": None,
            "final_response": None,
            "user_query": user_query,
        }

    async def process_chat(self, message: str, role_selected: str):
        """Aiguille le message WhatsApp vers le bon graphe LangGraph selon le rôle."""
        self.current_role = "PRODUCER" if "Producteur" in role_selected else "BUYER"
        
        if self.mc_runtime is None:
            conn_status = await self.initialize_apps()
            if self.mc_runtime is None:
                return conn_status

        # Sélection stricte du graphe compilé
        active_app = self.producer_app if self.current_role == "PRODUCER" else self.buyer_app
        
        # Pas de faux semblant : si c'est None, on lève l'erreur d'architecture
        if active_app is None:
            return f"❌ Erreur de compilation : Le graphe LangGraph de production pour le rôle '{self.current_role}' renvoie 'None'.\n\n_Vérifie que la fonction 'build_graph' dans 'graph_builder.py' possède bien un 'return workflow.compile(...)' à la fin._"

        # Configuration de thread unifiée basée sur le numéro de téléphone et le rôle
        config = {"configurable": {"thread_id": f"thread_{TEST_PHONE_NUMBER}_{self.current_role.lower()}"}}

        # Anti-blocage automatique si le statut est tombé en ERROR au tour d'avant
        if self.current_state and self.current_state.get("status") == "ERROR":
            print(f"⚠️ [SÉCURITÉ] Nettoyage du state suite à une erreur technique ({self.current_role}).")
            saved_mapping = self.current_state.get("available_mapping", {})
            self.current_state = self.create_initial_state(message)
            self.current_state["available_mapping"] = saved_mapping

        if self.current_state is None:
            inputs = self.create_initial_state(message)
        else:
            inputs = {**self.current_state, "user_query": message}
            # Nettoyage des flags d'affichage pour le nouveau tour de boucle LangGraph
            inputs["final_response"] = None
            inputs["response_strategy"] = None
            inputs["extracted_entities"] = {}

        try:
            result = await active_app.ainvoke(inputs, config=config)
            self.current_state = result
            
            response = result.get("final_response", "No response generated.")
            status = result.get("status")
            goal = result.get("current_goal")
            
            # Métadonnées pour le suivi technique en direct sur l'interface
            debug_info = f"\n\n⚙️ _[Objectif: {goal} | Statut: {status}]_"
            
            if status in ["COMPLETED", "COMPLETED_TRANSACTION"]:
                self.current_state = None  # Reset du tunnel à la fin d'un parcours réussi
                response = "✅ **OPÉRATION ENREGISTRÉE EN BASE DE DONNÉES !**\n\n" + response
            elif status == "ERROR":
                debug_info += "\n⚠️ _Session réinitialisée automatiquement pour le prochain essai._"

            return response + debug_info
            
        except Exception as e:
            self.current_state = None
            return f"❌ Erreur critique d'exécution du Graphe : {str(e)}\n\n_Le State a été réinitialisé._"


# Instantiate backend tester
tester = AgriConnectLiveTester()

# =====================================================================
# 🖥️ INTERFACE GRAPHIQUE
# =====================================================================

async def respond(message, role_radio, chat_history):
    if not message.strip():
        return "", chat_history
        
    bot_message = await tester.process_chat(message, role_radio)
    
    if chat_history is None:
        chat_history = []

    # Format par dictionnaire validé
    chat_history.append({"role": "user", "content": str(message)})
    chat_history.append({"role": "assistant", "content": str(bot_message)})
        
    return "", chat_history


def reset_session():
    tester.current_state = None
    return []


with gr.Blocks(theme=gr.themes.Soft()) as demo:
    gr.Markdown("# 🌾 AgriConnect — Centre de Test de l'Agent MarketCoach")
    gr.Markdown(f"Testez en temps réel l'exécution de la logique modulaire partagée. (WhatsApp : `{TEST_PHONE_NUMBER}`)")
    
    with gr.Row():
        role_selector = gr.Radio(
            choices=["🌾 Producteur (Vendre / Offre sur Enchère)", "💼 Acheteur (Créer / Publier un Marché)"],
            value="🌾 Producteur (Vendre / Offre sur Enchère)",
            label="Rôle WhatsApp Simulé",
            interactive=True
        )
    
    chatbot = gr.Chatbot(label="Console de Discussion WhatsApp", height=500)
    
    with gr.Row():
        msg_input = gr.Textbox(
            label="Message entrant de l'utilisateur",
            placeholder="Ex: Je veux faire une offre... ou Créer un appel d'offres...",
            scale=4
        )
        clear_btn = gr.Button("🗑️ Réinitialiser le State", variant="stop")

    # Déclencheurs d'événements
    msg_input.submit(respond, [msg_input, role_selector, chatbot], [msg_input, chatbot])
    clear_btn.click(reset_session, None, chatbot)
    
    # Reset automatique du State de l'agent si on change de rôle en cours de route
    role_selector.change(reset_session, None, chatbot)

if __name__ == "__main__":
    demo.launch(share=False)