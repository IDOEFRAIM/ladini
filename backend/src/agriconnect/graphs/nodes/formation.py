import json
import logging
import re
from typing import Any, Dict, List, Optional, TypedDict
from dataclasses import dataclass, field
from langgraph.graph import END, StateGraph

from agriconnect.graphs.prompts import FORMATION_SYSTEM_TEMPLATE, FORMATION_USER_TEMPLATE, STYLE_GUIDANCE

# MCP Protocols
from agriconnect.protocols.mcp import MCPRagServer, MCPContextServer

# A2A Protocols
from agriconnect.protocols.a2a import A2ADiscovery, A2AMessage, MessageType

# AG-UI Protocols
from agriconnect.protocols.ag_ui import (
    AgriResponse, AGUIComponent, ComponentType,
    TextBlock, ActionButton, ActionType, ListPicker,
)

from agriconnect.tools.formation import FormationTool
from agriconnect.tools.refine import RefineTool
logger = logging.getLogger("Agent.FormationCoach")


class FormationAgentState(TypedDict, total=False):
    user_query: str
    learner_profile: Dict[str, Any]
    intent: str
    urgency: str
    focus_topics: List[str]
    field_actions: List[str]
    safety_flags: List[str]
    optimized_query: str
    learning_modules: List[str]
    prerequisites: List[str]
    reasoning: str
    retrieved_context: str
    sources: List[Dict[str, Any]]       # overwrite each cycle (no accumulation)
    answer_draft: str
    final_response: str
    evaluation: Dict[str, float]
    status: str
    is_relevant: Optional[bool]
    rejection_reason:str
    warnings: List[str]                  # overwrite each cycle (no accumulation)
    critique_retry_count: int
    rewrited_retry_count: int
    degraded_mode: bool                  # True when critique_retry_count > MAX_RETRIES
    a2a_route_topic: Optional[str]       # set when query should be routed via A2A
    agri_response: Optional[AgriResponse]


@dataclass
class GenerationContext:
    state: FormationAgentState
    context: str
    query: str
    profile_text: str
    warnings: List[str]


@dataclass
class FormationConfig:
    llm_client: Any = None
    mcp_rag: Optional[MCPRagServer] = None
    mcp_context: Optional[MCPContextServer] = None
    a2a: Optional[A2ADiscovery] = None   # A2A channel for semantic routing
    retriever: Any = None
    evaluator: Any = None

