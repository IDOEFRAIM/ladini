"""Délai minimal avant première livraison d'un besoin récurrent (`domain/recurring_supply/start_policy.py`).

Fonctions PURES : dates fixes, aucune horloge, aucune base. Verrouille la règle

    minimum_start_date = aujourd'hui + lead_days

la validation de la configuration admin, l'alignement sur le calendrier (WEEKLY/MONTHLY/WEEKLY_DAYS) et les
frontières (fin de mois, année bissextile).
"""
from __future__ import annotations

from datetime import date

import pytest

from ladini.domain.recurring_supply.recurrence import (
    RecurrenceRule,
    generate_occurrence_dates,
)
from ladini.domain.recurring_supply.start_policy import (
    DEFAULT_START_LEAD_DAYS,
    MAX_START_LEAD_DAYS,
    InvalidLeadDays,
    first_due_date,
    minimum_start_date,
    resolve_start_date,
    validate_lead_days,
)

TODAY = date(2026, 10, 5)  # un lundi


def test_default_lead_time_is_four_days():
    assert DEFAULT_START_LEAD_DAYS == 4
    assert resolve_start_date(requested=None, today=TODAY, lead_days=DEFAULT_START_LEAD_DAYS).effective_start == date(2026, 10, 9)


@pytest.mark.parametrize("lead,expected", [(0, date(2026, 10, 5)), (1, date(2026, 10, 6)), (4, date(2026, 10, 9)), (7, date(2026, 10, 12))])
def test_no_explicit_date_starts_at_the_minimum(lead, expected):
    decision = resolve_start_date(requested=None, today=TODAY, lead_days=lead)
    assert decision.effective_start == decision.minimum == expected
    assert decision.adjusted is False and decision.requested is None


def test_explicit_date_after_the_minimum_is_respected():
    decision = resolve_start_date(requested=date(2026, 10, 20), today=TODAY, lead_days=4)
    assert decision.effective_start == date(2026, 10, 20) and decision.adjusted is False


def test_explicit_date_exactly_on_the_minimum_is_respected_not_adjusted():
    decision = resolve_start_date(requested=date(2026, 10, 9), today=TODAY, lead_days=4)
    assert decision.effective_start == date(2026, 10, 9) and decision.adjusted is False


@pytest.mark.parametrize("requested", [date(2026, 10, 6), date(2026, 10, 5), date(2026, 9, 1)])
def test_a_too_early_or_past_date_is_never_accepted_silently(requested):
    decision = resolve_start_date(requested=requested, today=TODAY, lead_days=4)
    assert decision.effective_start == date(2026, 10, 9)
    assert decision.adjusted is True and decision.requested == requested


def test_lead_zero_accepts_today():
    decision = resolve_start_date(requested=TODAY, today=TODAY, lead_days=0)
    assert decision.effective_start == TODAY and decision.adjusted is False


@pytest.mark.parametrize("bad", [-1, MAX_START_LEAD_DAYS + 1, 2.5, "4", None, True, [4]])
def test_invalid_lead_days_are_rejected(bad):
    with pytest.raises(InvalidLeadDays):
        validate_lead_days(bad)


@pytest.mark.parametrize("good", [0, 1, 4, 7, MAX_START_LEAD_DAYS, 4.0])
def test_valid_lead_days_are_accepted(good):
    assert validate_lead_days(good) == int(good)


def test_weekly_first_occurrence_is_the_minimum_then_every_seven_days():
    start = resolve_start_date(requested=None, today=TODAY, lead_days=4).effective_start
    rule = RecurrenceRule(recurrence_type="WEEKLY", starts_at=start)
    assert first_due_date(rule) == date(2026, 10, 9)
    assert generate_occurrence_dates(rule, from_date=TODAY, to_date=date(2026, 10, 31)) == [
        date(2026, 10, 9), date(2026, 10, 16), date(2026, 10, 23), date(2026, 10, 30)
    ]


