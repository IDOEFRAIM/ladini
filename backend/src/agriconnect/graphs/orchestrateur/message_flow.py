"""
Message Response Flow — Orchestrateur multi-experts AgriBot
============================================================

PHILOSOPHIE : "Salle de Crise" (pas "Standardiste")
-----------------------------------------------------
Tous les experts travaillent EN PARALLÈLE (fan-out / fan-in).
Le temps de réponse = max(expert_i), PAS sum(expert_i).

PATTERN :
  ANALYZE → route → [experts en parallèle] → SYNTHESIZE → TTS → PERSIST → END

  - Fan-out  : Le nœud PARALLEL_EXPERTS lance les experts activés simultanément.
  - Fan-in   : Le nœud SYNTHESIZE attend tous les résultats (via Reducer).
  - Reducer  : expert_responses: Annotated[List[ExpertResponse], operator.add]
               Chaque expert APPEND sa réponse ; pas de conflit d'écriture.
"""

import json
import os
import logging
import re
from typing import Any, Dict, List, Literal

from langgraph.types import Send

from langgraph.graph import END, StateGraph

from agriconnect.services.persistence import AgriPersister
from agriconnect.rag.components import get_groq_sdk
from agriconnect.graphs import route as routing
from agriconnect.graphs.orchestrateur.message_flow_setup import (
    init_db_and_memory,
    init_protocols,
    init_experts,
    init_services,
    init_tracing,
)
# NOTE: `init_tracing` is intentionally imported from
# `message_flow_setup` above — it accepts the orchestrator
# instance (self) and wires LangSmith specifically for this
# orchestration flow. Do NOT import `init_tracing` from
# `agriconnect.core.tracing` here (signature mismatch).
from agriconnect.core.tracing import get_tracing_config, trace_span
from agriconnect.core.setup import AgriContext
# Imports de vos structures et graphs.nodes
from ..state import GlobalAgriState
from agriconnect.graphs.nodes.sentinelle import ClimateSentinel
from agriconnect.graphs.nodes.formation import FormationCoach
from agriconnect.graphs.nodes.market import MarketCoach
from agriconnect.graphs.nodes.marketplace import MarketplaceAgent
from agriconnect.graphs.nodes.marketplace_v3 import MarketplaceAgentV3

# Expert mapping for host-centric instantiation
EXPERT_MAP = {
    "sentinelle": ClimateSentinel,
    "formation": FormationCoach,
    "market": MarketCoach,
    "marketplace": MarketplaceAgent,
    "marketplace_v3": MarketplaceAgentV3,
}

# Routing policy
MIN_CONFIDENCE = 0.7

# Services
from agriconnect.services.voice import VoiceEngine
from agriconnect.services.db_handler import AgriDatabase
from agriconnect.core.settings import settings
 
# Mémoire 3 niveaux (Profil + Épisodique + Optimiseur)
from agriconnect.services.memory import (
    UserFarmProfile,
    ProfileExtractor,
    EpisodicMemory,
    ContextOptimizer,
)
from agriconnect.graphs.orchestrateur.message_flow_router import MessageRouter
# ParallelExecutor removed — fan-out now handled by LangGraph Send

# ═══ Protocoles AgriConnect 2.0 (MCP + AG-UI) ═══
from agriconnect.protocols.mcp import MCPDatabaseServer, MCPRagServer, MCPWeatherServer, MCPContextServer
# Agent-to-agent protocol disabled: using local ExpertInvoker workflows
from agriconnect.protocols.ag_ui import AgriResponse, WhatsAppRenderer, WebRenderer, SMSRenderer

logger = logging.getLogger(__name__)


