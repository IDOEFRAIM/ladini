"""Plugin registry for MarketCoach action handlers."""

from __future__ import annotations

import importlib
import importlib.util
import json
import pkgutil
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Tuple,
    Type,
    Union,
)

from agriconnect.graphs.agents.market_coach.actions.tooling import ToolId, ToolResolver
from agriconnect.graphs.agents.market_coach.interpreter.intent import INTENT_CONFIG

ActionCallable = Callable[
    [Mapping[str, Any], Mapping[str, Any]], Tuple[str, Dict[str, Any]]
]


class DuplicateIntentError(RuntimeError):
    """Raised when two different handlers register the same intent/version."""


class InvalidRegistrationError(RuntimeError):
    """Raised when an action registration is structurally invalid."""


@dataclass
class ActionMetrics:
    """Lightweight metrics bucket for a single action (all versions)."""

    calls: int = 0
    errors: int = 0
    total_duration_ms: float = 0.0

    def record(self, duration_ms: float, error: bool) -> None:
        self.calls += 1
        self.total_duration_ms += float(duration_ms)
        if error:
            self.errors += 1

    @property
    def average_duration_ms(self) -> float:
        return self.total_duration_ms / self.calls if self.calls else 0.0


@dataclass
class TraceContext:
    """Per-execution trace metadata for observability and correlation."""

    trace_id: str
    action_id: str
    intent: str
    version: int
    handler_name: str
    tool_name: Optional[str] = None
    status: str = "PENDING"
    duration_ms: Optional[float] = None


@dataclass
class DomainEvent:
    """Base type for domain events (not yet published)."""

    name: str
    payload: Dict[str, Any]
    trace_id: str


class ActionStarted(DomainEvent):
    pass


class ActionCompleted(DomainEvent):
    pass


class ActionFailed(DomainEvent):
    pass


