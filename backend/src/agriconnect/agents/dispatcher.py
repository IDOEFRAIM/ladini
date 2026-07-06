"""Generic dispatcher primitives shared by conversational agents."""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Iterable, Literal, Optional, Tuple

ActionHandler = Callable[[Dict[str, Any], "ActionContext"], "HandlerReturn"]
HandlerReturn = Awaitable["PreparedAction"] | "PreparedAction"


@dataclass(slots=True)
class ActionContext:
    """Context provided to action handlers during preparation."""

    user_id: Optional[str] = None
    user_phone: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    state: Optional[Dict[str, Any]] = None


@dataclass(slots=True)
class PreparedAction:
    """Normalized representation of an action ready for execution."""

    transport: Literal["mcp", "db", "noop"] = "mcp"
    target: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    fallbacks: Tuple["PreparedAction", ...] = ()

    def with_fallback(self, fallback: "PreparedAction") -> "PreparedAction":
        return PreparedAction(
            transport=self.transport,
            target=self.target,
            payload=dict(self.payload),
            metadata=dict(self.metadata),
            fallbacks=self.fallbacks + (fallback,),
        )


@dataclass(slots=True)
class ActionSpec:
    """Declarative metadata for an intent/action mapping."""

    name: str
    mode: Literal["READ", "WRITE", "UTILITY"] = "READ"
    handler: ActionHandler = lambda payload, ctx: PreparedAction(target="")  # type: ignore
    description: str = ""
    required_fields: Tuple[str, ...] = ()
    metadata: Dict[str, Any] = field(default_factory=dict)


class BaseAgentDispatcher:
    """Registry + preparation helper shared by Market & Formation agents."""

    def __init__(self, agent_name: str) -> None:
        self.agent_name = agent_name
        self._actions: Dict[str, ActionSpec] = {}

    # ------------------------------------------------------------------
    # Registration & lookup
    # ------------------------------------------------------------------
    def register_action(self, intent: str, spec: ActionSpec) -> None:
        key = (intent or "").upper()
        if not key:
            raise ValueError("intent name must be a non-empty string")
        if key in self._actions:
            raise ValueError(f"Action '{intent}' already registered for {self.agent_name}")
        self._actions[key] = spec

    def has_action(self, intent: str) -> bool:
        return (intent or "").upper() in self._actions

    def get_action(self, intent: str) -> ActionSpec:
        key = (intent or "").upper()
        if key not in self._actions:
            raise KeyError(f"Unknown action '{intent}' for agent {self.agent_name}")
        return self._actions[key]

    def keys(self, *, mode: Optional[str] = None) -> Iterable[str]:
        if mode is None:
            return list(self._actions.keys())
        mode_up = mode.upper()
        return [name for name, spec in self._actions.items() if spec.mode == mode_up]

    def iter_actions(self, *, mode: Optional[str] = None) -> Iterable[ActionSpec]:
        if mode is None:
            return list(self._actions.values())
        mode_up = mode.upper()
        return [spec for spec in self._actions.values() if spec.mode == mode_up]

    # ------------------------------------------------------------------
    # Preparation helpers
    # ------------------------------------------------------------------
    async def prepare(
        self,
        intent: str,
        payload: Dict[str, Any],
        context: Optional[ActionContext] = None,
    ) -> PreparedAction:
        spec = self.get_action(intent)
        ctx = context or ActionContext()
        result = spec.handler(payload, ctx)
        if inspect.isawaitable(result):
            result = await result  # type: ignore[assignment]
        if not isinstance(result, PreparedAction):
            raise TypeError(
                f"Handler for '{intent}' did not return PreparedAction (got {type(result)!r})"
            )
        return result


async def _execute_single(
    prepared: PreparedAction,
    *,
    mcp_runtime: Any = None,
    db_service: Any = None,
) -> Any:
    if prepared.transport == "noop":
        return None
    if prepared.transport == "mcp":
        if mcp_runtime is None or not hasattr(mcp_runtime, "call_db"):
            raise RuntimeError("MCP runtime unavailable for MCP action")
        return await mcp_runtime.call_db(prepared.target, **prepared.payload)
    if prepared.transport == "db":
        if db_service is None:
            raise RuntimeError("Database service unavailable for DB action")
        method = getattr(db_service, prepared.target, None)
        if method is None:
            raise AttributeError(f"Database service missing method '{prepared.target}'")
        return await method(**prepared.payload)
    raise ValueError(f"Unsupported transport '{prepared.transport}'")


async def execute_prepared_action(
    prepared: PreparedAction,
    *,
    mcp_runtime: Any = None,
    db_service: Any = None,
) -> Any:
    """Executes a prepared action with automatic fallback handling."""

    try:
        return await _execute_single(prepared, mcp_runtime=mcp_runtime, db_service=db_service)
    except Exception as exc:
        if not prepared.fallbacks:
            raise
        last_error = exc
        for fallback in prepared.fallbacks:
            try:
                return await _execute_single(fallback, mcp_runtime=mcp_runtime, db_service=db_service)
            except Exception as fb_exc:  # pragma: no cover - diagnostics only
                last_error = fb_exc
                continue
        raise last_error
