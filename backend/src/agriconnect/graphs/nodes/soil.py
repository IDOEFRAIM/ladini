"""
AgriSoil Agent — Production Pedology System.

Features:
- **Soil Analysis**: Interpretation of SoilGrids data.
- **Crop Suitability**: Matching soil properties to crop needs.
- **Audit**: Logging of soil advice.
- **Robustness**: Fallbacks for missing local data.
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import END, StateGraph
from agriconnect.graphs.prompts import (
    SOIL_SYSTEM_TEMPLATE,
    SOIL_USER_TEMPLATE
)

# Protocols & Tools (Di)
from agriconnect.tools.soil import SoilDoctorTool 
from agriconnect.rag.components import get_groq_sdk
from agriconnect.agents.base import BaseAgent
from agriconnect.services.persistence import AgriPersister

logger = logging.getLogger("Agent.AgriSoil")

# ── Configuration & State ────────────────────────────────────────

@dataclass
class SoilConfig:
    llm_client: Any = None
    data_path: str = "backend/sources/raw_data/soil_grids"  # Configurable path
    ctx: Any = None  # Injected Context (DB/Memory)

class SoilState(TypedDict, total=False):
    # Input
    user_query: str
    user_level: str
    location_profile: Dict[str, Any]
    observation: str  # ex: "sec", "humide"
    
    # Internal Data
    soil_raw_data: Dict[str, Any]
    technical_diagnosis: Dict[str, Any]
    
    # RAG/Output
    final_response: str
    agri_response: Optional[Dict[str, Any]]
    
    # Status
    status: str
    warnings: List[str]
    handoff_to: str
    handoff_reason: str

class AgriSoilAgent(BaseAgent):
    """
    Production-Grade Soil Specialist.
    Combines deterministic SoilGrids data with LLM-based advisory.
    """
    _capabilities = ["SOIL_ANALYSIS", "FERTILIZER_ADVICE", "CROP_SUITABILITY"]

    def __init__(self, config: Optional[SoilConfig] = None, **overrides):
        cfg = config or SoilConfig()
        # Merge overrides
        for k, v in overrides.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
        
        self.data_path = cfg.data_path
        self.doctor = SoilDoctorTool()
        self.model_answer = "llama-3.3-70b-versatile"

        try:
            self.llm = cfg.llm_client if cfg.llm_client else get_groq_sdk()
        except Exception as exc:
            logger.error("LLM Init Failed: %s", exc)
            self.llm = None
            
        # Persistence (Audit)
        self.ctx = cfg.ctx
        if self.ctx and hasattr(self.ctx, 'db'):
            self.persister = AgriPersister(self.ctx.db, getattr(self.ctx, 'memory', None))
        else:
            self.persister = None
            logger.warning("AgriSoil: Running without persistence (Context missing)")

    # ════════════════════════════════════════════════════════════
    # NODES
    # ════════════════════════════════════════════════════════════
    
    async def diagnose_node(self, state: SoilState) -> SoilState:
        """Load data and execute technical diagnosis."""
        query = state.get("user_query", "")
        location = state.get("location_profile", {}) or {}
        village = location.get("village", "").lower()
        observation = state.get("observation", "normal")
        warnings = list(state.get("warnings", []))

        # 1. Market Handoff
        if re.search(r"prix|vente|achat", query, re.IGNORECASE):
            return {**state, "handoff_to": "MarketCoach", "handoff_reason": "Market query in soil agent"}

        # 2. Data Loading (File-Based for now)
        safe_name = re.sub(r'[^a-z0-9]', '', village)
        target_file = os.path.join(self.data_path, f"{safe_name}.json")
        
        sg_data = {}
        try:
            if os.path.exists(target_file):
                with open(target_file, 'r', encoding='utf-8') as f:
                    sg_data = json.load(f)
            else:
                warnings.append(f"Données SoilGrids introuvables pour {village}")
                # We permit continuation with empty data -> Doctor returns defaults
        except Exception as e:
            logger.error(f"Soil Data Error: {e}")
            warnings.append("Erreur interne lecture données sol.")

        # 3. Technical Diagnostic (Deterministic)
        try:
            technical_diag = self.doctor.get_diagnosis_from_soilgrids(sg_data, observation)
            status = "DIAGNOSED"
        except Exception as e:
            logger.error(f"SoilDoctor Tool Failed: {e}")
            technical_diag = {}
            status = "ERROR"
            warnings.append("Échec du diagnostic technique.")

        return {
            **state,
            "soil_raw_data": sg_data,
            "technical_diagnosis": technical_diag,
            "warnings": warnings,
            "status": status
        }

    async def generate_node(self, state: SoilState) -> SoilState:
        """Transform technical data into peasant-friendly advice."""
        if state.get("handoff_to") or state.get("status") == "ERROR":
            return state

        diag = state.get("technical_diagnosis", {})
        identite = diag.get("identite_pedologique", {})
        sante = diag.get("bilan_sante", {})
        eau = diag.get("gestion_eau", {})
        
        # Fallback if no data loaded
        if not diag:
             return {**state, "final_response": "Je n'ai pas de données pour cette zone spécifique. Pouvez-vous décrire le sol (couleur, texture) ?", "status": "NO_DATA"}

        # Prompt
        sys_prompt = SOIL_SYSTEM_TEMPLATE.format(
            nom_local=identite.get("nom_local", "Sol non identifié"),
            nom_technique=identite.get("nom_technique", "")
        )
        user_prompt = SOIL_USER_TEMPLATE.format(
            location=state.get("location_profile", {}).get("village", "votre zone"),
            query=state.get("user_query"),
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
            resp = self.llm.chat.completions.create(
                model=self.model_answer,
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.3
            )
            answer = resp.choices[0].message.content
            return {**state, "final_response": answer, "status": "GENERATED"}
            
        except Exception as e:
            return {**state, "final_response": "Erreur de génération des conseils.", "status": "LLM_ERROR"}

    async def finalize_node(self, state: SoilState) -> SoilState:
        """Audit log advice."""
        if state.get("status") == "GENERATED" and self.persister:
            self.persister.db.log_audit_action(
                agent_name="AgriSoil",
                action_type="SOIL_ADVICE",
                user_id=state.get("location_profile", {}).get("user_id", "anon"),
                protocol="SOIL_MANAGEMENT",
                payload={
                    "soil_type": state.get("technical_diagnosis", {}).get("identite_pedologique", {}).get("nom_local"),
                    "village": state.get("location_profile", {}).get("village")
                },
                resource="soilgrids_local",
                confidence=0.9
            )
            
        return {
            **state,
            "agri_response": {"response": state.get("final_response"), "diagnosis": state.get("technical_diagnosis")}
        }

    # ════════════════════════════════════════════════════════════
    # GRAPH
    # ════════════════════════════════════════════════════════════
    def build_graph(self):
        workflow = StateGraph(SoilState)
        
        workflow.add_node("DIAGNOSE", self.diagnose_node)
        workflow.add_node("GENERATE", self.generate_node)
        workflow.add_node("FINALIZE", self.finalize_node)
        
        workflow.set_entry_point("DIAGNOSE")
        
        workflow.add_edge("DIAGNOSE", "GENERATE")
        workflow.add_edge("GENERATE", "FINALIZE")
        workflow.add_edge("FINALIZE", END)
        
        return workflow.compile()

    async def run(self, query: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """Async execution wrapper."""
        initial_state = {
            "user_query": query,
            "location_profile": context,
            "observation": context.get("observation", "normal")
        }
        
        app = self.build_graph()
        final = await app.ainvoke(initial_state)
        
        if final.get("handoff_to"):
            return {
                "response": final.get("final_response", ""),
                "handoff_to": final.get("handoff_to"),
                "reason": final.get("handoff_reason")
            }

        return {
            "response": final.get("final_response"),
            "data": final.get("agri_response")
        }
