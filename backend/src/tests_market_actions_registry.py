from __future__ import annotations

from typing import Mapping, Any, Dict, List

from agriconnect.graphs.agents.market_coach.actions.tooling import (
    ToolId,
    ToolResolver,
    ToolResolutionError,
)
from agriconnect.graphs.agents.market_coach.registry import (
    ActionContext,
    ActionRegistration,
    ActionMetrics,
    ActionStarted,
    DuplicateIntentError,
    InvalidRegistrationError,
    MiddlewareCallable,
    PermissionResolver,
    REGISTRY,
    actions,
    describe,
    describe_json,
    on_after_action,
    on_before_action,
    on_error,
    prepare_market_action,
    register_action,
    register_middleware,
)


def _dummy_state() -> Mapping[str, Any]:
    return {"user_phone": "+22600000000"}


def _dummy_payload() -> Mapping[str, Any]:
    return {"foo": "bar"}


def test_tool_resolver_accepts_raw_string() -> None:
    name = ToolResolver.resolve_name("create_auction")
    assert name == "create_auction"


def test_tool_resolver_resolves_enum_value() -> None:
    name = ToolResolver.resolve_name(ToolId.CREATE_AUCTION)
    assert name == "create_auction"


def test_tool_resolver_rejects_none() -> None:
    try:
        ToolResolver.resolve_name(None)  # type: ignore[arg-type]
    except ToolResolutionError:
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("Expected ToolResolutionError for None")


def test_register_action_stores_metadata_and_prepares_market_action() -> None:
    REGISTRY.clear()

    @register_action("TEST_INTENT", mode="WRITE", version=2, deprecated=True, tool_id=ToolId.CREATE_AUCTION)
    def handler(state: Mapping[str, Any], payload: Mapping[str, Any]):  # type: ignore[override]
        return ToolId.CREATE_AUCTION, {"foo": "bar"}

    reg = REGISTRY.get("TEST_INTENT")
    assert reg is not None
    assert isinstance(reg, ActionRegistration)
    assert reg.version == 2
    assert reg.deprecated is True
    assert reg.tool_id == ToolId.CREATE_AUCTION

    tool_name, tool_args = prepare_market_action("TEST_INTENT", _dummy_state(), _dummy_payload())
    assert tool_name == "create_auction"
    assert tool_args == {"foo": "bar"}


def test_register_action_duplicate_intent_raises() -> None:
    REGISTRY.clear()

    @register_action("DUP_INTENT", mode="READ")
    def handler1(state: Mapping[str, Any], payload: Mapping[str, Any]):  # type: ignore[override]
        return "noop_tool", {}

    try:
        @register_action("DUP_INTENT", mode="READ")
        def handler2(state: Mapping[str, Any], payload: Mapping[str, Any]):  # type: ignore[override]
            return "noop_tool", {}
    except DuplicateIntentError:
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("Expected DuplicateIntentError for duplicate intent registration")


def test_register_action_invalid_version_raises() -> None:
    REGISTRY.clear()

    try:
        @register_action("BAD_VERSION", mode="READ", version=0)
        def handler(state: Mapping[str, Any], payload: Mapping[str, Any]):  # type: ignore[override]
            return "noop_tool", {}
    except InvalidRegistrationError:
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("Expected InvalidRegistrationError for version < 1")


def test_describe_and_actions_capability_filter() -> None:
    REGISTRY.clear()

    @register_action(
        "PROCUREMENT_CREATE_REQUEST",
        mode="WRITE",
        capability="PROCUREMENT",
        description="Create a procurement request",
        permissions=["market.write"],
    )
    def create_request(state: Mapping[str, Any], payload: Mapping[str, Any]):  # type: ignore[override]
        return "create_auction", {"amount": 1}

    @register_action(
        "MARKETPLACE_LIST_OFFERS",
        mode="READ",
        capability="MARKETPLACE",
        description="List marketplace offers",
        permissions=["market.read"],
    )
    def list_offers(state: Mapping[str, Any], payload: Mapping[str, Any]):  # type: ignore[override]
        return "list_offers", {}

    doc = describe()
    assert "actions" in doc
    assert len(doc["actions"]) == 2
    intents = {a["intent"] for a in doc["actions"]}
    assert "PROCUREMENT_CREATE_REQUEST" in intents
    assert "MARKETPLACE_LIST_OFFERS" in intents

    # JSON export should be valid JSON
    json_text = describe_json()
    assert isinstance(json_text, str)
    assert "PROCUREMENT_CREATE_REQUEST" in json_text

    # Capability filter
    procurement_actions: List[ActionRegistration] = actions(capability="PROCUREMENT")
    assert len(procurement_actions) == 1
    assert procurement_actions[0].intent == "PROCUREMENT_CREATE_REQUEST"


def test_middleware_and_hooks_and_metrics_are_invoked() -> None:
    REGISTRY.clear()
    # NOTE: middlewares and hooks are global and not cleared here on purpose;
    # tests are written so that re-registration is harmless.

    events: Dict[str, int] = {"before": 0, "after": 0, "error": 0, "middleware": 0}

    def before_hook(ctx: ActionContext) -> None:
        events["before"] += 1

    def after_hook(ctx: ActionContext, tool_name: str, tool_args: Dict[str, Any]) -> None:
        events["after"] += 1

    def error_hook(ctx: ActionContext, exc: Exception) -> None:  # noqa: ARG001
        events["error"] += 1

    def middleware(ctx: ActionContext, call_next: MiddlewareCallable):
        events["middleware"] += 1
        return call_next()

    on_before_action(before_hook)
    on_after_action(after_hook)
    on_error(error_hook)
    register_middleware(middleware)

    @register_action("OBS_INTENT", mode="READ", capability="ANALYTICS")
    def obs_handler(state: Mapping[str, Any], payload: Mapping[str, Any]):  # type: ignore[override]
        # No error
        return "noop_tool", {}

    # First call populates metrics and fires hooks/middleware
    tool_name, tool_args = prepare_market_action("OBS_INTENT", _dummy_state(), _dummy_payload())
    assert tool_name == "noop_tool"
    assert tool_args == {}

    # Metrics must have at least one call
    reg = REGISTRY["OBS_INTENT"]
    metrics = ActionMetrics()
    # we cannot access the internal metrics dict directly here, so we
    # rely on describe(), which exposes metrics.
    doc = describe()
    obs_docs = [a for a in doc["actions"] if a["intent"] == "OBS_INTENT"]
    assert len(obs_docs) == 1
    assert obs_docs[0]["metrics"]["calls"] >= 1

    # Hooks and middleware must have been invoked at least once
    assert events["before"] >= 1
    assert events["after"] >= 1
    assert events["middleware"] >= 1
    # No error on success path
    assert events["error"] == 0


def test_permission_resolver_allows_execution() -> None:
    REGISTRY.clear()

    @register_action("PERM_INTENT", mode="READ", permissions=["market.read"])
    def perm_handler(state: Mapping[str, Any], payload: Mapping[str, Any]):  # type: ignore[override]
        return "noop_tool", {}

    # If PermissionResolver.can_execute returned False, prepare_market_action
    # would raise PermissionError. We simply assert that it does not.
    tool_name, tool_args = prepare_market_action("PERM_INTENT", _dummy_state(), _dummy_payload())
    assert tool_name == "noop_tool"
    assert tool_args == {}
