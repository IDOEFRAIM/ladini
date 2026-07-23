from __future__ import annotations

from typing import Any, Mapping

from agriconnect.graphs.agents.market_coach.domain import DomainContext
from agriconnect.graphs.agents.market_coach.domain.profile import (
    ProfileService,
    ProfileGetMcpUserCommand,
    ProfileSwitchRoleCommand,
)
from agriconnect.graphs.agents.market_coach.actions.tooling import ToolId


def _state() -> Mapping[str, Any]:
    return {"user_phone": "+22600000005"}


def test_profile_get_mcp_user_domain_logic() -> None:
    ctx = DomainContext.from_state(_state())
    service = ProfileService(context=ctx)
    cmd = ProfileGetMcpUserCommand(phone="+22699999999")
    result = service.get_mcp_user(cmd)
    assert result.tool_id == ToolId.GET_USER_BY_PHONE
    assert result.tool_args["phone"] == "+22699999999"


def test_profile_switch_role_domain_logic() -> None:
    ctx = DomainContext.from_state(_state())
    service = ProfileService(context=ctx)
    cmd = ProfileSwitchRoleCommand(phone="+22600000005", target_role="buyer")
    result = service.switch_role(cmd)
    assert result.tool_id == ToolId.CREATE_AGENT_ACTION
    args = result.tool_args
    assert args["agent_name"] == "MarketCoach"
    assert args["payload"]["phone"] == "+22600000005"
    assert args["payload"]["target_role"] == "BUYER"
