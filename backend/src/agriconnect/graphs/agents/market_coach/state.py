"""Local state for MarketCoach sub-graph."""

import operator
from typing import Any, Dict, List, Optional, TypedDict, Annotated


def merge_fields(old_value: Any, new_value: Any) -> Any:
	if new_value is None:
		return old_value
	if isinstance(new_value, str) and not new_value.strip():
		return old_value
	return new_value


def merge_intent(old_value: Any, new_value: Any) -> Any:
	transactional_intents = {"REGISTER_SURPLUS", "CREATE_PRODUCT", "BUY_OFFER"}
	if new_value is None:
		return old_value
	if isinstance(new_value, str) and not new_value.strip():
		return old_value
	if str(new_value).upper() == "CHECK_PRICE" and str(old_value).upper() in transactional_intents:
		return old_value
	return new_value


class MarketAgentState(TypedDict, total=False):
	user_query: str
	user_profile: Dict[str, Any]
	user_level: str

	intent: Annotated[Optional[str], merge_intent]
	product: Annotated[Any, merge_fields]
	location: Annotated[Any, merge_fields]
	price_mentioned: Annotated[Optional[float], merge_fields]
	quantity_mentioned: Annotated[Optional[float], merge_fields]
	unit_mentioned: Annotated[Optional[str], merge_fields]
	normalized_quantity_kg: Optional[float]

	market_data: Dict[str, Any]
	scam_analysis: Dict[str, Any]
	final_response: str
	status: str

	warnings: Annotated[List[str], operator.add]
	missing_fields: Annotated[List[str], operator.add]
	validation_errors: Annotated[List[str], operator.add]
	validation_warnings: Annotated[List[str], operator.add]

	waiting_for_confirmation: bool
	transaction_payload: Dict[str, Any]
	transaction_hash: str
	audio_file_path: Optional[str]

	security_status: str
	security_reason: str
	requires_human: bool
	handoff_to: str
	clarification_needed: str
	pending_user_intent: Annotated[str, merge_fields]
	draft_data: Annotated[Dict[str, Any], merge_fields]
	proposed_action: Dict[str, Any]


__all__ = ["MarketAgentState", "merge_fields", "merge_intent"]
