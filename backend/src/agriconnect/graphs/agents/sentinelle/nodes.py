"""Sentinelle sub-graph nodes with local, independent business logic."""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
from agriconnect.graphs.agents.common.config import BaseAgentConfig

from langgraph.graph import END, StateGraph

from .prompts import SENTINELLE_SYSTEM_TEMPLATE, SENTINELLE_USER_TEMPLATE, STYLE_GUIDANCE
from .state import SentinelState
from agriconnect.rag.components import get_groq_sdk
from agriconnect.rag.metric import RAGEvaluator
from agriconnect.tools.sentinelle import SentinelleTool
from agriconnect.tools.refine import RefineTool
from agriconnect.tools.crop import BurkinaCropTool
from agriconnect.agent_test.base import BaseAgent
from agriconnect.services.persistence import AgriPersister

logger = logging.getLogger("Agent.ClimateSentinel")


@dataclass
class SentinelConfig:
	llm_client: Any = None
	evaluator: Optional[RAGEvaluator] = None
	mcp_session: Any = None
	mcp_rag: Any = None
	shield: Any = None
	ctx: Any = None


@dataclass
class ComposeContext:
	query: str
	location: str
	risk_summary: str
	metrics: Dict[str, Any]
	flood: Dict[str, Any]
	hazards: List[Dict[str, Any]]
	context: str
	agronomic_advice: Dict[str, Any] = field(default_factory=dict)
	surface_calc_info: str = ""
	user_level: str = "debutant"
	warnings: List[str] = field(default_factory=list)


class ClimateSentinel(BaseAgent):
	_capabilities = ["CHECK_WEATHER", "GET_ALERT", "FLOOD_RISK", "SATELLITE_DATA", "AGRO_METEO"]

	_PURCHASE_KEYWORDS = {
		"acheter",
		"achat",
		"fongicide",
		"pesticide",
		"herbicide",
		"intrant",
		"produit",
		"traitement",
		"insecticide",
		"engrais",
		"semence",
		"disponible",
		"trouver",
		"o\u00f9 trouver",
		"prix du produit",
	}
	_STYLE_GUIDANCE = STYLE_GUIDANCE

	def __init__(self, config: Optional[SentinelConfig] = None, **overrides):
		cfg = config or SentinelConfig()
		for k, v in overrides.items():
			if hasattr(cfg, k):
				setattr(cfg, k, v)

		try:
			self.llm = cfg.llm_client if cfg.llm_client else get_groq_sdk()
		except Exception as exc:
			logger.error("LLM Init Failed: %s", exc)
			self.llm = None

		self.model_planner = "llama-3.1-8b-instant"
		self.model_answer = "llama-3.3-70b-versatile"
		self.tools = SentinelleTool(llm_client=self.llm)
		self.refine = RefineTool(llm=self.llm)
		self.crop_tools = BurkinaCropTool()

		self.mcp_session = cfg.mcp_session
		self.shield = cfg.shield
		self.ctx = cfg.ctx
		if self.ctx and hasattr(self.ctx, "db"):
			self.persister = AgriPersister(self.ctx.db, getattr(self.ctx, "memory", None))
		else:
			self.persister = None
			logger.warning("ClimateSentinel: Running without persistence (Context missing)")

	def _purchase_handoff_needed(self, query: str, hazards: list) -> bool:
		q_lower = (query or "").lower()
		if any(kw in q_lower for kw in self._PURCHASE_KEYWORDS):
			return True
		for h in (hazards or []):
			label = str(h.get("label", "")).lower()
			if any(kw in label for kw in ("maladie", "insecte", "parasite", "ravageur")):
				return True
		return False

	def _compute_surface_info(self, query_text: str, et0: float) -> str:
		if not query_text or et0 <= 0:
			return ""

		ha_match = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:ha|hectare)", query_text)
		if ha_match:
			try:
				val = float(ha_match.group(1).replace(",", "."))
				loss = val * 10000 * et0
				return f"CALCUL: Pour {val}ha, perte eau = {loss:,.0f} L/jour."
			except ValueError:
				return ""
		return ""

	async def analyze_node(self, state: SentinelState) -> SentinelState:
		query = state.get("user_query", "")
		location = state.get("location_profile", {}) or {}
		warnings = list(state.get("warnings", []))

		match = re.search(r"prix|cours|vente|achat", query, re.IGNORECASE)
		if match:
			return {**state, "handoff_to": "MarketCoach", "handoff_reason": "Query is about market prices"}

		try:
			weather = state.get("weather_snapshot") or self.tools._fetch_real_weather(location)
			satellite = state.get("satellite_signals") or {}
		except Exception as e:
			logger.error("Sensor fetch failed: %s", e)
			weather = {}
			satellite = {}
			warnings.append("Données météo temps réel indisponibles.")

		metrics = self.tools._compute_metrics(weather, satellite)
		flood = self.tools._assess_flood_risk(weather, satellite, location)
		hazards = self.tools._derive_hazards(metrics, flood)
