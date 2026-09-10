"""`workspace/models.py::Workspace` — dataclass d'état durable, source de
vérité du routage (jamais le dernier message seul)."""
from __future__ import annotations

import time

from ladini.workspace.models import Workspace


class TestDefaults:
    def test_defaults_are_sensible(self):
        ws = Workspace(workspace_id="phone-1")
        assert ws.workspace_type == "producer"
        assert ws.active_goal == ""
        assert ws.active_form is None
        assert ws.metadata == {}
        assert ws.agent_state == {}
        assert ws.tunnel_locked is False
        assert ws.is_dirty is False

    def test_updated_at_defaults_to_now(self):
        before = time.time()
        ws = Workspace(workspace_id="phone-1")
        assert before <= ws.updated_at <= time.time()


class TestActiveAgentAndLockedAgent:
    def test_active_agent_is_always_market(self):
        assert Workspace(workspace_id="p1").active_agent == "market"

    def test_locked_agent_is_market_when_tunnel_locked(self):
        ws = Workspace(workspace_id="p1", tunnel_locked=True)
        assert ws.locked_agent == "market"

    def test_locked_agent_is_none_when_not_locked(self):
        ws = Workspace(workspace_id="p1", tunnel_locked=False)
        assert ws.locked_agent is None

    def test_locked_agent_setter_updates_tunnel_locked(self):
        ws = Workspace(workspace_id="p1")
        ws.locked_agent = "market"
        assert ws.tunnel_locked is True
        ws.locked_agent = None
        assert ws.tunnel_locked is False


class TestIsFresh:
    def test_fresh_without_agent_state(self):
        assert Workspace(workspace_id="p1").is_fresh is True

    def test_not_fresh_with_agent_state(self):
        assert Workspace(workspace_id="p1", agent_state={"x": 1}).is_fresh is False


class TestTouch:
    def test_touch_updates_the_timestamp(self, monkeypatch):
        ws = Workspace(workspace_id="p1", updated_at=0.0)
        ws.touch()
        assert ws.updated_at > 0.0


class TestCloseTunnel:
    def test_clears_goal_form_and_lock(self):
        ws = Workspace(workspace_id="p1", active_goal="SALES_PUBLISH_PRODUCT", active_form="FORM_X", tunnel_locked=True)
        ws.close_tunnel()
        assert ws.active_goal == ""
        assert ws.active_form is None
        assert ws.tunnel_locked is False

    def test_sets_a_cooldown_marker_and_marks_dirty(self):
        ws = Workspace(workspace_id="p1")
        ws.close_tunnel()
        assert "just_finished_action" in ws.metadata
        assert ws.is_dirty is True


class TestClearPostFormCooldown:
    def test_removes_the_cooldown_marker(self):
        ws = Workspace(workspace_id="p1", metadata={"just_finished_action": 123.0})
        ws.clear_post_form_cooldown()
        assert "just_finished_action" not in ws.metadata

    def test_marks_dirty_even_if_marker_absent(self):
        ws = Workspace(workspace_id="p1")
        ws.reset_dirty()
        ws.clear_post_form_cooldown()
        assert ws.is_dirty is True


class TestDirtyFlag:
    def test_mark_and_reset_dirty(self):
        ws = Workspace(workspace_id="p1")
        assert ws.is_dirty is False
        ws.mark_dirty()
        assert ws.is_dirty is True
        ws.reset_dirty()
        assert ws.is_dirty is False


class TestToDictFromDictRoundtrip:
    def test_to_dict_shape(self):
        ws = Workspace(
            workspace_id="p1", workspace_type="buyer", active_goal="G",
            active_form="F", metadata={"m": 1}, agent_state={"s": 1},
            updated_at=99.0, tunnel_locked=True,
        )
        d = ws.to_dict()
        assert d == {
            "workspace_id": "p1", "workspace_type": "buyer", "active_agent": "market",
            "active_goal": "G", "active_form": "F", "locked_agent": "market",
            "metadata": {"m": 1}, "langgraph_state": {"s": 1}, "updated_at": 99.0,
        }

    def test_from_dict_reconstructs_equivalent_workspace(self):
        original = Workspace(
            workspace_id="p1", workspace_type="buyer", active_goal="G",
            active_form="F", metadata={"m": 1}, agent_state={"s": 1},
            updated_at=99.0, tunnel_locked=True,
        )
        rebuilt = Workspace.from_dict(original.to_dict())
        assert rebuilt.workspace_id == original.workspace_id
        assert rebuilt.workspace_type == original.workspace_type
        assert rebuilt.active_goal == original.active_goal
        assert rebuilt.active_form == original.active_form
        assert rebuilt.metadata == original.metadata
        assert rebuilt.agent_state == original.agent_state
        assert rebuilt.tunnel_locked == original.tunnel_locked

    def test_from_dict_applies_defaults_for_missing_optional_fields(self):
        ws = Workspace.from_dict({"workspace_id": "p1"})
        assert ws.workspace_type == "producer"
        assert ws.active_goal == ""
        assert ws.active_form is None
        assert ws.metadata == {}
        assert ws.agent_state == {}
        assert ws.tunnel_locked is False

    def test_from_dict_missing_workspace_id_raises(self):
        import pytest
        with pytest.raises(KeyError):
            Workspace.from_dict({})


class TestRepr:
    def test_repr_includes_key_fields(self):
        ws = Workspace(workspace_id="p1", active_goal="G", active_form="F", tunnel_locked=True)
        r = repr(ws)
        assert "p1" in r
        assert "G" in r
        assert "F" in r
        assert "market" in r
