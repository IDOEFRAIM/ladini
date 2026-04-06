"""Local state for Marketplace v3 sub-graph."""

from typing import Any, Dict, List, Optional, TypedDict


class MarketplaceState(TypedDict, total=False):
	user_query: str
	user_phone: str
	zone_id: Optional[str]

	user_profile: Dict[str, Any]
	producer_id: Optional[str]
	farm_id: Optional[str]
	trust_score: Optional[Dict[str, Any]]

	intent: str
	parsed: Dict[str, Any]

	price_check: Optional[Dict[str, Any]]
	validation_warnings: List[str]
	requires_human_review: bool
	audit_signature: Optional[str]

	action_result: Dict[str, Any]
	status: str

	final_response: str
	agri_response: Optional[Dict[str, Any]]

	errors: List[str]
	retry_counter: int


__all__ = ["MarketplaceState"]
