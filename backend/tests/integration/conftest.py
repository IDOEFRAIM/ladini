"""Les tests d'intégration historiques supposent un menu producteur COMPLET (6-7 offres numérotées) : la shortlist
(`BUYER_SHORTLIST_SIZE`, défaut 5 en production) y est désactivée. `test_conversational_autonomy_e2e.py` la réactive explicitement."""
from __future__ import annotations

import pytest

from ladini.core.settings import settings


@pytest.fixture(autouse=True)
def _full_producer_menu(monkeypatch):
    monkeypatch.setattr(settings, "BUYER_SHORTLIST_SIZE", 0, raising=False)
