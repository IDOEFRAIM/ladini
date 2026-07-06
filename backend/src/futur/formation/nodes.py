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
import uuid as _uuid_mod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from agriconnect.graphs.agents.common.config import BaseAgentConfig
if TYPE_CHECKING:
	from futur.tools.formation import FormationTool
	from futur.tools.formation_advisor import FormationAdvisor
	from futur.tools.refine import RefineTool

from .prompts import (
	FORMATION_SYSTEM_TEMPLATE,
	FORMATION_USER_TEMPLATE,
	STYLE_GUIDANCE,
)
from .dispatcher import execute_formation_action
from agriconnect.agents import (
    DataGateway,
    extract_phone_from_state,
    extract_user_id_from_state,
    resolve_identity,
)
from .state import FormationState

logger = logging.getLogger("Agent.FormationCoach")

# ---------------------------------------------------------------------------
# AG-UI Component Builders
# ---------------------------------------------------------------------------

_KNOWN_CROPS = ["MAIS", "NIEBE", "SORGHO", "RIZ", "MIL", "ARACHIDE", "COTON", "SESAME", "CAFE_ROBUSTA", "TOMATE"]
_KNOWN_ZONES = ["CENTRE", "BOBO DIOULASSO", "BANFORA","Hauts-Bassins", "Boucle du Mouhoun"]

_CROP_ALIASES = {
	"mais": "Maïs",
	"maïs": "Maïs",
	"tomate": "Tomate",
	"tomates": "Tomate",
	"sorgho": "Sorgho",
	"niebe": "Niébé",
	"niébé": "Niébé",
	"riz": "Riz",
	"mil": "Mil",
	"arachide": "Arachide",
	"coton": "Coton",
	"sesame": "Sésame",
	"sésame": "Sésame",
}


