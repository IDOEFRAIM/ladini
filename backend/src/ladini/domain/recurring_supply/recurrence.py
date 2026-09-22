"""Récurrence de l'approvisionnement — fonctions PURES, sans DB, sans LLM, entièrement déterministes.

Mandat Phase 2 (§7) : le LLM comprend l'utilisateur, CE module décide des dates. Pas de RRULE, pas
d'expression cron — seulement les 4 cas nécessaires au pilote : `DAILY`, `WEEKLY_DAYS`, `WEEKLY`,
`ONE_OFF`. Toute date passée à ces fonctions doit être fournie par l'appelant (jamais `datetime.now()`
ici) : c'est ce qui rend le module testable avec des dates fixes, y compris aux limites (fin de mois,
29 février, changement d'année).

Convention des jours : ISO 8601 (1=lundi .. 7=dimanche), la même que `date.isoweekday()`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Sequence

# Fenêtre de matérialisation par défaut à la création/à l'extension quotidienne d'un besoin (mandat §8).
OCCURRENCE_WINDOW_DAYS = 7

RECURRENCE_TYPES = ("DAILY", "WEEKLY_DAYS", "WEEKLY", "ONE_OFF")


class InvalidRecurrenceRule(ValueError):
    """Règle de récurrence incohérente (ex: WEEKLY_DAYS sans jour, jour ISO hors 1..7)."""


@dataclass(frozen=True)
class RecurrenceRule:
    """Paramètres de récurrence d'un `RecurringNeed` — projection pure des colonnes DB pertinentes.

    `weekly_days`/`excluded_weekdays` : jours ISO (1=lundi..7=dimanche). `excluded_weekdays` s'applique
    à TOUS les types de récurrence (ex: DAILY + dimanche exclu = "tous les jours sauf le dimanche").
    """

    recurrence_type: str
    weekly_days: Sequence[int] = ()
    excluded_weekdays: Sequence[int] = ()
    starts_at: date = None  # type: ignore[assignment]
    ends_at: date | None = None

    def __post_init__(self) -> None:
        if self.recurrence_type not in RECURRENCE_TYPES:
            raise InvalidRecurrenceRule(f"recurrence_type inconnu : {self.recurrence_type!r}")
        if self.starts_at is None:
            raise InvalidRecurrenceRule("starts_at est obligatoire")
        for label, days in (("weekly_days", self.weekly_days), ("excluded_weekdays", self.excluded_weekdays)):
            for d in days:
                if not 1 <= d <= 7:
                    raise InvalidRecurrenceRule(f"{label} contient un jour ISO invalide : {d!r} (attendu 1..7)")
        if self.recurrence_type == "WEEKLY_DAYS" and not self.weekly_days:
            raise InvalidRecurrenceRule("WEEKLY_DAYS nécessite au moins un jour dans weekly_days")


def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


def _is_due(rule: RecurrenceRule, day: date) -> bool:
    """Un jour donné est-il concerné par la règle, avant application des exclusions ?"""
    weekday = day.isoweekday()
    if rule.recurrence_type == "DAILY":
        return True
    if rule.recurrence_type == "WEEKLY_DAYS":
        return weekday in rule.weekly_days
    if rule.recurrence_type == "WEEKLY":
        return weekday == _as_date(rule.starts_at).isoweekday()
    if rule.recurrence_type == "ONE_OFF":
        return day == _as_date(rule.starts_at)
    raise InvalidRecurrenceRule(rule.recurrence_type)  # pragma: no cover — __post_init__ l'exclut déjà


def generate_occurrence_dates(
    rule: RecurrenceRule,
    *,
    from_date: date | datetime,
    to_date: date | datetime,
) -> list[date]:
    """Dates concrètes dues sur `[from_date, to_date]` (bornes incluses), en tenant compte de
    `starts_at`/`ends_at` (fenêtre de validité du besoin) et `excluded_weekdays` (exclusion permanente).

    Fonction pure, déterministe, sans effet de bord — aucune écriture, aucune lecture d'horloge.
    """
    start = _as_date(from_date)
    end = _as_date(to_date)
    if end < start:
        return []

    lower = max(start, _as_date(rule.starts_at))
    upper = min(end, _as_date(rule.ends_at)) if rule.ends_at is not None else end
    if upper < lower:
        return []

    dates: list[date] = []
    day = lower
    one_day = timedelta(days=1)
    while day <= upper:
        if day.isoweekday() not in rule.excluded_weekdays and _is_due(rule, day):
            dates.append(day)
        day += one_day
    return dates


def occurrence_window(*, from_date: date | datetime, window_days: int = OCCURRENCE_WINDOW_DAYS) -> date:
    """Borne supérieure de la fenêtre courte de matérialisation (mandat §8 : aujourd'hui → J+7)."""
    return _as_date(from_date) + timedelta(days=window_days)
