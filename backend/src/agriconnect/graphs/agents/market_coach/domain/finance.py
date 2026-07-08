from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from .model import DomainContext, DomainResult
from agriconnect.graphs.agents.market_coach.actions.tooling import ToolId


@dataclass(frozen=True)
class FinanceGetSummaryCommand:
    """Immutable command for FINANCE_GET_SUMMARY read intent."""

    phone: str
    farm_id: str
    days: Optional[int] = None


@dataclass(frozen=True)
class FinanceLogExpenseCommand:
    """Immutable command for FINANCE_LOG_EXPENSE write intent."""

    phone: str
    farm_id: str
    amount: float
    label: Optional[str] = None
    category: Optional[str] = None


@dataclass
class FinanceService:
    context: DomainContext

    def get_summary(self, command: FinanceGetSummaryCommand) -> DomainResult:
        args: Dict[str, Any] = {"farm_id": str(command.farm_id)}
        if command.days is not None:
            args["days"] = int(command.days)
        return DomainResult(tool_id=ToolId.GET_EXPENSE_SUMMARY, tool_args=args)

    def log_expense(self, command: FinanceLogExpenseCommand) -> DomainResult:
        args: Dict[str, Any] = {
            "farm_id": str(command.farm_id),
            "label": str(command.label or "Dépense diverse"),
            "amount": float(command.amount),
            "category": str(command.category or "OTHER"),
        }
        return DomainResult(tool_id=ToolId.ADD_EXPENSE, tool_args=args)
