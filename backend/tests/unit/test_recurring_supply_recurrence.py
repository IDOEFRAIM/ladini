"""Récurrence de l'approvisionnement — fonctions pures (`domain/recurring_supply/recurrence.py`).

Toutes les dates sont FIXES (jamais `datetime.now()`) : la logique est déterministe et testée aux
limites (fin de mois, changement d'année, 29 février d'une année bissextile)."""
from __future__ import annotations

from datetime import date

import pytest

from ladini.domain.recurring_supply.recurrence import (
    InvalidRecurrenceRule,
    RecurrenceRule,
    generate_occurrence_dates,
    occurrence_window,
)


def d(s: str) -> date:
    return date.fromisoformat(s)


# ── DAILY ────────────────────────────────────────────────────────────────

def test_daily_generates_every_day_in_the_window():
    rule = RecurrenceRule(recurrence_type="DAILY", starts_at=d("2026-09-01"))
    got = generate_occurrence_dates(rule, from_date=d("2026-09-01"), to_date=d("2026-09-07"))
    assert got == [d(f"2026-09-0{i}") for i in range(1, 8)]


def test_daily_except_sunday_excludes_iso_weekday_7():
    rule = RecurrenceRule(recurrence_type="DAILY", excluded_weekdays=[7], starts_at=d("2026-09-01"))
    # 2026-09-06 est un dimanche (vérifié : isoweekday() == 7)
    assert d("2026-09-06").isoweekday() == 7
    got = generate_occurrence_dates(rule, from_date=d("2026-09-01"), to_date=d("2026-09-07"))
    assert d("2026-09-06") not in got
    assert len(got) == 6


# ── WEEKLY_DAYS ──────────────────────────────────────────────────────────

def test_weekly_days_keeps_only_the_listed_iso_weekdays():
    # lundi=1, mercredi=3, vendredi=5 ; 2026-09-01 est un mardi -> la semaine ne contient, dans l'ordre
    # chronologique, que mercredi 09-02, vendredi 09-04, puis lundi 09-07 (semaine suivante).
    rule = RecurrenceRule(recurrence_type="WEEKLY_DAYS", weekly_days=[1, 3, 5], starts_at=d("2026-09-01"))
    got = generate_occurrence_dates(rule, from_date=d("2026-09-01"), to_date=d("2026-09-07"))
    assert got == [d("2026-09-02"), d("2026-09-04"), d("2026-09-07")]
    assert {g.isoweekday() for g in got} == {1, 3, 5}


def test_weekly_days_without_any_day_is_rejected():
    with pytest.raises(InvalidRecurrenceRule):
        RecurrenceRule(recurrence_type="WEEKLY_DAYS", weekly_days=[], starts_at=d("2026-09-01"))


# ── WEEKLY ───────────────────────────────────────────────────────────────

def test_weekly_repeats_on_the_start_date_weekday_only():
    # 2026-09-01 est un mardi (isoweekday 2)
    assert d("2026-09-01").isoweekday() == 2
    rule = RecurrenceRule(recurrence_type="WEEKLY", starts_at=d("2026-09-01"))
    got = generate_occurrence_dates(rule, from_date=d("2026-09-01"), to_date=d("2026-09-15"))
    assert got == [d("2026-09-01"), d("2026-09-08"), d("2026-09-15")]


# ── ONE_OFF ──────────────────────────────────────────────────────────────

def test_one_off_produces_exactly_one_date_if_in_window():
    rule = RecurrenceRule(recurrence_type="ONE_OFF", starts_at=d("2026-09-04"))
    got = generate_occurrence_dates(rule, from_date=d("2026-09-01"), to_date=d("2026-09-07"))
    assert got == [d("2026-09-04")]


def test_one_off_produces_nothing_outside_the_window():
    rule = RecurrenceRule(recurrence_type="ONE_OFF", starts_at=d("2026-09-04"))
    got = generate_occurrence_dates(rule, from_date=d("2026-09-05"), to_date=d("2026-09-10"))
    assert got == []


# ── starts_at future / ends_at ───────────────────────────────────────────

def test_starts_at_in_the_future_delays_the_first_occurrence():
    rule = RecurrenceRule(recurrence_type="DAILY", starts_at=d("2026-09-05"))
    got = generate_occurrence_dates(rule, from_date=d("2026-09-01"), to_date=d("2026-09-07"))
    assert got == [d("2026-09-05"), d("2026-09-06"), d("2026-09-07")]


