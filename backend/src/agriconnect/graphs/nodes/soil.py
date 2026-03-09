import json
import logging
import os
import re
from typing import Any, Dict, List, TypedDict

from langgraph.graph import END, StateGraph

from agriconnect.graphs.prompts import (
    SOIL_SYSTEM_TEMPLATE,
    SOIL_USER_TEMPLATE
)
# Assurez-vous que le fichier contenant votre classe SoilDoctorTool est bien importé
from agriconnect.tools.soil import SoilDoctorTool 
#from .tools import SoilDoctorTool # Exemple d'import relatif
from agriconnect.rag.components import get_groq_sdk
# AG-UI removed from agent-level: return MCP-friendly dicts instead

logger = logging.getLogger("Agent.AgriSoil")

# ------------------------------------------------------------------ #
# 1. DÉFINITION DE L'ÉTAT (State)
# ------------------------------------------------------------------ #

class SoilState(TypedDict, total=False):
    user_query: str
    user_level: str
    location_profile: Dict[str, Any]
    observation: str  # ex: "sec", "humide"
    
    # Données internes
    soil_raw_data: Dict[str, Any]      # Le JSON SoilGrids brut
    technical_diagnosis: Dict[str, Any] # Le dictionnaire de sortie de SoilDoctorTool
    
    # Sortie
    final_response: str
    warnings: List[str]
    status: str
    

# ------------------------------------------------------------------ #
# 2. L'AGENT SPECIALISTE SOL
# ------------------------------------------------------------------ #

