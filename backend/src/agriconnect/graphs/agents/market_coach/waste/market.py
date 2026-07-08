"""Gradio harness redesigned as a lean client routing to the FastAPI AG-UI Server.

Run from repo root:
    python -m agriconnect.graphs.agents.market_coach.market
"""

from __future__ import annotations

import os
import sys
import json
from pathlib import Path
import httpx
import gradio as gr

# Allow running this file directly
_THIS_FILE = Path(__file__).resolve()
_BACKEND_SRC = _THIS_FILE.parents[4]  # .../backend/src
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

# Noise reduction
os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")

# Configuration de la cible du serveur AgriConnect Centralisé
FASTAPI_SERVER_URL = "http://127.0.0.1:8000"
TEST_PHONE_NUMBER = "+22601479800"


class AgriConnectServerClient:
    """Client HTTP léger consommant l'API d'exposition des Agents AgriConnect."""
    
    def __init__(self):
        self.current_role = "PRODUCER"
        self.session_id = f"session_live_{TEST_PHONE_NUMBER}"

    async def send_message_to_server(self, message: str, role_selected: str) -> str:
        """Envoie la requête au serveur FastAPI et parse le flux SSE AG-UI."""
        self.current_role = "PRODUCER" if "Producteur" in role_selected else "BUYER"
        endpoint_path = "/api/agents/producer" if self.current_role == "PRODUCER" else "/api/agents/buyer"
        
        url = f"{FASTAPI_SERVER_URL}{endpoint_path}"
        payload = {
            "message": message,
            "thread_id": f"thread_{TEST_PHONE_NUMBER}_{self.current_role.lower()}",
            "state_updates": {
                "user_phone": TEST_PHONE_NUMBER,
                "session_id": self.session_id
            }
        }
        print("Payload envoye",payload)
        final_text = "No response generated."
        ui_component_received = None
        active_node = "unknown"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                print("on commence a envoyer")
                async with client.stream("POST", url, json=payload) as response:
                    print("reponse recue",response)
                    if response.status_code != 200:
                        return f"❌ Erreur Serveur FastAPI (Code HTTP {response.status_code})."
                    
                    async for line in response.aiter_lines():
                        cleaned_line = line.strip()
                        if not cleaned_line.startswith("data: "):
                            continue
                        
                        raw_json = cleaned_line.replace("data: ", "")
                        try:
                            data = json.loads(raw_json)
                            active_node = data.get("node", active_node)
                            
                            if data.get("final_response"):
                                final_text = data["final_response"]
                            if data.get("ag_ui_component"):
                                ui_component_received = data["ag_ui_component"]
                        except json.JSONDecodeError:
                            continue

            debug_footer = f"\n\n⚙️ _[Nœud d'exécution final : {active_node}]_"
            
            if ui_component_received:
                component_type = ui_component_received.get("id", ["", "UnknownComponent"])[1]
                ui_box = (
                    f"\n\n### 📱 Composant AG-UI Injecté : `{component_type}`\n"
                    f"```json\n"
                    f"{json.dumps(ui_component_received, indent=2, ensure_ascii=False)}\n"
                    f"```"
                )
                return final_text + ui_box + debug_footer
                
            return final_text + debug_footer

        except httpx.ConnectError:
            return "❌ Connexion impossible avec le serveur FastAPI. Assure-toi d'avoir lancé ton backend."
        except Exception as e:
            print(f"❌ Erreur lors du streaming des données : {str(e)}")
            return f"❌ Erreur lors du streaming des données : {str(e)}"


agent_client = AgriConnectServerClient()

# =====================================================================
# 🖥️ INTERFACE GRAPHIQUE GRADIO (COMPATIBLE GRADIO 5/6+)
# =====================================================================

async def respond(message, role_radio, chat_history):
    if not message.strip():
        return "", chat_history
        
    bot_message = await agent_client.send_message_to_server(message, role_radio)
    
    if chat_history is None:
        chat_history = []

    # 🔥 CORRECTION : Gradio 5/6 exige strictement ce dictionnaire de messages
    chat_history.append({"role": "user", "content": str(message)})
    chat_history.append({"role": "assistant", "content": str(bot_message)})
        
    return "", chat_history


def reset_session():
    agent_client.session_id = f"session_live_{os.urandom(4).hex()}"
    return []


# Note: Utilisation de la méthode de lancement moderne recommandée par le warning
with gr.Blocks() as demo:
    gr.Markdown("# 🌾 AgriConnect — Client Léger de Test AG-UI")
    gr.Markdown("Cette interface interroge directement ton serveur centralisé FastAPI via HTTP Streaming.")
    
    with gr.Row():
        role_selector = gr.Radio(
            choices=["🌾 Producteur (Vendre / Offre sur Enchère)", "💼 Acheteur (Créer / Publier un Marché)"],
            value="🌾 Producteur (Vendre / Offre sur Enchère)",
            label="Rôle à interroger sur le serveur",
            interactive=True
        )
    
    # Pas de type="messages", car c'est désormais le type interne par défaut forcé
    chatbot = gr.Chatbot(label="Flux de communication AG-UI (Post-FastAPI)", height=520)
    
    with gr.Row():
        msg_input = gr.Textbox(
            label="Envoyer un message à l'API",
            placeholder="Ex: Je veux vendre 50 sacs de maïs...",
            scale=4
        )
        clear_btn = gr.Button("🗑️ Réinitialiser la Session", variant="stop")

    # Mappings d'événements
    msg_input.submit(respond, [msg_input, role_selector, chatbot], [msg_input, chatbot])
    clear_btn.click(reset_session, None, chatbot)
    role_selector.change(reset_session, None, chatbot)

if __name__ == "__main__":
    # Correction du premier warning : Passer le thème à la méthode launch() !
    demo.launch(share=False, server_port=7861, theme=gr.themes.Soft())