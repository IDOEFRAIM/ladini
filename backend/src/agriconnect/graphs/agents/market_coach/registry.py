"""Plugin registry for MarketCoach action handlers."""
from __future__ import annotations

import importlib
import importlib.util
import pkgutil
import sys
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Tuple, Union

from agriconnect.graphs.agents.market_coach.interpreter.intent import INTENT_CONFIG
ActionCallable = Callable[[Mapping[str, Any], Mapping[str, Any]], Tuple[str, Dict[str, Any]]]


@dataclass(frozen=True)
class ActionRegistration:
    intent: str
    is_write: bool
    lifecycle_mode: str
    handler: ActionCallable


REGISTRY: Dict[str, ActionRegistration] = {}
_ACTIONS_PACKAGE = "agriconnect.graphs.agents.market_coach.actions"


def register_action(
    intent_name: str,
    mode: Union[str, bool, None] = None,
    *,
    is_write: Optional[bool] = None,
):
    """Decorator used by action modules to register themselves."""

    def decorator(func: ActionCallable) -> ActionCallable:
        intent_key = (intent_name or "").upper().strip()
        if not intent_key:
            raise ValueError("Intent name must be provided for register_action")

        if is_write is not None:
            mode_flag = bool(is_write)
        elif isinstance(mode, str):
            mode_flag = mode.upper().strip() == "WRITE"
        elif isinstance(mode, bool):
            mode_flag = mode
        else:
            mode_flag = False

        cfg = INTENT_CONFIG.get(intent_key, {})
        lifecycle_mode = str(cfg.get("lifecycle_mode") or ("WRITE" if mode_flag else "READ"))
        lifecycle_mode = lifecycle_mode.upper().strip()
        if lifecycle_mode not in {"CREATE", "UPDATE", "READ"}:
            lifecycle_mode = "READ" if not mode_flag else "CREATE"

        entry = ActionRegistration(
            intent=intent_key,
            is_write=mode_flag,
            lifecycle_mode=lifecycle_mode,
            handler=func,
        )
        existing = REGISTRY.get(intent_key)
        if existing and existing.handler is not func:
            raise RuntimeError(f"Action already registered for intent {intent_key}")
        REGISTRY[intent_key] = entry
        return func

    return decorator


def load_all_actions(force_reload: bool = False) -> None:
    """Dynamically import every module under the actions package."""
    importlib.invalidate_caches()
    package = importlib.import_module(_ACTIONS_PACKAGE)
    if force_reload:
        REGISTRY.clear()

    for module_info in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
        module_name = module_info.name
        if module_name in sys.modules:
            if force_reload:
                importlib.reload(sys.modules[module_name])
            continue
        importlib.import_module(module_name)


def get_action(intent: str) -> Optional[ActionRegistration]:
    return REGISTRY.get((intent or "").upper())


def iter_actions():
    return REGISTRY.items()


def prepare_market_action(
    intent: str,
    state: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> Tuple[str, Dict[str, Any]]:
    """Resolve the handler for *intent* and return MCP call tuple."""
    registration = get_action(intent)
    if registration is None:
        raise ValueError(f"No MarketCoach action registered for intent '{intent}'.")

    tool_name, tool_args = registration.handler(state, payload)
    if not isinstance(tool_args, dict):
        tool_args = dict(tool_args or {})
    return str(tool_name), dict(tool_args)


def validate_integrity() -> None:
    """Ensure INTENT_CONFIG and the action registry stay in perfect sync."""
    intent_keys = frozenset(
        (key or "").upper()
        for key, cfg in INTENT_CONFIG.items()
        if not (cfg or {}).get("handled_by_flow")
    )
    registry_keys = frozenset(REGISTRY.keys())

    missing = sorted(intent_keys - registry_keys)
    orphan = sorted(registry_keys - intent_keys)

    if missing or orphan:
        message_lines = ["MarketCoach action registry integrity violation detected."]
        if missing:
            message_lines.append("Missing handlers: " + ", ".join(missing))
        if orphan:
            message_lines.append("Orphan handlers: " + ", ".join(orphan))
        raise RuntimeError("\n".join(message_lines))


__all__ = [
    "ActionCallable",
    "ActionRegistration",
    "REGISTRY",
    "register_action",
    "load_all_actions",
    "get_action",
    "iter_actions",
    "prepare_market_action",
    "validate_integrity",
]
