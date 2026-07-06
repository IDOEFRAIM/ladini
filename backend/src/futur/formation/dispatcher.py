from __future__ import annotations

from typing import Any, Dict, Optional

from agriconnect.agents import (
    ActionContext,
    ActionSpec,
    BaseAgentDispatcher,
    PreparedAction,
    execute_prepared_action,
)

FORMATION_DISPATCHER = BaseAgentDispatcher("formation")


class _FormationMCPAdapter:
    def __init__(self, runtime: Any):
        self._runtime = runtime

    async def call_db(self, tool_name: str, **kwargs: Any) -> Any:
        return await self._runtime.call_mcp(tool_name, **kwargs)


def _prep_get_crop_requirements(payload: Dict[str, Any], _ctx: ActionContext) -> PreparedAction:
    crop = payload.get("crop_type") or payload.get("crop")
    if not crop:
        raise ValueError("Missing required field: crop_type")
    primary = PreparedAction(
        transport="mcp",
        target="get_crop_requirements",
        payload={"crop_type": str(crop)},
    )
    fallback = PreparedAction(
        transport="db",
        target="get_crop_requirements",
        payload={"crop_type": str(crop)},
    )
    return primary.with_fallback(fallback)


def _prep_get_active_sanitary_risks(payload: Dict[str, Any], _ctx: ActionContext) -> PreparedAction:
    farm_id = payload.get("farm_id")
    if not farm_id:
        raise ValueError("Missing required field: farm_id")
    primary = PreparedAction(
        transport="mcp",
        target="get_active_sanitary_risks",
        payload={"farm_id": str(farm_id)},
    )
    fallback = PreparedAction(
        transport="db",
        target="get_active_sanitary_risks",
        payload={"farm_id": str(farm_id)},
    )
    return primary.with_fallback(fallback)


def _prep_get_cycle_economics(payload: Dict[str, Any], _ctx: ActionContext) -> PreparedAction:
    cycle_id = payload.get("cycle_id")
    if not cycle_id:
        raise ValueError("Missing required field: cycle_id")
    primary = PreparedAction(
        transport="mcp",
        target="get_cycle_economics",
        payload={"cycle_id": str(cycle_id)},
    )
    fallback = PreparedAction(
        transport="db",
        target="get_cycle_economics",
        payload={"cycle_id": str(cycle_id)},
    )
    return primary.with_fallback(fallback)


FORMATION_DISPATCHER.register_action(
    "FORMATION_GET_CROP_REQUIREMENTS",
    ActionSpec(
        name="FORMATION_GET_CROP_REQUIREMENTS",
        mode="READ",
        handler=_prep_get_crop_requirements,
        required_fields=("crop_type",),
    ),
)
FORMATION_DISPATCHER.register_action(
    "FORMATION_GET_ACTIVE_SANITARY_RISKS",
    ActionSpec(
        name="FORMATION_GET_ACTIVE_SANITARY_RISKS",
        mode="READ",
        handler=_prep_get_active_sanitary_risks,
        required_fields=("farm_id",),
    ),
)
FORMATION_DISPATCHER.register_action(
    "FORMATION_GET_CYCLE_ECONOMICS",
    ActionSpec(
        name="FORMATION_GET_CYCLE_ECONOMICS",
        mode="READ",
        handler=_prep_get_cycle_economics,
        required_fields=("cycle_id",),
    ),
)


async def execute_formation_action(
    intent: str,
    payload: Dict[str, Any],
    *,
    runtime: Any,
    db_service: Any,
    state: Optional[Dict[str, Any]] = None,
) -> Any:
    prepared = await FORMATION_DISPATCHER.prepare(
        intent,
        payload,
        ActionContext(state=state),
    )
    mcp_runtime = _FormationMCPAdapter(runtime)
    return await execute_prepared_action(
        prepared,
        mcp_runtime=mcp_runtime,
        db_service=db_service,
    )