@dataclass
class ActionRegistration:
    intent: str
    is_write: bool
    lifecycle_mode: str
    handler: ActionCallable
    # --- Versioning & lifecycle ---
    version: int = 1
    deprecated: bool = False
    # --- Tool abstraction ---
    tool_id: Optional[ToolId] = None
    # --- Documentation & classification ---
    description: Optional[str] = None
    capability: Optional[str] = None
    permissions: Tuple[str, ...] = field(default_factory=tuple)
    # --- Runtime behaviour hints ---
    timeout_seconds: Optional[float] = None
    max_retries: int = 0
    payload_model: Optional[Type[Any]] = None
    response_model: Optional[Type[Any]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


REGISTRY: Dict[str, ActionRegistration] = {}
_ACTIONS_PACKAGE = "agriconnect.graphs.agents.market_coach.actions"
_METRICS: Dict[str, ActionMetrics] = {}


@dataclass
class ActionContext:
    """Execution context passed through middleware and hooks."""

    registration: ActionRegistration
    state: Mapping[str, Any]
    payload: Mapping[str, Any]
    trace: TraceContext
    metrics: ActionMetrics
    events: List[DomainEvent] = field(default_factory=list)


class PermissionResolver:
    """Simple permission engine stub.

    For now this always returns True, but it inspects the registration
    so that richer logic can be plugged later without touching handlers.
    """

    @staticmethod
    def can_execute(ctx: ActionContext) -> bool:
        # Hook for future enforcement; currently permissive by design.
        # ``ctx.registration.permissions`` contains the declared permissions.
        return True


MiddlewareCallable = Callable[
    [ActionContext, Callable[[], Tuple[str, Dict[str, Any]]]],
    Tuple[str, Dict[str, Any]],
]

_MIDDLEWARES: List[MiddlewareCallable] = []
_BEFORE_HOOKS: List[Callable[[ActionContext], None]] = []
_AFTER_HOOKS: List[Callable[[ActionContext, str, Dict[str, Any]], None]] = []
_ERROR_HOOKS: List[Callable[[ActionContext, Exception], None]] = []


def register_action(
    intent_name: str,
    mode: Union[str, bool, None] = None,
    *,
    is_write: Optional[bool] = None,
    version: int = 1,
    deprecated: bool = False,
    tool_id: Optional[Union[ToolId, str]] = None,
    description: Optional[str] = None,
    capability: Optional[str] = None,
    permissions: Optional[Iterable[str]] = None,
    timeout_seconds: Optional[float] = None,
    max_retries: int = 0,
    payload_model: Optional[Type[Any]] = None,
    response_model: Optional[Type[Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
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
        lifecycle_mode = str(
            cfg.get("lifecycle_mode") or ("WRITE" if mode_flag else "READ")
        )
        lifecycle_mode = lifecycle_mode.upper().strip()
        if lifecycle_mode not in {"CREATE", "UPDATE", "READ"}:
            lifecycle_mode = "READ" if not mode_flag else "CREATE"

        # Normalise version
        try:
            version_int = int(version)
        except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
            raise InvalidRegistrationError(
                f"Invalid version for intent {intent_key}: {version!r}"
            ) from exc
        if version_int < 1:
            raise InvalidRegistrationError(
                f"Version must be >= 1 for intent {intent_key}"
            )

        # Normalise tool_id (optional in Phase 1 — legacy handlers use raw strings)
        tool_id_norm: Optional[ToolId]
        if isinstance(tool_id, ToolId):
            tool_id_norm = tool_id
        elif isinstance(tool_id, str) and tool_id:
            # Soft mapping: allow passing underlying MCP name by value
            try:
                tool_id_norm = ToolId(tool_id)
            except ValueError:
                # Unknown value — keep None, handler will likely return a raw string.
                tool_id_norm = None
        else:
            tool_id_norm = None

        entry = ActionRegistration(
            intent=intent_key,
            is_write=mode_flag,
            lifecycle_mode=lifecycle_mode,
            handler=func,
            version=version_int,
            deprecated=bool(deprecated),
            tool_id=tool_id_norm,
        )
        existing = REGISTRY.get(intent_key)
        if existing and existing.handler is not func:
            raise DuplicateIntentError(
                f"Action already registered for intent {intent_key}"
            )
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
    """Resolve the handler for *intent* and return MCP call tuple.

    Phase 1: still fully compatible with legacy handlers returning raw
    string tool names, but now routes the name through ``ToolResolver``
    so ToolId enums can be introduced incrementally.
    """

    registration = get_action(intent)
    if registration is None:
        raise ValueError(f"No MarketCoach action registered for intent '{intent}'.")
    metrics = _METRICS.setdefault(registration.intent, ActionMetrics())

    trace = TraceContext(
        trace_id=str(uuid.uuid4()),
        action_id=f"{registration.intent}:v{registration.version}",
        intent=registration.intent,
        version=registration.version,
        handler_name=f"{registration.handler.__module__}.{registration.handler.__name__}",
    )
    ctx = ActionContext(
        registration=registration,
        state=state,
        payload=payload,
        trace=trace,
        metrics=metrics,
    )

    # Permission check (currently permissive but centrally pluggable)
    if not PermissionResolver.can_execute(ctx):
        raise PermissionError(f"Execution not allowed for intent {registration.intent}")

    for hook in _BEFORE_HOOKS:
        hook(ctx)

    start = time.perf_counter()
    error: Optional[Exception] = None

    def _call_handler() -> Tuple[str, Dict[str, Any]]:
        raw_tool_name, raw_tool_args = registration.handler(ctx.state, ctx.payload)
        if not isinstance(raw_tool_args, dict):
            raw_tool_args = dict(raw_tool_args or {})
        resolved_name = ToolResolver.resolve_name(raw_tool_name)
        trace.tool_name = resolved_name
        return resolved_name, raw_tool_args

    # Build middleware pipeline (outermost first)
    def _build_pipeline(i: int) -> Callable[[], Tuple[str, Dict[str, Any]]]:
        if i >= len(_MIDDLEWARES):
            return _call_handler

        def _next() -> Tuple[str, Dict[str, Any]]:
            middleware = _MIDDLEWARES[i]
            return middleware(ctx, _build_pipeline(i + 1))

        return _next

    pipeline = _build_pipeline(0)

    try:
        tool_name, tool_args = pipeline()
        status = "OK"
    except Exception as exc:  # noqa: BLE001
        error = exc
        status = "ERROR"
        for hook in _ERROR_HOOKS:
            hook(ctx, exc)
        raise
    finally:
        duration_ms = (time.perf_counter() - start) * 1000.0
        trace.duration_ms = duration_ms
        trace.status = status
        metrics.record(duration_ms, error is not None)

    for hook in _AFTER_HOOKS:
        hook(ctx, tool_name, tool_args)

    return tool_name, dict(tool_args)


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


def register_middleware(middleware: MiddlewareCallable) -> None:
    """Register a new middleware in the execution pipeline.

    Middlewares are executed in registration order.
    """

    _MIDDLEWARES.append(middleware)


def on_before_action(hook: Callable[[ActionContext], None]) -> None:
    _BEFORE_HOOKS.append(hook)


def on_after_action(hook: Callable[[ActionContext, str, Dict[str, Any]], None]) -> None:
    _AFTER_HOOKS.append(hook)


def on_error(hook: Callable[[ActionContext, Exception], None]) -> None:
    _ERROR_HOOKS.append(hook)


def describe() -> Dict[str, Any]:
    """Return a structured description of the current registry.

    This is suitable for auto-documentation and monitoring endpoints.
    """

    actions: List[Dict[str, Any]] = []
    for intent, reg in sorted(REGISTRY.items(), key=lambda item: item[0]):
        metrics = _METRICS.get(intent)
        actions.append(
            {
                "intent": reg.intent,
                "version": reg.version,
                "description": reg.description,
                "mode": reg.lifecycle_mode,
                "is_write": reg.is_write,
                "capability": reg.capability,
                "permissions": list(reg.permissions),
                "payload_model": getattr(reg.payload_model, "__name__", None),
                "response_model": getattr(reg.response_model, "__name__", None),
                "lifecycle": {
                    "deprecated": reg.deprecated,
                    "timeout_seconds": reg.timeout_seconds,
                    "max_retries": reg.max_retries,
                },
                "handler": f"{reg.handler.__module__}.{reg.handler.__name__}",
                "tool": str(reg.tool_id.value) if reg.tool_id else None,
                "metadata": dict(reg.metadata),
                "metrics": {
                    "calls": metrics.calls if metrics else 0,
                    "errors": metrics.errors if metrics else 0,
                    "average_duration_ms": metrics.average_duration_ms
                    if metrics
                    else 0.0,
                },
            }
        )

    return {"actions": actions}


def describe_json(indent: Optional[int] = 2) -> str:
    """Return registry description as JSON string."""

    return json.dumps(describe(), indent=indent, sort_keys=True)


def actions(capability: Optional[str] = None) -> List[ActionRegistration]:
    """List actions, optionally filtered by capability."""

    if not capability:
        return list(REGISTRY.values())
    cap_key = capability.upper().strip()
    return [
        reg
        for reg in REGISTRY.values()
        if (reg.capability or "").upper().strip() == cap_key
    ]


__all__ = [
    "ActionCallable",
    "ActionRegistration",
    "ActionMetrics",
    "TraceContext",
    "DomainEvent",
    "ActionStarted",
    "ActionCompleted",
    "ActionFailed",
    "ActionContext",
    "PermissionResolver",
    "MiddlewareCallable",
    "DuplicateIntentError",
    "InvalidRegistrationError",
    "REGISTRY",
    "register_action",
    "load_all_actions",
    "get_action",
    "iter_actions",
    "prepare_market_action",
    "validate_integrity",
    "register_middleware",
    "on_before_action",
    "on_after_action",
    "on_error",
    "describe",
    "describe_json",
    "actions",
]
