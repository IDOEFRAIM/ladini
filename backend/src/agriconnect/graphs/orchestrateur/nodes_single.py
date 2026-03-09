"""Adapter for single-expert / safety nodes.

Initially this module re-exports the `ExpertInvoker` helper. During a
full refactor we will move `execute_solo_agent`, `execute_chat` and
`execute_rejection` here as standalone functions accepting the
`MessageResponseFlow` context. For now this acts as a stable import
point matching the requested structure.
"""
from .message_flow_helpers import ExpertInvoker

__all__ = ["ExpertInvoker"]
