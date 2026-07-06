"""
frontend/agro.py — Interactive CLI to test the Formation agent with an agronome.

Runs a REPL loop: the agronome types a question and gets a full answer
produced by the LangGraph workflow (analyze → retrieve → compose → critique).

Usage:
    set PYTHONPATH=backend/src && python frontend/agro.py
"""
import json
import logging
import os
import sys
from typing import Optional

import requests
import asyncio

# Removed: from agriconnect.agents.formation_agro import FormationAgro
from agriconnect.graphs.nodes.formation import FormationCoach, FormationConfig
from futur.rag.retriever import AgileRetriever

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-28s %(levelname)-5s %(message)s",
)

DEFAULT_PROFILE = {"niveau": "débutant", "région": "Boucle du Mouhoun"}

class LocalShield:
    """Mock Shield that routes 'search_agronomy_docs' to local AgileRetriever."""
    def __init__(self):
        try:
            self.retriever = AgileRetriever()
        except Exception as e:
            logging.warning(f"Failed to initialize AgileRetriever: {e}")
            self.retriever = None

    def call_tool(self, tool_name: str, args: dict):
        if tool_name == "search_agronomy_docs":
            query = args.get("query", "")
            level = args.get("level", "debutant")
            if not self.retriever:
                 return {"documents": []}
            
            # Use AgileRetriever.search -> List[NodeWithScore]
            nodes = self.retriever.search(query, user_level=level)
            
            # Format for FormationCoach (Shape A: direct RAG payload)
            documents = []
            for node in nodes:
                 # NodeWithScore -> dict
                 item = {
                     "text": node.get_content(),
                     "score": node.score,
                     "metadata": node.metadata or {}
                 }
                 documents.append(item)
            
            return {"documents": documents}
        return {"error": f"Tool {tool_name} not supported by LocalShield"}

def _read_profile() -> dict:
    """Optionally let the user set a learner profile at startup."""
    print("\nProfil apprenant (vide = défaut) :")
    print(f"  Défaut : {json.dumps(DEFAULT_PROFILE, ensure_ascii=False)}")
    raw = input("  JSON profil (Enter pour défaut) : ").strip()
    if not raw:
        return dict(DEFAULT_PROFILE)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        print("  ⚠️  JSON invalide, profil par défaut utilisé.")
        return dict(DEFAULT_PROFILE)


async def run():
    print("=" * 60)
    print("   FRONTEND AGRO — Test formation agent (FormationCoach)")
    print("=" * 60)

    profile = _read_profile()

    # Determine whether to use remote HTTP API or local agent
    api_url = os.environ.get("AGRO_API_URL")
    use_api = bool(api_url)
    
    # Initialize Local FormationCoach
    agent_graph = None
    if not use_api:
        shield = LocalShield()
        # Ensure we have an LLM available (via env vars)
        try:
            from agriconnect.core.get_llm import get_llm
            llm = get_llm()
            if not llm:
                logging.warning("No LLM available (GROQ_API_KEY missing?). FormationCoach may fail.")
        except Exception:
             llm = None
             
        config = FormationConfig(
            shield=shield,
            llm_client=llm
        )
        coach = FormationCoach(config)
        agent_graph = coach.build()
    else:
        print(f"Using remote API at {api_url}")

    print(f"\n  Profil actif : {json.dumps(profile, ensure_ascii=False)}")
    print("  Tapez 'quit' pour quitter, 'profile' pour changer de profil.\n")

    while True:
        try:
            q = input("🌱 Question > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAu revoir !")
            break

        if not q:
            continue
        if q.lower() in ("quit", "exit", "q"):
            print("Au revoir !")
            break
        if q.lower() == "profile":
            profile = _read_profile()
            print(f"  Profil mis à jour : {json.dumps(profile, ensure_ascii=False)}\n")
            continue
            
        if use_api:
            # Keep API logic unchanged (assuming API wraps FormationCoach similarly)
            try:
                resp = requests.post(
                    api_url,
                    json={"question": q, "profile": profile},
                    timeout=60,
                )
                if resp.status_code == 200:
                    payload = resp.json()
                    answer = payload.get("answer") or json.dumps(payload, ensure_ascii=False)
                else:
                    answer = f"HTTP {resp.status_code}: {resp.text}"
            except Exception as exc:
                answer = f"Erreur HTTP: {exc} — Mode local non supporté (API only)."
        else:
            # Local Graph Invocation
            try:
                inputs = {
                    "user_query": q, 
                    "learner_profile": profile,
                    # Optional: inject empty history or context if needed
                }
                # Sync invoke (LangGraph handles async loop if needed)
                result = await agent_graph.ainvoke(inputs)
                answer = result.get("final_response") or result.get("answer_draft") or "Pas de réponse générée."
                
                # Show warnings if any
                warnings = result.get("warnings", [])
                if warnings:
                    print(f"\n[Warnings]: {warnings}")
                    
            except Exception as e:
                logging.exception("Error running FormationCoach graph")
                answer = f"Erreur lors de l'exécution du graphe : {e}"

        print(f"\n{'─'*60}")
        print(answer)
        print(f"{'─'*60}\n")


if __name__ == "__main__":
    asyncio.run(run())