"""Sentinelle nodes as pure functions and runtime builder."""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from langgraph.graph import END, StateGraph

from .prompts import SENTINELLE_SYSTEM_TEMPLATE, SENTINELLE_USER_TEMPLATE, STYLE_GUIDANCE
from .state import SentinelState
from agriconnect.rag.components import get_groq_sdk
from agriconnect.rag.metric import RAGEvaluator
from agriconnect.tools.sentinelle import SentinelleTool
from agriconnect.tools.refine import RefineTool
from agriconnect.tools.crop import BurkinaCropTool
from agriconnect.services.persistence import AgriPersister

logger = logging.getLogger("Agent.ClimateSentinel")


@dataclass
class SentinelConfig(BaseAgentConfig):
    pass


@dataclass
class ComposeContext:
    query: str
    location: str
    risk_summary: str
    metrics: Dict[str, Any]
    flood: Dict[str, Any]
    hazards: List[Dict[str, Any]]
    context: str
    agronomic_advice: Dict[str, Any] = field(default_factory=dict)
    surface_calc_info: str = ""
    user_level: str = "debutant"
    warnings: List[str] = field(default_factory=list)


class SentinelRuntime:
    def __init__(self, config: Optional[SentinelConfig] = None, **overrides: Any):
        cfg = config or SentinelConfig()
        for k, v in overrides.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)

        try:
            self.llm = cfg.llm_client if cfg.llm_client else get_groq_sdk()
        except Exception as exc:
            logger.error("LLM Init Failed: %s", exc)
            self.llm = None

        self.model_planner = "llama-3.1-8b-instant"
        self.model_answer = "llama-3.3-70b-versatile"
        self.tools = SentinelleTool(llm_client=self.llm)
        self.refine = RefineTool(llm=self.llm)
        self.crop_tools = BurkinaCropTool()

        self.mcp_session = cfg.mcp_session
        self.shield = cfg.shield
        self.ctx = cfg.ctx
        if self.ctx and hasattr(self.ctx, "db"):
            self.persister = AgriPersister(self.ctx.db, getattr(self.ctx, "memory", None))
        else:
            self.persister = None
            logger.warning("ClimateSentinel: Running without persistence (Context missing)")


def build_runtime(config: Optional[SentinelConfig] = None, **overrides: Any) -> SentinelRuntime:
    return SentinelRuntime(config=config, **overrides)


_PURCHASE_KEYWORDS = {
    "acheter",
    "achat",
    "fongicide",
    "pesticide",
    "herbicide",
    "intrant",
    "produit",
    "traitement",
    "insecticide",
    "engrais",
    "semence",
    "disponible",
    "trouver",
    "o\u00f9 trouver",
    "prix du produit",
}


def _purchase_handoff_needed(query: str, hazards: list) -> bool:
    q_lower = (query or "").lower()
    if any(kw in q_lower for kw in _PURCHASE_KEYWORDS):
        return True
    for h in (hazards or []):
        label = str(h.get("label", "")).lower()
        if any(kw in label for kw in ("maladie", "insecte", "parasite", "ravageur")):
            return True
    return False


def _compute_surface_info(query_text: str, et0: float) -> str:
    if not query_text or et0 <= 0:
        return ""

    ha_match = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:ha|hectare)", query_text)
    if ha_match:
        try:
            val = float(ha_match.group(1).replace(",", "."))
            loss = val * 10000 * et0
            return f"CALCUL: Pour {val}ha, perte eau = {loss:,.0f} L/jour."
        except ValueError:
            return ""
    return ""


async def analyze_node(state: SentinelState, runtime: SentinelRuntime) -> Dict[str, Any]:
    query = state.get("user_query", "")
    location = state.get("location_profile", {}) or {}
    warnings = list(state.get("warnings", []))

    match = re.search(r"prix|cours|vente|achat", query, re.IGNORECASE)
    if match:
        return {"handoff_to": "MarketCoach", "handoff_reason": "Query is about market prices"}

    try:
        weather = state.get("weather_snapshot") or runtime.tools._fetch_real_weather(location)
        satellite = state.get("satellite_signals") or {}
    except Exception as e:
        logger.error("Sensor fetch failed: %s", e)
        weather = {}
        satellite = {}
        warnings.append("Données météo temps réel indisponibles.")

    metrics = runtime.tools._compute_metrics(weather, satellite)
    flood = runtime.tools._assess_flood_risk(weather, satellite, location)
    hazards = runtime.tools._derive_hazards(metrics, flood)

    crop_profile = None
    q_norm = query.lower()
    if "maïs" in q_norm or "mais" in q_norm:
        crop_profile = runtime.crop_tools.get_math_profile("maïs")
    elif "niebe" in q_norm:
        crop_profile = runtime.crop_tools.get_math_profile("niébé")

    advice = runtime.tools.synthesize_agronomic_advice(metrics, crop_profile)
    risk_summary = "\n".join([f"- {h['label']}: {h['explanation']}" for h in hazards]) or "Aucun risque majeur."

    return {
        "weather_snapshot": weather,
        "raw_metrics": metrics,
        "flood_risk": flood,
        "hazards": hazards,
        "risk_summary": risk_summary,
        "agronomic_advice": advice,
        "warnings": warnings,
        "status": "ANALYZED",
    }


