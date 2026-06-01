import json
import logging
import re
import os
import inspect
import unicodedata
from typing import Any, List, Dict, Optional
from .formation_advisor import FormationAdvisor

logger = logging.getLogger("FormationTool")

class FormationTool:
    def __init__(self, llm, json_refs_path="agronomic_refs.json", model_planner="llama-3.3-70b-versatile"):
        self.llm = llm
        self.model_planner = model_planner
        self.json_refs_path = json_refs_path
        self.advisor = FormationAdvisor()
        # On charge la "Vérité" du JSON en mémoire vive
        self.agri_knowledge = self._load_json_refs()

    def _load_json_refs(self) -> Dict:
        """Charge le fichier JSON des références agronomiques."""
        if os.path.exists(self.json_refs_path):
            with open(self.json_refs_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {"CULTURES": {}}

    # -----------------------------------------------------------------
    # 1. L'ANALYSEUR : Trouve l'intention et le TYPE
    # -----------------------------------------------------------------
    async def analyze_request(self, query: str) -> Dict[str, Any]:
        """
        Analyse la demande pour extraire l'intention pédagogique 
        ET identifier le type de culture (le 'Produit').
        """
        available_types = list(self.agri_knowledge.get("CULTURES", {}).keys())
        
        prompt = (
            f"Tu es l'ingénieur pédagogique d'AgriConnect. Analyse : '{query}'\n\n"
            "Extraits en JSON :\n"
            "1. intent : FORMATION, URGENCE ou CONSEIL.\n"
            f"2. identified_crop_type : Choisis strictement parmi {available_types} ou 'UNKNOWN'.\n"
            "3. focus_topics : Liste de mots-clés.\n"
            "4. safety_flags : Risques (pesticides, etc.).\n"
            "5. urgency : NORMAL, HAUTE, CRITIQUE.\n"
        )
        
        try:
            maybe_completion = self.llm.chat.completions.create(
                model=self.model_planner,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}
            )
            completion = await maybe_completion if inspect.isawaitable(maybe_completion) else maybe_completion
            return json.loads(completion.choices[0].message.content)
        except Exception:
            return {"intent": "FORMATION", "identified_crop_type": "UNKNOWN"}

    # -----------------------------------------------------------------
    # 2. LE RÉCUPÉRATEUR : Renvoie les caractéristiques du JSON
    # -----------------------------------------------------------------
    def get_crop_characteristics(self, crop_type: str) -> Dict[str, Any]:
        """
        C'est ici que l'IA va chercher 'La Vérité' après avoir identifié le type.
        Utile pour enrichir le contexte avant le diagnostic.
        """
        if not crop_type or crop_type == "UNKNOWN":
            return {}
            
        cultures = self.agri_knowledge.get("CULTURES", {}) or {}

        # Direct lookup
        specs = cultures.get(crop_type, {})

        # Fallback: case/diacritics-insensitive lookup
        if not specs and isinstance(crop_type, str):
            def _norm(value: str) -> str:
                value = value.strip()
                value = "".join(
                    ch for ch in unicodedata.normalize("NFKD", value) if not unicodedata.combining(ch)
                )
                return value.casefold()

            target = _norm(crop_type)
            for key, value in cultures.items():
                if isinstance(key, str) and _norm(key) == target:
                    specs = value or {}
                    break
        
        if not specs:
            logger.warning(f"Type {crop_type} identifié mais absent du JSON de référence.")
            
        return specs

    # -----------------------------------------------------------------
    # 3. LE DIAGNOSTIC : Croise tout (Analyse + JSON + MCP)
    # -----------------------------------------------------------------
    async def run_full_diagnostic(self, query: str, cycle_id: Optional[str] = None) -> Dict[str, Any]:
        """Exemple de workflow complet utilisant les fonctions ci-dessus."""
        
        # Étape 1 : Analyser (Intent + Type)
        analysis = await self.analyze_request(query)
        crop_type = analysis.get("identified_crop_type")

        # Étape 2 : Caractériser (On récupère la 'matière' du JSON)
        # C'est la fonction que tu voulais absolument garder !
        specs = self.get_crop_characteristics(crop_type)

        # Étape 3 : Récupérer le Réel (MCP)
        context_real = {}
        if cycle_id:
            from agriconnect.services.database.handlers import get_field_360_context
            res = await get_field_360_context(cycle_id)
            context_real = json.loads(res).get("data", {})

        # Étape 4 : Synthèse
        return {
            "pédagogie": analysis,
            "références_json": specs,
            "terrain_mcp": context_real,
            "diagnostic": "..." # Logique de comparaison ici
        }