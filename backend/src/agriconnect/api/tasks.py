import asyncio
import logging
import os
import sys
from twilio.rest import Client
from agriconnect.api.celery_app import celery_app
from agriconnect.graphs.agents.market_coach.nodes import build_graph
from agriconnect.api.dependencies import get_checkpointer, get_mc_runtime

logger = logging.getLogger("AgriConnect.Worker")

# Correction : chargement sécurisé (avec fallback au cas où l'env est vide)
TWILIO_ACCOUNT_SID = "ACcd7d65c7687f8672af5c6ed59ebc4f3a"
TWILIO_AUTH_TOKEN =  "4b95d9e3aa5830f96e149590a2ed86a7"
TWILIO_WHATSAPP_NUMBER = "whatsapp:+14155238886"

async def _run_graph(role: str, phone_number: str, user_query: str, state_updates: dict = None):
    runtime = await get_mc_runtime()
    async with runtime as live_runtime:
        async with get_checkpointer() as checkpointer:
            graph = build_graph(
                role=role,
                mc_runtime=live_runtime,
                checkpointer=checkpointer,
            )

            config = {"configurable": {"thread_id": phone_number}}
            inputs = {
                "user_query": user_query,
                "user_phone": phone_number,
                **(state_updates or {}),
            }

            return await graph.ainvoke(inputs, config=config)

@celery_app.task(
    bind=True, 
    max_retries=3, 
    autoretry_for=(Exception,),
    retry_backoff=5,
    retry_jitter=True
)
def process_agent_task(self, role, phone_number, user_query, state_updates=None):
    log_file = open("mcp_debug.log", "a")
    original_stderr = sys.stderr

    try:
        sys.stderr = log_file
        result = asyncio.run(_run_graph(role, phone_number, user_query, state_updates))
    except Exception as e:
        logger.error(f"Erreur lors de l'exécution du graphe: {e}")
        raise
    finally:
        sys.stderr = original_stderr
        log_file.close()

    final_text = result.get("final_response", "Je n'ai pas pu générer de réponse.")

    # 2. PUSH vers Twilio
    # ATTENTION : Si le message dépasse 24h après le dernier message de l'utilisateur, 
    # tu DOIS utiliser un ContentSid (Template) au lieu de 'body'.
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    
    try:
        message = client.messages.create(
            from_=TWILIO_WHATSAPP_NUMBER,
            to=f"whatsapp:{phone_number}",
            body=final_text # Fonctionne si dans la fenêtre des 24h
        )
        return {"status": "message_sent", "sid": message.sid}
    except Exception as e:
        logger.error(f"Erreur Twilio : {e}")
        raise e