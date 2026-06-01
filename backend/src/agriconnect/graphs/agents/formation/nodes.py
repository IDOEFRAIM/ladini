"""Formation sub-graph nodes (pure functions).

This module hosts the end-to-end Formation workflow as *maintainable* LangGraph
nodes (analyze → validate → consult_crop → compose → critique → evaluate).

Architectural principles:
- Every node is wrapped in try/except for crash-proof execution.
- Postel’s Law: accept any input, sanitize before passing to tools.
- AG-UI protocol: generate rich UI components when user input is needed.
- BurkinaCropTool + FormationAdvisor = agronomic truth engine.
"""

from __future__ import annotations

import json
import inspect
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from agriconnect.graphs.agents.common.config import BaseAgentConfig
if TYPE_CHECKING:
	from agriconnect.rag.retriever import AgileRetriever
	from agriconnect.tools.formation import FormationTool
	from agriconnect.tools.formation_advisor import FormationAdvisor
	from agriconnect.tools.refine import RefineTool

from .prompts import (
	FORMATION_SYSTEM_TEMPLATE,
	FORMATION_USER_TEMPLATE,
	STYLE_GUIDANCE,
)
from .state import FormationState

logger = logging.getLogger("Agent.FormationCoach")

# ---------------------------------------------------------------------------
# AG-UI Component Builders
# ---------------------------------------------------------------------------

_KNOWN_CROPS = ["MAIS", "NIEBE", "SORGHO", "RIZ", "MIL", "ARACHIDE", "COTON", "SESAME", "CAFE_ROBUSTA"]
_KNOWN_ZONES = ["Sahel", "Nord", "Centre-Nord", "Centre", "Centre-Ouest", "Centre-Sud", "Est", "Hauts-Bassins", "Boucle du Mouhoun", "Sud-Ouest", "Cascades"]


def _build_crop_picker_component(missing_fields: List[str]) -> Dict[str, Any]:
    """Build an AG-UI ListMenu or FormInput for missing agronomic info."""
    if "identified_crop" in missing_fields:
        return {
            "type": "ListMenu",
            "props": {
                "title": "Quelle culture concerne votre question ?",
                "items": [{"id": c.lower(), "label": c.capitalize()} for c in _KNOWN_CROPS],
                "target_field": "identified_crop",
            },
        }
    if "zone_category" in missing_fields:
        return {
            "type": "ListMenu",
            "props": {
                "title": "Dans quelle zone êtes-vous ?",
                "items": [{"id": z.lower().replace(" ", "_"), "label": z} for z in _KNOWN_ZONES],
                "target_field": "zone_category",
            },
        }
    # Generic form for other fields
    fields = []
    for f in missing_fields[:3]:
        label_map = {
            "area_ha": "Superficie (hectares)",
            "crop_name": "Nom de la culture",
            "cycle_id": "Identifiant du cycle",
        }
        fields.append({"name": f, "label": label_map.get(f, f), "type": "text", "required": True})
    return {
        "type": "FormInputComponent",
        "props": {
            "title": "Précisez votre situation pour un conseil personnalisé",
            "fields": fields,
        },
    }


def _safe_str(value: Any, default: str = "") -> str:
    """Postel's Law: coerce to string or return default."""
    if value is None:
        return default
    return str(value).strip() or default


def _safe_float(value: Any, default: float = 1.0) -> float:
    """Postel's Law: coerce to float or return default."""
    if value is None:
        return default
    try:
        f = float(str(value).replace(",", ".").strip())
        return f if f > 0 else default
    except (ValueError, TypeError):
        return default


def _format_profile(profile: Dict[str, Any]) -> str:
	"""Format learner profile as a short plain-text context for prompts."""
	if not profile:
		return "Profil apprenant: non renseigné."
	parts: List[str] = []
	for key in ("niveau", "culture_actuelle", "zone", "superficie"):
		if key in profile and profile.get(key) is not None:
			parts.append(f"{key}={profile.get(key)}")
	return "Profil apprenant: " + ", ".join(parts) if parts else "Profil apprenant: non renseigné."


