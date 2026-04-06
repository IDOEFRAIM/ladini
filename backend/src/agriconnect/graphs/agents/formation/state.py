"""Local state for Formation sub-graph."""

from typing import Any, Dict, List, Optional, TypedDict


class FormationState(TypedDict, total=False):
	# Input
	user_query: str
	learner_profile: Dict[str, Any]

	# Internal Reasoning
	intent: str
	urgency: str
	focus_topics: List[str]
	field_actions: List[str]
	safety_flags: List[str]
	optimized_query: str

	# Knowledge Retrieval
	retrieved_context: str
	sources: List[Dict[str, Any]]

	# Draft & Refine
	learning_modules: List[str]
	prerequisites: List[str]
	reasoning: str
	answer_draft: str
	evaluation: Dict[str, float]

	# Final Output
	final_response: str
	agri_response: Optional[Dict[str, Any]]
	expert_responses: List[Dict[str, Any]]
	concepts_appris: List[str]

	# Status & Guards
	status: str
	warnings: List[str]
	critique_retry_count: int
	rewrited_retry_count: int
	degraded_mode: bool
	requires_human: bool
	required_domain: str
	handoff_to: str
	handoff_reason: str
	clarification_needed: str


__all__ = ["FormationState"]
