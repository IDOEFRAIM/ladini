"""MCP servers package.

Avoid eager imports here to prevent side-effects when a submodule is executed
directly with `python -m ...` (which can otherwise trigger duplicate module
initialization warnings).
"""

__all__ = []
