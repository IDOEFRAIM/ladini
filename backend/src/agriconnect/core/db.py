"""Compatibility shim for database helpers.

The real implementation now lives in `agriconnect.infrastructure.database.db`.
This module re-exports that implementation to avoid widespread import changes
during the refactor.
"""

from agriconnect.infrastructure.database.db import *  # noqa: F401,F403

__all__ = [name for name in globals() if not name.startswith("_")]
