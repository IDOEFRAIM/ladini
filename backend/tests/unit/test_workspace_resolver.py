"""`workspace/resolver.py::WorkspaceResolver` — charge ou crée un workspace et
maintient l'état de tunnel (`tunnel_locked`) à jour à chaque tour."""
from __future__ import annotations

from agriconnect.workspace.models import Workspace
from agriconnect.workspace.resolver import WorkspaceResolver
from tests.conftest import run


class FakeWorkspaceStore:
    def __init__(self, existing: Workspace | None = None) -> None:
        self._existing = existing

    async def get(self, workspace_id: str):
        return self._existing


class TestWorkspaceResolverNewWorkspace:
    def test_creates_a_new_workspace_when_none_exists(self):
        resolver = WorkspaceResolver(store=FakeWorkspaceStore(existing=None))
        ws = run(resolver.resolve("phone-1"))
        assert ws.workspace_id == "phone-1"
        assert ws.workspace_type == "producer"

    def test_new_workspace_uses_the_requested_type(self):
        resolver = WorkspaceResolver(store=FakeWorkspaceStore(existing=None))
        ws = run(resolver.resolve("phone-1", workspace_type="buyer"))
        assert ws.workspace_type == "buyer"


class TestWorkspaceResolverExistingWorkspace:
    def test_updates_workspace_type_when_it_changed(self):
        existing = Workspace(workspace_id="phone-1", workspace_type="producer")
        resolver = WorkspaceResolver(store=FakeWorkspaceStore(existing=existing))
        ws = run(resolver.resolve("phone-1", workspace_type="buyer"))
        assert ws.workspace_type == "buyer"
        assert ws.is_dirty is True

    def test_same_workspace_type_does_not_mark_dirty_for_that_reason(self):
        existing = Workspace(workspace_id="phone-1", workspace_type="producer")
        resolver = WorkspaceResolver(store=FakeWorkspaceStore(existing=existing))
        ws = run(resolver.resolve("phone-1", workspace_type="producer"))
        assert ws.is_dirty is False

    def test_no_requested_type_leaves_existing_type_untouched(self):
        existing = Workspace(workspace_id="phone-1", workspace_type="buyer")
        resolver = WorkspaceResolver(store=FakeWorkspaceStore(existing=existing))
        ws = run(resolver.resolve("phone-1"))
        assert ws.workspace_type == "buyer"


class TestWorkspaceResolverTunnelState:
    def test_active_goal_marks_the_tunnel_locked(self):
        existing = Workspace(workspace_id="phone-1", active_goal="SALES_PUBLISH_PRODUCT")
        resolver = WorkspaceResolver(store=FakeWorkspaceStore(existing=existing))
        ws = run(resolver.resolve("phone-1"))
        assert ws.tunnel_locked is True

    def test_active_form_marks_the_tunnel_locked(self):
        existing = Workspace(workspace_id="phone-1", active_form="AUCTION_CREATE")
        resolver = WorkspaceResolver(store=FakeWorkspaceStore(existing=existing))
        ws = run(resolver.resolve("phone-1"))
        assert ws.tunnel_locked is True

    def test_no_goal_or_form_leaves_the_tunnel_unlocked(self):
        existing = Workspace(workspace_id="phone-1", active_goal="", active_form=None)
        resolver = WorkspaceResolver(store=FakeWorkspaceStore(existing=existing))
        ws = run(resolver.resolve("phone-1"))
        assert ws.tunnel_locked is False

    def test_previously_locked_tunnel_is_unlocked_once_goal_clears(self):
        existing = Workspace(workspace_id="phone-1", active_goal=None, active_form=None, tunnel_locked=True)
        resolver = WorkspaceResolver(store=FakeWorkspaceStore(existing=existing))
        ws = run(resolver.resolve("phone-1"))
        assert ws.tunnel_locked is False


class TestWorkspaceResolverStaleMetadata:
    def test_stale_router_clarification_is_removed_and_marks_dirty(self):
        existing = Workspace(workspace_id="phone-1", metadata={"router_clarification": "stale"})
        resolver = WorkspaceResolver(store=FakeWorkspaceStore(existing=existing))
        ws = run(resolver.resolve("phone-1"))
        assert "router_clarification" not in ws.metadata
        assert ws.is_dirty is True

    def test_no_router_clarification_key_is_a_noop(self):
        existing = Workspace(workspace_id="phone-1", metadata={"other": "value"})
        resolver = WorkspaceResolver(store=FakeWorkspaceStore(existing=existing))
        ws = run(resolver.resolve("phone-1"))
        assert ws.is_dirty is False
        assert ws.metadata == {"other": "value"}


class TestWorkspaceResolverDefaultStore:
    def test_default_constructor_builds_a_real_workspace_store(self):
        from agriconnect.workspace.store import WorkspaceStore
        resolver = WorkspaceResolver()
        assert isinstance(resolver.store, WorkspaceStore)