class FormationCoach:
    def __init__(self, config: Optional[FormationConfig] = None, **overrides):
        """Initialize the FormationCoach.

        Prefers a single `FormationConfig` parameter. Backwards-compatible kwargs
        (`llm_client`, `mcp_rag`, `mcp_context`, `retriever`, `evaluator`) are
        accepted via `overrides` to avoid breaking existing callers.
        """
        # Merge config and overrides
        cfg = config or FormationConfig()
        # allow overrides from legacy kwargs
        for k, v in overrides.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)

        llm_client = cfg.llm_client
        self.tool = FormationTool(llm=llm_client)
        self.refine = RefineTool(llm=llm_client)

        # Protocol Servers
        self.mcp_rag = cfg.mcp_rag
        self.mcp_context = cfg.mcp_context
        self.a2a = cfg.a2a  # Optional A2A channel for semantic routing

        self.model_planner = "llama-3.1-8b-instant"
        self.model_answer = "llama-3.3-70b-versatile"

        # Regex for detecting market/price queries that should be routed via A2A
        self._market_re = re.compile(
            r"prix|price|march\u00e9|marche|vente|vendre|achat|acheter|FCFA|CFA|tarif|cours",
            re.IGNORECASE,
        )

        try:
            from agriconnect.rag.components import get_groq_sdk
            self.llm = llm_client if llm_client else get_groq_sdk()
        except Exception as exc:
            logger.error("Impossible d'initialiser le LLM : %s", exc)
            self.llm = None

        # Retro-compatibility wrapper if old retriever passed
        if cfg.retriever and not cfg.mcp_rag:
            logger.warning("Using legacy retriever. Please migrate to MCPRagServer.")
            # No simple wrapper possible without proper MCP init, assuming mcp_rag is passed by orchestrator

    # ------------------------------------------------------------------ #
    # Nœuds du graphe                                                    #
    # ------------------------------------------------------------------ #

    def analyze_node(self, state: FormationAgentState) -> FormationAgentState:
        """
        Analyse la question + Récupère le contexte via MCP Context.
        Détecte les requêtes de type prix/marché et les marque pour routage A2A.
        """
        query = state.get("user_query", "").strip()
        if not query:
            warnings = list(state.get("warnings", []))
            warnings.append("La question de formation est vide.")
            return {"status": "ERROR", "warnings": warnings}

        # ── A2A semantic routing: market / price topics ───────────────────────
        if self.a2a and self._market_re.search(query):
            logger.info(
                "FormationCoach: market topic detected in query — routing to A2A market.price_check"
            )
            return {
                "status": "A2A_ROUTE",
                "a2a_route_topic": "market.price_check",
                "warnings": list(state.get("warnings", [])),
            }

        profile = self._get_profile_for_analyze(state)
        warnings = list(state.get("warnings", []))

        analysis = self.tool._analyze_request(query, profile)
        return self._assemble_analyze_result(analysis, warnings)

    def a2a_dispatch_node(self, state: FormationAgentState) -> FormationAgentState:
        """Emit an A2A request for the detected topic and build a holding response."""
        state = dict(state)
        query = state.get("user_query", "")
        topic = state.get("a2a_route_topic", "market.price_check")
        warnings = list(state.get("warnings", []))

        try:
            msg = A2AMessage(
                message_type=MessageType.REQUEST,
                sender_id="agent_formation",
                receiver_id="agent_marketplace",
                intent="A2A_REQUEST",
                topic=topic,
                payload={"query": query, "source_agent": "formation"},
            )
            ack = self.a2a.send_message(msg)
            logger.info("A2A dispatch to %s — correlation=%s", topic, msg.correlation.correlation_id)
            final_text = (
                "📊 Cette question porte sur les prix du marché. "
                "Je consulte l'agent marché pour vous obtenir les informations les plus à jour."
            )
        except Exception as exc:
            warnings.append(f"A2A dispatch échoué : {exc}")
            final_text = (
                "👋 Pour les questions de prix, je vous recommande de contacter "
                "directement votre agent de marché local."
            )

        state["final_response"] = final_text
        state["warnings"] = warnings
        state["status"] = "COMPLETED"
        return state

    def _get_profile_for_analyze(self, state: FormationAgentState) -> Dict[str, Any]:
        """Retrieve learner profile preferring MCP context when available.

        Kept as a helper to reduce branching in `analyze_node`.
        """
        profile = {}
        if self.mcp_context:
            user_id = state.get("user_profile", {}).get("id", "anonymous")
            try:
                profile = self.mcp_context.read_user_context(user_id)
            except Exception as e:
                logger.warning(f"MCP Context read failed: {e}")
                profile = state.get("learner_profile", {})
        else:
            profile = state.get("learner_profile", {})
        return profile

    def _assemble_analyze_result(self, analysis: Dict[str, Any], warnings: List[str]) -> FormationAgentState:
        """Compose the analyze_node result dict from analysis output.

        Centralizing this makes tests easier and reduces method-level complexity.
        """
        intent = analysis.get("intent", "FORMATION")
        focus_topics = analysis.get("focus_topics", [])
        field_actions = analysis.get("field_actions", [])
        safety = analysis.get("safety_flags", [])
        urgency = analysis.get("urgency", "NORMAL")
        warnings.extend(analysis.get("warnings", []))
        is_relevant = analysis.get("is_relevant", True)
        rejection_reason = analysis.get("rejection_reason", "")

        return {
            "intent": intent,
            "focus_topics": focus_topics,
            "field_actions": field_actions,
            "safety_flags": safety,
            "urgency": urgency,
            "warnings": warnings,
            "is_relevant": is_relevant,
            "rejection_reason": rejection_reason,
            "status": "ANALYZED",
        }

    def retrieve_node(self, state: FormationAgentState) -> FormationAgentState:
        """
        Recherche RAG via MCPRagServer.
        """
        warnings = list(state.get("warnings", []))
        query = state.get("user_query", "").strip()

        if not query:
            return {"status": "ERROR", "warnings": warnings}

        profile = self._get_profile_for_retrieval(state)
        optimized_query = self._get_optimized_query(state, query, profile)

        return self._call_mcp_rag_and_parse(optimized_query, profile, warnings)

    def _get_profile_for_retrieval(self, state: FormationAgentState) -> Dict[str, Any]:
        profile = state.get("learner_profile", {})
        if self.mcp_context:
            try:
                profile = self.mcp_context.read_user_context(state.get("user_profile", {}).get("id"))
            except Exception:
                pass
        return profile

    def _get_optimized_query(self, state: FormationAgentState, query: str, profile: Dict[str, Any]) -> str:
        # Handle retries: reuse optimized query if already present
        if state.get("status") == "RETRY_SEARCH" and state.get("optimized_query"):
            return state.get("optimized_query")

        plan = self.tool._plan_retrieval(query, profile)
        return plan.get("optimized_query") or query

    def _normalize_sources(self, sources_raw: Any) -> List[Dict[str, Any]]:
        norm_sources: List[Dict[str, Any]] = []
        for s in sources_raw or []:
            if isinstance(s, dict):
                norm_sources.append({"title": s.get("source", s.get("title", "Doc")), "uri": s.get("uri")})
            else:
                norm_sources.append({"title": str(s), "uri": None})
        return norm_sources

    def _call_mcp_rag_and_parse(self, optimized_query: str, profile: Dict[str, Any], warnings: List[str]) -> FormationAgentState:
        if not self.mcp_rag:
            warnings.append("MCP RAG Server manquant.")
            return {"status": "NO_CONTEXT", "warnings": warnings}

        try:
            search_level = profile.get("niveau", "debutant")
            args = {"query": optimized_query, "level": search_level, "top_k": 4}
            resp = self.mcp_rag.call_tool("search_agronomy_docs", args)

            # Delegate parsing/validation to helper to reduce branching here
            parsed_context, parsed_sources = self._parse_mcp_response(resp)
            norm_sources = self._normalize_sources(parsed_sources)

            return {
                "optimized_query": optimized_query,
                "retrieved_context": parsed_context or "",
                "sources": norm_sources,
                "status": "CONTEXT_FOUND",
                "warnings": warnings,
            }

        except Exception as e:
            logger.error(f"MCP RAG Error: {e}")
            warnings.append(f"Erreur MCP RAG: {e}")
            return {"status": "NO_CONTEXT", "warnings": warnings}

    def _parse_mcp_response(self, resp: Dict[str, Any]) -> tuple[str, List[Dict[str, Any]]]:
        """Parse MCP RAG response and return (context_text, sources).

        Raises ValueError on invalid or empty responses.
        """
        if not resp or resp.get("status") != "ok":
            raise ValueError("MCP response invalid or error status")

        content_list = resp.get("content") or []
        if not content_list:
            raise ValueError("MCP response empty content")

        raw_text = content_list[0].get("text", "")
        try:
            parsed = json.loads(raw_text)
        except Exception:
            parsed = raw_text if isinstance(raw_text, dict) else {}

        context_text = parsed.get("context") if isinstance(parsed, dict) else str(parsed)
        sources = parsed.get("sources", []) if isinstance(parsed, dict) else []
        return context_text, sources


    def compose_node(self, state: FormationAgentState) -> FormationAgentState:
        warnings = list(state.get("warnings", []))

        # Off-topic handling remains straightforward
        if state.get("is_relevant") is False:
            rejection = state.get("rejection_reason") or "Désolé, je ne peux répondre qu'aux questions agricoles."
            final_rejection = f"😊 **Bonjour !**\n\n{rejection}\n\nEn tant qu'expert AgriConnect, je suis à votre disposition pour toute question sur vos cultures."
            agri_response = AgriResponse()
            agri_response.add_text(final_rejection)
            return {
                "answer_draft": final_rejection,
                "final_response": final_rejection,
                "agri_response": agri_response,
                "status": "OFF_TOPIC",
                "warnings": warnings,
            }

        # Prepare inputs
        context = state.get("retrieved_context", "").strip()
        query = state.get("user_query", "").strip()
        profile_text = self.tool._format_profile(state.get("learner_profile", {}))

        if not query:
            return {"warnings": warnings, "status": "ERROR"}

        gen_ctx = GenerationContext(state=state, context=context, query=query, profile_text=profile_text, warnings=warnings)
        final_answer, warnings = self._generate_final_answer(gen_ctx)

        agri_response = self._build_agri_response(final_answer, query)

        return {
            "answer_draft": final_answer,
            "final_response": final_answer,
            "agri_response": agri_response,
            "warnings": warnings,
            "status": "ANSWER_GENERATED",
        }

    def _generate_final_answer(self, gen_ctx: GenerationContext):
        """Wrap the LLM prompt construction and call, returning (answer, warnings).

        Keeps `compose_node` focused on high-level flow.
        """
        final_answer = "Réponse technique indisponible."
        state = gen_ctx.state
        context = gen_ctx.context
        query = gen_ctx.query
        profile_text = gen_ctx.profile_text
        warnings = gen_ctx.warnings
        try:
            profile = state.get("learner_profile", {})
            level = str(profile.get("niveau", "standard")).lower()
            style_guidance = STYLE_GUIDANCE.get(level, STYLE_GUIDANCE["default"])

            system_content = FORMATION_SYSTEM_TEMPLATE.format(
                style_guidance=style_guidance,
                culture_context=f"Culture: {profile.get('culture_actuelle', 'N/A')}"
            )
            user_content = FORMATION_USER_TEMPLATE.format(
                query=query,
                feedback_hallucination=state.get("feedback_hallucination", ""),
                intent=state.get("intent", "FORMATION"),
                urgency=state.get("urgency", "NORMAL"),
                profile_text=profile_text,
                context=context,
            )

            completion = self.llm.chat.completions.create(
                model=self.model_answer,
                messages=[{"role": "system", "content": system_content}, {"role": "user", "content": user_content}],
                temperature=0.35,
                max_tokens=900,
            )
            final_answer = completion.choices[0].message.content
        except Exception as e:
            logger.error(f"LLM Error: {e}")
            final_answer = "Désolé, je rencontre une difficulté technique pour formuler le conseil."
            warnings.append(str(e))
        return final_answer, warnings

    def _build_agri_response(self, final_answer: str, query: str) -> AgriResponse:
        """Construct an `AgriResponse` with the main text and quick-reply picker."""
        agri_response = AgriResponse()
        agri_response.add_text(final_answer)

        # Quick replies based on simple keywords in the question
        ql = query.lower()
        if "maladie" in ql or "ravageur" in ql:
            agri_response.add(ListPicker(
                title="Que voulez-vous faire ?",
                items=[{"id": "photo_upload", "label": "📷 Envoyer une photo"}, {"id": "call_expert", "label": "📞 Parler à un agent"}],
            ))
        else:
            agri_response.add(ListPicker(
                title="Aller plus loin :",
                items=[{"id": "more_details", "label": "🔍 Plus de détails"}, {"id": "related_topic", "label": "🌾 Sujet associé"}],
            ))
        return agri_response

    def evaluate_node(self, state: FormationAgentState) -> FormationAgentState:
        warnings = list(state.get("warnings", []))

        if not getattr(self, "evaluator", None):
            warnings.append("Évaluation automatique indisponible.")
            return {"warnings": warnings}

        query = state.get("user_query", "")
        context = state.get("retrieved_context", "")
        answer = state.get("final_response", "")

        if not answer or not context:
            return {"warnings": warnings}

        try:
            scores = self.evaluator.evaluate_all(
                query=query,
                context=context,
                answer=answer,
            )
            return{
                "evaluation": scores,
                "warnings": warnings,
                "status": "EVALUATED",
            }
        except Exception as exc:
            warnings.append(f"Évaluation échouée : {exc}")
            return {"warnings": warnings}


    # ------------------------------------------------------------------ #
    # build                                                       
    # ------------------------------------------------------------------ #


    def build(self):
        workflow = StateGraph(FormationAgentState)
        
        # nœuds
        workflow.add_node("analyze", self.analyze_node)
        workflow.add_node("a2a_dispatch", self.a2a_dispatch_node)
        workflow.add_node("retrieve", self.retrieve_node)
        workflow.add_node("rewrite", self.refine.rewrite_query_node) 
        workflow.add_node("compose", self.compose_node)
        workflow.add_node("critique", self.refine.critique_node)  
        workflow.add_node("evaluate", self.evaluate_node)

        workflow.set_entry_point("analyze")

        # After analyze: A2A‐route or normal flow
        def _route_after_analyze(state: FormationAgentState) -> str:
            if state.get("status") == "A2A_ROUTE":
                return "a2a_dispatch"
            return self.refine.route_after_analyze(state)

        workflow.add_conditional_edges("analyze", _route_after_analyze)
        workflow.add_edge("a2a_dispatch", END)
        
        workflow.add_conditional_edges(
            "retrieve", 
            self.refine.route_retrieval,
            {"compose": "compose", "rewrite": "rewrite"}
        )
        
        # Modif : Routage conditionnel après Rewrite pour éviter boucle infinie
        workflow.add_conditional_edges(
            "rewrite", 
            self.refine.route_after_rewrite,
            {"retrieve": "retrieve", "compose": "compose"}
        )
        
        workflow.add_edge("compose", "critique")
        
        # DEGRADED_MODE loop guard: force evaluate when retry limit exceeded
        def _route_critique(state: FormationAgentState) -> str:
            critique_retries  = int(state.get("critique_retry_count", 0))
            rewrited_retries  = int(state.get("rewrited_retry_count", 0))
            if critique_retries > 2 or rewrited_retries > 2:
                logger.error(
                    "FormationCoach DEGRADED_MODE: critique_retries=%d rewrited_retries=%d",
                    critique_retries, rewrited_retries,
                )
                # Mutate state so downstream nodes know we are degraded
                state["degraded_mode"] = True
                state["status"] = "DEGRADED_MODE"
                return "evaluate"
            return "evaluate" if state.get("status") == "VALIDATED" else "compose"

        workflow.add_conditional_edges(
            "critique",
            _route_critique,
            {"evaluate": "evaluate", "compose": "compose"}
        )
        
        workflow.add_edge("evaluate", END)
        return workflow.compile()


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    agent = FormationCoach()
    workflow = agent.build()
    # Exemple d’état initial
    state = {
        "user_query": "Que sais tu sur la saison culture au burkina?",
        "learner_profile": {"niveau": "débutant", "région": "Boucle du Mouhoun"},
    }
    result = workflow.invoke(state)
    print(json.dumps(result, indent=2, ensure_ascii=False))