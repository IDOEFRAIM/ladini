"""Formation sub-graph nodes as pure functions."""

import json
import logging
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from agriconnect.protocols.mcp import MCPContextServer, MCPRagServer
from agriconnect.services.persistence import AgriPersister
from agriconnect.graphs.agents.common.config import BaseAgentConfig
from agriconnect.graphs.agents.common.domains import AgentDomain
from agriconnect.tools.formation import FormationTool
from agriconnect.tools.refine import RefineTool

from .prompts import (
	FORMATION_SYSTEM_STRICT,
	FORMATION_SYSTEM_TEMPLATE,
	FORMATION_USER_TEMPLATE,
	STYLE_GUIDANCE,
)
from .state import FormationState

logger = logging.getLogger("Agent.FormationCoach")


def _map_suggested_domain(raw_domain: Optional[str]) -> Optional[str]:
	"""Map free-form planner domain suggestions to AgentDomain values."""
	if not raw_domain:
		return None

	normalized = str(raw_domain).strip().lower()
	normalized = "".join(
		ch
		for ch in unicodedata.normalize("NFKD", normalized)
		if not unicodedata.combining(ch)
	)
	if normalized in {
		"market",
		"marche",
		"market_expert",
		"agricultural_market",
		"prix",
		"vente",
		"commerce",
	}:
		return AgentDomain.MARKET.value
	if normalized in {"formation", "training", "agronomy", "agronomie"}:
		return AgentDomain.FORMATION.value
	if normalized in {"weather", "meteo", "climate", "sentinelle"}:
		return AgentDomain.SENTINELLE.value
	if normalized in {"marketplace"}:
		return AgentDomain.MARKETPLACE.value
	return None


@dataclass
class FormationConfig(BaseAgentConfig):
	pass


@dataclass
class FormationRuntime:
	tool: FormationTool
	refine: RefineTool
	mcp_rag: Optional[MCPRagServer]
	mcp_context: Optional[MCPContextServer]
	shield: Any
	persister: Optional[AgriPersister]
	model_planner: str = "llama-3.1-8b-instant"
	model_answer: str = "llama-3.3-70b-versatile"


def build_runtime(config: Optional[FormationConfig] = None, **overrides: Any) -> FormationRuntime:
	"""Create (and cache) a FormationRuntime for a given FormationConfig."""
	cfg = config or FormationConfig()
	for key, value in overrides.items():
		if hasattr(cfg, key):
			setattr(cfg, key, value)

	if cfg._runtime is not None:
		return cfg._runtime

	if cfg.shield is None:
		raise ValueError("Formation workflow requires a configured 'shield'")

	if cfg.ctx and hasattr(cfg.ctx, "db"):
		persister = AgriPersister(cfg.ctx.db, getattr(cfg.ctx, "memory", None))
	else:
		persister = None
		logger.warning("Formation workflow: running without persistence (Context missing)")

	runtime = FormationRuntime(
		tool=FormationTool(llm=cfg.llm_client),
		refine=RefineTool(llm=cfg.llm_client),
		mcp_rag=cfg.mcp_rag,
		mcp_context=cfg.mcp_context,
		shield=cfg.shield,
		persister=persister,
	)
	cfg._runtime = runtime
	return runtime


async def analyze_query_node(state: FormationState, agent_config: FormationConfig) -> Dict[str, Any]:
	"""Analyze user query and detect if another domain is required."""
	runtime = build_runtime(agent_config)
	query = state.get("user_query")

	try:
		prompt = (
			"Tu es l'Expert Agronome Senior (formation technique).\n"
			f"Requete: {query}\n\n"
			"Analyse la requete et reponds STRICTEMENT en JSON avec ces cles:\n"
			"- is_out_of_scope: boolean (true seulement si la demande concerne principalement prix/marche/vente/achat)\n"
			"- suggested_domain: string (ex: market, formation, sentinelle, marketplace)\n"
			"- intent: string\n"
			"- urgency: string\n"
			"- focus_topics: array[string]\n"
			"- safety_flags: array[string]\n"
			"- optimized_query: string"
		)
		resp = runtime.tool.llm.chat.completions.create(
			model=runtime.model_planner,
			messages=[{"role": "user", "content": prompt}],
			response_format={"type": "json_object"},
			temperature=0,
		)
		analysis = json.loads(resp.choices[0].message.content)
		# Sanitize optimized_query: planner must return a search-friendly reformulation
		optimized = analysis.get("optimized_query", query)
		if isinstance(optimized, str) and (
			"Tu es" in optimized or "Requete:" in optimized or "repond" in optimized.lower()
		):
			optimized = query

		if bool(analysis.get("is_out_of_scope")):
			required_domain = _map_suggested_domain(analysis.get("suggested_domain")) or AgentDomain.MARKET.value
			return {
				"required_domain": required_domain,
				"status": "HANDOFF",
				"handoff_reason": f"expertise_required:{analysis.get('suggested_domain', 'market')}",
				"intent": analysis.get("intent", "OUT_OF_SCOPE"),
				"optimized_query": optimized,
			}

		return {
			"intent": analysis.get("intent", "GENERAL_QUERY"),
			"urgency": analysis.get("urgency", "LOW"),
			"focus_topics": analysis.get("focus_topics", []),
			"safety_flags": analysis.get("safety_flags", []),
			"optimized_query": optimized,
		}
	except Exception as exc:
		logger.error("Analysis failed: %s", exc)
		return {"status": "ERROR", "final_response": "Erreur d'analyse."}


