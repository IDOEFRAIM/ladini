"""DatabaseEngine — façade DÉLÉGUANTE vers `core.database`.

⚠️ Historique : ce module créait autrefois son PROPRE moteur asyncpg + pool,
en parallèle du moteur de `core.database`. Résultat : deux pools de connexions
vers la même DB managée → épuisement des slots en production.

Désormais ce module ne crée AUCUN moteur. Il délègue intégralement à
`core.database`, garantissant un pool unique partagé par tout le backend
(dispatcher AgriDatabaseService + services @transactional).

Conservé uniquement pour la compatibilité d'import (`from ...engine import engine`).
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agriconnect.core.database import (
    close_db as _core_close_db,
)
from agriconnect.core.database import (
    get_engine as _core_get_engine,
)
from agriconnect.core.database import (
    get_sessionmaker as _core_get_sessionmaker,
)

logger = logging.getLogger("AgriConnect.Engine")


class DatabaseEngine:
    """Façade fine : tous les accès pointent vers le moteur unique de core.database."""

    def engine(self) -> AsyncEngine:
        eng = _core_get_engine()
        if eng is None:
            raise RuntimeError("DATABASE_URL manquante ou init_db() non exécuté.")
        return eng

    def sessionmaker(self) -> async_sessionmaker[AsyncSession]:
        sm = _core_get_sessionmaker()
        if sm is None:
            raise RuntimeError(
                "Sessionmaker indisponible ; assurez-vous que init_db() a tourné."
            )
        return sm

    async def dispose(self) -> None:
        await _core_close_db()


# Singleton importable partout (délègue au pool unique de core.database).
engine = DatabaseEngine()
