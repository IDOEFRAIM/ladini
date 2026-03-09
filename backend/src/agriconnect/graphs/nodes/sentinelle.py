import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, TypedDict, Tuple

from pydantic import BaseModel, field_validator
from langgraph.graph import END, StateGraph
from agriconnect.graphs.prompts import (
    SENTINELLE_USER_TEMPLATE,
    SENTINELLE_SYSTEM_TEMPLATE,
    STYLE_GUIDANCE,
)

from agriconnect.rag.components import get_groq_sdk
from agriconnect.rag.metric import RAGEvaluator

# MCP servers injected by orchestrator (Host-Centric DI)

from agriconnect.tools.sentinelle import SentinelleTool
from agriconnect.tools.refine import RefineTool
from agriconnect.agents.base import BaseAgent
logger = logging.getLogger("Agent.ClimateSentinel")
# AG-UI removed from agent-level: return MCP-friendly dicts instead

# Mots-clés déclenchant un hand-off vers Marketplace (l'utilisateur
# a besoin d'acheter un intrant pour appliquer le traitement conseillé).
_PURCHASE_KEYWORDS = {
    "acheter", "achat", "fongicide", "pesticide", "herbicide", "intrant",
    "produit", "traitement", "insecticide", "engrais", "semence",
    "disponible", "trouver", "o\u00f9 trouver", "prix du produit",
}


class SentinelState(TypedDict, total=False):
    user_query: str
    location_profile: Dict[str, Any]
    user_level: str
    weather_snapshot: Dict[str, Any]
    satellite_signals: Dict[str, Any]
    raw_metrics: Dict[str, Any]
    flood_risk: Dict[str, Any]
    hazards: List[Dict[str, Any]]
    risk_summary: str
    optimized_query: str
    retrieved_context: str
    sources: List[Dict[str, Any]]
    final_response: str
    evaluation: Dict[str, float]
    status: str
    warnings: List[str]
    security_status: str
    security_reason: str
    critique_retry_count: int
    rewrited_retry_count: int
    degraded_mode: bool  # True when loop guards fire
    # HITL / Hand-off flags — interceptés par l'orchestrateur
    requires_human: bool
    handoff_to: str
    handoff_reason: str
    clarification_needed: str


@dataclass
class ComposeContext:
    query: str
    location: str
    risk_summary: str
    metrics: Dict[str, Any]
    flood: Dict[str, Any]
    hazards: List[Dict[str, Any]]
    context: str
    surface_calc_info: str = ""
    user_level: str = "debutant"
    warnings: List[str] = field(default_factory=list)


