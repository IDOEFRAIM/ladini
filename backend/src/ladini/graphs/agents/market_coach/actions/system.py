"""Action handlers for the System domain."""

from __future__ import annotations

from typing import Any, Dict, Mapping, Tuple

from ladini.graphs.agents.market_coach.actions.system_dto import (
    SystemBindZonePayload,
    SystemCommitTransactionPayload,
    SystemGetPendingPayload,
    SystemReportAnomalyPayload,
)
from ladini.graphs.agents.market_coach.actions.tooling import ToolResolver
from ladini.graphs.agents.market_coach.domain import DomainContext
from ladini.graphs.agents.market_coach.domain.system import (
    SystemBindZoneCommand,
    SystemCommitTransactionCommand,
    SystemGetPendingCommand,
    SystemReportAnomalyCommand,
    SystemService,
)
from ladini.graphs.agents.market_coach.registry import register_action


@register_action("SYSTEM_GET_PENDING", mode="READ")
def prep_system_get_pending(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    """Prépare la consultation des actions en attente.

    Outil MCP : get_pending_actions(agent_name?, limit?).
    ATTENTION : le MCP n'utilise PAS phone. On passe le nom de l'agent.
    """
    context = DomainContext.from_state(state)
    dto = SystemGetPendingPayload.from_payload(payload)
    command = SystemGetPendingCommand(phone=context.phone or "", limit=dto.limit)
    service = SystemService(context=context)
    result = service.get_pending(command)
    tool_name = ToolResolver.resolve_name(result.tool_id or "get_pending_actions")
    return tool_name, dict(result.tool_args)


@register_action("SYSTEM_REPORT_ANOMALY", mode="WRITE")
def prep_system_report_anomaly(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    context = DomainContext.from_state(state)
    dto = SystemReportAnomalyPayload.from_payload(payload)
    command = SystemReportAnomalyCommand(
        phone=context.phone or "",
        description=dto.description,
        zone=dto.zone,
        anomaly_type=dto.anomaly_type,
        target_id=dto.target_id,
    )
    service = SystemService(context=context)
    result = service.report_anomaly(command)
    tool_name = ToolResolver.resolve_name(result.tool_id or "report_anomaly")
    return tool_name, dict(result.tool_args)


@register_action("SYSTEM_BIND_ZONE", mode="WRITE")
def prep_system_bind_zone(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    context = DomainContext.from_state(state)
    dto = SystemBindZonePayload.from_payload(payload)
    command = SystemBindZoneCommand(phone=context.phone or "", zone=dto.zone)
    service = SystemService(context=context)
    result = service.bind_zone(command)
    tool_name = ToolResolver.resolve_name(result.tool_id or "create_agent_action")
    return tool_name, dict(result.tool_args)


@register_action("SYSTEM_COMMIT_TRANSACTION", mode="WRITE")
def prep_system_commit_transaction(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    context = DomainContext.from_state(state)
    dto = SystemCommitTransactionPayload.from_payload(payload)
    command = SystemCommitTransactionCommand(
        phone=context.phone or "", staging_id=dto.staging_id
    )
    service = SystemService(context=context)
    result = service.commit_transaction(command)
    tool_name = ToolResolver.resolve_name(result.tool_id or "commit_staged_transaction")
    return tool_name, dict(result.tool_args)
