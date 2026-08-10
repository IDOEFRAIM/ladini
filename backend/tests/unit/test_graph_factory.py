"""`graphs/factory.py::GraphFactory` — cache de graphes LangGraph par
(rôle normalisé, identité runtime, identité checkpointer)."""
from __future__ import annotations

from unittest.mock import MagicMock


def _patch_build_graph(monkeypatch, side_effect=None):
    import agriconnect.graphs.factory as factory_mod
    calls = []

    def _fake_build_graph(**kwargs):
        calls.append(kwargs)
        return side_effect() if side_effect else MagicMock(name="graph")

    monkeypatch.setattr(factory_mod, "build_graph", _fake_build_graph)
    return calls


class TestGraphFactory:
    def test_builds_a_graph_on_first_call(self, monkeypatch):
        from agriconnect.graphs.factory import GraphFactory
        calls = _patch_build_graph(monkeypatch)
        factory = GraphFactory()
        rt, cp = object(), object()
        graph = factory.get_graph("PRODUCER", mc_runtime=rt, checkpointer=cp)
        assert graph is not None
        assert len(calls) == 1
        assert calls[0]["role"] == "PRODUCER"

    def test_caches_the_graph_for_identical_role_runtime_and_checkpointer(self, monkeypatch):
        from agriconnect.graphs.factory import GraphFactory
        calls = _patch_build_graph(monkeypatch)
        factory = GraphFactory()
        rt, cp = object(), object()
        g1 = factory.get_graph("PRODUCER", mc_runtime=rt, checkpointer=cp)
        g2 = factory.get_graph("PRODUCER", mc_runtime=rt, checkpointer=cp)
        assert g1 is g2
        assert len(calls) == 1

    def test_role_is_normalized_for_cache_key_purposes(self, monkeypatch):
        from agriconnect.graphs.factory import GraphFactory
        calls = _patch_build_graph(monkeypatch)
        factory = GraphFactory()
        rt, cp = object(), object()
        g1 = factory.get_graph("buyer", mc_runtime=rt, checkpointer=cp)
        g2 = factory.get_graph("ACHETEUSE", mc_runtime=rt, checkpointer=cp)
        assert g1 is g2
        assert len(calls) == 1

    def test_different_role_produces_a_different_cached_graph(self, monkeypatch):
        from agriconnect.graphs.factory import GraphFactory
        calls = _patch_build_graph(monkeypatch)
        factory = GraphFactory()
        rt, cp = object(), object()
        g1 = factory.get_graph("PRODUCER", mc_runtime=rt, checkpointer=cp)
        g2 = factory.get_graph("BUYER", mc_runtime=rt, checkpointer=cp)
        assert g1 is not g2
        assert len(calls) == 2

    def test_different_runtime_identity_produces_a_different_cached_graph(self, monkeypatch):
        from agriconnect.graphs.factory import GraphFactory
        calls = _patch_build_graph(monkeypatch)
        factory = GraphFactory()
        cp = object()
        g1 = factory.get_graph("PRODUCER", mc_runtime=object(), checkpointer=cp)
        g2 = factory.get_graph("PRODUCER", mc_runtime=object(), checkpointer=cp)
        assert g1 is not g2
        assert len(calls) == 2

    def test_missing_runtime_or_checkpointer_is_never_cached(self, monkeypatch):
        from agriconnect.graphs.factory import GraphFactory
        calls = _patch_build_graph(monkeypatch)
        factory = GraphFactory()
        factory.get_graph("PRODUCER")
        factory.get_graph("PRODUCER")
        assert len(calls) == 2
        assert factory._cache == {}

    def test_missing_checkpointer_only_is_never_cached(self, monkeypatch):
        from agriconnect.graphs.factory import GraphFactory
        calls = _patch_build_graph(monkeypatch)
        factory = GraphFactory()
        rt = object()
        factory.get_graph("PRODUCER", mc_runtime=rt)
        factory.get_graph("PRODUCER", mc_runtime=rt)
        assert len(calls) == 2

    def test_clear_empties_the_cache(self, monkeypatch):
        from agriconnect.graphs.factory import GraphFactory
        calls = _patch_build_graph(monkeypatch)
        factory = GraphFactory()
        rt, cp = object(), object()
        factory.get_graph("PRODUCER", mc_runtime=rt, checkpointer=cp)
        factory.clear()
        factory.get_graph("PRODUCER", mc_runtime=rt, checkpointer=cp)
        assert len(calls) == 2

    def test_extra_kwargs_are_forwarded_to_build_graph(self, monkeypatch):
        from agriconnect.graphs.factory import GraphFactory
        calls = _patch_build_graph(monkeypatch)
        factory = GraphFactory()
        llm, mcp = object(), object()
        factory.get_graph("PRODUCER", llm_client=llm, mcp_session=mcp)
        assert calls[0]["llm_client"] is llm
        assert calls[0]["mcp_session"] is mcp
