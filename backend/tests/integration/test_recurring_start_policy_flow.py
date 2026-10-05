"""Délai minimal avant première livraison — conversation RÉELLE (graphe réel, harnais).

Le DOMAINE (`start_policy`, réglage admin) décide de la date ; le LLM n'extrait que l'intention temporelle.
Double du service : `tests/harness/recurring.py` (aujourd'hui = 5 octobre 2026, délai = 4 jours).
"""
from __future__ import annotations

import pytest

from tests.harness import ConversationHarness, new_task

pytestmark = pytest.mark.integration


def _chevres(**extra):
    return new_task("CREATE_RECURRING_NEED", product="chèvre", quantity=3.0, recurrence_type="WEEKLY", **extra)


@pytest.fixture()
def conv():
    with ConversationHarness(role="BUYER", channel="whatsapp") as harness:
        yield harness


def test_no_explicit_date_announces_today_plus_lead_time_never_tomorrow(conv):
    t1 = conv.send("j'ai besoin de 3 chèvres chaque semaine", llm=_chevres())
    assert "Première livraison prévue : 9 octobre" in t1.response
    assert "demain" not in t1.response.lower()
    assert t1.draft()["starts_at"] == "2026-10-09"


def test_a_too_early_date_is_pushed_back_and_explained(conv):
    t1 = conv.send("j'ai besoin de 3 chèvres chaque semaine, commence demain", llm=_chevres(starts_at="2026-10-06"))
    assert "au plus tôt le 9 octobre" in t1.response
    assert "Première livraison prévue : 9 octobre" in t1.response
    assert t1.draft()["starts_at"] == "2026-10-09"


def test_a_valid_explicit_date_is_respected_without_an_explanation(conv):
    t1 = conv.send("3 chèvres chaque semaine à partir du 20 octobre", llm=_chevres(starts_at="2026-10-20"))
    assert "Première livraison prévue : 20 octobre" in t1.response
    assert "au plus tôt" not in t1.response
    assert t1.draft()["starts_at"] == "2026-10-20"


def test_asap_means_the_minimum_date_not_tomorrow(conv):
    # « dès que possible » : le modèle renvoie starts_at=null -> le domaine retient le minimum
    t1 = conv.send("3 chèvres chaque semaine dès que possible", llm=_chevres(starts_at=None))
    assert t1.draft()["starts_at"] == "2026-10-09"


def test_confirmation_creates_the_need_at_the_domain_date(conv):
    conv.send("j'ai besoin de 3 chèvres chaque semaine", llm=_chevres())
    t2 = conv.send("oui")
    created = [kw for tool, kw in conv.runtime.calls if tool == "create_recurring_need"]
    assert len(created) == 1 and created[0]["starts_at"] == "2026-10-09"
    assert "9 octobre" in t2.response


def test_a_setting_changed_between_summary_and_confirmation_shows_the_real_date(conv):
    conv.send("j'ai besoin de 3 chèvres chaque semaine", llm=_chevres())  # annonce le 9 (délai 4)
    conv.server.lead_days = 7  # l'admin change le réglage avant la confirmation
    t2 = conv.send("oui")
    # Le service retient la valeur COURANTE à la création (12 octobre) et le message l'affiche.
    assert "12 octobre" in t2.response
