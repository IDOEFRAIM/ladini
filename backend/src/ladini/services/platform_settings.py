"""Réglages GLOBAUX de la plateforme, administrables — source de vérité UNIQUE pour le recurring.

`get_recurring_settings(session)` est LA primitive de lecture : le service des besoins récurrents, l'API
admin et l'API de consultation passent tous par elle (jamais une valeur côté frontend + un défaut backend +
une constante de cron). Sans ligne en base, la valeur par défaut est `DEFAULT_START_LEAD_DAYS` — déterministe.

Pas de cache : une lecture par création de besoin (une ligne indexée par clé), donc une modification admin est
visible immédiatement par tous les processus/instances.

Concurrence : verrou de ligne + version. `expected_version` (optionnel) refuse une écriture basée sur une lecture
périmée ; sans lui, le dernier écrit gagne et l'audit garde l'ancienne/la nouvelle valeur.

Portée : un changement n'affecte QUE les besoins créés ensuite — jamais `RecurringNeed.starts_at` déjà écrit.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ladini.domain.governance.models import PlatformSetting
from ladini.domain.identity.models import User
from ladini.domain.intelligence.models import AuditLog
from ladini.domain.recurring_supply.start_policy import (
    DEFAULT_START_LEAD_DAYS,
    MAX_START_LEAD_DAYS,
    MIN_START_LEAD_DAYS,
    InvalidLeadDays,
    validate_lead_days,
)

RECURRING_START_LEAD_KEY = "recurring_supply.minimum_start_lead_days"
AUDIT_ACTION_SETTING_CHANGED = "PLATFORM_SETTING_CHANGED"
ADMIN_ROLE = "ADMIN"


class SettingsError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class RecurringSettings:
    minimum_start_lead_days: int
    #: "DEFAULT" tant qu'aucun admin n'a enregistré de valeur, "DATABASE" ensuite.
    source: str
    version: int
    updated_at: datetime | None = None
    updated_by_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "minimum_start_lead_days": self.minimum_start_lead_days,
            "source": self.source,
            "version": self.version,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "updated_by_id": self.updated_by_id,
            "bounds": {"min": MIN_START_LEAD_DAYS, "max": MAX_START_LEAD_DAYS},
            "default": DEFAULT_START_LEAD_DAYS,
        }


def _lead_days_from_row(row: PlatformSetting | None) -> int:
    if row is None:
        return int(DEFAULT_START_LEAD_DAYS)
    try:
        return int(validate_lead_days(row.value))
    except InvalidLeadDays:
        # Ligne corrompue à la main : ne jamais faire échouer la création d'un besoin, retomber sur le défaut.
        return int(DEFAULT_START_LEAD_DAYS)


async def get_recurring_settings(session: AsyncSession) -> RecurringSettings:
    row = (
        await session.execute(select(PlatformSetting).where(PlatformSetting.key == RECURRING_START_LEAD_KEY))
    ).scalar_one_or_none()
    if row is None:
        return RecurringSettings(DEFAULT_START_LEAD_DAYS, "DEFAULT", 0)
    return RecurringSettings(
        minimum_start_lead_days=_lead_days_from_row(row),
        source="DATABASE",
        version=int(row.version),
        updated_at=row.updated_at,
        updated_by_id=str(row.updated_by_id) if row.updated_by_id else None,
    )


async def _require_admin(session: AsyncSession, actor_id: Any) -> uuid.UUID:
    try:
        actor_uuid = uuid.UUID(str(actor_id))
    except (ValueError, AttributeError, TypeError):
        raise SettingsError(400, "actor_id manquant ou invalide.") from None
    role = (await session.execute(select(User.role).where(User.id == actor_uuid))).scalar_one_or_none()
    if str(role or "").upper() != ADMIN_ROLE:
        raise SettingsError(403, "Réservé aux administrateurs.")
    return actor_uuid


async def update_recurring_settings(
    session: AsyncSession,
    *,
    actor_id: Any,
    minimum_start_lead_days: Any,
    expected_version: int | None = None,
    ip_address: str | None = None,
) -> RecurringSettings:
    """Met à jour le délai minimal avant première livraison. Admin uniquement, validé, audité.

    L'appelant (route HTTP) commit la session ; cette fonction ne commit pas."""
    actor_uuid = await _require_admin(session, actor_id)
    try:
        new_value = validate_lead_days(minimum_start_lead_days)
    except InvalidLeadDays as exc:
        raise SettingsError(422, str(exc)) from None

    row = (
        await session.execute(
            select(PlatformSetting).where(PlatformSetting.key == RECURRING_START_LEAD_KEY).with_for_update()
        )
    ).scalar_one_or_none()
    old_value = _lead_days_from_row(row)
    old_version = int(row.version) if row is not None else 0
    if expected_version is not None and int(expected_version) != old_version:
        raise SettingsError(409, "Le réglage a changé entre-temps : rechargez la valeur actuelle.")

    if row is None:
        row = PlatformSetting(key=RECURRING_START_LEAD_KEY, value=new_value, version=1, updated_by_id=actor_uuid)
        session.add(row)
    else:
        row.value = new_value
        row.version = old_version + 1
        row.updated_by_id = actor_uuid
        row.updated_at = datetime.now()
    session.add(
        AuditLog(
            actor_id=actor_uuid,
            action=AUDIT_ACTION_SETTING_CHANGED,
            entity_type="PLATFORM_SETTING",
            entity_id=RECURRING_START_LEAD_KEY,
            old_value={"minimum_start_lead_days": old_value, "version": old_version},
            new_value={"minimum_start_lead_days": new_value, "version": old_version + 1},
            ip_address=ip_address,
        )
    )
    await session.flush()
    return RecurringSettings(new_value, "DATABASE", old_version + 1, row.updated_at, str(actor_uuid))


__all__ = [
    "RECURRING_START_LEAD_KEY",
    "AUDIT_ACTION_SETTING_CHANGED",
    "RecurringSettings",
    "SettingsError",
    "get_recurring_settings",
    "update_recurring_settings",
]