def _load_local_agronomic_refs() -> Dict[str, Any]:
	"""Load the local agronomic_refs.json as fallback knowledge."""
	try:
		refs_path = Path(__file__).parent / "agronomic_refs.json"
		if refs_path.exists():
			return json.loads(refs_path.read_text(encoding="utf-8"))
	except Exception:
		pass
	return {"CULTURES": {}}


def _dedupe(items: List[str]) -> List[str]:
	seen: set[str] = set()
	out: List[str] = []
	for item in items:
		if item in seen:
			continue
		seen.add(item)
		out.append(item)
	return out


def _ensure_llm(llm_client: Any = None) -> Any:
	"""Resolve an LLM client.

	We keep this resilient for local dev: if no key is configured, we return None
	and downstream nodes fall back to deterministic outputs.
	"""
	if llm_client is not None:
		return llm_client
	try:
		from agriconnect.core.get_llm import get_llm

		llm = get_llm()
		if llm:
			logger.info("LLM client initialised via get_llm()")
		else:
			logger.error("LLM client not available — set GROQ_API_KEY.")
		return llm
	except Exception as exc:
		logger.exception("LLM resolution failed: %s", exc)
		return None


def _build_agri_response(final_answer: str, query: str, state: FormationState) -> Dict[str, Any]:
	"""Build a MCP-friendly structured payload (cards/actions)."""
	title = "Conseil agricole"
	body_lines = [final_answer]
	actions = state.get("field_actions") or []
	if actions:
		body_lines.append("\nActions recommandées :")
		for action in actions:
			body_lines.append(f"  - {action}")
	body = "\n".join(body_lines)

	ql = (query or "").lower()
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

	return {
		"text": body,
		"agent": "formation",
		"cards": [{"title": title, "body": body, "picker": {"title": picker_title, "items": list_items}}],
		"actions": actions,
		"suggested": [],
	}


@dataclass
class FormationConfig(BaseAgentConfig):
	"""Runtime DI container for the Formation graph."""

	# Optional injection points
	llm_client: Any = None
	default_profile: Optional[Dict[str, Any]] = None

	# Optional overrides
	model_planner: str = "llama-3.1-8b-instant"
	model_answer: str = "llama-3.3-70b-versatile"


@dataclass
class FormationRuntime:
	llm: Any
	tool: "FormationTool"
	refine: "RefineTool"
	advisory: Optional["FormationAdvisor"]
	retriever: Optional["AgileRetriever"]
	default_profile: Dict[str, Any]
	model_planner: str
	model_answer: str

	def ensure_advisory(self) -> "FormationAdvisor":
		if self.advisory is None:
			from agriconnect.tools.formation_advisor import FormationAdvisor

			self.advisory = FormationAdvisor()
		return self.advisory

	def ensure_retriever(self) -> "AgileRetriever":
		if self.retriever is None:
			from agriconnect.rag.retriever import AgileRetriever

			self.retriever = AgileRetriever()
		return self.retriever


def _create_runtime(cfg: FormationConfig) -> FormationRuntime:
	llm = _ensure_llm(cfg.llm_client)
	from agriconnect.tools.formation import FormationTool
	from agriconnect.tools.refine import RefineTool

	# FormationTool signature is intentionally small (llm + model_planner)
	tool = FormationTool(llm=llm, model_planner=cfg.model_planner)
	refine = RefineTool(llm=llm)
	# Push resolved llm back into tools (they may have been None at init)
	try:
		tool.llm = llm
	except Exception:
		pass
	try:
		refine.llm = llm
	except Exception:
		pass

	default_profile = cfg.default_profile or {"niveau": "debutant", "culture_actuelle": "Maïs", "zone": "Centre", "superficie": 1.0}
	return FormationRuntime(
		llm=llm,
		tool=tool,
		refine=refine,
		advisory=None,
		retriever=None,
		default_profile=default_profile,
		model_planner=cfg.model_planner,
		model_answer=cfg.model_answer,
	)


