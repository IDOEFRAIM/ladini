"""Délai minimal avant la PREMIÈRE livraison d'un besoin récurrent — fonctions PURES.

    minimum_start_date = aujourd'hui (métier) + `lead_days`

`lead_days` est une configuration ADMIN globale (`services/platform_settings.py`) — jamais une constante
de flow. Ce module ne connaît ni la base ni l'horloge : l'appelant fournit `today` et `lead_days`.

Le délai de départ n'est PAS la fréquence (`recurrence.py`), ni la fenêtre de confirmation producteur
(`RECURRING_PRODUCER_CONFIRMATION_LEAD_DAYS`), ni la fenêtre de récupération : trois notions distinctes.

Sémantique de l'ancre : `RecurrenceRule` ancre WEEKLY sur le jour de semaine de `starts_at` et MONTHLY
sur son quantième. `effective_start` est donc la date même de la première échéance pour ces deux types ;
pour DAILY/WEEKLY_DAYS la première échéance est la première date valide du calendrier >= `effective_start`
(`first_due_date`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from ladini.domain.recurring_supply.recurrence import RecurrenceRule, next_due_date

#: Valeur par défaut quand aucune configuration admin n'existe encore (comportement initial souhaité).
DEFAULT_START_LEAD_DAYS: int = 4

#: Bornes de la configuration admin. Aucune convention de bornes n'existe dans le dépôt pour ce type de
#: paramètre (le réglage voisin `RECURRING_PRODUCER_CONFIRMATION_LEAD_DAYS` n'est borné que par `max(0, …)`) ;
#: 30 jours est un plafond de sécurité contre une faute de frappe, pas une règle métier.
MIN_START_LEAD_DAYS: int = 0
MAX_START_LEAD_DAYS: int = 30


class InvalidLeadDays(ValueError):
    """`minimum_recurring_start_lead_days` hors bornes ou de mauvais type."""


def validate_lead_days(value: object) -> int:
    """Entier entre `MIN_START_LEAD_DAYS` et `MAX_START_LEAD_DAYS`. Les booléens et les flottants non
    entiers sont refusés (`True` n'est pas « 1 jour »)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidLeadDays("Le délai doit être un nombre entier de jours.")
    if isinstance(value, float):
        if not value.is_integer():
            raise InvalidLeadDays("Le délai doit être un nombre entier de jours.")
        value = int(value)
    if not MIN_START_LEAD_DAYS <= value <= MAX_START_LEAD_DAYS:
        raise InvalidLeadDays(f"Le délai doit être compris entre {MIN_START_LEAD_DAYS} et {MAX_START_LEAD_DAYS} jours.")
    return int(value)


def minimum_start_date(today: date, lead_days: int) -> date:
    return today + timedelta(days=validate_lead_days(lead_days))


@dataclass(frozen=True)
class StartDecision:
    #: Date de début RETENUE (écrite dans `RecurringNeed.starts_at`).
    effective_start: date
    #: Plus petite date admissible (`today + lead_days`).
    minimum: date
    #: Date demandée par l'utilisateur, `None` s'il n'en a pas donné (ou « dès que possible »).
    requested: date | None
    #: `True` si la date demandée était trop proche et a été repoussée au minimum.
    adjusted: bool
    lead_days: int


def resolve_start_date(*, requested: date | None, today: date, lead_days: int) -> StartDecision:
    """Date de début d'un nouveau besoin : la date demandée si elle respecte le délai, sinon le minimum.

    - aucune date, « dès que possible », « le plus tôt possible » (-> `requested=None`) : le minimum ;
    - date >= minimum : respectée telle quelle ;
    - date < minimum (demain, aujourd'hui, passée) : jamais acceptée silencieusement — `adjusted=True`,
      l'appelant doit le dire à l'utilisateur.
    """
    minimum = minimum_start_date(today, lead_days)
    if requested is None:
        return StartDecision(minimum, minimum, None, False, int(lead_days))
    if requested >= minimum:
        return StartDecision(requested, minimum, requested, False, int(lead_days))
    return StartDecision(minimum, minimum, requested, True, int(lead_days))


def first_due_date(rule: RecurrenceRule) -> date | None:
    """Première échéance réelle de la règle (`starts_at` inclus) — ce que l'on annonce à l'utilisateur."""
    first: date | None = next_due_date(rule, from_date=rule.starts_at)
    return first


__all__ = [
    "DEFAULT_START_LEAD_DAYS",
    "MIN_START_LEAD_DAYS",
    "MAX_START_LEAD_DAYS",
    "InvalidLeadDays",
    "StartDecision",
    "first_due_date",
    "minimum_start_date",
    "resolve_start_date",
    "validate_lead_days",
]
