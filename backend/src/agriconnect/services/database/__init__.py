"""Database service package.

Stable import surface:
	`from agriconnect.services.database import AgriDatabaseService, get_db`
"""

from __future__ import annotations

from typing import Any

from agriconnect.core.database import get_db


def __getattr__(name: str) -> Any:
	if name == "AgriDatabaseService":
		from .d import AgriDatabaseService
		return AgriDatabaseService
	raise AttributeError(name)


__all__ = ["AgriDatabaseService", "get_db"]