def initialize_runtime(config: Optional[FormationConfig] = None, **overrides: Any) -> FormationRuntime:
	"""Initialize runtime once and cache it on the config instance."""
	cfg = config or FormationConfig()
	for key, value in overrides.items():
		if hasattr(cfg, key):
			setattr(cfg, key, value)
	if cfg._runtime is None:
		cfg._runtime = _create_runtime(cfg)
	return cfg._runtime


def _get_runtime(agent_config: FormationConfig) -> FormationRuntime:
	if agent_config._runtime is None:
		initialize_runtime(agent_config)
	return agent_config._runtime



async def analyze_node(state: FormationState, agent_config: FormationConfig) -> Dict[str, Any]:
	"""Node 1: Analyze user query — extract intent, crop type, urgency."""
	try:
		runtime = _get_runtime(agent_config)
		query = (state.get("user_query") or "").strip()
		warnings: List[str] = list(state.get("warnings") or [])

		if not query:
			warnings.append("La question de formation est vide.")
			return {"status": "ERROR", "warnings": _dedupe(warnings), "ag_ui_component": None}

		# 1) Analyse (async) : intention + type de culture
		analysis: Dict[str, Any]
		try:
			analysis = await runtime.tool.analyze_request(query)
		except Exception as exc:
			logger.error("FormationTool.analyze_request failed: %s", exc)
			analysis = {"intent": "FORMATION", "identified_crop_type": "UNKNOWN"}

		# 2) Vérité JSON (fiche culture)
		crop_type = _safe_str(analysis.get("identified_crop_type"), "UNKNOWN")
		crop_specs: Dict[str, Any] = {}
		if crop_type and crop_type != "UNKNOWN":
			crop_specs = runtime.tool.get_crop_characteristics(crop_type) or {}
		else:
			warnings.append("Aucune culture spécifique identifiée dans la requête.")

		# 3) Matière pédagogique
		intent = _safe_str(analysis.get("intent"), "FORMATION")
		focus_topics = list(analysis.get("focus_topics") or [])
		field_actions = list(analysis.get("field_actions") or [])
		safety_flags = list(analysis.get("safety_flags") or [])
		urgency = _safe_str(analysis.get("urgency"), "NORMAL")

		warnings = _dedupe(warnings + list(analysis.get("warnings") or []))

		# 4) Historique d'apprentissage
		existing = list(state.get("concepts_appris") or [])
		for topic in focus_topics:
			if topic not in existing:
				existing.append(topic)

		return {
			"intent": intent,
			"identified_crop": crop_type,
			"crop_specs": crop_specs,
			"focus_topics": focus_topics,
			"field_actions": field_actions,
			"safety_flags": safety_flags,
			"urgency": urgency,
			"warnings": warnings,
			"status": "ANALYZED",
			"concepts_appris": existing,
		}
	except Exception as exc:
		logger.exception("analyze_node CRASH: %s", exc)
		return {
			"status": "ERROR",
			"warnings": [f"Erreur analyse: {exc}"],
			"ag_ui_component": None,
		}



