"""Adapter exposing the parallel executor as `ParallelEngine`.

Re-exports the existing `ParallelExecutor` implementation to match the
new suggested layout (`nodes_parallel.py`).
"""
from .message_flow_parallel import ParallelExecutor as ParallelEngine, RequestContext

__all__ = ["ParallelEngine", "RequestContext"]