def test_ends_at_stops_generation_after_the_last_valid_day():
    rule = RecurrenceRule(recurrence_type="DAILY", starts_at=d("2026-09-01"), ends_at=d("2026-09-03"))
    got = generate_occurrence_dates(rule, from_date=d("2026-09-01"), to_date=d("2026-09-07"))
    assert got == [d("2026-09-01"), d("2026-09-02"), d("2026-09-03")]


def test_a_window_entirely_before_starts_at_yields_nothing():
    rule = RecurrenceRule(recurrence_type="DAILY", starts_at=d("2026-09-10"))
    assert generate_occurrence_dates(rule, from_date=d("2026-09-01"), to_date=d("2026-09-05")) == []


def test_a_window_entirely_after_ends_at_yields_nothing():
    rule = RecurrenceRule(recurrence_type="DAILY", starts_at=d("2026-09-01"), ends_at=d("2026-09-05"))
    assert generate_occurrence_dates(rule, from_date=d("2026-09-10"), to_date=d("2026-09-15")) == []


# ── limites temporelles ──────────────────────────────────────────────────

def test_crossing_a_month_boundary():
    rule = RecurrenceRule(recurrence_type="DAILY", starts_at=d("2026-09-28"))
    got = generate_occurrence_dates(rule, from_date=d("2026-09-28"), to_date=d("2026-10-02"))
    assert got == [d("2026-09-28"), d("2026-09-29"), d("2026-09-30"), d("2026-10-01"), d("2026-10-02")]


def test_crossing_a_year_boundary():
    rule = RecurrenceRule(recurrence_type="DAILY", starts_at=d("2026-12-30"))
    got = generate_occurrence_dates(rule, from_date=d("2026-12-30"), to_date=d("2027-01-02"))
    assert got == [d("2026-12-30"), d("2026-12-31"), d("2027-01-01"), d("2027-01-02")]


def test_february_of_a_leap_year_includes_the_29th():
    # 2028 est bissextile (divisible par 4, pas par 100)
    rule = RecurrenceRule(recurrence_type="DAILY", starts_at=d("2028-02-27"))
    got = generate_occurrence_dates(rule, from_date=d("2028-02-27"), to_date=d("2028-03-01"))
    assert got == [d("2028-02-27"), d("2028-02-28"), d("2028-02-29"), d("2028-03-01")]


def test_february_of_a_non_leap_year_skips_straight_to_march_1st():
    # 2026 n'est pas bissextile : pas de 29 février.
    rule = RecurrenceRule(recurrence_type="DAILY", starts_at=d("2026-02-27"))
    got = generate_occurrence_dates(rule, from_date=d("2026-02-27"), to_date=d("2026-03-01"))
    assert got == [d("2026-02-27"), d("2026-02-28"), d("2026-03-01")]


# ── validation ───────────────────────────────────────────────────────────

def test_an_out_of_range_iso_weekday_is_rejected():
    with pytest.raises(InvalidRecurrenceRule):
        RecurrenceRule(recurrence_type="WEEKLY_DAYS", weekly_days=[0], starts_at=d("2026-09-01"))
    with pytest.raises(InvalidRecurrenceRule):
        RecurrenceRule(recurrence_type="DAILY", excluded_weekdays=[8], starts_at=d("2026-09-01"))


def test_an_unknown_recurrence_type_is_rejected():
    with pytest.raises(InvalidRecurrenceRule):
        RecurrenceRule(recurrence_type="MONTHLY", starts_at=d("2026-09-01"))


def test_a_reversed_window_yields_nothing():
    rule = RecurrenceRule(recurrence_type="DAILY", starts_at=d("2026-09-01"))
    assert generate_occurrence_dates(rule, from_date=d("2026-09-10"), to_date=d("2026-09-01")) == []


# ── fenêtre de matérialisation (J -> J+7) ─────────────────────────────────

def test_occurrence_window_is_seven_days_by_default():
    assert occurrence_window(from_date=d("2026-09-01")) == d("2026-09-08")


def test_occurrence_window_accepts_a_custom_size():
    assert occurrence_window(from_date=d("2026-09-01"), window_days=1) == d("2026-09-02")