class MessageResponseFlow:
    """
    Orchestrateur ('Le Conseil') AgriConnect — version parallèle.

    Améliorations vs v1 séquentielle :
    1. Fan-out  : les experts s'exécutent en parallèle (ThreadPool).
    2. Fan-in   : un nœud SYNTHESIZE fusionne les réponses.
    3. Reducers : expert_responses est une liste additive (pas d'écrasement).
    4. Latence  : max(t_expert) au lieu de sum(t_expert).
    """


    def __init__(self, llm_client=None):
        self.llm = llm_client if llm_client is not None else get_groq_sdk()
        self.ctx = AgriContext(llm_client=self.llm).bootstrap()
        self._init_db_and_memory()
        self._init_protocols()
        self._init_experts()
        self._init_services()
        self._init_tracing()
        # Host-centric DI: ensure orchestrator is the authority and expose MCP session
        if getattr(self, "mcp_shield", None):
            try:
                setattr(self.mcp_shield, "authority", self)
            except Exception:
                pass
        if getattr(self, "mcp_session", None):
            try:
                setattr(self.ctx, "mcp_session", self.mcp_session)
                setattr(self.ctx, "mcp_shield", getattr(self, "mcp_shield", None))
            except Exception:
                pass
        # Router instance for modular routing logic
        try:
            self.router = routing.Router(self.llm, self.ctx)
        except Exception:
            # Fallback to module-level functions if Router cannot be instantiated
            self.router = None

        self.graph = self.build_graph()

    def _init_db_and_memory(self):
        return init_db_and_memory(self)

    def _init_protocols(self):
        """Delegate to setup module which builds the full Shield stack.

        After this call, the following are available:
        - self.mcp_shield  (MCPPermissionClient)
        - self.mcp_host    (MCPPermissionHostApp)
        - self.mcp_session (MCPSessionManager)
        All injected into self.ctx for expert DI.
        """
        init_protocols(self)
        return getattr(self, "mcp_shield", None)

    def _init_experts(self):
        return init_experts(self)

    def _init_services(self):
        return init_services(self)

    def _init_tracing(self):
        """
        Initialize tracing for this orchestrator.

        NOTE: This calls the `init_tracing` imported from
        `message_flow_setup`, which expects the orchestrator
        instance (`self`). Do NOT replace it with
        `agriconnect.core.tracing.init_tracing` (different signature).
        """
        return init_tracing(self)

    # ==================================================================
    # 1. ANALYSE DES BESOINS (The Chairman)
    # ==================================================================
