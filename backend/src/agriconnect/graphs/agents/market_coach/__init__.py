__all__ = ["graph"]


def __getattr__(name):
    if name == "graph":
        from .graph import get_agent_graph
        return get_agent_graph
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