def test_weekly_days_first_occurrence_is_the_first_schedule_valid_date_on_or_after_the_minimum():
    # minimum = vendredi 9 ; jour de livraison configuré = lundi (1) -> lundi 12, jamais avant le minimum
    start = resolve_start_date(requested=None, today=TODAY, lead_days=4).effective_start
    rule = RecurrenceRule(recurrence_type="WEEKLY_DAYS", weekly_days=(1,), starts_at=start)
    assert first_due_date(rule) == date(2026, 10, 12)
    # minimum = mercredi 7 (lead=2) ; jour configuré = vendredi (5) -> vendredi 9
    wed = resolve_start_date(requested=None, today=TODAY, lead_days=2).effective_start
    assert first_due_date(RecurrenceRule(recurrence_type="WEEKLY_DAYS", weekly_days=(5,), starts_at=wed)) == date(2026, 10, 9)


def test_daily_with_an_excluded_weekday_skips_it_on_the_first_day():
    # minimum = samedi 10 (lead=5) ; dimanche exclu : le premier jour dû est bien le samedi
    start = resolve_start_date(requested=None, today=TODAY, lead_days=5).effective_start
    assert start == date(2026, 10, 10)
    rule = RecurrenceRule(recurrence_type="DAILY", excluded_weekdays=(6,), starts_at=start)
    assert first_due_date(rule) == date(2026, 10, 11)  # samedi exclu (6) -> dimanche


def test_monthly_anchors_on_the_effective_start_day_and_never_before_the_minimum():
    # Le 5 octobre + 4 jours = 9 octobre : un besoin mensuel « le 6 » ne peut PAS démarrer le 6 octobre.
    start = resolve_start_date(requested=date(2026, 10, 6), today=TODAY, lead_days=4).effective_start
    assert start == date(2026, 10, 9)
    rule = RecurrenceRule(recurrence_type="MONTHLY", starts_at=start)
    assert first_due_date(rule) == date(2026, 10, 9)
    assert generate_occurrence_dates(rule, from_date=TODAY, to_date=date(2026, 12, 31)) == [
        date(2026, 10, 9), date(2026, 11, 9), date(2026, 12, 9)
    ]
    # Demande explicite « à partir du 6 novembre » : respectée, ancre = 6.
    explicit = resolve_start_date(requested=date(2026, 11, 6), today=TODAY, lead_days=4)
    assert explicit.effective_start == date(2026, 11, 6) and not explicit.adjusted


def test_month_rollover_end_of_month():
    decision = resolve_start_date(requested=None, today=date(2026, 10, 29), lead_days=4)
    assert decision.effective_start == date(2026, 11, 2)


def test_year_rollover():
    assert minimum_start_date(date(2026, 12, 30), 4) == date(2027, 1, 3)


def test_february_leap_and_non_leap_boundaries():
    assert minimum_start_date(date(2028, 2, 26), 4) == date(2028, 3, 1)  # 2028 bissextile : 29 février existe
    assert minimum_start_date(date(2027, 2, 26), 4) == date(2027, 3, 2)
    assert minimum_start_date(date(2028, 2, 25), 4) == date(2028, 2, 29)


def test_monthly_anchored_on_the_31st_falls_back_to_the_last_day_after_a_lead_time_start():
    start = resolve_start_date(requested=None, today=date(2026, 1, 27), lead_days=4).effective_start
    assert start == date(2026, 1, 31)
    rule = RecurrenceRule(recurrence_type="MONTHLY", starts_at=start)
    assert generate_occurrence_dates(rule, from_date=date(2026, 1, 27), to_date=date(2026, 3, 31)) == [
        date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31)
    ]


def test_lead_time_is_not_hardcoded_in_the_flow_or_service():
    """Aucune constante `timedelta(days=4)`/`+ 4` métier dans le flow conversationnel ni dans le service : la valeur
    vient de `get_recurring_settings` (source unique) ; seul `start_policy` porte le DÉFAUT."""
    import inspect

    import ladini.graphs.agents.market_coach.flows.buyer.recurring_need as flow
    import ladini.services.database.recurring_supply as svc

    for module in (flow, svc):
        src = inspect.getsource(module)
        assert "timedelta(days=4)" not in src and "DEFAULT_START_LEAD_DAYS" not in src
    assert "get_recurring_settings" in inspect.getsource(svc)
    assert "(_today() + timedelta(days=1))" not in inspect.getsource(svc).split("def _start_decision")[0]
