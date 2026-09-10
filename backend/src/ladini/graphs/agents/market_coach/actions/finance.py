"""Action handlers for the Finance domain."""

from __future__ import annotations

from typing import Any, Dict, Mapping, Tuple

from ladini.graphs.agents.market_coach.actions.finance_dto import (
    FinanceGetSummaryPayload,
    FinanceLogExpensePayload,
)
from ladini.graphs.agents.market_coach.actions.tooling import ToolResolver
from ladini.graphs.agents.market_coach.domain import DomainContext
from ladini.graphs.agents.market_coach.domain.finance import (
    FinanceGetSummaryCommand,
    FinanceLogExpenseCommand,
    FinanceService,
)
from ladini.graphs.agents.market_coach.registry import register_action


@register_action("FINANCE_GET_SUMMARY", mode="READ")
def prep_finance_get_summary(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    """Prépare le bilan comptable synthétique."""
    context = DomainContext.from_state(state)
    dto = FinanceGetSummaryPayload.from_payload(payload)
    command = FinanceGetSummaryCommand(
        phone=context.phone,
        farm_id=dto.farm_id,
        days=dto.days,
    )
    service = FinanceService(context=context)
    result = service.get_summary(command)
    tool_name = ToolResolver.resolve_name(result.tool_id or "get_expense_summary")
    return tool_name, dict(result.tool_args)


@register_action("FINANCE_LOG_EXPENSE", mode="WRITE")
def prep_finance_log_expense(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    context = DomainContext.from_state(state)
    dto = FinanceLogExpensePayload.from_payload(payload)
    command = FinanceLogExpenseCommand(
        phone=context.phone,
        farm_id=dto.farm_id,
        amount=dto.amount,
        label=dto.label,
        category=dto.category,
    )
    service = FinanceService(context=context)
    result = service.log_expense(command)
    tool_name = ToolResolver.resolve_name(result.tool_id or "add_expense")
    return tool_name, dict(result.tool_args)