class AgriSoilAgent:
    """
    Agent optimisé pour la pédologie.
    Il utilise SoilDoctorTool (Python pur) pour le diagnostic technique
    et un LLM uniquement pour la reformulation emphatique.
    """

    def __init__(self, llm_client=None):
        # Instanciation de votre outil
        self.doctor = SoilDoctorTool()
        
        # Modèle pour la réponse finale (Rapide et efficace)
        self.model_answer = "llama-3.3-70b-versatile" 

        try:
            self.llm = llm_client if llm_client else get_groq_sdk()
        except Exception as exc:
            logger.error("Impossible d'initialiser le LLM : %s", exc)
            self.llm = None

    # ------------------------------------------------------------------ #
    # NODE A : CHARGEMENT & DIAGNOSTIC (100% Python, Pas d'IA)
    # ------------------------------------------------------------------ #
    
    def diagnose_node(self, state: SoilState) -> SoilState:
        """
        Charge les données et exécute SoilDoctorTool.
        C'est ici que la 'Magie' technique opère sans latence.
        """
        location_profile = state.get("location_profile", {})
        # Role separation: refuse market-related queries
        _market_re = re.compile(r"prix|price|march\u00e9|marche|vente|vendre|achat|acheter|FCFA|CFA|tarif|cours", re.IGNORECASE)
        if _market_re.search(state.get("user_query", "")):
            warnings = list(state.get("warnings", []))
            warnings.append("Question marché détectée — hors-sujet pour AgriSoilAgent")
            return {"status": "OFF_TOPIC", "warnings": warnings, "rejection_reason": "Question marché — agent Sol uniquement."}
        village = location_profile.get("village", "").lower()
        observation = state.get("observation", "normal")
        warnings = list(state.get("warnings", []))

        # 1. Chargement du fichier JSON SoilGrids
        # Nettoyage du nom pour éviter les erreurs de chemin ---- a dapter avec postgress
        safe_name = re.sub(r'[^a-z0-9]', '', village)
        path = f"backend/sources/raw_data/soil_grids/{safe_name}.json"
        
        sg_data = {}
        try:
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    sg_data = json.load(f)
            else:
                warnings.append(f"Données SoilGrids introuvables pour {village}")
                # Fallback : on envoie un dict vide, le Doctor gérera ou on aura des valeurs par défaut
        except Exception as e:
            warnings.append(f"Erreur lecture fichier: {str(e)}")

        # 2. Appel au Docteur (Votre outil)
        try:
            # Cette méthode est purement déterministe (Python)
            diagnosis = self.doctor.get_diagnosis_from_soilgrids(sg_data, observation)
            status = "DIAGNOSIS_COMPLETE"
        except Exception as e:
            logger.error(f"Echec SoilDoctor: {e}")
            diagnosis = {}
            warnings.append("Le diagnostic technique a échoué.")
            status = "ERROR"

        state = dict(state)
        state.update({
            "soil_raw_data": sg_data,
            "technical_diagnosis": diagnosis,
            "warnings": warnings,
            "status": status
        })
        return state

    # ------------------------------------------------------------------ #
    # NODE B : RÉDACTION DE LA RÉPONSE (LLM)
    # ------------------------------------------------------------------ #

    def respond_node(self, state: SoilState) -> SoilState:
        """
        Transforme le JSON technique du Doctor en conseil paysan.
        """
        diag = state.get("technical_diagnosis", {})
        query = state.get("user_query", "")
        location = state.get("location_profile", {}).get("village", "votre zone")
        warnings = list(state.get("warnings", []))

        if not diag:
            return {"final_response": "Je n'ai pas pu analyser le sol de cette zone.", "status": "NO_DATA"}

        # Extraction des clés spécifiques de VOTRE SoilDoctorTool
        identite = diag.get("identite_pedologique", {})
        sante = diag.get("bilan_sante", {})
        eau = diag.get("gestion_eau", {})

        if not self.llm:
            warnings.append("LLM indisponible.")
            fallback = (
                f"Diagnostic technique : Sol de type {identite.get('nom_local', 'inconnu')}. "
                f"Conseil principal : {sante.get('action_organique', 'N/A')}. "
                f"Technique d'eau recommandée : {eau.get('strategie', 'N/A')}."
            )
            state = dict(state)
            resp = {"text": fallback, "agent": "soil", "cards": [{"title": "Analyse sol (mode secours)", "body": fallback}], "actions": [], "suggested": []}
            state.update({
                "final_response": fallback,
                "agri_response": resp,
                "status": "LLM_DOWN",
            })
            return state

        # Formatage des templates
        system_prompt = SOIL_SYSTEM_TEMPLATE.format(
            nom_local=identite.get("nom_local", "Sol non identifié"),
            nom_technique=identite.get("nom_technique", "")
        )
        user_prompt = SOIL_USER_TEMPLATE.format(
            location=location,
            query=query,
            nom_local=identite.get("nom_local", "inconnu"),
            atouts=identite.get("atouts", "N/A"),
            cultures=", ".join(identite.get("cultures_adaptees", [])) or "non déterminé",
            fertilite=sante.get("fertilite", "N/A"),
            action_organique=sante.get("action_organique", "N/A"),
            alerte_ph=sante.get("alerte_ph", "Normal"),
            besoin_eau=eau.get("besoin_eau", "N/A"),
            strategie_eau=eau.get("strategie", "N/A")
        )

        try:
            completion = self.llm.chat.completions.create(
                model=self.model_answer,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.3, # Bas pour rester fidèle aux données techniques
                max_tokens=400
            )
            response = completion.choices[0].message.content

            state = dict(state)
            resp = {"text": response, "agent": "soil", "cards": [{"title": "Analyse sol", "body": response}], "actions": [], "suggested": []}
            state.update({
                "final_response": response,
                "agri_response": resp,
                "status": "DONE",
            })
            return state
        
        except Exception as e:
            warnings.append(f"Erreur LLM : {e}")
            # Fallback simple si le LLM plante
            fallback = (
                f"Diagnostic technique : Sol de type {identite.get('nom_local')}. "
                f"Conseil principal : {sante.get('action_organique')}. "
                f"Technique d'eau recommandée : {eau.get('strategie')}."
            )
            state = dict(state)
            resp = {"text": fallback, "agent": "soil", "cards": [{"title": "Analyse sol (erreur)", "body": fallback}], "actions": [], "suggested": []}
            state.update({
                "final_response": fallback,
                "agri_response": resp,
                "status": "FALLBACK",
            })
            return state

    # ------------------------------------------------------------------ #
    # CONSTRUCTION DU GRAPHE
    # ------------------------------------------------------------------ #

    def build(self):
        workflow = StateGraph(SoilState)

        workflow.add_node("diagnose", self.diagnose_node)
        workflow.add_node("respond", self.respond_node)

        workflow.set_entry_point("diagnose")
        workflow.add_edge("diagnose", "respond")
        workflow.add_edge("respond", END)

        return workflow.compile()

# --- EXEMPLE D'UTILISATION (Simulé) ---
if __name__ == "__main__":
    # Simulation des données brutes (ce qui serait dans le JSON)
    # Pour tester sans fichier, on peut mocker la partie chargement ou créer un fichier dummy.
    logging.basicConfig(level=logging.INFO)
    
    agent = AgriSoilAgent()
    workflow = agent.build()
    
    state_input = {
        "user_query": "Est-ce que je peux faire du maïs ici ?",
        "location_profile": {"village": "Loumbila"}, # Assurez-vous d'avoir loumbila.json ou le code gérera l'erreur
        "observation": "Le sol est sec"
    }
    
    print("--- 🚜 Démarrage Agent Sol ---")
    result = workflow.invoke(state_input)
    
    print("\n--- 📝 Réponse Finale ---")
    print(result["final_response"])
    
    print("\n--- 🔧 Diagnostic Technique (Debug) ---")
    print(json.dumps(result["technical_diagnosis"], indent=2, ensure_ascii=False))