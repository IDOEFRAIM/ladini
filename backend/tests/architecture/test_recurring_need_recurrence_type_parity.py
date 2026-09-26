"""Garde architecturale (Phase 3, mandat MONTHLY §13/§31) : le contrat `recurrence_type` doit
rester IDENTIQUE entre le domaine pur (`RECURRENCE_TYPES`), le miroir SQLAlchemy
(`CheckConstraint`) et le contrat Drizzle synchronisé (`schema_contract/drizzle_snapshot.json`)
— la prochaine fréquence ajoutée à ce domaine ne doit pas se retrouver implémentée dans 3 couches
sur 5, comme MONTHLY l'a d'abord été avant que ce test n'existe.

Extraction volontairement SIMPLE (une regex sur les littéraux entre quotes du texte de la
contrainte) : le format `... IN ('A','B',...)` est fixe et connu ici — un vrai parseur SQL serait
une fragilité inutile pour ce seul besoin (mandat §13 : « ne construis pas un parser SQL complexe
fragile uniquement pour cela »).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ladini.domain.recurring_supply.recurrence import RECURRENCE_TYPES

_CHECK_NAME = "recurring_needs_recurrence_type_chk"
_VALUES_RE = re.compile(r"'([A-Z_]+)'")
_SCHEMA_CONTRACT = Path(__file__).resolve().parents[2] / "schema_contract" / "drizzle_snapshot.json"


def _sqlalchemy_recurrence_values() -> set[str]:
    from ladini.domain.recurring_supply.models import RecurringNeed

    for c in RecurringNeed.__table__.constraints:
        if getattr(c, "name", None) == _CHECK_NAME:
            return set(_VALUES_RE.findall(str(c.sqltext)))
    raise AssertionError(f"{_CHECK_NAME} introuvable sur le miroir SQLAlchemy (models.py)")


def _drizzle_contract_recurrence_values() -> set[str]:
    snapshot = json.loads(_SCHEMA_CONTRACT.read_text(encoding="utf-8"))
    check = snapshot["tables"]["marketplace.recurring_needs"]["checkConstraints"][_CHECK_NAME]
    return set(_VALUES_RE.findall(check["value"]))


def test_recurrence_types_are_identical_across_domain_sqlalchemy_and_drizzle_contract():
    domain = set(RECURRENCE_TYPES)
    sqlalchemy = _sqlalchemy_recurrence_values()
    drizzle = _drizzle_contract_recurrence_values()
    assert domain == sqlalchemy == drizzle, (
        f"divergence recurrence_type entre couches : domaine={domain} sqlalchemy={sqlalchemy} "
        f"drizzle(schema_contract)={drizzle}"
    )


def test_the_canonical_recurrence_set_is_exactly_the_five_pilot_values():
    """Fige la liste elle-même (pas seulement sa cohérence inter-couches) : un changement ici doit
    être délibéré, jamais un ajout oublié dans une seule couche."""
    assert set(RECURRENCE_TYPES) == {"DAILY", "WEEKLY_DAYS", "WEEKLY", "MONTHLY", "ONE_OFF"}