def _build_crop_picker_component(missing_fields: List[str], state: Optional[FormationState] = None) -> Dict[str, Any]:
    """Build an AG-UI ListMenu or FormInput for missing agronomic info."""
    state = state or {}
    if "identified_crop" in missing_fields:
        dynamic_crops = [str(c).strip() for c in (state.get("declared_crops") or []) if str(c).strip()]
        if not dynamic_crops:
            dynamic_crops = [
                str(t).strip()
                for t in (state.get("focus_topics") or [])
                if str(t).strip() and str(t).strip().isalpha()
            ]
        crop_items = dynamic_crops or _KNOWN_CROPS
        return {
            "type": "ListMenu",
            "props": {
                "title": "Quelle culture concerne votre question ?",
                "items": [{"id": c.lower().replace(" ", "_"), "label": c} for c in crop_items],
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
    if "variety_name" in missing_fields:
        specs = state.get("crop_specs") or {}
        zone = _safe_str(state.get("zone_category") or (state.get("learner_profile") or {}).get("zone"), "Centre")
        varieties_map = specs.get("varieties") if isinstance(specs, dict) else {}
        variety_candidates: List[str] = []
        if isinstance(varieties_map, dict):
            for k, values in varieties_map.items():
                if str(k).strip().lower() == zone.lower() and isinstance(values, list):
                    variety_candidates = [str(v).strip() for v in values if str(v).strip()]
                    break
        if not variety_candidates:
            variety_candidates = ["Variété locale", "Variété améliorée"]
        return {
            "type": "ListMenu",
            "props": {
                "title": "Quelle variété utilisez-vous ?",
                "items": [{"id": v.lower().replace(" ", "_"), "label": v} for v in variety_candidates],
                "target_field": "variety_name",
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


def _extract_crop_from_query(query: str, focus_topics: Optional[List[str]] = None) -> str:
	"""Best-effort crop extraction directly from user utterance (flexible, non-rigid)."""
	q = (query or "").lower()
	for alias, canonical in _CROP_ALIASES.items():
		if alias in q:
			return canonical
	for topic in focus_topics or []:
		t = str(topic or "").strip().lower()
		if t in _CROP_ALIASES:
			return _CROP_ALIASES[t]
	return ""


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
	fertilizer_lines = state.get("fertilizer_instructions") or []
	if fertilizer_lines:
		body_lines.append("\nDosages pratiques (100 m²) :")
		for line in fertilizer_lines:
			body_lines.append(f"  - {line}")
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


def _build_fertilizer_component(breakdown: List[Dict[str, Any]]) -> Dict[str, Any]:
	"""Expose fertilizer plan as AG-UI friendly data table."""
	columns = [
		{"id": "stage", "label": "Étape"},
		{"id": "product", "label": "Produit"},
		{"id": "kg_per_ha", "label": "kg/ha"},
		{"id": "kg_per_100m2", "label": "kg / 100 m²"},
		{"id": "local_equivalent", "label": "Mesure locale"},
		{"id": "mode", "label": "Mode"},
	]
	rows = [
		{
			"stage": entry.get("stage", ""),
			"product": entry.get("product", ""),
			"kg_per_ha": entry.get("kg_per_ha", 0),
			"kg_per_100m2": entry.get("kg_per_100m2", 0),
			"local_equivalent": entry.get("local_equivalent", ""),
			"mode": entry.get("mode", ""),
		}
		for entry in breakdown
	]
	return {
		"type": "DataTable",
		"props": {
			"title": "Plan d'engrais pour 100 m²",
			"columns": columns,
			"rows": rows,
		},
	}


@dataclass
class FormationConfig(BaseAgentConfig):
	"""Runtime DI container for the Formation graph.

	Inherited from BaseAgentConfig:
	  llm_client, mcp_rag, mcp_context, mcp_session, shield, retriever,
	  evaluator, ctx, _runtime
	"""

	# Optional injection points
	llm_client: Any = None
	default_profile: Optional[Dict[str, Any]] = None
	db_service: Optional[Any] = None  # AgriDatabaseService instance

	# LLM model orchestration (planner / writer / fallback)
	model_planner: str = "llama-3.1-8b-instant"
	model_answer: str = "llama-3.3-70b-versatile"
	model_fallback: str = "llama-3.1-8b-instant"  # offline / cheap fallback
	enable_llm: bool = True  # False → deterministic-only (no LLM calls)


@dataclass
class FormationRuntime:
	llm: Any
	tool: "FormationTool"
	refine: "RefineTool"
	advisory: Optional["FormationAdvisor"]
	default_profile: Dict[str, Any]
	model_planner: str
	model_answer: str
	model_fallback: str = "llama-3.1-8b-instant"
	enable_llm: bool = True
	db_service: Optional[Any] = None  # AgriDatabaseService
	mcp_rag: Optional[Any] = None
	mcp_context: Optional[Any] = None
	mcp_session: Optional[Any] = None
	shield: Optional[Any] = None

	def ensure_advisory(self) -> "FormationAdvisor":
		if self.advisory is None:
			from futur.tools.formation_advisor import FormationAdvisor

			self.advisory = FormationAdvisor()
		return self.advisory

	def ensure_db(self) -> Any:
		"""Lazily resolve AgriDatabaseService."""
		if self.db_service is not None:
			return self.db_service
		try:
			from agriconnect.services.database import AgriDatabaseService

			self.db_service = AgriDatabaseService()
			logger.info("AgriDatabaseService lazily initialized for FormationRuntime")
			return self.db_service
		except Exception as exc:
			logger.warning("Cannot initialize AgriDatabaseService: %s", exc)
			return None

	def get_mcp_runtime(self) -> Any:
		"""Return the best available MCP runtime/session (priority order)."""
		for candidate in (self.mcp_session, self.mcp_context, self.mcp_rag):
			if candidate is not None:
				return candidate
		return None

	async def call_mcp(self, tool_name: str, **kwargs: Any) -> Any:
		"""Call an MCP tool in a resilient way across runtime implementations."""
		runtime = self.get_mcp_runtime()
		if runtime is None:
			raise RuntimeError("MCP runtime unavailable")

		safe_kwargs = {k: v for k, v in kwargs.items() if v is not None}

		# 1) Market-style runtime facade
		call_db = getattr(runtime, "call_db", None)
		if callable(call_db):
			resp = call_db(tool_name, **safe_kwargs)
			return await resp if inspect.isawaitable(resp) else resp

		# 2) Direct session/tool client
		call_tool = getattr(runtime, "call_tool", None)
		if callable(call_tool):
			try:
				resp = call_tool(tool_name, safe_kwargs)
			except TypeError:
				resp = call_tool(tool_name=tool_name, arguments=safe_kwargs)
			return await resp if inspect.isawaitable(resp) else resp

		# 3) Wrapped db client pattern
		db_client = getattr(runtime, "db_client", None)
		if db_client is not None and hasattr(db_client, "call_tool"):
			resp = db_client.call_tool(tool_name, safe_kwargs)
			return await resp if inspect.isawaitable(resp) else resp

		raise RuntimeError(f"Unsupported MCP runtime type for tool '{tool_name}'")


def _create_runtime(cfg: FormationConfig) -> FormationRuntime:
	llm = _ensure_llm(cfg.llm_client) if cfg.enable_llm else None
	from futur.tools.formation import FormationTool
	from futur.tools.refine import RefineTool

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
		default_profile=default_profile,
		model_planner=cfg.model_planner,
		model_answer=cfg.model_answer,
		model_fallback=cfg.model_fallback,
		enable_llm=cfg.enable_llm,
		db_service=cfg.db_service,
		mcp_rag=cfg.mcp_rag,
		mcp_context=cfg.mcp_context,
		mcp_session=cfg.mcp_session,
		shield=cfg.shield,
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


def _log_tool_call(
	state: FormationState,
	tool_name: str,
	args: Dict[str, Any],
	result: Any,
	*,
	success: bool = True,
	error: str = "",
) -> List[Dict[str, Any]]:
	"""Append a tool-call record to state['tool_calls'] and return the updated list."""
	calls = list(state.get("tool_calls") or [])
	calls.append({
		"tool": tool_name,
		"args": args,
		"success": success,
		"error": error,
		"ts": datetime.now(timezone.utc).isoformat(),
		"result_preview": str(result)[:200] if result else "",
	})
	return calls


# ---------------------------------------------------------------------------
# LOAD_PROFILE — fetch user identity + history from DB
# ---------------------------------------------------------------------------

async def load_profile_node(state: FormationState, agent_config: FormationConfig) -> Dict[str, Any]:
	"""Node 0: Resolve user identity from DB and populate personalization fields.

	Uses the shared identity resolution (agriconnect.agents.identity) for DRY
	phone-first lookup, farm fetching, and crop inference.
	"""
	try:
		from agriconnect.workspace.context_guard import ContextGuard

		allowed, guard_msg = ContextGuard.ensure_agent(state, expected_agent="formation")
		if not allowed:
			logger.warning("ContextGuard: tool execution blocked (formation)")
			warnings: List[str] = list(state.get("warnings") or [])
			warnings.append("agent_mismatch")
			return {
				"warnings": _dedupe(warnings),
				"status": "TOOLS_SKIPPED",
				"response_strategy": "ASK_CLARIFICATION",
				"final_response": guard_msg,
				"degraded_mode": True,
				"tool_calls": list(state.get("tool_calls") or []),
			}
		runtime = _get_runtime(agent_config)
		warnings: List[str] = list(state.get("warnings") or [])
		tool_calls: List[Dict[str, Any]] = list(state.get("tool_calls") or [])
		session_id = state.get("session_id") or str(_uuid_mod.uuid4())

		# --- Extract identity from state (shared utility) ---
		phone = extract_phone_from_state(state)
		user_id = extract_user_id_from_state(state)

		if not phone and not user_id:
			return {
				"profile_loaded": False,
				"session_id": session_id,
				"status": "NEEDS_PHONE",
				"response_strategy": "ASK_CLARIFICATION",
				"missing_info": ["user_phone"],
				"final_response": "Pour personnaliser le conseil, partagez votre numéro WhatsApp.",
				"ag_ui_component": {
					"type": "FormInputComponent",
					"props": {
						"title": "Numéro WhatsApp requis",
						"fields": [{"name": "user_phone", "label": "Numéro WhatsApp", "type": "text", "required": True}],
					},
				},
				"warnings": _dedupe(warnings),
				"tool_calls": tool_calls,
			}

		# --- Build DataGateway (shared MCP/DB facade) ---
		db = runtime.ensure_db()
		mcp_runtime = runtime.get_mcp_runtime()
		gateway = DataGateway(mcp_runtime=mcp_runtime, db_service=db)

		if not gateway.is_available:
			logger.info("load_profile_node: no backend available — static profile")
			warnings.append("Base de données indisponible — profil statique utilisé.")
			return {
				"profile_loaded": False,
				"session_id": session_id,
				"learner_profile": state.get("learner_profile") or runtime.default_profile,
				"warnings": _dedupe(warnings),
				"tool_calls": tool_calls,
				"status": "PROFILE_STATIC",
			}

		# --- Shared identity resolution (DRY) ---
		identity = await resolve_identity(
			gateway,
			phone=phone,
			user_id=user_id,
			fetch_farms=True,
			fetch_crops=True,
		)

		# Build learner_profile from resolved identity
		base_profile = state.get("learner_profile") or {}
		profile = {
			**runtime.default_profile,
			**base_profile,
			"user_id": identity.user_id or "",
			"phone": identity.phone or "",
			"name": identity.name or base_profile.get("name", ""),
			"zone": identity.zone_name or base_profile.get("zone", "Centre"),
			"niveau": base_profile.get("niveau", "debutant"),
		}

		# --- Fetch training memory/history ---
		training_history: List[Dict[str, Any]] = []
		if identity.user_id:
			try:
				history = await gateway.call("get_agent_memory", user_id=identity.user_id)
				if isinstance(history, list):
					training_history = history[-20:]
				tool_calls = _log_tool_call(state, "get_agent_memory", {"user_id": identity.user_id}, history)
			except Exception as exc:
				logger.debug("load_profile_node: get_agent_memory unavailable: %s", exc)

		# --- Fetch crop requirements if crop already identified ---
		crop_requirements: Optional[Dict[str, Any]] = None
		identified_crop = _safe_str(state.get("identified_crop") or state.get("crop_name"))
		if identified_crop and identified_crop != "UNKNOWN":
			try:
				crop_requirements = await gateway.call("get_crop_requirements", crop_type=identified_crop)
				tool_calls = _log_tool_call(state, "get_crop_requirements", {"crop_type": identified_crop}, crop_requirements)
			except Exception as exc:
				logger.warning("load_profile_node: get_crop_requirements failed: %s", exc)

		# --- Zone enrichment from farms ---
		if not identity.zone_name and identity.farms:
			zone = identity.farms[0].get("zone_name", "")
			if zone:
				profile["zone"] = zone

		declared_crops = list(state.get("declared_crops") or [])
		for c in identity.declared_crops:
			if c not in declared_crops:
				declared_crops.append(c)

		logger.info(
			"load_profile_node: user_id=%s phone=%s plots=%d crops=%s",
			identity.user_id, identity.phone, len(identity.farms), declared_crops,
		)

		return {
			"db_user_id": identity.user_id or None,
			"user_phone": identity.phone or None,
			"learner_profile": profile,
			"profile_loaded": identity.resolved,
			"training_history": training_history,
			"user_plots": identity.farms,
			"active_cycles": [],
			"declared_crops": declared_crops,
			"crop_requirements": crop_requirements,
			"session_id": session_id,
			"tool_calls": tool_calls,
			"warnings": _dedupe(warnings),
			"status": "PROFILE_LOADED" if identity.resolved else "PROFILE_STATIC",
		}
	except Exception as exc:
		logger.exception("load_profile_node CRASH: %s", exc)
		return {
			"profile_loaded": False,
			"session_id": state.get("session_id") or str(_uuid_mod.uuid4()),
			"warnings": [f"Erreur chargement profil: {exc}"],
			"status": "PROFILE_STATIC",
		}


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
		# 3) Matière pédagogique
		intent = _safe_str(analysis.get("intent"), "FORMATION")
		focus_topics = list(analysis.get("focus_topics") or [])
		field_actions = list(analysis.get("field_actions") or [])
		safety_flags = list(analysis.get("safety_flags") or [])
		urgency = _safe_str(analysis.get("urgency"), "NORMAL")

		crop_type = _safe_str(analysis.get("identified_crop_type") or analysis.get("identified_crop"), "UNKNOWN")
		if not crop_type or crop_type == "UNKNOWN":
			extracted = _extract_crop_from_query(query, focus_topics)
			if extracted:
				crop_type = extracted
				logger.info("analyze_node: crop extracted from query=%s", crop_type)

		zone = _safe_str(state.get("zone_category") or (state.get("learner_profile") or {}).get("zone"), "Centre")
		crop_specs: Dict[str, Any] = {}
		if crop_type and crop_type != "UNKNOWN":
			try:
				advisor = runtime.ensure_advisory()
				profile = await advisor.crop_tool._fetch_profile(crop_type, zone)
				crop_specs = profile.model_dump()
			except Exception as exc:
				logger.warning("analyze_node: MCP crop_specs unavailable for crop=%s zone=%s: %s", crop_type, zone, exc)
				warnings.append(f"Fiche technique MCP indisponible pour {crop_type}: {exc}")
				crop_specs = runtime.tool.get_crop_characteristics(crop_type) or {}
		else:
			warnings.append("Aucune culture spécifique identifiée dans la requête.")

		warnings = _dedupe(warnings + list(analysis.get("warnings") or []))

		# 4) Historique d'apprentissage
		existing = list(state.get("concepts_appris") or [])
		for topic in focus_topics:
			if topic not in existing:
				existing.append(topic)

		return {
			"intent": intent,
			"identified_crop": crop_type,
			"zone_category": zone,
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
			component = _build_crop_picker_component(missing, state)
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

		# Dynamic clarification only when crop already identified and MCP returns multiple varieties
		crop_specs = state.get("crop_specs") or {}
		variety_name = _safe_str(state.get("variety_name"))
		query = _safe_str(state.get("user_query")).lower()
		zone_for_varieties = _safe_str(updates.get("zone_category") or zone, "Centre")
		variety_candidates: List[str] = []
		if isinstance(crop_specs, dict):
			varieties_map = crop_specs.get("varieties") or {}
			if isinstance(varieties_map, dict):
				for key, values in varieties_map.items():
					if _safe_str(key).lower() == zone_for_varieties.lower() and isinstance(values, list):
						variety_candidates = [str(v).strip() for v in values if str(v).strip()]
						break

		if len(variety_candidates) > 1 and not variety_name:
			mentionned = any(v.lower() in query for v in variety_candidates)
			if not mentionned and urgency != "CRITIQUE":
				component = _build_crop_picker_component(["variety_name"], state)
				return {
					"status": "NEEDS_CLARIFICATION",
					"response_strategy": "ASK_CLARIFICATION",
					"missing_info": ["variety_name"],
					"ag_ui_component": component,
					"final_response": component["props"]["title"],
					"warnings": _dedupe(warnings),
				}

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
		from agriconnect.workspace.context_guard import ContextGuard

		allowed, guard_msg = ContextGuard.ensure_agent(state, expected_agent="formation")
		if not allowed:
			logger.warning("ContextGuard: tool execution blocked (formation)")
			warnings: List[str] = list(state.get("warnings") or [])
			warnings.append("agent_mismatch")
			return {
				"warnings": _dedupe(warnings),
				"status": "TOOLS_SKIPPED",
				"response_strategy": "ASK_CLARIFICATION",
				"final_response": guard_msg,
				"degraded_mode": True,
				"tool_calls": list(state.get("tool_calls") or []),
			}
		runtime = _get_runtime(agent_config)
		warnings: List[str] = list(state.get("warnings") or [])
		ag_ui_payloads: List[Dict[str, Any]] = list(state.get("ag_ui_payloads") or [])

		# Sanitize inputs (Postel's Law)
		crop_type = _safe_str(state.get("identified_crop"), "")
		zone = _safe_str(state.get("zone_category"), "Centre")
		profile = state.get("learner_profile") or runtime.default_profile
		area_ha = _safe_float(profile.get("superficie") or state.get("area_ha"), 1.0)

		if not crop_type or crop_type == "UNKNOWN":
			extracted = _extract_crop_from_query(_safe_str(state.get("user_query")), state.get("focus_topics") or [])
			if extracted:
				crop_type = extracted
			else:
				component = _build_crop_picker_component(["identified_crop"], state)
				return {
					"status": "NEEDS_CLARIFICATION",
					"response_strategy": "ASK_CLARIFICATION",
					"missing_info": ["identified_crop"],
					"ag_ui_component": component,
					"final_response": component["props"]["title"],
					"warnings": _dedupe(warnings),
				}

		logger.info(
			"consult_crop_node: crop=%s zone=%s area_ha=%s",
			crop_type, zone, area_ha,
		)

		# --- Primary: FormationAdvisor (DB-backed INERA profiles) ---
		technical_canvas: Dict[str, Any] = {}
		canvas_md = ""
		advisor_error = False
		localized_response = ""
		localized_status = "UNAVAILABLE"
		localized_payload: Dict[str, Any] = {}
		fertilizer_instructions: List[str] = []
		fertilizer_breakdown: List[Dict[str, Any]] = []

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

		# --- Localized deterministic answer for WhatsApp ---
		if technical_canvas:
			try:
				localized_payload = await runtime.tool.run_full_diagnostic(
					query=state.get("user_query") or "",
					crop_type=crop_type,
					zone=zone,
					rag_context=state.get("retrieved_context"),
					rag_sources=state.get("sources"),
					area_ha=area_ha,
				)
			except Exception as exc:
				logger.warning("consult_crop_node: run_full_diagnostic failed: %s", exc)
				warnings.append(f"Diagnostic détaillé indisponible: {exc}")
				localized_payload = {}

		if localized_payload:
			localized_response = localized_payload.get("final_response", "")
			localized_status = localized_payload.get("status", "OK")
			fertilizer_instructions = list(localized_payload.get("fertilizer_instructions") or [])
			fertilizer_breakdown = list(localized_payload.get("fertilizer_breakdown") or [])
			warnings.extend(localized_payload.get("warnings") or [])
			warnings = _dedupe(warnings)
			if fertilizer_breakdown:
				ag_ui_payloads.append(_build_fertilizer_component(fertilizer_breakdown))

		retrieval_mode = "MCP_FULL" if technical_canvas else ("JSON_ONLY" if crop_specs else "DEGRADED")
		degraded_mode = (
			localized_payload.get("degraded_mode") if localized_payload else (advisor_error and not crop_specs)
		)

		return {
			"technical_canvas": technical_canvas,
			"canvas_markdown": canvas_md,
			"crop_specs": crop_specs,
			"advisor_error": advisor_error,
			"retrieval_mode": retrieval_mode,
			"degraded_mode": bool(degraded_mode),
			"localized_response": localized_response,
			"localized_status": localized_status,
			"fertilizer_instructions": fertilizer_instructions,
			"fertilizer_breakdown": fertilizer_breakdown,
			"ag_ui_payloads": ag_ui_payloads,
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
			"localized_response": "",
			"localized_status": "ERROR",
			"fertilizer_instructions": [],
			"fertilizer_breakdown": [],
			"ag_ui_payloads": list(state.get("ag_ui_payloads") or []),
			"status": "CONSULTED",
			"warnings": [f"Erreur consultation agronomique: {exc}"],
		}


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

		raw_crop = _safe_str(state.get("identified_crop"), "")
		has_specific_crop = bool(raw_crop and raw_crop.upper() != "UNKNOWN")
		crop_name = raw_crop or "la culture"
		system_content = (
			f"{FORMATION_SYSTEM_TEMPLATE.format(style_guidance=style_guidance, culture_context=crop_name)}\n"
			"CONSIGNE CRITIQUE : Utilise prioritairement les caractéristiques techniques fournies dans le contexte (issues de la base de référence)."
		)
		if has_specific_crop:
			system_content += (
				"\nTON ATTENDU : conseiller agricole de terrain, directif et concret. "
				"Structure la réponse en trois sections explicitement titrées : \"SEMIS & IMPLANTATION\", \"EAU & HUMIDITÉ\" et \"PROTECTION & SÉCURITÉ\". "
				"Chaque section doit contenir au moins deux consignes actionnables (doses, fréquences, outils, seuils) et rappeler les alertes de sécurité pertinentes."
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
		if has_specific_crop:
			user_content += (
				"\n\nFORMAT À RESPECTER :\n"
				"1. SEMIS & IMPLANTATION : gestes clés pour réussir l'implantation (dates, densité, fertilisation de départ).\n"
				"2. EAU & HUMIDITÉ : planning d'irrigation ou de conservation de l'humidité selon le stade.\n"
				"3. PROTECTION & SÉCURITÉ : surveillance maladies/ravageurs, EPI et mesures de sécurité."
			)

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
		localized_response = state.get("localized_response") or ""
		localized_status = state.get("localized_status") or ""
		ag_ui_payloads = list(state.get("ag_ui_payloads") or [])

		if localized_response:
			final_answer = localized_response
			if localized_status and localized_status.upper() != "OK":
				warnings.append(f"Statut diagnostic: {localized_status}")
			return {
				"answer_draft": final_answer,
				"final_response": final_answer,
				"agri_response": _build_agri_response(final_answer, query, state),
				"warnings": _dedupe(warnings),
				"status": "ANSWER_GENERATED",
				"ag_ui_component": None,
				"ag_ui_payloads": ag_ui_payloads,
			}

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
			"ag_ui_payloads": ag_ui_payloads,
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
			"ag_ui_payloads": list(state.get("ag_ui_payloads") or []),
		}


def critique_node(state: FormationState, agent_config: FormationConfig) -> Dict[str, Any]:
	runtime = _get_runtime(agent_config)
	result = runtime.refine.critique_node(state)
	count = int(state.get("critique_retry_count", 0))
	if "critique_retry_count" not in result:
		result["critique_retry_count"] = count + 1
	return result


async def call_tools_node(state: FormationState, agent_config: FormationConfig) -> Dict[str, Any]:
	"""Node 4b: Orchestrate supplementary MCP/DB tool calls based on intent.

	Calls FormationAdvisor, weather, sanitary risks, cycle economics, etc.
	depending on state.intent and available data. Results enrich the context
	before composition.
	"""
	try:
		runtime = _get_runtime(agent_config)
		warnings: List[str] = list(state.get("warnings") or [])
		tool_calls: List[Dict[str, Any]] = list(state.get("tool_calls") or [])
		extra_context_parts: List[str] = []
		degraded_mode = bool(state.get("degraded_mode"))

		db = runtime.ensure_db()
		intent = _safe_str(state.get("intent"), "FORMATION")
		crop = _safe_str(state.get("identified_crop"))
		phone = state.get("user_phone")
		user_plots = state.get("user_plots") or []

		# --- 1. Crop requirements from DB (if not already loaded) ---
		crop_req = state.get("crop_requirements")
		if not crop_req and crop and crop != "UNKNOWN":
			try:
				crop_req = await execute_formation_action(
					"FORMATION_GET_CROP_REQUIREMENTS",
					{"crop_type": crop},
					runtime=runtime,
					db_service=db,
					state=state,
				)
			except Exception:
				raise
			try:
				tool_calls = _log_tool_call(state, "get_crop_requirements", {"crop_type": crop}, crop_req)
				if crop_req:
					extra_context_parts.append(
						f"=== EXIGENCES AGRONOMIQUES DB ({crop}) ===\n"
						+ json.dumps(crop_req, indent=2, ensure_ascii=False)
					)
			except Exception as exc:
				logger.warning("call_tools_node: get_crop_requirements failed: %s", exc)

		# --- 2. Sanitary risks for user's farm ---
		if intent in ("URGENCE", "CONSEIL") and user_plots:
			farm_id = user_plots[0].get("id") or user_plots[0].get("farm_id")
			if farm_id:
				try:
					risks = await execute_formation_action(
						"FORMATION_GET_ACTIVE_SANITARY_RISKS",
						{"farm_id": farm_id},
						runtime=runtime,
						db_service=db,
						state=state,
					)
				except Exception:
					raise
				try:
					tool_calls = _log_tool_call(state, "get_active_sanitary_risks", {"farm_id": farm_id}, risks)
					if risks:
						risk_text = "\n".join(f"- {r.get('name', 'Risque')}: {r.get('description', '')}" for r in risks[:5])
						extra_context_parts.append(f"=== RISQUES SANITAIRES ACTIFS ===\n{risk_text}")
				except Exception as exc:
					logger.warning("call_tools_node: get_active_sanitary_risks failed: %s", exc)

		# --- 3. Cycle economics if cycle_id is available ---
		cycle_id = state.get("cycle_id")
		if cycle_id:
			try:
				econ = await execute_formation_action(
					"FORMATION_GET_CYCLE_ECONOMICS",
					{"cycle_id": cycle_id},
					runtime=runtime,
					db_service=db,
					state=state,
				)
			except Exception:
				raise
			try:
				tool_calls = _log_tool_call(state, "get_cycle_economics", {"cycle_id": cycle_id}, econ)
				if econ:
					extra_context_parts.append(
						f"=== ÉCONOMIE DU CYCLE ===\n"
						+ json.dumps(econ, indent=2, ensure_ascii=False)
					)
			except Exception as exc:
				logger.warning("call_tools_node: get_cycle_economics failed: %s", exc)

		# --- 4. RAG via MCP if available ---
		rag_result = None
		rag_warning: Optional[str] = None
		if not state.get("retrieved_context"):
			try:
				query = state.get("optimized_query") or state.get("user_query") or ""
				rag_result = await runtime.call_mcp("retrieve", query=query)
			except Exception as mcp_exc:
				logger.warning("call_tools_node: MCP retrieve failed, fallback to invoke_query_rag: %s", mcp_exc)
				try:
					from futur.formation.tools import invoke_query_rag

					rag_result = await invoke_query_rag(query)
				except Exception as rag_exc:
					rag_warning = (
						"Serveur RAG indisponible — la réponse s'appuie uniquement sur les données locales."
					)
					warnings.append(rag_warning)
					degraded_mode = True
					logger.warning("call_tools_node: invoke_query_rag failed: %s", rag_exc)
			try:
				tool_calls = _log_tool_call(state, "invoke_query_rag", {"query": query}, rag_result)
				if isinstance(rag_result, dict):
					if rag_result.get("text"):
						extra_context_parts.append(f"=== RAG MCP ===\n{rag_result['text']}")
					elif rag_result.get("context_text"):
						extra_context_parts.append(f"=== RAG MCP ===\n{rag_result['context_text']}")
				elif isinstance(rag_result, str):
					extra_context_parts.append(f"=== RAG MCP ===\n{rag_result}")
			except Exception as exc:
				logger.warning("call_tools_node: RAG MCP failed: %s", exc)
		elif rag_warning and rag_warning not in warnings:
			warnings.append(rag_warning)

		# Merge extra context
		existing_context = state.get("retrieved_context") or ""
		if extra_context_parts:
			existing_context = existing_context + "\n\n" + "\n\n".join(extra_context_parts)

		logger.info("call_tools_node: %d tool calls executed, %d extra context parts", len(tool_calls), len(extra_context_parts))

		return {
			"retrieved_context": existing_context,
			"crop_requirements": crop_req,
			"tool_calls": tool_calls,
			"warnings": _dedupe(warnings),
			"degraded_mode": degraded_mode,
			"status": "TOOLS_CALLED",
		}
	except Exception as exc:
		logger.exception("call_tools_node CRASH: %s", exc)
		return {
			"warnings": [f"Erreur appel outils: {exc}"],
			"status": "TOOLS_CALLED",
		}


async def evaluate_node(state: FormationState, agent_config: FormationConfig) -> Dict[str, Any]:
	"""Node 7: Evaluate quality of the final answer with real scoring.

	Scoring dimensions (0.0–1.0):
	  - completeness: are all focus_topics addressed?
	  - technical_grounding: is crop_specs / canvas data used?
	  - safety_coverage: are safety_flags addressed?
	  - personalization: is learner profile reflected?
	Overall session_score = weighted average.
	"""
	try:
		_get_runtime(agent_config)
		warnings: List[str] = list(state.get("warnings") or [])
		final_text = (state.get("final_response") or "").lower()

		# --- Completeness: check focus_topics coverage ---
		topics = state.get("focus_topics") or []
		if topics:
			hits = sum(1 for t in topics if t.lower() in final_text)
			completeness = hits / len(topics)
		else:
			completeness = 0.5  # no topics extracted = neutral

		# --- Technical grounding ---
		has_canvas = bool(state.get("canvas_markdown"))
		has_specs = bool(state.get("crop_specs"))
		has_rag = bool(state.get("retrieved_context"))
		tech_grounding = 0.0
		if has_canvas:
			tech_grounding += 0.5
		if has_specs:
			tech_grounding += 0.3
		if has_rag:
			tech_grounding += 0.2

		# --- Safety coverage ---
		flags = state.get("safety_flags") or []
		if flags:
			safety_hits = sum(1 for f in flags if f.lower() in final_text)
			safety_score = safety_hits / len(flags)
		else:
			safety_score = 1.0  # no flags = no risk

		# --- Personalization ---
		profile = state.get("learner_profile") or {}
		personalization = 0.5  # baseline
		if state.get("profile_loaded"):
			personalization += 0.3
		if profile.get("zone") and profile["zone"].lower() in final_text:
			personalization += 0.2
		personalization = min(personalization, 1.0)

		# --- Weighted score ---
		session_score = round(
			completeness * 0.3 + tech_grounding * 0.3 + safety_score * 0.2 + personalization * 0.2,
			3,
		)

		evaluation = {
			"completeness": round(completeness, 3),
			"technical_grounding": round(tech_grounding, 3),
			"safety_coverage": round(safety_score, 3),
			"personalization": round(personalization, 3),
			"overall": session_score,
		}

		# Quality gate
		if session_score < 0.3:
			warnings.append(f"Qualité de réponse faible (score={session_score}). Vérification recommandée.")
		if state.get("degraded_mode"):
			warnings.append("Réponse générée en mode dégradé — données agronomiques incomplètes.")

		logger.info("evaluate_node: score=%s eval=%s", session_score, evaluation)

		return {
			"evaluation": evaluation,
			"session_score": session_score,
			"warnings": _dedupe(warnings),
			"status": "EVALUATED",
		}
	except Exception as exc:
		logger.exception("evaluate_node CRASH: %s", exc)
		return {
			"evaluation": {"overall": 0.0, "error": str(exc)},
			"session_score": 0.0,
			"warnings": [f"Erreur évaluation: {exc}"],
			"status": "EVALUATED",
		}


async def log_session_node(state: FormationState, agent_config: FormationConfig) -> Dict[str, Any]:
	"""Node 8: Persist the pedagogical session to the database.

	Logs: conversation record, agent action, concepts learned.
	Skips gracefully when DB is unavailable.
	"""
	try:
		runtime = _get_runtime(agent_config)
		warnings: List[str] = list(state.get("warnings") or [])
		tool_calls: List[Dict[str, Any]] = list(state.get("tool_calls") or [])

		db = runtime.ensure_db()
		if db is None:
			logger.info("log_session_node: no DB — skipping session logging")
			return {"session_logged": False, "status": "SESSION_SKIPPED"}

		user_id = state.get("db_user_id") or ""
		query = state.get("user_query") or ""
		response = state.get("final_response") or ""
		crop = state.get("identified_crop") or ""
		session_score = state.get("session_score") or 0.0

		# --- 1. Log conversation ---
		try:
			try:
				conv_result = await runtime.call_mcp(
					"log_conversation",
					user_id=user_id,
					query=query,
					response=response[:2000],
					agent_type="formation",
					crop=crop,
					confidence_score=session_score,
					execution_path=[c.get("tool", "") for c in tool_calls],
				)
			except Exception:
				conv_result = await db.log_conversation(
					user_id=user_id,
					query=query,
					response=response[:2000],
					agent_type="formation",
					crop=crop,
					confidence_score=session_score,
					execution_path=[c.get("tool", "") for c in tool_calls],
				)
			tool_calls = _log_tool_call(state, "log_conversation", {"user_id": user_id}, conv_result)
		except Exception as exc:
			logger.warning("log_session_node: log_conversation failed: %s", exc)
			tool_calls = _log_tool_call(state, "log_conversation", {"user_id": user_id}, None, success=False, error=str(exc))

		# --- 2. Log agent action (for audit trail) ---
		try:
			action_payload = {
				"session_id": state.get("session_id"),
				"intent": state.get("intent"),
				"crop": crop,
				"score": session_score,
				"retrieval_mode": state.get("retrieval_mode"),
				"concepts_appris": state.get("concepts_appris") or [],
				"degraded_mode": state.get("degraded_mode", False),
			}
			try:
				action_result = await runtime.call_mcp(
					"create_agent_action",
					agent_name="formation",
					action_type="TRAINING_SESSION",
					payload=action_payload,
					user_id=user_id or None,
					ai_reasoning=state.get("reasoning") or f"Formation session score={session_score}",
				)
			except Exception:
				action_result = await db.create_agent_action(
					agent_name="formation",
					action_type="TRAINING_SESSION",
					payload=action_payload,
					user_id=user_id or None,
					ai_reasoning=state.get("reasoning") or f"Formation session score={session_score}",
				)
			tool_calls = _log_tool_call(state, "create_agent_action", {"agent_name": "formation"}, action_result)
		except Exception as exc:
			logger.warning("log_session_node: create_agent_action failed: %s", exc)
			tool_calls = _log_tool_call(state, "create_agent_action", {}, None, success=False, error=str(exc))

		logger.info("log_session_node: session %s logged (score=%.3f)", state.get("session_id"), session_score)

		return {
			"session_logged": True,
			"tool_calls": tool_calls,
			"warnings": _dedupe(warnings),
			"status": "SESSION_LOGGED",
		}
	except Exception as exc:
		logger.exception("log_session_node CRASH: %s", exc)
		return {
			"session_logged": False,
			"warnings": [f"Erreur logging session: {exc}"],
			"status": "SESSION_LOGGED",
		}


# ---------------------------------------------------------------------------
# Routing helpers (kept here to keep graph wiring declarative)
# ---------------------------------------------------------------------------


def route_after_analyze(state: FormationState) -> str:
	"""After analyze, always go to validation."""
	return "validate"


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
	"load_profile_node",
	"analyze_node",
	"validate_node",
	"consult_crop_node",
	"call_tools_node",
	"compose_node",
	"critique_node",
	"evaluate_node",
	"log_session_node",
	"route_after_analyze",
	"route_after_validate",
	"route_after_critique",
	"FormationState",
]