class ClimateSentinel(BaseAgent):
    """Agent de veille climatique — hérite de BaseAgent pour HITL/handoff."""

    _capabilities = [
        "CHECK_WEATHER", "GET_ALERT", "FLOOD_RISK",
        "SATELLITE_DATA", "AGRO_METEO",
    ]

    _STYLE_GUIDANCE = STYLE_GUIDANCE

    def __init__(
        self,
        llm_client=None,
        evaluator: Optional[RAGEvaluator] = None,
        mcp_session=None,
    ):
        self.model_planner = "llama-3.1-8b-instant"
        self.model_answer = "llama-3.3-70b-versatile"

        try:
            self.llm = llm_client if llm_client else get_groq_sdk()
        except Exception as exc:
            logger.error("Impossible d'initialiser le LLM : %s", exc)
            self.llm = None

        # Create RefineTool after LLM resolved so it has a valid client
        self.refine = RefineTool(llm=self.llm)
            
        self.tools = SentinelleTool(llm_client=self.llm)

        # MCP session injected by orchestrator (Host-Centric pattern)
        self.mcp_session = mcp_session
        self.db_server = mcp_session  # backward-compat alias
        self.rag_server = None  # RAG server managed separately if needed

        try:
            self.evaluator = evaluator if evaluator else RAGEvaluator()
        except Exception as exc:
            logger.warning("Évaluateur indisponible : %s", exc)
            self.evaluator = None

    # ------------------------------------------------------------------ #
    # Helpers                                                            #

    def _build_context(self, nodes: List[Any]) -> str:
        """Construit le contexte RAG à partir des nœuds récupérés (MCP RAGDocument)."""
        if not nodes:
            return ""
        
        context_parts = []
        for i, doc in enumerate(nodes):
            # Adapt to RAGDocument pydantic model from server
            text = getattr(doc, "excerpt", "") or getattr(doc, "content", "")
            source_name = getattr(doc, "source", "Inconnu")
            
            context_parts.append(
                f"--- DOCUMENT {i+1} ({source_name}) ---\n{text}\n"
            )
            
        return "\n".join(context_parts)

    def _serialize_sources(self, nodes: List[Any]) -> List[Dict[str, Any]]:
        """Extrait les métadonnées pour la traçabilité (MCP RAGDocument)."""
        sources = []
        for doc in nodes:
            # Adapt to RAGDocument pydantic model
            sources.append({
                "source": getattr(doc, "source", "Unknown"),
                "type": "doc",
                "similarity": getattr(doc, "score", 0.0)
            })
        return sources

    def _purchase_handoff_needed(self, query: str, hazards: list) -> bool:
        """
        Détecte si la requête ou les risques impliquent l'achat d'un produit.
        Pourquoi : main dans la main avec Marketplace sans passer par le routeur central.
        """
        q_lower = (query or "").lower()
        if any(kw in q_lower for kw in _PURCHASE_KEYWORDS):
            return True
        for h in (hazards or []):
            label = str(h.get("label", "")).lower()
            if any(kw in label for kw in ("maladie", "insecte", "parasite", "ravageur", "traitement")):
                return True
        return False

    def _format_location(self, profile: Dict) -> str:
        """Formate le profil géographique."""
        if not profile:
            return "Zone inconnue (Burkina Faso)"
        
        parts = []
        if profile.get("village"): parts.append(profile["village"])
        if profile.get("zone"): parts.append(profile["zone"])
        return ", ".join(parts) if parts else "Burkina Faso"
        
    def _fallback_response(self, query, location, risk_summary, metrics) -> str:
        """Génère une réponse de secours en cas d'échec du LLM."""
        return (
            "Désolé, je rencontre des difficultés techniques pour analyser votre demande en détail. "
            "Cependant, voici les observations actuelles :\n\n"
            f"📍 {location}\n"
            f"⚠️ Risques : {risk_summary}\n"
            f"🌧️ Pluie du jour : {metrics.get('precip_mm', 0)} mm\n\n"
            "Veuillez réessayer dans quelques instants."
        )

    def _assess_security(self, query: str, warnings: List[str]) -> Dict[str, str]:
        """Run moderation and return security status and reason; may append warnings."""
        moderation = self.tools._moderate_request(query)
        security_status = "SCAM_DETECTED" if moderation.get("is_scam") else "SAFE"
        security_reason = moderation.get("reason", "")
        if security_reason:
            warnings.append(security_reason)
        return {"security_status": security_status, "security_reason": security_reason}

    def _compute_signals(self, weather: Dict[str, Any], satellite: Dict[str, Any], location: Dict[str, Any]) -> Dict[str, Any]:
        """Compute metrics, flood risk and hazards from tools; return packed results."""
        metrics = self.tools._compute_metrics(weather, satellite)
        flood = self.tools._assess_flood_risk(weather, satellite, location)
        hazards = self.tools._derive_hazards(metrics, flood)
        return {"metrics": metrics, "flood": flood, "hazards": hazards}

    def _build_signals_state(self, metrics: Dict[str, Any], flood: Dict[str, Any], hazards: List[Dict[str, Any]], warnings: List[str]) -> Dict[str, Any]:
        """Prepare the state fields derived from signals."""
        if not hazards:
            warnings.append("Aucun risque majeur détecté (veille standard).")

        summary_lines = [
            f"- {hazard['label']} (niveau {hazard['severity']}): {hazard['explanation']}"
            for hazard in hazards
        ]
        risk_summary = "\n".join(summary_lines) or "Pas d'anomalie critique détectée."

        return {
            "raw_metrics": metrics,
            "flood_risk": flood,
            "hazards": hazards,
            "risk_summary": risk_summary,
            "warnings": warnings,
            "status": "SIGNALS_READY",
        }

    # ── Pydantic surface extractor (replaces raw regex) ──────────────
    class _SurfaceQuery(BaseModel):
        """Validates and normalises a surface value extracted from user text."""
        value: float
        unit: str  # 'ha' or 'm2'

        @field_validator('value')
        @classmethod
        def positive(cls, v: float) -> float:
            if v <= 0:
                raise ValueError('Surface must be positive')
            return v

        @property
        def area_m2(self) -> float:
            return self.value * 10_000 if self.unit == 'ha' else self.value

        @property
        def label(self) -> str:
            return f"{self.value} hectares" if self.unit == 'ha' else f"{self.value} m²"

    def _compute_surface_info(self, query_text: str, et0: float) -> str:
        """Extract surface (ha or m2) from text and compute water loss message."""
        if not query_text or et0 <= 0:
            return ""

        ha_match = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:ha|hectare|hectares)", query_text)
        m2_match = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:m2|m²|mc|metres?\s*carres?|mètres?\s*carrés?)", query_text)

        if not (ha_match or m2_match):
            return ""

        try:
            if ha_match:
                raw = ha_match.group(1).replace(",", ".")
                sq = self._SurfaceQuery(value=float(raw), unit='ha')
            else:
                raw = m2_match.group(1).replace(",", ".")
                sq = self._SurfaceQuery(value=float(raw), unit='m2')

            loss_liters = sq.area_m2 * et0
            return (
                f"CALCUL AUTOMATIQUE EFFECTUÉ : Pour {sq.label} avec une ET0 de {et0}mm, "
                f"la perte en eau est de {loss_liters:,.0f} litres AUJOURD'HUI. "
                "Intègre ce chiffre IMPÉRATIVEMENT dans ta réponse."
            )
        except Exception:
            return ""

    def _call_llm_for_compose(self, system_content: str, user_content: str) -> str:
        """Call the LLM for compose_node and return answer or raise Exception."""
        if not self.llm:
            raise RuntimeError("LLM indisponible")

        completion = self.llm.chat.completions.create(
            model=self.model_answer,
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_content},
            ],
            temperature=0.35,
            max_tokens=650,
        )
        answer = completion.choices[0].message.content
        if not answer:
            raise ValueError("Réponse vide du LLM.")
        return answer

    def _gather_compose_inputs(self, state: SentinelState) -> Dict[str, Any]:
        """Collect and return the small set of inputs compose_node needs."""
        # Use ComposeContext to group related parameters
        ctx = ComposeContext(
            query=state.get("user_query", ""),
            location=self._format_location(state.get("location_profile", {})),
            risk_summary=state.get("risk_summary", ""),
            metrics=state.get("raw_metrics", {}),
            flood=state.get("flood_risk", {}),
            hazards=state.get("hazards", []),
            context=state.get("retrieved_context", ""),
            surface_calc_info="",
            user_level=state.get("user_level", "debutant"),
            warnings=list(state.get("warnings", [])),
        )
        return ctx

    def _build_compose_prompt(
        self,
        ctx: "ComposeContext",
    ) -> "Tuple[str, str]":
        """Return (system_content, user_content) for compose_node using a ComposeContext."""
        niveau = str(ctx.user_level or "debutant").lower()
        style_guidance = self._STYLE_GUIDANCE.get(niveau, self._STYLE_GUIDANCE["default"]) 

        system_content = SENTINELLE_SYSTEM_TEMPLATE + f"\n\nCONSIGNE STYLE: {style_guidance}"

        # Dynamic date — never hardcode
        now = datetime.now(timezone.utc)
        current_date_str = now.strftime("%d %B %Y, %H:%Mh UTC")

        # Single serialisation pass — json.dumps once each (no double-encoding)
        user_content = SENTINELLE_USER_TEMPLATE.format(
            current_date_str=current_date_str,
            # Wrap user input to prevent prompt injection
            query=f"<user_input>{ctx.query}</user_input>",
            location=ctx.location,
            risk_summary=ctx.risk_summary,
            metrics_json=json.dumps(ctx.metrics, ensure_ascii=False),
            flood_data=json.dumps(ctx.flood, ensure_ascii=False),
            hazard_json=json.dumps(ctx.hazards, ensure_ascii=False),
            context=ctx.context,
            surface_calc_info=ctx.surface_calc_info,
        )

        return system_content, user_content


    
    def analyze_node(self, state: SentinelState) -> SentinelState:
        query = state.get("user_query", "").strip()
        warnings = list(state.get("warnings", []))
        # Role separation: refuse market-related queries
        _market_re = re.compile(r"prix|price|march\u00e9|marche|vente|vendre|achat|acheter|FCFA|CFA|tarif|cours", re.IGNORECASE)
        if _market_re.search(query):
            warnings.append("Question marché détectée — hors-sujet pour ClimateSentinel")
            state = dict(state)
            state.update({"warnings": warnings, "status": "OFF_TOPIC", "rejection_reason": "Question marché — agent climat uniquement."})
            return state
        location = state.get("location_profile", {}) or {}
        weather = state.get("weather_snapshot") or self.tools._fetch_real_weather(location)
        satellite = state.get("satellite_signals") or {}

        if not query:
            warnings.append("La requête de vigilance est vide.")
            state = dict(state)
            state.update({"warnings": warnings, "status": "ERROR"})
            return state

        # Security / moderation
        sec = self._assess_security(query, warnings)
        if sec["security_status"] == "SCAM_DETECTED":
            state = dict(state)
            state.update({
                "warnings": warnings,
                "security_status": sec["security_status"],
                "security_reason": sec["security_reason"],
                "status": "SCAM_DETECTED",
            })
            return state

        # Signals (metrics, flood, hazards)
        signals = self._compute_signals(weather, satellite, location)
        built = self._build_signals_state(signals["metrics"], signals["flood"], signals["hazards"], warnings)

        state = dict(state)
        state.update(built)
        state.update({
            "security_status": sec["security_status"],
            "security_reason": sec["security_reason"],
        })
        return state

    def retrieve_node(self, state: SentinelState) -> SentinelState:
        warnings = list(state.get("warnings", []))
        security_status = state.get("security_status", "SAFE")

        if security_status == "SCAM_DETECTED":
            warnings.append("Requête bloquée par le module anti-fraude.")
            return {
                "warnings": warnings,
                "security_status": security_status,
                "security_reason": state.get("security_reason", ""),
                "status": "SCAM_DETECTED",
            }

        query = state.get("user_query", "")
        risk_summary = state.get("risk_summary", "")
        hazards = state.get("hazards", [])

        plan = self.tools._plan_retrieval(query, risk_summary, hazards)
        optimized_query = plan.get("optimized_query") or query
        warnings.extend(plan.get("warnings", []))

        if not getattr(self, "retriever", None):
            warnings.append("Moteur RAG indisponible.")
            state = dict(state)
            resp = {
                "text": "Aucun moteur de récupération de documents RAG n'est configuré.",
                "agent": "sentinelle",
                "cards": [{"title": "Contexte introuvable", "body": "Aucun moteur de récupération de documents RAG n'est configuré."}],
                "actions": [],
                "suggested": [],
            }
            state.update({
                "optimized_query": optimized_query,
                "retrieved_context": "",
                "sources": [],
                "warnings": warnings,
                "security_status": security_status,
                "security_reason": state.get("security_reason", ""),
                "status": "NO_CONTEXT",
                "agri_response": resp,
            })
            return state

        nodes = self.retriever.search(
            optimized_query,
            user_level=state.get("user_level", "debutant"),
        )
        
        state = dict(state)
        if not nodes:
            warnings.append("Aucun document pertinent trouvé.")
            state = dict(state)
            resp = {
                "text": "Aucun document pertinent n'a été retrouvé pour enrichir la réponse.",
                "agent": "sentinelle",
                "cards": [{"title": "Pas de sources trouvées", "body": "Aucun document pertinent n'a été retrouvé pour enrichir la réponse."}],
                "actions": [],
                "suggested": [],
            }
            state.update({
                "optimized_query": optimized_query,
                "retrieved_context": "",
                "sources": [],
                "warnings": warnings,
                "security_status": security_status,
                "security_reason": state.get("security_reason", ""),
                "status": "NO_CONTEXT",
                "agri_response": resp,
            })
            return state

        state.update({
            "optimized_query": optimized_query,
            "retrieved_context": self._build_context(nodes),
            "sources": self._serialize_sources(nodes),
            "warnings": warnings,
            "security_status": security_status,
            "security_reason": state.get("security_reason", ""),
            "status": "CONTEXT_READY",
        })
        return state

    def compose_node(self, state: SentinelState) -> SentinelState:
        warnings = list(state.get("warnings", []))
        security_status = state.get("security_status", "SAFE")

        if security_status == "SCAM_DETECTED":
            alert = self._build_scam_response(state)
            state = dict(state)
            resp = {"text": alert, "agent": "sentinelle", "cards": [{"title": "Alerte sécurité", "body": alert}], "actions": [], "suggested": []}
            state.update({
                "final_response": alert,
                "agri_response": resp,
                "warnings": warnings,
                "status": "SCAM_DETECTED",
                "security_status": security_status,
                "security_reason": state.get("security_reason", ""),
            })
            return state

        ctx = self._gather_compose_inputs(state)

        ctx.surface_calc_info = self._compute_surface_info(ctx.query.lower(), ctx.metrics.get("et0_mm", 0.0))
        fallback = self._fallback_response(ctx.query, ctx.location, ctx.risk_summary, ctx.metrics)

        if not ctx.context:
            ctx.warnings.append("Réponse générée sans contexte vérifié.")

        if not self.llm:
            ctx.warnings.append("LLM indisponible (mode secours).")
            state = dict(state)
            resp = {"text": fallback, "agent": "sentinelle", "cards": [{"title": "Veille climatique (mode secours)", "body": fallback}], "actions": [], "suggested": []}
            state.update({
                "final_response": fallback,
                "agri_response": resp,
                "warnings": ctx.warnings,
                "status": "LLM_DOWN",
            })
            return state

        try:
            system_content, user_content = self._build_compose_prompt(ctx)

            answer = self._call_llm_for_compose(system_content, user_content)
            state = dict(state)
            resp = {"text": answer, "agent": "sentinelle", "cards": [{"title": "Veille climatique", "body": answer}], "actions": [], "suggested": []}
            state.update({
                "final_response": answer,
                "agri_response": resp,
                "warnings": ctx.warnings,
                "status": "ANSWER_READY",
            })
            # ── Hand-off vers Marketplace si besoin d'achat détecté ──
            if self._purchase_handoff_needed(ctx.query, state.get("hazards", [])):
                state = self.handoff(
                    state,
                    target_agent="marketplace",
                    context={
                        "query": ctx.query,
                        "hazards": state.get("hazards", []),
                        "risk_summary": ctx.risk_summary,
                    },
                    reason="Traitement phytosanitaire recommandé — besoin d'achat détecté",
                )
            return state
        except Exception as exc:
            ctx.warnings.append(f"Erreur LLM: {exc}")
            state = dict(state)
            resp = {"text": fallback, "agent": "sentinelle", "cards": [{"title": "Veille climatique (erreur)", "body": fallback}], "actions": [], "suggested": []}
            state.update({
                "final_response": fallback,
                "agri_response": resp,
                "warnings": ctx.warnings,
                "status": "LLM_ERROR",
            })
            return state

    def evaluate_node(self, state: SentinelState) -> SentinelState:
        warnings = list(state.get("warnings", []))
        if state.get("security_status") == "SCAM_DETECTED":
            warnings.append("Évaluation ignorée : requête bloquée pour suspicion de fraude.")
            state = dict(state)
            state.update({
                "warnings": warnings,
                "status": "SCAM_DETECTED",
                "security_status": state.get("security_status", "SAFE"),
            })
            return state

        if not self.evaluator:
            warnings.append("Évaluateur RAG indisponible.")
            state = dict(state)
            state.update({"warnings": warnings})
            return state

        query = state.get("user_query", "")
        context = state.get("retrieved_context", "")
        answer = state.get("final_response", "")

        if self._should_skip_evaluation(query, context, answer):
            state = dict(state)
            state.update({"warnings": warnings})
            return state

        try:
            scores = self.evaluator.evaluate_all(query=query, context=context, answer=answer)
            state = dict(state)
            state.update({"evaluation": scores, "warnings": warnings, "status": "EVALUATED"})
            return state
        except Exception as exc:
            warnings.append(f"Évaluation échouée: {exc}")
            state = dict(state)
            state.update({"warnings": warnings})
            return state

    def _should_skip_evaluation(self, query: str, context: str, answer: str) -> bool:
        """Decide whether to skip automatic evaluation (missing inputs)."""
        return not query or not context or not answer

    def _build_scam_response(self, state: SentinelState) -> str:
        reason = state.get("security_reason") or "Demande suspecte détectée."
        return (
            "🚨 **ALERTE SÉCURITÉ**\n"
            f"{reason}\n\n"
            "AgriConnect ne demande jamais de paiement ni de code Orange/Moov Money. "
            "Ne partagez pas vos informations sensibles et contactez un conseiller officiel."
        )


    # ------------------------------------------------------------------ #
    # Graphe                                                             #
    # ------------------------------------------------------------------ #

    def build(self):
        workflow = StateGraph(SentinelState)
        
        # Ajout des nœuds
        workflow.add_node("analyze", self.analyze_node)
        workflow.add_node("retrieve", self.retrieve_node)
        workflow.add_node("rewrite", self.refine.rewrite_query_node)  # ✅ Ajout du nœud manquant
        workflow.add_node("compose", self.compose_node)
        workflow.add_node("critique", self.refine.critique_node)      # ✅ Ajout du nœud manquant
        workflow.add_node("evaluate", self.evaluate_node)
        
        workflow.set_entry_point("analyze")

        # Conditional routing restored: analyze -> retrieve|compose
        # retrieve -> compose|rewrite (depending on retrieval result)
        # rewrite -> retrieve|compose (depending on rewrite outcome)
        workflow.add_conditional_edges("analyze", self.refine.route_after_analyze)

        workflow.add_conditional_edges(
            "retrieve",
            self.refine.route_retrieval,
            {"compose": "compose", "rewrite": "rewrite"}
        )

        # Keep rewrite conditional routing to allow retrieval retry or compose
        workflow.add_conditional_edges(
            "rewrite",
            self.refine.route_after_rewrite,
            {"retrieve": "retrieve", "compose": "compose"}
        )

        workflow.add_edge("compose", "critique")

        # ── DEGRADED_MODE loop guard ─────────────────────────────────
        def _route_critique(state: SentinelState) -> str:
            """Break potential critique->compose loops by forcing evaluate earlier.

            If any retry count is present (>=1) we consider the flow degraded and
            force the evaluation step to avoid long recursion during testing.
            """
            critique_retries = int(state.get("critique_retry_count", 0))
            rewrited_retries = int(state.get("rewrited_retry_count", 0))
            if critique_retries >= 1 or rewrited_retries >= 1:
                state["degraded_mode"] = True  # type: ignore[index]
                state["status"] = "DEGRADED_MODE"
                return "evaluate"
            return "evaluate" if state.get("status") == "VALIDATED" else "compose"

        workflow.add_conditional_edges(
            "critique",
            _route_critique,
            {"evaluate": "evaluate", "compose": "compose"},
        )

        workflow.add_edge("evaluate", END)
        # Allow a higher recursion limit for complex conditional routing
        try:
            return workflow.compile(config={"recursion_limit": 200})
        except TypeError:
            # Fallback if older langgraph versions don't accept config
            return workflow.compile()

    # Demo / smoke-test moved to: backend/scripts/sentinelle_smoke_test.py
    # This prevents side-effects on import and keeps the module import-safe.