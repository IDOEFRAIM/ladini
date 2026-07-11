"""BaseMarketplaceModel — socle Pydantic v2 commun à tous les DTO marketplace.

Objectifs (contrainte DRY) :
  - Hydratation directe depuis un objet SQLAlchemy (`from_attributes`).
  - Tolérance camelCase (payloads Drizzle / front) ↔ snake_case (backend) via
    `alias_generator=to_camel` + `populate_by_name`.
  - Validation standardisée des montants/quantités (>= 0).
  - `Decimal` pour l'argent et les quantités (précision financière, aligné sur
    les colonnes `numeric` du schéma — source de vérité).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict


def to_camel(s: str) -> str:
    head, *tail = s.split("_")
    return head + "".join(w.capitalize() for w in tail)


class BaseMarketplaceModel(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
        alias_generator=to_camel,
        str_strip_whitespace=True,
        extra="ignore",
        # Sérialise les Decimal en float dans model_dump(mode="json") si besoin API
        ser_json_inf_nan="constants",
    )

    def to_db(self) -> dict[str, Any]:
        """Payload snake_case prêt pour l'ORM (jamais d'alias camelCase)."""
        return self.model_dump(by_alias=False, exclude_none=True)

    @staticmethod
    def _non_negative(v: Decimal | float | None) -> Decimal | float | None:
        if v is not None and v < 0:
            raise ValueError("La valeur doit être positive ou nulle.")
        return v