async def retrieve_node(state: SentinelState, runtime: SentinelRuntime) -> Dict[str, Any]:
    hazards = state.get("hazards", [])
    if not hazards:
        return {"retrieved_context": "Pas d'aléas nécessitant un protocole complexe."}
    return {"retrieved_context": "", "status": "RETRIEVED"}


async def generate_node(state: SentinelState, runtime: SentinelRuntime) -> Dict[str, Any]:
    if state.get("handoff_to"):
        return {}

    ctx = ComposeContext(
        query=state.get("user_query", ""),
        location=str(state.get("location_profile", {}).get("zone", "Burkina Faso")),
        risk_summary=state.get("risk_summary", ""),
        metrics=state.get("raw_metrics", {}),
        flood=state.get("flood_risk", {}),
        hazards=state.get("hazards", []),
        context=state.get("retrieved_context", ""),
        agronomic_advice=state.get("agronomic_advice", {}),
        surface_calc_info=_compute_surface_info(state.get("user_query", ""), state.get("raw_metrics", {}).get("et0_mm", 0)),
    )

    sys_prompt = SENTINELLE_SYSTEM_TEMPLATE
    user_prompt = SENTINELLE_USER_TEMPLATE.format(
        current_date_str=datetime.now().strftime("%d/%m/%Y"),
        query=ctx.query,
        location=ctx.location,
        risk_summary=ctx.risk_summary,
        metrics_json=json.dumps(ctx.metrics, ensure_ascii=False),
        agronomic_advice=json.dumps(ctx.agronomic_advice, ensure_ascii=False),
        flood_data=json.dumps(ctx.flood, ensure_ascii=False),
        hazard_json=json.dumps(ctx.hazards, ensure_ascii=False),
        context=ctx.context,
        surface_calc_info=ctx.surface_calc_info,
    )

    try:
        resp = runtime.llm.chat.completions.create(
            model=runtime.model_answer,
            messages=[{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_prompt}],
            temperature=0.3,
        )
        answer = resp.choices[0].message.content
        new_state = {"final_response": answer, "status": "GENERATED"}

        if _purchase_handoff_needed(ctx.query, ctx.hazards):
            new_state["start_handoff_flow"] = True
            new_state["handoff_to"] = "MarketCoach"
            new_state["handoff_reason"] = "Traitement phytosanitaire recommandé"
        return new_state
    except Exception as e:
        logger.error("Generation failed: %s", e)
        return {"final_response": "Service météo momentanément indisponible.", "status": "ERROR"}


async def finalize_node(state: SentinelState, runtime: SentinelRuntime) -> Dict[str, Any]:
    if state.get("handoff_to"):
        return {}

    return {
        "agri_response": {"response": state.get("final_response"), "hazards": state.get("hazards")},
        "status": "SUCCESS",
    }


async def audit_node(state: SentinelState, runtime: SentinelRuntime) -> Dict[str, Any]:
    if runtime.persister is None:
        return {}
    if not state.get("hazards"):
        return {}

    try:
        runtime.persister.db.log_audit_action(
            agent_name="ClimateSentinel",
            action_type="ALERT_SENT",
            user_id=state.get("location_profile", {}).get("user_id", "anon"),
            protocol="RISK_PROTOCOL",
            payload={"hazards": state.get("hazards"), "location": state.get("location_profile")},
            resource="meteo_api",
            confidence=1.0,
        )
    except Exception:
        logger.warning("Audit write failed for sentinelle")

    return {}


__all__ = [
    "SentinelConfig",
    "SentinelRuntime",
    "build_runtime",
    "analyze_node",
    "retrieve_node",
    "generate_node",
    "finalize_node",
    "audit_node",
    "SentinelState",
    "SENTINELLE_SYSTEM_TEMPLATE",
    "SENTINELLE_USER_TEMPLATE",
    "STYLE_GUIDANCE",
]
