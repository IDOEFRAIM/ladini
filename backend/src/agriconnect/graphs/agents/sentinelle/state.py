"""Local state for Sentinelle sub-graph."""

from typing import Any, Dict, List, Optional, TypedDict


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
	agronomic_advice: Dict[str, Any]
	start_handoff_flow: bool

	optimized_query: str
	retrieved_context: str
	sources: List[Dict[str, Any]]

	final_response: str
	agri_response: Optional[Dict[str, Any]]

	status: str
	warnings: List[str]
	security_status: str
	security_reason: str
	handoff_to: str
	handoff_reason: str
	evaluation: Dict[str, float]


__all__ = ["SentinelState"]
