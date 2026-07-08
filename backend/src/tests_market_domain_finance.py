from __future__ import annotations

from typing import Any, Mapping

from agriconnect.graphs.agents.market_coach.domain import DomainContext
from agriconnect.graphs.agents.market_coach.domain.finance import (
    FinanceService,
    FinanceGetSummaryCommand,
    FinanceLogExpenseCommand,
)
from agriconnect.graphs.agents.market_coach.actions.tooling import ToolId


def _state() -> Mapping[str, Any]:
    return {"user_phone": "+22600000004"}


def test_finance_get_summary_domain_logic() -> None:
    ctx = DomainContext.from_state(_state())
    service = FinanceService(context=ctx)
    cmd = FinanceGetSummaryCommand(phone="+22600000004", farm_id="F1", days=30)
    result = service.get_summary(cmd)
    assert result.tool_id == ToolId.GET_EXPENSE_SUMMARY
    args = result.tool_args
    assert args["farm_id"] == "F1"
    assert args["days"] == 30


def test_finance_log_expense_domain_logic() -> None:
    ctx = DomainContext.from_state(_state())
    service = FinanceService(context=ctx)
    cmd = FinanceLogExpenseCommand(phone="+22600000004", farm_id="F1", amount=500.0, label="engrais", category="INPUT")
    result = service.log_expense(cmd)
    assert result.tool_id == ToolId.ADD_EXPENSE
    args = result.tool_args
    assert args["farm_id"] == "F1"
    assert args["amount"] == 500.0
    assert args["label"] == "engrais"
    assert args["category"] == "INPUT"
