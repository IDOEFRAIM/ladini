"""Appelant de test qui vient de LIRE l'état avant d'écrire (B26 : la version de l'état observé est obligatoire).

`observed_update` lit en SQL la version courante (besoin : `updated_at` en µs ; occurrence : `version`) puis appelle le service —
exactement ce que fait un écran présenté juste avant la réponse de l'acheteur. Les versions explicites priment.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import text

from ladini.services.database.recurring_supply import (
    RECURRING_OCCURRENCE_ACTIONS,
    recurring_need_version_of,
)


async def observed_update(svc: Any, session: Any, *, recurring_need_id: Any, action: str, **kw: Any) -> Any:
    need_id = str(recurring_need_id)
    if action in RECURRING_OCCURRENCE_ACTIONS:
        if "expected_occurrence_version" not in kw:
            day = kw.get("occurrence_date")
            day = day if isinstance(day, datetime) else datetime.combine(day, datetime.min.time())
            row = (await session.execute(
                text("select version from marketplace.recurring_need_occurrences where recurring_need_id = :n and occurrence_date = :d"),
                {"n": need_id, "d": day})).first()
            kw["expected_occurrence_version"] = int(row[0]) if row else 0  # aucune occurrence : refus du service, pas de comparaison
    elif "expected_version" not in kw:
        row = (await session.execute(
            text("select updated_at from marketplace.recurring_needs where id = :n"), {"n": need_id})).first()
        kw["expected_version"] = recurring_need_version_of(row[0]) if row else 0
    return await svc.update_recurring_need(recurring_need_id=recurring_need_id, action=action, **kw)
