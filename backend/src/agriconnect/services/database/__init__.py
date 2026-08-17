"""Database service package.

Stable import surface:
        `from agriconnect.services.database import AgriDatabaseService, get_db`

L'import de `d.py` (et de ses 11 mixins) reste LAZY par défaut : le package
peut être importé sans charger toute la couche DB. Contrepartie (§5.19) : un
mixin cassé n'était détecté qu'au premier accès à l'attribut — en prod.
Poser `AGRICONNECT_EAGER_IMPORTS=1` (CI, tests, smoke de démarrage) force
l'import complet ici et fait échouer le build immédiatement.
"""

from __future__ import annotations

import os
from typing import Any

from agriconnect.core.database import get_db

if os.getenv("AGRICONNECT_EAGER_IMPORTS", "").strip().lower() in {"1", "true", "yes"}:
    from .d import AgriDatabaseService  # noqa: F401 — fail-fast CI


def __getattr__(name: str) -> Any:
    if name == "AgriDatabaseService":
        from .d import AgriDatabaseService

        return AgriDatabaseService
    raise AttributeError(name)


__all__ = ["AgriDatabaseService", "get_db"]