async def validate_node(state: FormationState, agent_config: FormationConfig) -> Dict[str, Any]:
	"""Node 2: Validate that we have enough info for a precise agronomic answer.

	If critical info is missing (crop type, zone), route to ASK_CLARIFICATION
	and generate an AG-UI component to query the user.
	"""
	try:
		warnings: List[str] = list(state.get("warnings") or [])
		missing: List[str] = []

		# Check critical fields
		crop = _safe_str(state.get("identified_crop"))
		zone = _safe_str(state.get("zone_category") or (state.get("learner_profile") or {}).get("zone"))
		urgency = _safe_str(state.get("urgency"), "NORMAL")

		# Crop is essential for technical advice
		if not crop or crop == "UNKNOWN":
			missing.append("identified_crop")

		# Zone is needed for calibrated recommendations (fertilization, variety)
		if not zone:
			missing.append("zone_category")

		# If urgency is CRITIQUE, skip clarification and proceed (best-effort)
		if missing and urgency != "CRITIQUE":
			logger.info("validate_node: missing fields %s — asking user", missing)
			component = _build_crop_picker_component(missing)
			return {
				"status": "NEEDS_CLARIFICATION",
				"response_strategy": "ASK_CLARIFICATION",
				"missing_info": missing,
				"ag_ui_component": component,
				"final_response": component["props"]["title"],
				"warnings": _dedupe(warnings),
			}

		# Set defaults for missing fields using Postel's Law
		updates: Dict[str, Any] = {
			"status": "VALIDATED",
			"response_strategy": "PROVIDE_ANSWER",
			"missing_info": [],
			"warnings": _dedupe(warnings),
		}
		if not zone:
			updates["zone_category"] = "Centre"  # Default zone
			warnings.append("Zone non spécifiée — utilisation de 'Centre' par défaut.")
			updates["warnings"] = _dedupe(warnings)
		else:
			updates["zone_category"] = zone

		return updates
	except Exception as exc:
		logger.exception("validate_node CRASH: %s", exc)
		return {
			"status": "VALIDATED",
			"response_strategy": "PROVIDE_ANSWER",
			"missing_info": [],
			"warnings": [f"Erreur validation: {exc}"],
		}


async def consult_crop_node(state: FormationState, agent_config: FormationConfig) -> Dict[str, Any]:
	"""Node 3: Consult BurkinaCropTool + FormationAdvisor for technical data.

	Applies Postel's Law: sanitizes all parameters before calling MCP/DB tools.
	On failure, falls back to local agronomic_refs.json.
	"""
	try:
		runtime = _get_runtime(agent_config)
		warnings: List[str] = list(state.get("warnings") or [])

		# Sanitize inputs (Postel's Law)
		crop_type = _safe_str(state.get("identified_crop"), "Maïs")
		zone = _safe_str(state.get("zone_category"), "Centre")
		profile = state.get("learner_profile") or runtime.default_profile
		area_ha = _safe_float(profile.get("superficie") or state.get("area_ha"), 1.0)

		logger.info(
			"consult_crop_node: crop=%s zone=%s area_ha=%s",
			crop_type, zone, area_ha,
		)

		# --- Primary: FormationAdvisor (DB-backed INERA profiles) ---
		technical_canvas: Dict[str, Any] = {}
		canvas_md = ""
		advisor_error = False

		try:
			advisory = runtime.ensure_advisory()
			canvas = await advisory.generate_technical_diagnosis(
				crop=crop_type,
				zone=zone,
				area_ha=area_ha,
			)
			if canvas.get("error"):
				logger.warning("Advisor returned error: %s", canvas.get("message"))
				advisor_error = True
				warnings.append(f"Advisor: {canvas.get('message', 'indisponible')}")
			else:
				technical_canvas = canvas
				canvas_md = advisory.format_as_markdown(canvas)
		except Exception as exc:
			logger.error("consult_crop_node advisor FAIL: %s", exc)
			advisor_error = True
			warnings.append(f"Moteur INERA indisponible: {exc}")

		# --- Fallback: Local JSON refs (case/diacritics-insensitive) ---
		crop_specs = state.get("crop_specs") or {}
		if not crop_specs and advisor_error:
			import unicodedata

			def _norm(s: str) -> str:
				s = s.strip()
				s = "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))
				return s.casefold()

			refs = _load_local_agronomic_refs()
			cultures = refs.get("CULTURES", {})
			# Direct lookup first, then fuzzy
			crop_specs = cultures.get(crop_type.upper(), {})
			if not crop_specs:
				target = _norm(crop_type)
				for key, val in cultures.items():
					if isinstance(key, str) and _norm(key) == target:
						crop_specs = val or {}
						break
			if crop_specs:
				logger.info("Using local JSON fallback for crop=%s", crop_type)
			else:
				warnings.append("Aucune fiche locale trouvée non plus.")

		retrieval_mode = "MCP_FULL" if technical_canvas else ("JSON_ONLY" if crop_specs else "DEGRADED")

		return {
			"technical_canvas": technical_canvas,
			"canvas_markdown": canvas_md,
			"crop_specs": crop_specs,
			"advisor_error": advisor_error,
			"retrieval_mode": retrieval_mode,
			"degraded_mode": advisor_error and not crop_specs,
			"status": "CONSULTED",
			"warnings": _dedupe(warnings),
		}
	except Exception as exc:
		logger.exception("consult_crop_node CRASH: %s", exc)
		return {
			"technical_canvas": {},
			"canvas_markdown": "",
			"advisor_error": True,
			"retrieval_mode": "DEGRADED",
			"degraded_mode": True,
			"status": "CONSULTED",
			"warnings": [f"Erreur consultation agronomique: {exc}"],
		}


