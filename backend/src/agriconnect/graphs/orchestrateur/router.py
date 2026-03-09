"""Adapter: modern router interface for orchestrateur.

This module re-exports the existing `MessageRouter` under the
`AgriRouter` name so the code can progressively migrate to the
new file layout without breaking imports.
"""
from .message_flow_router import MessageRouter as AgriRouter

__all__ = ["AgriRouter"]
