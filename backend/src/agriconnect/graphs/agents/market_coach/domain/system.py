from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from agriconnect.graphs.agents.market_coach.actions.tooling import ToolId

from .model import DomainContext, DomainResult


@dataclass(frozen=True)
class SystemGetPendingCommand:
    phone: str
    limit: Optional[int] = None


@dataclass(frozen=True)
class SystemReportAnomalyCommand:
    phone: str
    description: str
    zone: Optional[str] = None
    anomaly_type: Optional[str] = None
    target_id: Optional[str] = None


@dataclass(frozen=True)
class SystemBindZoneCommand:
    phone: str
    zone: str


@dataclass(frozen=True)
class SystemCommitTransactionCommand:
    phone: str
    staging_id: str


@dataclass
class SystemService:
    context: DomainContext

    def get_pending(self, command: SystemGetPendingCommand) -> DomainResult:
        args: Dict[str, Any] = {"agent_name": "MarketCoach"}
        # If backend later supports limit, we can add it here when present.
        return DomainResult(tool_id=ToolId.GET_PENDING_ACTIONS, tool_args=args)

    def report_anomaly(self, command: SystemReportAnomalyCommand) -> DomainResult:
        zone = str(command.zone or "")
        description = str(command.description)
        args: Dict[str, Any] = {
            "zone_id": zone,
            "title": description[:80],
            "level": "MEDIUM",
        }
        return DomainResult(tool_id=ToolId.REPORT_ANOMALY, tool_args=args)

    def bind_zone(self, command: SystemBindZoneCommand) -> DomainResult:
        args: Dict[str, Any] = {
            "agent_name": "MarketCoach",
            "action_type": "SYSTEM_BIND_ZONE",
            "payload": {"phone": str(command.phone), "zone_name": str(command.zone)},
        }
        return DomainResult(tool_id=ToolId.CREATE_AGENT_ACTION, tool_args=args)

    def commit_transaction(
        self, command: SystemCommitTransactionCommand
    ) -> DomainResult:
        args: Dict[str, Any] = {
            "transaction_id": str(command.staging_id),
            "approved": True,
        }
        return DomainResult(tool_id=ToolId.COMMIT_STAGED_TRANSACTION, tool_args=args)