def retrieve_node(state: FormationState, agent_config: FormationConfig) -> Dict[str, Any]:
	runtime = _get_runtime(agent_config)
	warnings: List[str] = list(state.get("warnings") or [])
	query = (state.get("user_query") or "").strip()
	if not query:
		return {"status": "ERROR", "warnings": _dedupe(warnings)}

	profile = state.get("learner_profile") or runtime.default_profile

	# No planner dependency here: keep retrieval deterministic
	optimized_query = str(state.get("optimized_query") or query)

	user_level = str(profile.get("niveau", "debutant")).lower()
	try:
		retriever = runtime.ensure_retriever()
		nodes = retriever.search(optimized_query, user_level=user_level)
	except Exception as exc:
		logger.error("RAG retriever error: %s", exc)
		warnings.append(f"Erreur RAG: {exc}")
		return {"status": "NO_CONTEXT", "warnings": _dedupe(warnings)}

	if not nodes:
		warnings.append("Aucun document trouvé par le retriever.")
		return {
			"optimized_query": optimized_query,
			"retrieved_context": "",
			"sources": [],
			"status": "NO_CONTEXT",
			"warnings": _dedupe(warnings),
		}

	parts: List[str] = []
	sources: List[Dict[str, Any]] = []
	for n in nodes:
		meta = getattr(n.node, "metadata", {}) or {}
		filename = meta.get("filename") or meta.get("source") or "document"
		content = n.node.get_content() if hasattr(n.node, "get_content") else str(n.node)
		parts.append(f"SOURCE ({filename}): {content}")
		sources.append({"title": filename, "uri": meta.get("uri")})

	context_text = "\n\n".join(parts)
	existing_sources = list(state.get("sources") or [])
	merged = existing_sources + [s for s in sources if s not in existing_sources]

	return {
		"optimized_query": optimized_query,
		"retrieved_context": context_text,
		"sources": merged,
		"status": "CONTEXT_FOUND",
		"warnings": _dedupe(warnings),
	}


def grade_sources_node(state: FormationState, agent_config: FormationConfig) -> Dict[str, Any]:
	_get_runtime(agent_config)
	warnings: List[str] = list(state.get("warnings") or [])
	sources = state.get("sources") or []

	if not sources:
		warnings.append("Aucun document récupéré pour vérification.")
		return {"status": "NO_CONTEXT", "warnings": _dedupe(warnings), "document_grade": 0}

	# Simple heuristic grade (avoids extra LLM call during testing)
	scores = [(1 if s.get("uri") else 0.5) + (0.5 if s.get("title") else 0) for s in sources]
	avg = int(sum(scores) / len(scores) * 10)

	if avg < 3:
		warnings.append(f"Documents jugés peu fiables (score {avg}/10).")
		return {"status": "NO_CONTEXT", "warnings": _dedupe(warnings), "document_grade": avg}

	return {"status": "CONTEXT_READY", "warnings": _dedupe(warnings), "document_grade": avg}


