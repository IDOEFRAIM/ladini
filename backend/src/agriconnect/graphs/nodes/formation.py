import json
import logging
import re
from typing import Any, Dict, List, Optional, TypedDict
from dataclasses import dataclass
from langgraph.graph import END, StateGraph

from agriconnect.graphs.prompts import FORMATION_SYSTEM_TEMPLATE, FORMATION_USER_TEMPLATE, STYLE_GUIDANCE
from agriconnect.protocols.mcp import MCPRagServer, MCPContextServer
from agriconnect.tools.formation import FormationTool
from agriconnect.tools.refine import RefineTool
from agriconnect.agents.base import BaseAgent

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
    requires_human: bool
    handoff_to: str
    clarification_needed: str


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
    retriever: Any = None
    evaluator: Any = None


class FormationCoach(BaseAgent):
    """
    Production FormationCoach (merged v2).
    MCP-first, returns simple dicts (AG-UI removed), and omits per-agent A2A.
    """
    _capabilities = ["LEARN", "HOW_TO", "BEST_PRACTICE", "TRAINING_MODULE"]

    def __init__(self, config: Optional[FormationConfig] = None, **overrides):
        cfg = config or FormationConfig()
        for k, v in overrides.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)

        self.tool = FormationTool(llm=cfg.llm_client)
        self.refine = RefineTool(llm=cfg.llm_client)

        self.mcp_rag = cfg.mcp_rag
        self.mcp_context = cfg.mcp_context

        self.model_planner = "llama-3.1-8b-instant"
        self.model_answer = "llama-3.3-70b-versatile"

        self._market_re = re.compile(
            r"prix|price|march\u00e9|marche|vente|vendre|achat|acheter|FCFA|CFA|tarif|cours",
            re.IGNORECASE,
        )

        self.llm = cfg.llm_client
        self._ensure_llm()

        try:
            self.tool.llm = self.llm
        except Exception:
            pass
        try:
            self.refine.llm = self.llm
        except Exception:
            pass

    def _ensure_llm(self):
        if self.llm:
            return
        try:
            from agriconnect.core.get_llm import get_llm

            self.llm = get_llm()
            if self.llm is None:
                logger.error("LLM client not available. Set GROQ_API_KEY or pass an explicit llm_client.")
            else:
                logger.info("LLM initialized via agriconnect.core.get_llm.get_llm()")
        except Exception as exc:
            logger.exception("Error resolving LLM client: %s", exc)
            self.llm = None

    # ---------------- Graph Nodes and Helpers -----------------

    def analyze_node(self, state: FormationAgentState) -> FormationAgentState:
        query = state.get("user_query", "").strip()
        if not query:
            warnings = list(state.get("warnings", []))
            warnings.append("La question de formation est vide.")
            return {"status": "ERROR", "warnings": warnings}

        if self._market_re.search(query):
            warnings = list(state.get("warnings", []))
            warnings.append("Question marché détectée — hors-sujet pour FormationCoach")
            return {"status": "OFF_TOPIC", "warnings": warnings}

        profile = self._get_profile_for_analyze(state)
        warnings = list(state.get("warnings", []))
        analysis = self.tool._analyze_request(query, profile)
        result = self._assemble_analyze_result(analysis, warnings)

        existing = list(state.get("concepts_appris", []))
        learned = result.get("focus_topics", []) or []
        for c in learned:
            if c not in existing:
                existing.append(c)
        result["concepts_appris"] = existing
        return result

    def _get_profile_for_analyze(self, state: FormationAgentState) -> Dict[str, Any]:
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
        intent = analysis.get("intent", "FORMATION")
        focus_topics = analysis.get("focus_topics", [])
        field_actions = analysis.get("field_actions", [])
        safety = analysis.get("safety_flags", [])
        urgency = analysis.get("urgency", "NORMAL")
        warnings = self._dedupe_warnings(warnings + list(analysis.get("warnings", [])))
        return {
            "intent": intent,
            "focus_topics": focus_topics,
            "field_actions": field_actions,
            "safety_flags": safety,
            "urgency": urgency,
            "warnings": warnings,
            "status": "ANALYZED",
            "concepts_appris": [],
        }

    def _dedupe_warnings(self, warnings: List[str]) -> List[str]:
        seen = set()
        out: List[str] = []
        for w in warnings:
            if w not in seen:
                seen.add(w)
                out.append(w)
        return out

    def grade_sources_node(self, state: FormationAgentState) -> FormationAgentState:
        warnings = list(state.get("warnings", []))
        sources = state.get("sources", []) or []
        if not sources:
            warnings.append("Aucun document récupéré pour vérification.")
            return {"status": "NO_CONTEXT", "warnings": self._dedupe_warnings(warnings)}

        try:
            if not self.llm:
                scores = [(1 if s.get("uri") else 0.5) + (0.5 if s.get("title") else 0) for s in sources]
                avg = int(sum(scores) / len(scores) * 10)
            else:
                prompt = f"Grade these sources for relevance to the question (0-10): {json.dumps(sources, ensure_ascii=False)}"
                resp = self.llm.chat.completions.create(
                    model=self.model_planner, messages=[{"role": "user", "content": prompt}], temperature=0, max_tokens=30
                )
                raw = resp.choices[0].message.content
                try:
                    avg = int(float(re.findall(r"\d+", raw)[0]))
                except Exception:
                    avg = 5
        except Exception as exc:
            logger.warning("Source grading failed: %s", exc)
            avg = 5

        if avg < 5:
            warnings.append(f"Documents jugés peu fiables (score {avg}/10).")
            return {"status": "NO_CONTEXT", "warnings": self._dedupe_warnings(warnings), "document_grade": avg}

        return {"status": "CONTEXT_READY", "warnings": self._dedupe_warnings(warnings), "document_grade": avg}

    def retrieve_node(self, state: FormationAgentState) -> FormationAgentState:
        warnings = list(state.get("warnings", []))
        query = state.get("user_query", "").strip()
        if not query:
            return {"status": "ERROR", "warnings": warnings}

        profile = self._get_profile_for_retrieval(state)
        optimized_query = self._get_optimized_query(state, query, profile)
        result = self._call_mcp_rag_and_parse(optimized_query, profile, warnings)

        existing_sources = list(state.get("sources", []))
        new_sources = result.get("sources", []) or []
        merged = existing_sources + [s for s in new_sources if s not in existing_sources]
        result["sources"] = merged
        merged_warnings = list(state.get("warnings", [])) + list(result.get("warnings", []))
        result["warnings"] = self._dedupe_warnings(merged_warnings)
        return result

    def _get_profile_for_retrieval(self, state: FormationAgentState) -> Dict[str, Any]:
        profile = state.get("learner_profile", {})
        if self.mcp_context:
            try:
                profile = self.mcp_context.read_user_context(state.get("user_profile", {}).get("id"))
            except Exception:
                pass
        return profile

    def _get_optimized_query(self, state: FormationAgentState, query: str, profile: Dict[str, Any]) -> str:
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
        context = state.get("retrieved_context", "").strip()
        query = state.get("user_query", "").strip()
        profile_text = self.tool._format_profile(state.get("learner_profile", {}))

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
            profile = state.get("learner_profile", {})
            level = str(profile.get("niveau", "standard")).lower()
            style_guidance = STYLE_GUIDANCE.get(level, STYLE_GUIDANCE["default"])

            degraded = bool(state.get("degraded_mode", False))
            base_system = FORMATION_SYSTEM_TEMPLATE.format(
                style_guidance=style_guidance,
                culture_context=f"Culture: {profile.get('culture_actuelle', 'N/A')}"
            )
            if degraded:
                system_content = (
                    "-- DEGRADE MODE: sources unreliable; answer cautiously and avoid unverified specifics.\n" + base_system
                )
            else:
                system_content = base_system

            field_actions_text = "\n".join([f"- {a}" for a in (state.get("field_actions") or [])])
            user_content = FORMATION_USER_TEMPLATE.format(
                query=query,
                feedback_hallucination=state.get("feedback_hallucination", ""),
                intent=state.get("intent", "FORMATION"),
                urgency=state.get("urgency", "NORMAL"),
                profile_text=profile_text,
                context=context,
                field_actions=field_actions_text,
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

    def _build_agri_response(self, final_answer: str, query: str, state: FormationAgentState) -> Dict[str, Any]:
        title = "Conseil agricole"
        body_lines = [final_answer]
        actions = state.get("field_actions") or []
        if actions:
            body_lines.append("\nActions recommandées :")
            for a in actions:
                body_lines.append(f"- {a}")
        body = "\n".join(body_lines)

        ql = query.lower()
        if "maladie" in ql or "ravageur" in ql:
            list_items = [{"id": "photo_upload", "label": "📷 Envoyer une photo"}, {"id": "call_expert", "label": "📞 Parler à un agent"}]
        else:
            list_items = [{"id": "more_details", "label": "🔍 Plus de détails"}, {"id": "related_topic", "label": "🌾 Sujet associé"}]

        resp = {"text": body, "agent": "formation", "actions": [], "cards": [{"title": title, "body": body}], "suggested": list_items}
        return resp

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
            scores = self.evaluator.evaluate_all(query=query, context=context, answer=answer)
            return {"evaluation": scores, "warnings": warnings, "status": "EVALUATED"}
        except Exception as exc:
            warnings.append(f"Évaluation échouée : {exc}")
            return {"warnings": warnings}

    def build(self):
        workflow = StateGraph(FormationAgentState)
        workflow.add_node("analyze", self.analyze_node)
        workflow.add_node("retrieve", self.retrieve_node)
        workflow.add_node("grade_sources", self.grade_sources_node)
        workflow.add_node("rewrite", self.refine.rewrite_query_node)
        workflow.add_node("compose", self.compose_node)
        workflow.add_node("critique", self.refine.critique_node)
        workflow.add_node("evaluate", self.evaluate_node)

        workflow.set_entry_point("analyze")
        workflow.add_conditional_edges("analyze", self.refine.route_after_analyze)

        workflow.add_edge("retrieve", "grade_sources")
        workflow.add_conditional_edges(
            "grade_sources",
            lambda s: "compose" if s.get("status") == "CONTEXT_READY" else END,
            {"compose": "compose", END: END},
        )

        workflow.add_conditional_edges(
            "rewrite",
            self.refine.route_after_rewrite,
            {"retrieve": "retrieve", "compose": "compose"},
        )

        workflow.add_edge("compose", "critique")

        def _route_critique(state: FormationAgentState) -> str:
            critique_retries = int(state.get("critique_retry_count", 0))
            rewrited_retries = int(state.get("rewrited_retry_count", 0))
            if critique_retries > 2 or rewrited_retries > 2:
                logger.error(
                    "FormationCoach DEGRADED_MODE: critique_retries=%d rewrited_retries=%d",
                    critique_retries,
                    rewrited_retries,
                )
                state["degraded_mode"] = True
                state["status"] = "DEGRADED_MODE"
                return "evaluate"
            return "evaluate" if state.get("status") == "VALIDATED" else "compose"

        workflow.add_conditional_edges("critique", _route_critique, {"evaluate": "evaluate", "compose": "compose"})
        workflow.add_edge("evaluate", END)
        return workflow.compile()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    agent = FormationCoach()
    workflow = agent.build()
    state = {
        "user_query": "Que sais tu sur la saison culture au burkina?",
        "learner_profile": {"niveau": "débutant", "région": "Boucle du Mouhoun"},
    }
    result = workflow.invoke(state)
    print(json.dumps(result, indent=2, ensure_ascii=False))
