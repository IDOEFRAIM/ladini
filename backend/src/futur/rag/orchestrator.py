import json
import logging
from .retriever import AgileRetriever
from .components import get_llm_client
from .config import get_rag_profile

logger = logging.getLogger(__name__)

# Prompts de synthèse adaptés au profil
_SYNTHESIS_PROMPTS = {
    "simple": (
        "Tu es AgriConnect, un conseiller agricole bienveillant.\n"
        "RÈGLES ABSOLUES :\n"
        "1. Parle en français simple, comme à un voisin.\n"
        "2. Pas de jargon scientifique. Utilise des exemples concrets du village.\n"
        "3. Réponse COURTE (3-5 phrases max).\n"
        "4. Si la météo est mauvaise (pluie), dis clairement d'attendre.\n"
        "5. Si tu ne sais pas, dis-le honnêtement."
    ),
    "standard": (
        "Tu es AgriConnect, un conseiller agricole expert.\n"
        "RÈGLES :\n"
        "1. Réponse claire et structurée en français.\n"
        "2. Cite tes sources si disponibles dans le contexte.\n"
        "3. Si la météo est mauvaise, avertis.\n"
        "4. Si le contexte manque, dis-le."
    ),
    "technique": (
        "Tu es AgriConnect, un expert agronome de haut niveau.\n"
        "RÈGLES ABSOLUES :\n"
        "1. Réponse DÉTAILLÉE et TECHNIQUE (doses, délais, noms latins si pertinent).\n"
        "2. CITE toutes tes sources (nom du fichier, passage clé).\n"
        "3. Distingue les faits (contexte) des hypothèses.\n"
        "4. Si la météo est mauvaise, explique l'impact phytosanitaire précis.\n"
        "5. Ajoute des recommandations alternatives si possible."
    ),
}


class RAGOrchestrator:
    def __init__(self):
        self.retriever = AgileRetriever()
        self.llm = get_llm_client()

    def get_weather(self, location="local"):
        """
        Mock Weather Tool. In a real deployment, this would fetch from the weather_db or API.
        """
        # For demonstration context as requested in the "Scenario"
        return {
            "location": location,
            "condition": "Pluie imminente",
            "forecast": "Orages prévus dans les 2 prochaines heures. Précipitation > 10mm.",
            "humidity": "85%"
        }

    def process_query(self, user_query: str, user_level: str = "debutant"):
        """
        Agentic Flow adaptatif selon le profil utilisateur.

        - debutant     : retrieval rapide, réponse simple
        - intermediaire : HyDe + rerank moyen, réponse structurée
        - expert       : HyDe technique + rerank large, réponse détaillée avec sources
        """
        profile = get_rag_profile(user_level)
        logger.info("[RAGOrchestrator] query=%s | level=%s | tone=%s", user_query[:60], user_level, profile.tone)
        
        # --- Step 1: Decision/Routing ---
        router_prompt = (
            "You are an orchestrator for an agricultural assistant. "
            "Analyze the user's query and decide what information is needed.\n"
            "Return a JSON object with keys:\n"
            "- 'needs_weather': boolean (true if query implies time-sensitive action like 'today', 'now', 'can I spray')\n"
            "- 'agronomy_search_query': string (optimized query for a knowledge base search)\n"
            "- 'reasoning': string (why you made this plan)"
        )
        
        try:
            plan_response = self.llm.chat.completions.create(
                messages=[
                    {"role": "system", "content": router_prompt},
                    {"role": "user", "content": user_query}
                ],
                model="llama-3.1-8b-instant",
                response_format={"type": "json_object"}
            )
            content = plan_response.choices[0].message.content
            if content is None:
                raise ValueError("Empty response from LLM")
            plan = json.loads(content)
        except Exception as e:
            print(f"Router Error: {e}. Fallback to basic RAG.")
            plan = {"needs_weather": False, "agronomy_search_query": user_query}

        print(f"Plan: {plan}")

        context_parts = []

        # --- Step 2: Weather Tool ---
        weather_summary = ""
        weather_data = None
        if plan.get("needs_weather"):
            weather_data = self.get_weather()
            weather_summary = f"MÉTÉO ACTUELLE: {weather_data['condition']}, {weather_data['forecast']}"
            context_parts.append(f"--- CONTEXTE MÉTÉO ---\n{weather_summary}")
        
        # --- Step 3: RAG Retrieval (Context-Aware) ---
        # If we have weather info, we use it to refine the retrieve (e.g. look for 'rain effects')
        search_query = plan.get("agronomy_search_query", user_query)
        if weather_summary and weather_data:
            # Augment query to find rules about weather
            search_query += f" impact de {weather_data['condition']} sur cette action"
            
        logger.info("Searching Knowledge Base for: %s", search_query[:80])
        nodes = self.retriever.search(search_query, user_level=user_level)
        
        knowledge_text = "\n\n".join([f"SOURCE ({n.node.metadata.get('filename')}): {n.node.get_content()}" for n in nodes])
        context_parts.append(f"--- CONNAISSANCES AGRONOMIQUES ---\n{knowledge_text}")
        
        full_context = "\n\n".join(context_parts)

        # --- Step 4: Final Synthesis (adapté au profil) ---
        system_prompt_final = _SYNTHESIS_PROMPTS.get(profile.tone, _SYNTHESIS_PROMPTS["standard"])
        
        final_response = self.llm.chat.completions.create(
            messages=[
                {"role": "system", "content": system_prompt_final},
                {"role": "user", "content": f"Context:\n{full_context}\n\nQuery: {user_query}"}
            ],
            model="llama-3.1-8b-instant"
        )
        
        return final_response.choices[0].message.content

if __name__ == "__main__":
    orchestrator = RAGOrchestrator()
    # Test Scenario
    response = orchestrator.process_query("Dois-je traiter mes tomates contre le mildiou aujourd'hui ?")
    print("\n--- RESPONSE ---\n", response)
