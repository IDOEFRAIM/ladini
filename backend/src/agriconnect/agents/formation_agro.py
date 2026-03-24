"""
Formation Agent — standalone test harness for the agronome.

    Implements the **full** FormationCoach logic (analyze → retrieve → grade →
    compose → critique → evaluate) but wired to the repository RAG retriever
    directly (no MCP server, no agent-to-agent broker, no audio/TTS).

The goal is to let an agronome test the formation reasoning in isolation:

    from agriconnect.agents.formation_agro import FormationAgro
    agent = FormationAgro()
    print(agent.ask("Comment faire la rotation des cultures ?"))
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional, TypedDict
from dataclasses import dataclass
from langgraph.graph import END, StateGraph

from agriconnect.graphs.prompts import (
    FORMATION_SYSTEM_TEMPLATE,
    FORMATION_USER_TEMPLATE,
    STYLE_GUIDANCE,
)
# AG-UI removed from agent-level: return MCP-friendly dicts instead
from agriconnect.tools.formation import FormationTool
from agriconnect.tools.refine import RefineTool
from agriconnect.rag.retriever import AgileRetriever

logger = logging.getLogger("Agent.FormationAgro")

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

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
    sources: List[Dict[str, Any]]
    answer_draft: str
    final_response: str
    evaluation: Dict[str, float]
    status: str
    warnings: List[str]
    critique_retry_count: int
    rewrited_retry_count: int
    degraded_mode: bool
    agri_response: Optional[Dict[str, Any]]
    concepts_appris: List[str]
    document_grade: int


@dataclass
class GenerationContext:
    state: FormationAgentState
    context: str
    query: str
    profile_text: str
    warnings: List[str]

# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class FormationAgro:
    """Self-contained formation agent for agronome testing.

    Uses the RAG ``AgileRetriever`` directly (no MCP / no agent-to-agent).
    """

    def __init__(self, llm_client: Any = None, learner_profile: Optional[Dict[str, Any]] = None):
        # LLM ------------------------------------------------------------------
        self.llm = llm_client
        self._ensure_llm()

        # Tools -----------------------------------------------------------------
        self.tool = FormationTool(llm=self.llm)
        self.refine = RefineTool(llm=self.llm)
        # Push resolved llm back into tools (they may have been None at init)
        try:
            self.tool.llm = self.llm
        except Exception:
            pass
        try:
            self.refine.llm = self.llm
        except Exception:
            pass

        # RAG retriever (direct, no MCP) ---------------------------------------
        self.retriever = AgileRetriever()

        # Models ----------------------------------------------------------------
        self.model_planner = "llama-3.1-8b-instant"
        self.model_answer = "llama-3.3-70b-versatile"

        # Market-query detector (off-topic for formation) -----------------------
        self._market_re = re.compile(
            r"prix|price|marché|marche|vente|vendre|achat|acheter|FCFA|CFA|tarif|cours",
            re.IGNORECASE,
        )

        # Default learner profile -----------------------------------------------

    # ------------------------------------------------------------------
    # LLM resolution
    # ------------------------------------------------------------------

    def _ensure_llm(self):
        if self.llm:
            return
        try:
            from agriconnect.core.get_llm import get_llm
            self.llm = get_llm()
            if self.llm:
                logger.info("LLM client initialised via get_llm()")
            else:
                logger.error("LLM client not available — set GROQ_API_KEY.")
        except Exception as exc:
            logger.exception("LLM resolution failed: %s", exc)
            self.llm = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _dedupe(warnings: List[str]) -> List[str]:
        seen: set = set()
        out: List[str] = []
        for w in warnings:
            if w not in seen:
                seen.add(w)
                out.append(w)
        return out

    # ------------------------------------------------------------------
    # Graph nodes
    # ------------------------------------------------------------------

    def analyze_node(self, state: FormationAgentState) -> FormationAgentState:
        query = state.get("user_query", "").strip()
        warnings: List[str] = list(state.get("warnings") or [])

        if not query:
            warnings.append("La question de formation est vide.")
            return {"status": "ERROR", "warnings": warnings}

        # Off-topic: market queries → reject immediately
        if self._market_re.search(query):
            warnings.append("Question marché détectée — hors-sujet pour FormationCoach")
            return {"status": "OFF_TOPIC", "warnings": warnings}

        profile = state.get("learner_profile") or self.default_profile
        analysis = self.tool._analyze_request(query, profile)

        intent = analysis.get("intent", "FORMATION")
        focus_topics = analysis.get("focus_topics", [])
        field_actions = analysis.get("field_actions", [])
        safety = analysis.get("safety_flags", [])
        urgency = analysis.get("urgency", "NORMAL")
        warnings = self._dedupe(warnings + list(analysis.get("warnings", [])))

        # Accumulate learned concepts
        existing = list(state.get("concepts_appris") or [])
        for c in focus_topics:
            if c not in existing:
                existing.append(c)

        return {
            "intent": intent,
            "focus_topics": focus_topics,
            "field_actions": field_actions,
            "safety_flags": safety,
            "urgency": urgency,
            "warnings": warnings,
            "status": "ANALYZED",
            "concepts_appris": existing,
        }

    def retrieve_node(self, state: FormationAgentState) -> FormationAgentState:
        """RAG retrieval using ``AgileRetriever.search`` directly."""
        warnings: List[str] = list(state.get("warnings") or [])
        query = state.get("user_query", "").strip()
        if not query:
            return {"status": "ERROR", "warnings": warnings}

        profile = state.get("learner_profile") or self.default_profile

        # Optimise the query via the formation tool planner
        if state.get("status") == "RETRY_SEARCH" and state.get("optimized_query"):
            optimized_query = state["optimized_query"]
        else:
            plan = self.tool._plan_retrieval(query, profile)
            optimized_query = plan.get("optimized_query") or query

        user_level = str(profile.get("niveau", "debutant")).lower()

        try:
            nodes = self.retriever.search(optimized_query, user_level=user_level)
        except Exception as exc:
            logger.error("RAG retriever error: %s", exc)
            warnings.append(f"Erreur RAG: {exc}")
            return {"status": "NO_CONTEXT", "warnings": self._dedupe(warnings)}

        if not nodes:
            warnings.append("Aucun document trouvé par le retriever.")
            return {
                "optimized_query": optimized_query,
                "retrieved_context": "",
                "sources": [],
                "status": "NO_CONTEXT",
                "warnings": self._dedupe(warnings),
            }

        # Build context text + sources list
        parts: List[str] = []
        sources: List[Dict[str, Any]] = []
        for n in nodes:
            meta = getattr(n.node, "metadata", {}) or {}
            filename = meta.get("filename") or meta.get("source") or "document"
            content = n.node.get_content() if hasattr(n.node, "get_content") else str(n.node)
            parts.append(f"SOURCE ({filename}): {content}")
            sources.append({"title": filename, "uri": meta.get("uri")})

        context_text = "\n\n".join(parts)

        # Merge with any pre-existing sources
        existing_sources = list(state.get("sources") or [])
        merged = existing_sources + [s for s in sources if s not in existing_sources]

        return {
            "optimized_query": optimized_query,
            "retrieved_context": context_text,
            "sources": merged,
            "status": "CONTEXT_FOUND",
            "warnings": self._dedupe(warnings),
        }

    def grade_sources_node(self, state: FormationAgentState) -> FormationAgentState:
        """Grade retrieved sources; mark NO_CONTEXT if too low."""
        warnings: List[str] = list(state.get("warnings") or [])
        sources = state.get("sources") or []

        if not sources:
            warnings.append("Aucun document récupéré pour vérification.")
            return {"status": "NO_CONTEXT", "warnings": self._dedupe(warnings), "document_grade": 0}

        # Simple heuristic grade (avoids extra LLM call during testing)
        scores = [(1 if s.get("uri") else 0.5) + (0.5 if s.get("title") else 0) for s in sources]
        avg = int(sum(scores) / len(scores) * 10)

        if avg < 3:
            warnings.append(f"Documents jugés peu fiables (score {avg}/10).")
            return {"status": "NO_CONTEXT", "warnings": self._dedupe(warnings), "document_grade": avg}

        return {"status": "CONTEXT_READY", "warnings": self._dedupe(warnings), "document_grade": avg}

    def compose_node(self, state: FormationAgentState) -> FormationAgentState:
        warnings: List[str] = list(state.get("warnings") or [])

        context = state.get("retrieved_context", "").strip()
        query = state.get("user_query", "").strip()
        profile = state.get("learner_profile") or self.default_profile
        profile_text = self.tool._format_profile(profile)

        if not query:
            return {"warnings": warnings, "status": "ERROR"}

        gen_ctx = GenerationContext(state=state, context=context, query=query, profile_text=profile_text, warnings=warnings)
        final_answer, warnings = self._generate_final_answer(gen_ctx)

        agri_response = self._build_agri_response(final_answer, query, state)

        return {
            "answer_draft": final_answer,
            "final_response": final_answer,
            "agri_response": agri_response,
            "warnings": warnings,
            "status": "ANSWER_GENERATED",
        }

    def _generate_final_answer(self, gen_ctx: GenerationContext):
        final_answer = "Réponse technique indisponible."
        state = gen_ctx.state
        context = gen_ctx.context
        query = gen_ctx.query
        profile_text = gen_ctx.profile_text
        warnings = gen_ctx.warnings

        try:
            profile = state.get("learner_profile") or {}
            level = str(profile.get("niveau", "standard")).lower()
            style_guidance = STYLE_GUIDANCE.get(level, STYLE_GUIDANCE["default"])

            degraded = bool(state.get("degraded_mode", False))
            base_system = FORMATION_SYSTEM_TEMPLATE.format(
                style_guidance=style_guidance,
                culture_context=f"Culture: {profile.get('culture_actuelle', 'N/A')}",
            )
            system_content = (
                "-- MODE DÉGRADÉ: sources peu fiables, reste prudent.\n" + base_system
                if degraded
                else base_system
            )

            field_actions_text = "\n".join(f"- {a}" for a in (state.get("field_actions") or []))
            user_content = FORMATION_USER_TEMPLATE.format(
                query=query,
                feedback_hallucination=state.get("feedback_hallucination", ""),
                intent=state.get("intent", "FORMATION"),
                urgency=state.get("urgency", "NORMAL"),
                profile_text=profile_text,
                context=context,
            )
            # Append field_actions if any (template may not have a placeholder)
            if field_actions_text:
                user_content += f"\n\nACTIONS TERRAIN :\n{field_actions_text}"

            completion = self.llm.chat.completions.create(
                model=self.model_answer,
                messages=[
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": user_content},
                ],
                temperature=0.35,
                max_tokens=900,
            )
            final_answer = completion.choices[0].message.content
        except Exception as e:
            logger.error("LLM Error: %s", e)
            final_answer = "Désolé, je rencontre une difficulté technique pour formuler le conseil."
            warnings.append(str(e))
        return final_answer, warnings

    @staticmethod
    def _build_agri_response(final_answer: str, query: str, state: FormationAgentState) -> Dict[str, Any]:
        title = "Conseil agricole"
        body_lines = [final_answer]
        actions = state.get("field_actions") or []
        if actions:
            body_lines.append("\nActions recommandées :")
            for a in actions:
                body_lines.append(f"  - {a}")
        body = "\n".join(body_lines)

        ql = query.lower()
        if "maladie" in ql or "ravageur" in ql:
            list_items = [
                {"id": "photo_upload", "label": "📷 Envoyer une photo"},
                {"id": "call_expert", "label": "📞 Parler à un agent"},
            ]
            picker_title = "Que voulez-vous faire ?"
        else:
            list_items = [
                {"id": "more_details", "label": "🔍 Plus de détails"},
                {"id": "related_topic", "label": "🌾 Sujet associé"},
            ]
            picker_title = "Aller plus loin :"

        resp = {
            "text": body,
            "agent": "formation",
            "cards": [{"title": title, "body": body, "picker": {"title": picker_title, "items": list_items}}],
            "actions": actions,
            "suggested": [],
        }
        return resp

    def critique_wrapper(self, state: FormationAgentState) -> FormationAgentState:
        """Wraps RefineTool.critique_node to guarantee critique_retry_count always increments."""
        result = self.refine.critique_node(state)
        count = int(state.get("critique_retry_count", 0))
        if "critique_retry_count" not in result:
            result["critique_retry_count"] = count + 1
        return result

    def evaluate_node(self, state: FormationAgentState) -> FormationAgentState:
        warnings: List[str] = list(state.get("warnings") or [])
        warnings.append("Évaluation automatique indisponible (mode test).")
        return {"warnings": warnings, "status": "EVALUATED"}

    # ------------------------------------------------------------------
    # Build LangGraph workflow
    # ------------------------------------------------------------------

    def build(self) -> Any:
        wf = StateGraph(FormationAgentState)

        wf.add_node("analyze", self.analyze_node)
        wf.add_node("retrieve", self.retrieve_node)
        wf.add_node("grade_sources", self.grade_sources_node)
        wf.add_node("rewrite", self.refine.rewrite_query_node)
        wf.add_node("compose", self.compose_node)
        wf.add_node("critique", self.critique_wrapper)
        wf.add_node("evaluate", self.evaluate_node)

        wf.set_entry_point("analyze")

        # analyze → routing
        wf.add_conditional_edges("analyze", self.refine.route_after_analyze)

        # retrieve → grade_sources → compose | END
        wf.add_edge("retrieve", "grade_sources")
        def _route_grade(s: FormationAgentState) -> str:
            if s.get("status") == "CONTEXT_READY":
                return "compose"
            # No (or poor) context → compose in degraded mode instead of stopping
            s["degraded_mode"] = True
            return "compose"

        wf.add_conditional_edges(
            "grade_sources",
            _route_grade,
            {"compose": "compose"},
        )

        # rewrite loop guard
        wf.add_conditional_edges(
            "rewrite",
            self.refine.route_after_rewrite,
            {"retrieve": "retrieve", "compose": "compose"},
        )

        wf.add_edge("compose", "critique")

        # critique → evaluate | compose (with degraded-mode guard)
        def _route_critique(state: FormationAgentState) -> str:
            crit = int(state.get("critique_retry_count", 0))
            rew = int(state.get("rewrited_retry_count", 0))
            status = state.get("status", "")

            # VALIDATED or hard cap → finish
            # DEGRADED_MODE thresholds: critique >= 2, rewrite > 0 (no rewrites allowed)
            if status == "VALIDATED" or crit >= 2 or rew > 0:
                if crit >= 2 or rew > 0:
                    logger.warning("DEGRADED_MODE: critique=%d rewrite=%d", crit, rew)
                return "evaluate"
            return "compose"

        wf.add_conditional_edges(
            "critique",
            _route_critique,
            {"evaluate": "evaluate", "compose": "compose"},
        )

        wf.add_edge("evaluate", END)
        return wf.compile()

    # ------------------------------------------------------------------
    # Public convenience API
    # ------------------------------------------------------------------

    def ask(self, question: str, learner_profile: Optional[Dict[str, Any]] = None) -> str:
        """Run the full formation workflow and return a plain-text answer."""
        if not question or not question.strip():
            return "Posez votre question de formation."

        state: FormationAgentState = {
            "user_query": question,
            "learner_profile": learner_profile or self.default_profile,
        }

        try:
            workflow = self.build()
            result = workflow.invoke(state)
            if isinstance(result, dict):
                if result.get("final_response"):
                    return result["final_response"]
                if result.get("answer_draft"):
                    return result["answer_draft"]
                # Return a readable dump when no text answer was generated
                return json.dumps(result, indent=2, ensure_ascii=False, default=str)
            return str(result)
        except Exception as exc:
            logger.exception("FormationAgro workflow error: %s", exc)
            return f"Erreur interne: {exc}"

    async def aask(self, question: str, learner_profile: Optional[Dict[str, Any]] = None) -> str:
        """Run the full formation workflow asynchronously and return a plain-text answer."""
        if not question or not question.strip():
            return "Posez votre question de formation."

        state: FormationAgentState = {
            "user_query": question,
            "learner_profile": learner_profile or self.default_profile,
        }

        try:
            workflow = self.build()
            result = await workflow.ainvoke(state)
            if isinstance(result, dict):
                if result.get("final_response"):
                    return result["final_response"]
                if result.get("answer_draft"):
                    return result["answer_draft"]
                return json.dumps(result, indent=2, ensure_ascii=False, default=str)
            return str(result)
        except Exception as exc:
            logger.exception("FormationAgro async workflow error: %s", exc)
            return f"Erreur interne: {exc}"

    # Keep legacy name for backward compat
    handle_question = ask