async def retrieve_node(state: FormationState, agent_config: FormationConfig) -> Dict[str, Any]:
	runtime = build_runtime(agent_config)
	if state.get("required_domain"):
		return {}

	query = state.get("optimized_query") or state.get("user_query")
	try:
		if runtime.mcp_rag:
			docs = await runtime.mcp_rag.search(query, limit=3)
			context_str = "\n\n".join([doc.page_content for doc in docs])
			sources = [{"title": doc.metadata.get("source"), "page": doc.metadata.get("page")} for doc in docs]
		else:
			context_str = "Connaissances générales agricoles (RAG non connecté)."
			sources = []
		return {"retrieved_context": context_str, "sources": sources}
	except Exception as exc:
		logger.error("Retrieval failed: %s", exc)
		warnings = list(state.get("warnings") or [])
		warnings.append("RAG_UNAVAILABLE")
		return {"retrieved_context": "", "warnings": warnings}


async def generate_answer_node(state: FormationState, agent_config: FormationConfig) -> Dict[str, Any]:
	runtime = build_runtime(agent_config)
	if state.get("required_domain"):
		return {}

	context = state.get("retrieved_context", "")
	profile = state.get("learner_profile", {})
	query = state.get("user_query")

	sys_prompt = FORMATION_SYSTEM_STRICT.format(
		context=context,
		profile=json.dumps(profile, ensure_ascii=False),
	)

	try:
		resp = runtime.tool.llm.chat.completions.create(
			model=runtime.model_answer,
			messages=[
				{"role": "system", "content": sys_prompt},
				{"role": "user", "content": query},
			],
			temperature=0.3,
		)
		draft = resp.choices[0].message.content

		if "pesticide" in draft.lower() and "protection" not in draft.lower():
			draft += (
				"\n\n⚠️ ATTENTION: Utilisez toujours des équipements de protection "
				"(gants, masque) lors de la manipulation de produits phytosanitaires."
			)

		return {"answer_draft": draft}
	except Exception as exc:
		logger.error("Generation failed: %s", exc)
		return {"status": "ERROR", "final_response": "Erreur de génération."}


async def finalize_node(state: FormationState, agent_config: FormationConfig) -> Dict[str, Any]:
	runtime = build_runtime(agent_config)
	if state.get("required_domain"):
		return {
			"status": "HANDOFF",
			"required_domain": state.get("required_domain"),
			"handoff_reason": state.get("handoff_reason", "domain_handoff_required"),
		}

	draft = state.get("answer_draft", "Désolé, je ne peux pas répondre pour le moment.")
	return {
		"final_response": draft,
		"status": "SUCCESS",
		"agri_response": {"response": draft, "sources": state.get("sources")},
		"expert_responses": [
			{"expert": "formation", "response": draft, "is_lead": True, "has_alerts": False}
		],
	}


async def audit_node(state: FormationState, agent_config: FormationConfig) -> Dict[str, Any]:
	runtime = build_runtime(agent_config)
	if runtime.persister is None:
		return {}
	if state.get("intent") == "GENERAL_QUERY" or state.get("status") != "SUCCESS":
		return {}

	try:
		user_id = state.get("learner_profile", {}).get("user_id", "anonymous")
		runtime.persister.db.log_audit_action(
			agent_name="FormationCoach",
			action_type="ADVICE_GIVEN",
			user_id=user_id,
			protocol="RAG_ADVISORY",
			payload={
				"query": state.get("user_query"),
				"topics": state.get("focus_topics"),
				"sources": state.get("sources"),
			},
			resource="vector_store",
			confidence=0.9,
		)
	except Exception as exc:
		logger.warning("Audit write failed: %s", exc)

	return {}


__all__ = [
	"FormationConfig",
	"FormationRuntime",
	"build_runtime",
	"analyze_query_node",
	"retrieve_node",
	"generate_answer_node",
	"finalize_node",
	"audit_node",
	"FormationState",
	"FORMATION_SYSTEM_TEMPLATE",
	"FORMATION_SYSTEM_STRICT",
	"FORMATION_USER_TEMPLATE",
	"STYLE_GUIDANCE",
]
