import json
import logging
import re
from typing import Any, Dict, List, Optional, TypedDict
from dataclasses import dataclass
from langgraph.graph import END, StateGraph
import inspect
import asyncio

from agriconnect.graphs.prompts import (
    FORMATION_SYSTEM_TEMPLATE,
    FORMATION_SYSTEM_STRICT,
    FORMATION_USER_TEMPLATE,
    STYLE_GUIDANCE,
)
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
    shield: Any = None
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
        # Shield is required for all MCP calls to enforce permissions/audit
        self.shield = cfg.shield
        if self.shield is None:
            raise ValueError("FormationCoach requires a configured 'shield' in FormationConfig")

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
            warnings.append(f"Documents jugés peu fiables (score {avg}/10). -- passage en mode dégradé.")
            # Instead of blocking, continue in degraded mode: allow composition
            # but mark the state so generation is cautious about specifics.
            return {
                "status": "CONTEXT_DEGRADED",
                "warnings": self._dedupe_warnings(warnings),
                "document_grade": avg,
                "degraded_mode": True,
            }

        return {"status": "CONTEXT_READY", "warnings": self._dedupe_warnings(warnings), "document_grade": avg}

    async def retrieve_node(self, state: FormationAgentState) -> FormationAgentState:
        warnings = list(state.get("warnings", []))
        query = state.get("user_query", "").strip()
        if not query:
            return {"status": "ERROR", "warnings": warnings}

        profile = self._get_profile_for_retrieval(state)
        optimized_query = self._get_optimized_query(state, query, profile)
        result = await self._call_mcp_rag_and_parse(optimized_query, profile, warnings)

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
                meta = s.get("metadata") if isinstance(s.get("metadata"), dict) else {}
                # Prefer explicit uri/source fields, fallback to metadata.source/file_path
                uri = s.get("uri") or s.get("source") or meta.get("source") or meta.get("file_path")

                title = s.get("title") or meta.get("title") or meta.get("filename")
                if not title or str(title).lower().startswith("document sans titre"):
                    # Derive a readable title from available path-like fields
                    candidate_path = uri or meta.get("file_path") or meta.get("source")
                    try:
                        import os as _os

                        if candidate_path:
                            title = _os.path.splitext(_os.path.basename(str(candidate_path)))[0]
                    except Exception:
                        title = title or "Document"

                norm_sources.append({"title": title, "uri": uri or "db_interne"})
            else:
                norm_sources.append({"title": str(s), "uri": None})
        return norm_sources

    async def _call_mcp_rag_and_parse(self, optimized_query: str, profile: Dict[str, Any], warnings: List[str]) -> FormationAgentState:
        if not getattr(self, "shield", None) and not getattr(self, "mcp_rag", None):
            warnings.append("MCP RAG Server manquant.")
            return {"status": "NO_CONTEXT", "warnings": warnings}

        try:
            search_level = profile.get("niveau", "debutant")
            # Determine top_k adaptively: prefer explicit override in profile,
            # else increase for queries mentioning large/complete calendars or similar.
            try:
                top_k = int(profile.get("search_top_k") or 0)
            except Exception:
                top_k = 0

            if not top_k:
                qlow = (optimized_query or "").lower()
                if any(k in qlow for k in ("calend", "complet", "entier", "plan", "programme")):
                    top_k = 8
                else:
                    # Default: balanced small set
                    top_k = 4

            args = {"query": optimized_query, "level": search_level, "top_k": top_k}

            resp = None

            # 1) Preferred path: Shield (ensures permission + audit)
            shield = getattr(self, "shield", None)
            if shield:
                try:
                    if hasattr(shield, "call_tool"):
                        maybe = shield.call_tool("search_agronomy_docs", args)
                    elif hasattr(shield, "call"):
                        maybe = shield.call("search_agronomy_docs", args)
                    else:
                        maybe = shield("search_agronomy_docs", args)
                    resp = await maybe if inspect.isawaitable(maybe) else maybe
                except Exception as e:
                    logger.debug("Shield call failed for search_agronomy_docs: %s", e)


            # In production the Shield is mandatory: do NOT fallback to direct MCP clients.
            if resp is None:
                raise RuntimeError("Shield did not return a response for search_agronomy_docs; direct MCP calls are disallowed in production")

            # Validate response shape early.
            # Accepted shapes:
            # 1) MCP envelope: {"status": "ok", "content": [{"text": "{...}"}]}
            # 2) Direct payload: {"query": ..., "documents": [...], "context_text": ...}
            if not resp:
                logger.warning("MCP RAG returned no results or error: %s", resp)
                warnings.append("Aucun document récupéré pour vérification.")
                return {"status": "NO_CONTEXT", "warnings": warnings}

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
            logger.exception("MCP RAG Error: %s", e)
            warnings.append(f"Erreur MCP RAG: {e}")
            return {"status": "NO_CONTEXT", "warnings": warnings}

    def _parse_mcp_response(self, resp: Dict[str, Any]) -> tuple[str, List[Dict[str, Any]]]:
        if not isinstance(resp, dict) or not resp:
            raise ValueError("MCP response invalid")

        def _assemble_context_from_docs(docs: List[Dict[str, Any]]) -> str:
            parts: List[str] = []
            for d in docs or []:
                # try common fields for excerpt/text
                text = d.get("text") or d.get("excerpt") or d.get("content") or d.get("body") or ""
                meta = d.get("metadata") if isinstance(d.get("metadata"), dict) else {}
                title = d.get("title") or meta.get("title") or meta.get("filename") or d.get("uri") or "Document"
                header = f"Source: {title}"
                if d.get("uri"):
                    header += f" ({d.get('uri')})"
                parts.append(header + "\n" + str(text))
            # explicit delimiter between documents so the LLM can see boundaries
            return "\n---\n".join(parts)

        # Shape A: direct RAG payload returned by manager/server wrappers.
        if "documents" in resp or "context_text" in resp:
            if "error" in resp and not resp.get("documents"):
                raise ValueError(f"MCP RAG error: {resp.get('error')}")
            documents = resp.get("documents") or []
            # prefer server-provided context_text but fall back to assembling from documents
            context_text = str(resp.get("context_text") or _assemble_context_from_docs(documents) or "")
            sources = documents
            return context_text, sources

        # Shape B: MCP envelope with content[0].text JSON payload.
        if resp.get("status") != "ok":
            raise ValueError("MCP response invalid or error status")

        content_list = resp.get("content") or []
        if not content_list:
            raise ValueError("MCP response empty content")

        raw_text = content_list[0].get("text", "")
        if isinstance(raw_text, dict):
            parsed = raw_text
        else:
            try:
                parsed = json.loads(raw_text)
            except Exception:
                parsed = {}

        documents = parsed.get("documents") or parsed.get("sources") or []
        context_text = str(parsed.get("context") or parsed.get("context_text") or _assemble_context_from_docs(documents) or "")
        sources = documents or []
        return context_text, sources

    async def compose_node(self, state: FormationAgentState) -> FormationAgentState:
        warnings = list(state.get("warnings", []))
        context = state.get("retrieved_context", "").strip()
        query = state.get("user_query", "").strip()
        profile_text = self.tool._format_profile(state.get("learner_profile", {}))

        if not query:
            return {"warnings": warnings, "status": "ERROR"}

        gen_ctx = GenerationContext(state=state, context=context, query=query, profile_text=profile_text, warnings=warnings)
        final_answer, warnings = await self._generate_final_answer(gen_ctx)
        agri_response = self._build_agri_response(final_answer, query, state)

        return {
            "answer_draft": final_answer,
            "final_response": final_answer,
            "agri_response": agri_response,
            "warnings": warnings,
            "status": "ANSWER_GENERATED",
        }

    async def _generate_final_answer(self, gen_ctx: GenerationContext):
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
            # Prefer the strict system prompt when available to enforce scope
            base_template = FORMATION_SYSTEM_STRICT if globals().get("FORMATION_SYSTEM_STRICT") else FORMATION_SYSTEM_TEMPLATE
            base_system = base_template.format(
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

            maybe = self.llm.chat.completions.create(
                model=self.model_answer,
                messages=[{"role": "system", "content": system_content}, {"role": "user", "content": user_content}],
                temperature=0.35,
                max_tokens=900,
            )
            completion = await maybe if inspect.isawaitable(maybe) else maybe
            final_answer = getattr(getattr(completion, "choices", [None])[0], "message", {}).content if completion and getattr(completion, "choices", None) else str(getattr(completion, "content", completion))
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

        # Append a concise "Sources consultées" section to increase transparency
        try:
            srcs = state.get("sources") or []
        except Exception:
            srcs = []

        if srcs:
            sources_lines = ["\n\n📚 Sources consultées :"]
            for s in srcs:
                try:
                    st = s.get("title") if isinstance(s, dict) else str(s)
                    uri = s.get("uri") if isinstance(s, dict) else None
                    if uri:
                        sources_lines.append(f"- {st} ({uri})")
                    else:
                        sources_lines.append(f"- {st}")
                except Exception:
                    sources_lines.append(f"- {s}")
            body = body + "\n" + "\n".join(sources_lines)

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
    # Try to resolve a real ShieldHub first for standalone execution.
    # This keeps Shield-first behavior while avoiding hard failure when
    # running this module directly from CLI.
    real_shield = None
    try:
        from agriconnect.protocols.mcp.security.shield_hub import ShieldHub

        real_shield = ShieldHub(session_id="formation_standalone")
        logger.info("Using real ShieldHub for standalone run.")
    except Exception as exc:
        logger.info("Real ShieldHub unavailable in standalone mode: %s", exc)

    # Allow a local mock shield for quick standalone testing by setting
    # the environment variable `AGRICONNECT_ALLOW_MOCK_SHIELD=1`.
    try:
        import os

        allow_mock = bool(os.environ.get("AGRICONNECT_ALLOW_MOCK_SHIELD"))
    except Exception:
        allow_mock = False

    mock_shield = None
    if allow_mock and real_shield is None:
        try:
            # Import the MCP helpers lazily to avoid import-time side effects
            from agriconnect.protocols.mcp import get_mcp_rag_server, get_mcp_context_server

            class _LocalMockShield:
                def call_tool(self, name: str, args: dict):
                    # For standalone testing we return a lightweight, deterministic
                    # successful response for the RAG search tool to avoid heavy
                    # model/index dependencies and event-loop juggling.
                    if name == "search_agronomy_docs":
                        sample_payload = {
                            "status": "ok",
                            "content": [
                                {"text": json.dumps({"context": "Extrait contextuel simulé.", "sources": [{"title": "Guide local", "uri": "local://guide1"}]})}
                            ],
                        }
                        return sample_payload

                    # Otherwise, attempt to route to a local MCP RAG server if available.
                    try:
                        rag = get_mcp_rag_server()
                        if hasattr(rag, "call_tool_sync"):
                            return rag.call_tool_sync(name, args)
                    except Exception:
                        pass

                    return {"ok": False, "error": "mock_shield call failed"}

                # compatibility
                def call(self, name: str, args: dict):
                    return self.call_tool(name, args)

            mock_shield = _LocalMockShield()
            logger.info("Using local mock shield for standalone run.")
        except Exception as exc:
            logger.warning("Could not initialize mock shield: %s", exc)

    try:
        if real_shield is not None:
            cfg = FormationConfig(shield=real_shield)
            agent = FormationCoach(cfg)
        elif mock_shield is not None:
            cfg = FormationConfig(shield=mock_shield)
            agent = FormationCoach(cfg)
        else:
            agent = FormationCoach()
    except ValueError as exc:
        logger.error("FormationCoach instantiation failed: %s", exc)
        print(
            "FormationCoach requires a configured 'shield' in FormationConfig.\n"
            "Instantiate FormationCoach within the application where a Shield is available,\n"
            "or set AGRICONNECT_ALLOW_MOCK_SHIELD=1 to run a local mock shield for testing.\n"
            "Tip: prefer 'python -m agriconnect.graphs.nodes.formation' with PYTHONPATH=backend/src.",
            flush=True,
        )
    else:
        workflow = agent.build()
        state = {
            "user_query": "Que sais tu sur la saison culture au burkina?",
            "learner_profile": {"niveau": "débutant", "région": "Boucle du Mouhoun"},
        }

        # For standalone/mock runs, avoid using the full StateGraph engine which
        # can trigger recursion/async loop complexities. Instead, run a simple
        # sequential pipeline: analyze -> retrieve -> compose.
        if mock_shield:
            try:
                import asyncio
                import inspect as _inspect

                analyzed = agent.analyze_node(state)

                if _inspect.iscoroutinefunction(agent.retrieve_node):
                    retrieved = asyncio.run(agent.retrieve_node(analyzed))
                else:
                    retrieved = agent.retrieve_node(analyzed)

                # Merge results into state for composition
                merged_state = dict(state)
                merged_state.update(analyzed or {})
                merged_state.update(retrieved or {})

                if _inspect.iscoroutinefunction(agent.compose_node):
                    composed = asyncio.run(agent.compose_node(merged_state))
                else:
                    composed = agent.compose_node(merged_state)

                final = dict(merged_state)
                final.update(composed or {})
                print(json.dumps(final, indent=2, ensure_ascii=False))
            except Exception as exc:
                logger.exception("Standalone run failed: %s", exc)
                # As a last resort, try the compiled workflow invocation
                try:
                    import asyncio

                    if hasattr(workflow, "ainvoke"):
                        result = asyncio.run(workflow.ainvoke(state))
                    else:
                        result = workflow.invoke(state)
                    print(json.dumps(result, indent=2, ensure_ascii=False))
                except Exception as exc2:
                    logger.exception("Fallback workflow invocation failed: %s", exc2)
        else:
            try:
                import asyncio

                if hasattr(workflow, "ainvoke"):
                    result = asyncio.run(workflow.ainvoke(state))
                else:
                    result = workflow.invoke(state)
            except Exception:
                # Fallback to synchronous invoke when async invocation fails
                result = workflow.invoke(state)
            print(json.dumps(result, indent=2, ensure_ascii=False))
