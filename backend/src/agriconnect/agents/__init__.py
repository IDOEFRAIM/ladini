"""Common agent utilities for AgriConnect — DRY architecture.

Modules:
  - dispatcher: intent→action preparation & execution
  - gateway: unified MCP/DB data access
  - identity: phone-first user resolution
  - onboarding: step-based new user registration
  - forms: DRY slot-filling form engine
"""

from .dispatcher import (
    ActionContext,
    ActionSpec,
    BaseAgentDispatcher,
    PreparedAction,
    execute_prepared_action,
)
from .forms import (
    AUCTION_FORM,
    CROP_CYCLE_FORM,
    FORM_REGISTRY,
    PRODUCT_FORM,
    FormSpec,
    FormStepResult,
    SlotSpec,
    run_form_step,
)
from .gateway import DataGateway
from .identity import (
    UserIdentity,
    extract_phone_from_state,
    extract_user_id_from_state,
    resolve_identity,
)
from .onboarding import (
    OnboardingResult,
    OnboardingState,
    OnboardingStep,
    run_onboarding_step,
)
from .reducers import (
    _KEEP,
    merge_dict,
    replace_list,
    replace_value,
)

__all__ = [
    # Dispatcher
    "ActionContext",
    "ActionSpec",
    "BaseAgentDispatcher",
    "PreparedAction",
    "execute_prepared_action",
    # Gateway
    "DataGateway",
    # Identity
    "UserIdentity",
    "extract_phone_from_state",
    "extract_user_id_from_state",
    "resolve_identity",
    # Onboarding
    "OnboardingResult",
    "OnboardingState",
    "OnboardingStep",
    "run_onboarding_step",
    # Forms (DRY slot-filling)
    "FormSpec",
    "FormStepResult",
    "SlotSpec",
    "run_form_step",
    "PRODUCT_FORM",
    "AUCTION_FORM",
    "CROP_CYCLE_FORM",
    "FORM_REGISTRY",
    # Reducers (shared state primitives)
    "_KEEP",
    "merge_dict",
    "replace_list",
    "replace_value",
]