##a2a
    # A2A discovery and manifest helpers removed — Host-centric flow uses static EXPERT_MAP

    def _call_llm_routing(self, query: str, expert_catalog: str) -> Dict[str, Any]:
        """Call LLM for intent classification."""
        system_prompt = self._build_routing_prompt(expert_catalog)
        try:
            response = self.llm.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[{"role": "system", "content": system_prompt},
                        {"role": "user", "content": query}],
                temperature=0,
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content
            logger.info("LLM raw routing response: %s", raw)
            return json.loads(raw)
        except Exception as e:
            logger.exception("LLM routing error: %s", e)
            raise

    def _build_routing_prompt(self, expert_catalog: str) -> str:
        """Build system prompt for routing."""
        # Avare & Stratégique system prompt: short catalog, max 2 experts, require confidence score
        return (
            "Tu es le 'Chairman' d'AgriConnect. Tu dois choisir le STRICT MINIMUM d'experts.\n"
            "Analyse la requête de l'agriculteur en fonction des experts DISPONIBLES ci-dessous :\n\n"
            f"{expert_catalog}\n\n"
            "DIRECTIVES :\n"
            "- Sélectionne au maximum 2 experts. NE JAMAIS en choisir plus sauf cas critique.\n"
            "- Retourne un champ 'confidence_score' entre 0.0 et 1.0 indiquant la confiance de ta décision.\n"
            "- Si confidence_score < 0.7, ne déclenche PAS d'expert (renvoie sélection vide).\n"
            "- 'sentinelle' pour maladies/météo, 'market' pour prix, 'formation' pour tutoriels, 'marketplace' pour achats.\n"
            "- Si la question est une salutation simple, choisis 'CHAT'.\n"
            "- Si hors-sujet/arnaque, choisis 'REJECT'.\n\n"
            "FORMAT DE SORTIE (JSON strict) :\n"
            '{"intent": "CHAT"|"SOLO"|"COUNCIL"|"REJECT", ' \
            '"selected_experts": ["nom_expert"], "confidence_score": 0.0, "reason": "texte court"}'
        )

    def _ensure_experts_selected(self, analysis: Dict[str, Any]) -> None:
        """Ensure at least one expert is selected."""
        sel = analysis.get("selected_experts") or []
        if not sel:
            from agriconnect.core.settings import settings as _settings
            analysis["selected_experts"] = ["dev_stub"] if _settings.DEBUG else ["sentinelle"]

    def analyze_needs(self, state: GlobalAgriState) -> Dict[str, Any]:
        """Simplified analysis: return selected expert names.

        - If a Router is available, use it but enforce the output format.
        - If no router, use a tiny deterministic keyword mapping.
        - If nothing selected or analysis fails -> fail-closed (empty selection).
        """
        # Build a short expert catalog for the Avare prompt
        expert_catalog = (
            "sentinelle: maladies/météo; market: prix/tendances; "
            "formation: tutoriels; marketplace: achats"
        )

        # Try Router first (if present) but validate shape and confidence
        try:
            if getattr(self, "router", None):
                analysis = self.router.analyze_needs(state)
            else:
                analysis = self._call_llm_routing(state.get("requete_utilisateur", ""), expert_catalog)

            if not isinstance(analysis, dict):
                raise ValueError("Invalid analysis response")

            intent = (analysis.get("intent") or "").upper()
            selected = analysis.get("selected_experts") or []
            confidence = float(analysis.get("confidence_score") or 0.0)

            # Enforce numeric confidence threshold (fail-closed on low confidence)
            if confidence < MIN_CONFIDENCE:
                logger.info("Routing low confidence (%.2f) — failing closed", confidence)
                return {"needs": {"selected_experts": [], "confidence_score": confidence, "intent": intent, "reason": analysis.get("reason")}, "execution_path": ["analyze"]}

            # Normalize intent
            if intent == "CHAT":
                return {"needs": {"intent": "CHAT", "selected_experts": [], "confidence_score": confidence, "reason": analysis.get("reason")}, "execution_path": ["analyze"]}

            if intent == "REJECT":
                return {"needs": {"intent": "REJECT", "selected_experts": [], "confidence_score": confidence, "reason": analysis.get("reason")}, "execution_path": ["analyze"]}

            # Limit number of experts to 2 as per policy
            if isinstance(selected, list) and len(selected) > 2:
                selected = selected[:2]

            return {"needs": {"selected_experts": selected, "confidence_score": confidence, "intent": intent, "reason": analysis.get("reason")}, "execution_path": ["analyze"]}
        except Exception as e:
            logger.exception("analyze_needs failed: %s", e)
            return {"needs": {"selected_experts": [], "confidence_score": 0.0, "intent": "REJECT", "reason": "analyze_error"}, "execution_path": ["analyze_error"]}


    # Manifest scoring and id-mapping removed in Host-centric mode



    def route_step(self, state: GlobalAgriState) -> Dict[str, Any]:
        """Dynamic Capability Routing step.

        Uses local workflow manifests and a lightweight router (heuristic)
        to select which experts to fan-out to.
        """
        # Route step now defers to analyze_needs for deterministic selection
        analysis = self.analyze_needs(state)
        selected = analysis.get("needs", {}).get("selected_experts", []) or []
        state["selected_experts"] = selected
        return {"next_experts": selected, "execution_path": ["route"], "routing_reason": "deterministic"}


    def execute_rejection(self, state: GlobalAgriState) -> Dict[str, Any]:
        """Prépare une réponse pédagogique en cas de message hors-sujet ou suspect."""
        reason = state.get("needs", {}).get("reason") or ""
        
        # Message par défaut pour le Sahel
        msg = (
            "Désolé, je suis AgriBot et je ne peux répondre qu'aux questions sur l'agriculture, "
            "l'élevage et les prix du marché. Comment puis-je vous aider pour vos cultures ?"
        )
        
        # Si c'est une arnaque détectée
        if "arnaque" in reason.lower() or "argent" in reason.lower():
            msg = "Attention : Pour votre sécurité, ne partagez jamais de codes secrets ou de transferts d'argent via ce chat."

        return {
            "final_response": msg,
            "execution_path": ["rejection_node"],
        }
    
    # ==================================================================
    # 2. ROUTAGE DYNAMIQUE
    # ==================================================================
    def route_flow(self, state: GlobalAgriState):
        """Route to the correct node(s).

        Returns:
            - A **string** (``"EXECUTE_CHAT"`` / ``"REJECT"``) for simple routes.
            - A **list of Send** objects for expert fan-out (replaces ThreadPoolExecutor).
        """
        # Evaluate intent first (CHAT/REJECT) then dispatch experts
        needs = state.get("needs", {}) or {}
        intent = (needs.get("intent") or "").upper()

        if intent == "CHAT":
            return "EXECUTE_CHAT"
        if intent == "REJECT":
            return "REJECT"

        selected = needs.get("selected_experts", []) or []
        # Fail-closed: if no experts selected, reject
        if not selected:
            return "REJECT"

        # Build Send fan-out for the chosen experts (max 2 enforced earlier)
        lead = selected[0]
        sends = [Send("RUN_EXPERT", {**state, "_expert_target": name, "_expert_is_lead": name == lead}) for name in selected]
        return sends

    # execute_solo_agent removed: Host orchestrator uses Run-Expert + Send for all experts

    # ==================================================================
    # 4. RUN_EXPERT — LangGraph Send target (fan-out / fan-in)
    # ==================================================================
    def run_expert_node(self, state: GlobalAgriState) -> Dict[str, Any]:
        """LangGraph Send target: execute ONE expert identified by ``_expert_target``.

        Each Send() call triggers this node with a different ``_expert_target``.
        The reducer on ``expert_responses`` (``operator.add``) collects all results.
        """
        from ..state import ExpertResponse

        name = state.get("_expert_target", "sentinelle")
        is_lead = state.get("_expert_is_lead", False)
        query = state.get("requete_utilisateur", "")
        context = {
            "zone_id": state.get("zone_id", "Bobo-Dioulasso"),
            "crop": state.get("crop", "Céréales"),
            "user_level": state.get("user_level", "debutant"),
            "user_phone": state.get("user_phone", ""),
        }

        try:
            trust = self._get_expert_trust_level(name)
            ctx = {**context, "trust_level": trust}

            ExpertClass = EXPERT_MAP.get(name)
            if ExpertClass is None:
                logger.warning("Unknown expert invoked: %s", name)
                res = {"response": "Expert indisponible."}
            else:
                # Instantiate the expert with host-provided security/session
                try:
                    expert = ExpertClass(mcp_shield=getattr(self, "mcp_shield", None), mcp_session=getattr(self, "mcp_session", None), llm=self.llm, ctx=self.ctx)
                except TypeError:
                    # Fallback to minimal constructor signatures
                    expert = ExpertClass(mcp_session=getattr(self, "mcp_session", None))

                # Prefer common handler names if implemented
                res = None
                for method in ("handle", "run", "execute", "respond", "call", "process"):
                    if hasattr(expert, method):
                        try:
                            res = getattr(expert, method)(query, ctx)
                        except Exception as e:
                            logger.exception("Expert %s.%s failed: %s", name, method, e)
                            res = {"response": "Expert execution error."}
                        break

                if res is None:
                    # No callable handler found; try attribute or __str__
                    res = getattr(expert, "response", None) or {"response": str(expert)}

            resp_text = res.get("response", "") if isinstance(res, dict) else str(res)
            has_alerts = res.get("has_alerts", False) if isinstance(res, dict) else False

            response = ExpertResponse(
                expert=name,
                response=resp_text,
                is_lead=is_lead,
                has_alerts=has_alerts,
            )
            return {"expert_responses": [response], "execution_path": [f"expert_{name}"]}
        except Exception as e:
            logger.error("Expert %s failed: %s", name, e)
            return {
                "expert_responses": [ExpertResponse(expert=name, response="", is_lead=is_lead, has_alerts=False)],
                "execution_path": [f"expert_{name}_error"],
            }

    # ==================================================================
    # 5. HITL GATE — suspends execution when human approval is required
    # ==================================================================
    def hitl_gate(self, state: GlobalAgriState) -> Dict[str, Any]:
        """Check if any expert flagged HITL.  If so, ``interrupt()`` the graph.

        When the graph is resumed the human decision is injected as
        ``hitl_status`` (``"APPROVED"`` or ``"REJECTED"``).
        """
        from langgraph.types import interrupt

        if not state.get("requires_validation"):
            return {}

        payload = state.get("pending_action_payload") or {}
        action_id = state.get("pending_action_id", "unknown")

        logger.info("HITL_GATE: suspending graph — action=%s payload=%s", action_id, payload)

        decision = interrupt({
            "action_id": action_id,
            "payload": payload,
            "message": "Cette action nécessite une validation humaine.",
        })

        human_status = "APPROVED" if decision in ("approved", "APPROVED", True) else "REJECTED"
        logger.info("HITL_GATE: resumed — decision=%s", human_status)

        if human_status == "REJECTED":
            return {
                "hitl_status": "REJECTED",
                "final_response": "Action annulée par l'opérateur.",
                "execution_path": ["hitl_rejected"],
            }

        return {"hitl_status": "APPROVED", "execution_path": ["hitl_approved"]}

    def execute_chat(self, state: GlobalAgriState) -> Dict[str, Any]:
        """Simplified chat handling: no A2A, no invoker.

        - Short greetings are answered locally.
        - Otherwise a polite handoff message is returned; actual expert dispatch
          is performed by `route_flow` -> `RUN_EXPERT` sends.
        """
        # Short-circuit simple chat intents to avoid remote round-trips for greetings
        query = state.get("requete_utilisateur", "") or ""
        q = query.strip().lower()
        greetings = ("bonjour", "salut", "bonsoir", "hello", "hi")
        if any(q.startswith(g) for g in greetings) or len(q.split()) <= 3:
            return {"final_response": "Bonjour ! Je suis AgriBot — comment puis-je vous aider aujourd'hui ?", "execution_path": ["chat_shortcircuit"]}

        # For non-trivial chat, instruct that the Host will route to experts.
        return {"final_response": "Je transfère votre demande à nos experts. Vous recevrez une réponse sous peu.", "execution_path": ["chat_forward"]}

    def synthesize_results(self, state: GlobalAgriState) -> Dict[str, Any]:
        """Combine expert responses into a single final_response."""
        expert_responses = state.get("expert_responses", []) or []
        if not expert_responses:
            final = state.get("final_response", "Aucun expert disponible pour le moment.")
            return {"final_response": final, "execution_path": ["synthesize_fallback"]}

        # Use the LLM to intelligently merge expert responses and avoid repetition
        full_text = "\n".join([f"{r['expert']}: {r['response']}" for r in expert_responses])
        prompt = (
            "Tu es l'agronome en chef. Fusionne ces rapports d'experts en une seule réponse fluide "
            "et sans répétitions pour l'agriculteur :\n" + full_text
        )
        try:
            final_msg = self.llm.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[{"role": "user", "content": prompt}]
            ).choices[0].message.content
        except Exception as e:
            logger.exception("LLM synthèse failed: %s", e)
            final_msg = "\n\n".join([f"{r.get('expert')}: {r.get('response', '')}" for r in expert_responses])

        return {"final_response": final_msg, "execution_path": ["smart_synthesize"], "expert_responses": expert_responses}

    def _get_expert_trust_level(self, agent_name: str) -> str:
        """Return a trust hint for a given expert. Can be extended to dynamic policies."""
        # Simple heuristic: marketplace/market are low-trust for writes; formation is low; sentinelle is read-only
        if agent_name.lower().startswith("market") or "marketplace" in agent_name.lower():
            return "LOW"
        if agent_name.lower().startswith("formation"):
            return "LOW"
        if agent_name.lower().startswith("sentinelle") or "sentinel" in agent_name.lower():
            return "READ_ONLY"
        return "ASK"
   
    # ==================================================================
    # 5. NETTOYAGE TTS
    # ==================================================================

    def clean_for_tts(self, text: str) -> str:
        """Supprime le Markdown et le HTML avant la synthèse vocale."""
        text = re.sub(r"[*_`#]", "", text)
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    # ==================================================================
    # 6. GÉNÉRATION AUDIO (TTS)
    # ==================================================================

    def generate_audio(self, state: GlobalAgriState) -> Dict[str, Any]:
        """Convertit final_response en fichier .wav via Azure TTS."""
        # Audio generation is temporarily disabled to avoid TTS side-effects
        # during local development and CI runs. This short-circuits the
        # TTS pipeline and always returns None for now.
        logger.info("Audio generation disabled (dev mode). Skipping TTS.")
        return {"audio_url": None}

    # ==================================================================
    # 7. PERSISTANCE (DB)
    # ==================================================================

    def persist(self, state: GlobalAgriState) -> Dict[str, Any]:
        """
        persistence de l'état final dans la base de données.
        """
        # Initialisation du persister avec les objets du contexte
        persister = AgriPersister(db=self.ctx.db, memory=self.ctx.memory)
        
        # Une seule ligne pour tout gérer
        persister.save_all(state)
        
        return {}
    # ==================================================================
    # BUILDER — Graphe LangGraph
    # ==================================================================
    def build_graph(self):
        """Build graph using the decomposed orchestrateur.flow builder."""
        from .flow import build_graph
        return build_graph(self)

    def _dispatch_node(self, node_key: str, state: Dict[str, Any]) -> Dict[str, Any]:
        """Dispatch to the appropriate node based on routing decision.

        For the lightweight ``run()`` path (no StateGraph runtime).
        Mirrors the Send-based graph logic sequentially.
        """
        if node_key == "EXECUTE_CHAT":
            return self.execute_chat(state)
        elif node_key == "REJECT":
            return self.execute_rejection(state)
        else:
            # Both PARALLEL_EXPERTS and SOLO_* go through run_expert_node
            needs = state.get("needs", {})
            selected = needs.get("selected_experts", [])
            if not selected and node_key.startswith("SOLO_"):
                selected = [node_key.replace("SOLO_", "").lower()]
            if not selected:
                selected = ["sentinelle"]
            lead = selected[0]
            all_responses = []
            for name in selected:
                expert_state = {**state, "_expert_target": name, "_expert_is_lead": name == lead}
                out = self.run_expert_node(expert_state)
                all_responses.extend(out.get("expert_responses", []))
            state["expert_responses"] = all_responses
            return self.synthesize_results(state)

    def _finalize_pipeline(self, state: Dict[str, Any]) -> None:
        """Execute final pipeline steps: audio generation and persistence."""
        try:
            audio = self.generate_audio(state)
            state.update(audio or {})
        except Exception:
            state["audio_url"] = None

        try:
            self.persist(state)
        except Exception:
            logger.debug("persist failed")

    def run(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Lightweight runner: execute the main pipeline without requiring the StateGraph runtime.

        This keeps the __main__ quick-tests working even if the external StateGraph API
        isn't available at runtime.
        """
        s = dict(state)

        # ANALYZE
        try:
            analysis = self.analyze_needs(s)
        except Exception as e:
            logger.debug("analyze_needs failed: %s", e)
            analysis = {"needs": {"intent": "REJECT", "reason": "analyze_error"}}

        s["needs"] = analysis.get("needs", {})

        # ROUTE
        try:
            node_key = self.route_flow(s)
        except Exception:
            node_key = "REJECT"

        # Dispatch
        out = self._dispatch_node(node_key, s)
        s.update(out or {})

        # AUDIO + PERSIST
        self._finalize_pipeline(s)

        return s


if __name__ == "__main__":
    # Quick local smoke test for the orchestrator when executed as a script.
    import logging

    logging.basicConfig(level=logging.INFO)
    print("Starting MessageResponseFlow smoke run...")
    flow = MessageResponseFlow()
    sample_states = [
        {"requete_utilisateur": "bonjour"},
        {"requete_utilisateur": "Quels sont les prix du maïs aujourd'hui ?", "zone_id": "Ouagadougou"},
        {"requete_utilisateur": "Ma plante a des taches noires sur les feuilles"},
    ]
    for st in sample_states:
        print("\n---\nQuery:", st.get("requete_utilisateur"))
        res = flow.run(st)
        print("Result:\n", res)
# NOTE: Smoke tests for the orchestrator have been moved to
# `backend/scripts/orchestrator_smoke_test.py` to keep the module
# clean and importable. Run that script directly for quick local
# checks (from repo root):
#
#   cd backend; $env:PYTHONPATH='src'; python -u scripts/orchestrator_smoke_test.py

