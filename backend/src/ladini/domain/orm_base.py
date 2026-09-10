"""Socle ORM partagé — Base déclarative unique + helpers communs.

Tous les modules `*/models.py` importent CE `Base` (jamais un `declarative_base()`
local) : c'est ce qui garantit un registre de mappers unique, condition pour
que les `relationship("NomDeClasse", ...)` inter-fichiers se résolvent
correctement (SQLAlchemy résout ces chaînes paresseusement contre le
registre du `Base` partagé — l'ordre d'import entre fichiers n'a pas
d'importance tant qu'ils partagent ce même `Base`).
"""

from __future__ import annotations

import uuid

from sqlalchemy.inspection import inspect
from sqlalchemy.orm import declarative_base


class _ToDictMixin:
    def to_dict(self) -> dict:
        mapper = inspect(self).mapper
        data = {}
        for attr in mapper.column_attrs:
            key = attr.key
            val = getattr(self, key)
            data[key] = str(val) if isinstance(val, uuid.UUID) else val
        return data


Base = declarative_base(cls=_ToDictMixin)


def _uuid4() -> str:
    return str(uuid.uuid4())


__all__ = ["Base", "_uuid4", "_ToDictMixin"]
