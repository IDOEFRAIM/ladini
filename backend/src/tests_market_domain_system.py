from __future__ import annotations

from typing import Any, Mapping

from agriconnect.graphs.agents.market_coach.domain import DomainContext
from agriconnect.graphs.agents.market_coach.domain.system import (
    SystemService,
    SystemGetPendingCommand,
    SystemReportAnomalyCommand,
)
from agriconnect.graphs.agents.market_coach.actions.tooling import ToolId


def _state() -> Mapping[str, Any]:
    return {"user_phone": "+22600000001"}


def test_system_get_pending_domain_logic() -> None:
    ctx = DomainContext.from_state(_state())
    service = SystemService(context=ctx)
    cmd = SystemGetPendingCommand(phone="+22600000001", limit=None)
    result = service.get_pending(cmd)
    assert result.tool_id == ToolId.GET_PENDING_ACTIONS
    assert result.tool_args["agent_name"] == "MarketCoach"


def test_system_report_anomaly_domain_logic() -> None:
    ctx = DomainContext.from_state(_state())
    service = SystemService(context=ctx)
    cmd = SystemReportAnomalyCommand(phone="+22600000001", zone="ZONE-1", description="Anomalie très longue" * 10)
    result = service.report_anomaly(cmd)
    assert result.tool_id == ToolId.REPORT_ANOMALY
    args = result.tool_args
    assert args["zone_id"] == "ZONE-1"
    assert len(args["title"]) <= 80
    assert args["level"] == "MEDIUM"
