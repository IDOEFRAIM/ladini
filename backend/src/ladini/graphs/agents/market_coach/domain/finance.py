from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from ladini.graphs.agents.market_coach.actions.tooling import ToolId

from .model import DomainContext, DomainResult


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
        # Idem `log_expense` : `command.phone` existait mais n'était pas
        # transmis, donc `get_expense_summary` n'était scopé que par `farm_id`
        # (fuite inter-locataire — audit sécurité agent 2026-09-10).
        args: Dict[str, Any] = {
            "farm_id": str(command.farm_id),
            "producer_phone": str(command.phone),
        }
        if command.days is not None:
            args["days"] = int(command.days)
        return DomainResult(tool_id=ToolId.GET_EXPENSE_SUMMARY, tool_args=args)

    def log_expense(self, command: FinanceLogExpenseCommand) -> DomainResult:
        # `command.phone` était porté par la commande mais JAMAIS transmis à
        # l'outil : `add_expense` ne recevait que `farm_id`, une valeur issue
        # du payload, et l'utilisait comme seul critère de ciblage (audit
        # sécurité agent 2026-09-10 — IDOR sur `farm_id`).
        args: Dict[str, Any] = {
            "farm_id": str(command.farm_id),
            "producer_phone": str(command.phone),
            "label": str(command.label or "Dépense diverse"),
            "amount": float(command.amount),
            "category": str(command.category or "OTHER"),
        }
        return DomainResult(tool_id=ToolId.ADD_EXPENSE, tool_args=args)