def rewrite_node(state: FormationState, agent_config: FormationConfig) -> Dict[str, Any]:
	runtime = _get_runtime(agent_config)
	return runtime.refine.rewrite_query_node(state)



async def _generate_final_answer(
	runtime: FormationRuntime,
	gen_ctx: Tuple[FormationState, str, str, str, List[str]],
) -> Tuple[str, List[str]]:
	"""Generate final answer.

	LLM calls are async; when no LLM is configured we return a deterministic
	fallback containing the merged context.
	"""

	state, context, query, profile_text, warnings = gen_ctx
	final_answer = "Réponse technique indisponible."

	try:
		profile = state.get("learner_profile") or {}
		level = str(profile.get("niveau", "standard")).lower()
		style_guidance = STYLE_GUIDANCE.get(level, STYLE_GUIDANCE["default"])

		crop_name = state.get("identified_crop") or "la culture"
		system_content = (
			f"{FORMATION_SYSTEM_TEMPLATE.format(style_guidance=style_guidance, culture_context=crop_name)}\n"
			"CONSIGNE CRITIQUE : Utilise prioritairement les caractéristiques techniques fournies dans le contexte (issues de la base de référence)."
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
		if field_actions_text:
			user_content += f"\n\nACTIONS TERRAIN RECOMMANDÉES :\n{field_actions_text}"

		if runtime.llm is None:
			final_answer = (
				"Je peux vous aider, mais le moteur LLM n'est pas configuré. "
				"Voici le contexte technique disponible :\n\n" + context
			)
		else:
			maybe_completion = runtime.llm.chat.completions.create(
				model=runtime.model_answer,
				messages=[
					{"role": "system", "content": system_content},
					{"role": "user", "content": user_content},
				],
				temperature=0.1,
				max_tokens=1000,
			)
			completion = await maybe_completion if inspect.isawaitable(maybe_completion) else maybe_completion
			final_answer = completion.choices[0].message.content
	except Exception as exc:
		logger.error("LLM Error: %s", exc)
		warnings.append(f"Erreur de génération : {str(exc)}")
		final_answer = (
			"Désolé, je ne peux pas rédiger la réponse. Voici les données brutes :\n\n" + context
		)

	return final_answer, warnings


async def compose_node(state: FormationState, agent_config: FormationConfig) -> Dict[str, Any]:
	"""Node 5: Compose the final pedagogical answer using all available context."""
	try:
		runtime = _get_runtime(agent_config)
		warnings: List[str] = list(state.get("warnings") or [])
		query = (state.get("user_query") or "").strip()
		profile = state.get("learner_profile") or runtime.default_profile
		profile_text = _format_profile(profile)

		# 1. Contexte technique (déjà récupéré par consult_crop_node)
		crop_type = _safe_str(state.get("identified_crop"), "la culture")
		crop_specs = state.get("crop_specs") or {}
		canvas_md = state.get("canvas_markdown") or ""

		# Build context hierarchy: 1. Canvas (INERA) | 2. JSON specs | 3. Degraded
		json_context = f"=== RÉFÉRENCES AGRONOMIQUES ({crop_type}) ===\n"
		if crop_specs:
			json_context += json.dumps(crop_specs, indent=2, ensure_ascii=False)
		else:
			json_context += "Aucune fiche technique spécifique trouvée dans la base."

		augmented_context = (
			f"{json_context}\n\n"
			"=== ANALYSE TECHNIQUE COMPLÉMENTAIRE (INERA) ===\n"
			f"{canvas_md if canvas_md else 'Non disponible.'}"
		)

		# 2. GÉNÉRATION FINALE
		final_answer, warnings = await _generate_final_answer(
			runtime,
			(state, augmented_context, query, profile_text, warnings),
		)

		return {
			"answer_draft": final_answer,
			"final_response": final_answer,
			"agri_response": _build_agri_response(final_answer, query, state),
			"warnings": _dedupe(warnings),
			"status": "ANSWER_GENERATED",
			"ag_ui_component": None,
		}
	except Exception as exc:
		logger.exception("compose_node CRASH: %s", exc)
		fallback = (
			"Désolé, une erreur technique est survenue lors de la rédaction du conseil. "
			"Veuillez reformuler votre question ou réessayer."
		)
		return {
			"answer_draft": fallback,
			"final_response": fallback,
			"agri_response": {"text": fallback, "agent": "formation", "cards": [], "actions": [], "suggested": []},
			"warnings": [f"Erreur composition: {exc}"],
			"status": "ANSWER_GENERATED",
			"ag_ui_component": None,
		}


def critique_node(state: FormationState, agent_config: FormationConfig) -> Dict[str, Any]:
	runtime = _get_runtime(agent_config)
	result = runtime.refine.critique_node(state)
	count = int(state.get("critique_retry_count", 0))
	if "critique_retry_count" not in result:
		result["critique_retry_count"] = count + 1
	return result


def evaluate_node(state: FormationState, agent_config: FormationConfig) -> Dict[str, Any]:
	_get_runtime(agent_config)
	warnings: List[str] = list(state.get("warnings") or [])
	warnings.append("Évaluation automatique indisponible (mode test).")
	return {"warnings": _dedupe(warnings), "status": "EVALUATED"}


# ---------------------------------------------------------------------------
# Routing helpers (kept here to keep graph wiring declarative)
# ---------------------------------------------------------------------------


def route_after_analyze(state: FormationState) -> str:
	"""After analyze, always go to validation."""
	return "validate"


def route_after_grade(state: FormationState) -> str:
	if state.get("status") == "CONTEXT_READY":
		return "compose"
	# No/poor context → compose in degraded mode instead of stopping
	state["degraded_mode"] = True
	return "compose"


def route_after_retrieve(state: FormationState) -> str:
	"""Route after retrieval.

	- CONTEXT_FOUND: run grading
	- NO_CONTEXT: try rewrite loop
	- ERROR/MAX_RETRIES: compose anyway (deterministic fallback)
	"""
	status = str(state.get("status") or "")
	if status == "CONTEXT_FOUND":
		return "grade"
	if status in ("ERROR", "MAX_RETRIES"):
		return "compose"
	# NO_CONTEXT (or anything else) -> rewrite
	return "rewrite"


def route_after_rewrite(state: FormationState, agent_config: FormationConfig) -> str:
	runtime = _get_runtime(agent_config)
	return runtime.refine.route_after_rewrite(state)


def route_after_critique(state: FormationState) -> str:
	crit = int(state.get("critique_retry_count", 0))
	rew = int(state.get("rewrited_retry_count", 0))
	status = str(state.get("status") or "")
	# VALIDATED or hard cap → finish
	if status == "VALIDATED" or crit >= 2 or rew > 0:
		if crit >= 2 or rew > 0:
			logger.warning("DEGRADED_MODE: critique=%d rewrite=%d", crit, rew)
		return "evaluate"
	return "compose"


def route_after_validate(state: FormationState) -> str:
	"""After validate: if clarification needed, stop; otherwise consult crop."""
	strategy = state.get("response_strategy") or ""
	if strategy == "ASK_CLARIFICATION":
		return "__end__"
	return "consult_crop"


__all__ = [
	"FormationConfig",
	"FormationRuntime",
	"initialize_runtime",
	"analyze_node",
	"validate_node",
	"consult_crop_node",
	"retrieve_node",
	"grade_sources_node",
	"rewrite_node",
	"compose_node",
	"critique_node",
	"evaluate_node",
	"route_after_analyze",
	"route_after_validate",
	"route_after_retrieve",
	"route_after_grade",
	"route_after_rewrite",
	"route_after_critique",
	"FormationState",
]
