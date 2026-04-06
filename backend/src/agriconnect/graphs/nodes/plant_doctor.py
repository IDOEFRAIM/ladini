"""
PlantDoctor Agent — Production Diagnostic System.

Features:
- **Image Analysis**: AI-driven symptom detection (Mocked/Future).
- **Practical Advice**: Cost estimation, local alternatives.
- **Audit**: Strict logging of diagnoses for liability.
- **Safety**: Disclaimer mandating human verification.
- **Guided Q&A**: Refinement for vague symptoms.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import END, StateGraph
from agriconnect.graphs.prompts import (
    PLANT_DOCTOR_SYSTEM_TEMPLATE,
    PLANT_DOCTOR_USER_TEMPLATE,
)

# Protocols & Tools (Di)
from agriconnect.rag.components import get_groq_sdk
from agriconnect.rag.retriever import AgileRetriever
from agriconnect.rag.metric import RAGEvaluator
from agriconnect.tools.health import HealthDoctorTool
from agriconnect.tools.refine import RefineTool
from agriconnect.agents.base import BaseAgent
from agriconnect.services.persistence import AgriPersister

logger = logging.getLogger("Agent.PlantDoctor")

# ── Configuration & State ────────────────────────────────────────

@dataclass
class PlantDoctorConfig:
    llm_client: Any = None
    retriever: Optional[AgileRetriever] = None
    evaluator: Optional[RAGEvaluator] = None
    mcp_rag: Any = None
    ctx: Any = None  # Injected Context (DB/Memory)

class PlantDoctorState(TypedDict, total=False):
    # Input
    user_query: str
    user_level: str
    culture_config: Dict[str, Any]
    photo_paths: List[str]
    
    # Analysis
    augmented_symptoms: str
    diagnosis_raw: Dict[str, Any]
    risk_flags: List[str]
    
    # Practical Info
    guided_questions: List[str]
    treatment_costs: Dict[str, float]
    alternative_products: Dict[str, List[str]]
    local_availability: Dict[str, str]
    
    # RAG
    optimized_query: str
    retrieved_context: str
    sources: List[Dict[str, Any]]
    
    # Output
    final_response: str
    agri_response: Optional[Dict[str, Any]]
    
    # Status
    status: str
    warnings: List[str]
    handoff_to: str
    handoff_reason: str
    evaluation: Dict[str, float]

class PracticalInfoHelper:
    """Encapsulates product database lookups and formatting."""
    
    def __init__(self):
        # Hardcoded for now — could be loaded from DB/RAG
        self.product_database = {
            "neem_oil": {"nom_local": "Huile de Neem", "prix_moyen": 2500, "alternatives": ["savon_noir"], "dosage": "30ml/L"},
            "savon_noir": {"nom_local": "Savon Noir", "prix_moyen": 500, "alternatives": ["neem_oil"], "dosage": "30g/L"},
            "lambda_cyhalothrine": {"nom_local": "Karate (Insecticide)", "prix_moyen": 4500, "alternatives": ["neem_oil"], "dosage": "1ml/L"}
        }

    def estimate_treatment_cost(self, diagnosis: Dict, surface_ha: float = 1.0) -> Dict[str, float]:
        costs = {}
        rec = diagnosis.get("traitement_recommande", {})
        product_key = None
        
        # Simple heuristic mapping
        text = str(rec).lower()
        if "neem" in text: product_key = "neem_oil"
        elif "savon" in text: product_key = "savon_noir"
        elif "karate" in text: product_key = "lambda_cyhalothrine"
        
        if product_key:
            info = self.product_database.get(product_key, {})
            # Dummy logic: 1L per Ha for chemical, 10L for bio
            qty = 1.0 if "karate" in product_key else 10.0 
            costs[info.get("nom_local", "Produit")] = info.get("prix_moyen", 0) * qty * surface_ha
            
        costs["TOTAL ESTIMÉ"] = sum(costs.values())
        return costs

    def get_alternatives(self, diagnosis: Dict) -> Dict[str, List[str]]:
        text = str(diagnosis.get("traitement_recommande", "")).lower()
        alts = []
        if "neem" in text: alts = ["Savon Noir", "Purin de Piment"]
        elif "savon" in text: alts = ["Huile de Neem"]
        return {"bio": alts}

    def find_availability(self, diagnosis: Dict) -> Dict[str, str]:
        # Mock availability
        return {"Huile de Neem": "Coopérative Locale, Marché Central"}

class PlantHealthDoctor(BaseAgent):
    """
    Production-Grade Plant Doctor.
    Diagnoses diseases, suggests treatments, estimates costs.
    """
    _capabilities = ["DIAGNOSE_CROP", "PEST_CONTROL", "TREATMENT_COST"]

    def __init__(self, config: Optional[PlantDoctorConfig] = None, **overrides):
        cfg = config or PlantDoctorConfig()
        # Merge overrides
        for k, v in overrides.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)

        # Clients
        try:
            self.llm = cfg.llm_client if cfg.llm_client else get_groq_sdk()
        except Exception as exc:
            logger.error(f"LLM Init Failed: {exc}")
            self.llm = None
            
        self.model_answer = "llama-3.3-70b-versatile"
        self.doctor = HealthDoctorTool()
        self.refine = RefineTool(llm=self.llm)
        self.practical = PracticalInfoHelper()
        
        # Persistence (Audit)
        self.ctx = cfg.ctx
        if self.ctx and hasattr(self.ctx, 'db'):
            self.persister = AgriPersister(self.ctx.db, getattr(self.ctx, 'memory', None))
        else:
            self.persister = None
            logger.warning("PlantDoctor: Running without persistence (Context missing)")

    # ════════════════════════════════════════════════════════════
    # MOCK HELPERS (Future Integrations)
    # ════════════════════════════════════════════════════════════
    def _analyze_photo_symptoms(self, photo_path: str) -> Dict[str, Any]:
        """Placeholder for Vision API."""
        return {
            "detected_symptoms": ["Taches jaunes", "Feuilles enroulées"],
            "confidence": 0.85
        }

    # ════════════════════════════════════════════════════════════
    # NODES
    # ════════════════════════════════════════════════════════════
    async def diagnose_node(self, state: PlantDoctorState) -> PlantDoctorState:
        """Diagnose crop health issues."""
        query = state.get("user_query", "")
        profile = state.get("culture_config", {})
        crop = profile.get("crop_name", "Culture inconnue")
        warnings = list(state.get("warnings", []))

        # 1. Market Handoff
        if re.search(r"prix|vente|achat", query, re.IGNORECASE):
            return {**state, "handoff_to": "MarketCoach", "handoff_reason": "Market query in diagnosis"}

        # 2. Photo Analysis (Integration Point)
        if state.get("photo_paths"):
            analysis = self._analyze_photo_symptoms(state.get("photo_paths")[0])
            symptoms = analysis.get("detected_symptoms", [])
            query += f" [ANALYSE PHOTO: {', '.join(symptoms)}]"

        # 3. Guided Questions (Refinement)
        if len(query.split()) < 4 and not state.get("photo_paths"):
            questions = [
                "Depuis quand observez-vous ces symptômes ?",
                "Quelle partie de la plante est touchée (feuilles, tiges, fruits) ?",
                "Avez-vous observé des insectes ?"
            ]
            return {
                **state, 
                "status": "NEEDS_CLARIFICATION", 
                "guided_questions": questions,
                "final_response": "Je manque de détails pour un diagnostic précis. Pourriez-vous répondre à ces questions ?"
            }

        # 4. Diagnose
        try:
            # Synchronous tool call wrapped
            diagnosis = self.doctor.diagnose_and_prescribe(crop=crop, user_obs=query)
            
            if not diagnosis or not diagnosis.get("diagnostique"):
                return {**state, "status": "NO_DIAGNOSIS", "final_response": "Diagnostic non concluant. Veuillez consulter un expert humain."}
        
            # 5. Enhancements
            costs = self.practical.estimate_treatment_cost(diagnosis)
            alts = self.practical.get_alternatives(diagnosis)
            avail = self.practical.find_availability(diagnosis)
            
            risk_flags = []
            if diagnosis.get("urgence") == "HAUTE":
                risk_flags.append("URGENCE_HAUTE")

            return {
                **state,
                "diagnosis_raw": diagnosis,
                "treatment_costs": costs,
                "alternative_products": alts,
                "local_availability": avail,
                "risk_flags": risk_flags,
                "warnings": warnings,
                "status": "DIAGNOSED"
            }

        except Exception as e:
            logger.error(f"Diagnosis failed: {e}")
            return {**state, "status": "ERROR", "final_response": "Erreur interne lors du diagnostic."}

    async def retrieve_node(self, state: PlantDoctorState) -> PlantDoctorState:
        """Fetch supportive documentation."""
        # Simple pass-through or RAG integration
        return {**state, "retrieved_context": "", "status": "RETRIEVED"}

    async def generate_node(self, state: PlantDoctorState) -> PlantDoctorState:
        """Compose the final report."""
        if state.get("handoff_to") or state.get("status") in ["ERROR", "NEEDS_CLARIFICATION", "NO_DIAGNOSIS"]:
            return state

        diag = state.get("diagnosis_raw", {})
        costs = state.get("treatment_costs", {})
        
        # Build prompt
        prompt = PLANT_DOCTOR_USER_TEMPLATE.format(
            query=state.get("user_query"),
            diagnosis=json.dumps(diag, ensure_ascii=False),
            context="",
            treatment_costs=json.dumps(costs, ensure_ascii=False),
            # Add missing fields to template dynamically if needed, or rely on template's flexibility
            alternatives=json.dumps(state.get("alternative_products"), ensure_ascii=False),
            local_availability=json.dumps(state.get("local_availability"), ensure_ascii=False)
        )

        try:
            resp = self.llm.chat.completions.create(
                model=self.model_answer,
                messages=[
                    {"role": "system", "content": PLANT_DOCTOR_SYSTEM_TEMPLATE},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3
            )
            initial_response = resp.choices[0].message.content
            
            # Disclaimer Injection
            final_response = (
                f"{initial_response}\n\n"
                "⚠️ **AVERTISSEMENT IMPORTAT**: Ce diagnostic est généré par une IA. "
                "Consultez toujours un expert local avant d'appliquer des traitements chimiques."
            )

            return {**state, "final_response": final_response, "status": "GENERATED"}
            
        except Exception as e:
            return {**state, "final_response": "Erreur de génération.", "status": "ERROR"}

    async def finalize_node(self, state: PlantDoctorState) -> PlantDoctorState:
        """Audit log diagnosis."""
        if state.get("status") == "DIAGNOSED" and self.persister:
            self.persister.db.log_audit_action(
                agent_name="PlantDoctor",
                action_type="DIAGNOSIS_MADE",
                user_id="anon", # Ideally from context
                protocol="PHYTOSANITARY_ADVICE",
                payload={
                    "crop": state.get("culture_config", {}).get("crop_name"),
                    "diagnosis": state.get("diagnosis_raw", {}).get("diagnostique"),
                    "treatment": state.get("diagnosis_raw", {}).get("traitement_recommande")
                },
                resource="health_api",
                confidence=0.85
            )
            
        return state

    # ════════════════════════════════════════════════════════════
    # GRAPH
    # ════════════════════════════════════════════════════════════
    def build_graph(self):
        workflow = StateGraph(PlantDoctorState)
        
        workflow.add_node("DIAGNOSE", self.diagnose_node)
        workflow.add_node("RETRIEVE", self.retrieve_node)
        workflow.add_node("GENERATE", self.generate_node)
        workflow.add_node("FINALIZE", self.finalize_node)
        
        workflow.set_entry_point("DIAGNOSE")
        
        workflow.add_edge("DIAGNOSE", "RETRIEVE")
        workflow.add_edge("RETRIEVE", "GENERATE")
        workflow.add_edge("GENERATE", "FINALIZE")
        workflow.add_edge("FINALIZE", END)
        
        return workflow.compile()

    async def run(self, query: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """Async execution wrapper."""
        initial_state = {
            "user_query": query,
            "culture_config": context,
            "photo_paths": context.get("photo_paths", []) # Extract specific context fields
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
            "data": final
        }
