"""`flows/buyer/gps_delivery_gate.py` — implémentation UNIQUE du gate GPS
2 étapes, partagée par `order_tracking.py::finalize_winner` et
`preorder.py::create_preorder`. Avant cette consolidation, chaque flow
portait sa propre copie légèrement différente — exactement la fragilité
dénoncée par l'utilisateur (2026-08-14) : ajouter/corriger la logique de
localisation demandait de toucher plusieurs endroits, et il était facile
d'en oublier un. Voir [[precommande-architecture-consolidation-2026-08]]."""
from __future__ import annotations

from typing import Any, Dict

from tests.conftest import run

from agriconnect.graphs.agents.market_coach.flows.buyer.gps_delivery_gate import (
    _GPS_FIRST_TIME_PROMPT,
    _GPS_HABITUAL_PROMPT,
    _GPS_TEXT_REMINDER,
    enter_gps_stage,
    resolve_gps_stage,
)

_MODULE = "agriconnect.graphs.agents.market_coach.flows.buyer.gps_delivery_gate"


class _RuntimeNoLLM:
    llm = None


class TestEnterGpsStage:
    def test_a_stored_default_offers_reuse(self, monkeypatch):
        async def _fake(mc_runtime, phone):
            return 12.35, -1.5
        monkeypatch.setattr(f"{_MODULE}._get_stored_location", _fake)

        result = run(enter_gps_stage(_RuntimeNoLLM(), "+22670000001"))

        assert result["gps_stage"] is True
        assert result["gps_default"] == {"lat": 12.35, "lon": -1.5}
        assert result["prompt"] == _GPS_HABITUAL_PROMPT

    def test_no_stored_default_asks_to_share_one(self, monkeypatch):
        async def _fake(mc_runtime, phone):
            return None, None
        monkeypatch.setattr(f"{_MODULE}._get_stored_location", _fake)

        result = run(enter_gps_stage(_RuntimeNoLLM(), "+22670000001"))

        assert result["gps_default"] is None
        assert result["prompt"] == _GPS_FIRST_TIME_PROMPT


class TestResolveGpsStage:
    def test_a_shared_location_re_reads_the_profile_and_resolves(self, monkeypatch):
        async def _fake(mc_runtime, phone):
            return 13.0, -1.0
        monkeypatch.setattr(f"{_MODULE}._get_stored_location", _fake)

        result = run(resolve_gps_stage(
            _RuntimeNoLLM(), "+22670000001",
            location_shared=True, is_yes=False, gps_default=None,
        ))

        assert result.resolved is True
        assert result.lat == 13.0
        assert result.lon == -1.0

    def test_a_shared_location_that_failed_to_persist_asks_to_reshare(self, monkeypatch):
        async def _fake(mc_runtime, phone):
            return None, None
        monkeypatch.setattr(f"{_MODULE}._get_stored_location", _fake)

        result = run(resolve_gps_stage(
            _RuntimeNoLLM(), "+22670000001",
            location_shared=True, is_yes=False, gps_default=None,
        ))

        assert result.resolved is False
        assert "repartager" in result.message

    def test_confirming_the_habitual_default_resolves_with_those_coordinates(self):
        result = run(resolve_gps_stage(
            _RuntimeNoLLM(), "+22670000001",
            location_shared=False, is_yes=True, gps_default={"lat": 12.35, "lon": -1.5},
        ))

        assert result.resolved is True
        assert result.lat == 12.35
        assert result.lon == -1.5

    def test_confirming_without_a_known_default_asks_to_share_one(self):
        result = run(resolve_gps_stage(
            _RuntimeNoLLM(), "+22670000001",
            location_shared=False, is_yes=True, gps_default=None,
        ))

        assert result.resolved is False
        assert result.message == _GPS_FIRST_TIME_PROMPT

    def test_free_text_falls_back_to_the_canned_reminder_without_an_llm(self):
        result = run(resolve_gps_stage(
            _RuntimeNoLLM(), "+22670000001",
            location_shared=False, is_yes=False, gps_default=None,
            user_text="depuis quand œuf a tete",
        ))

        assert result.resolved is False
        assert result.message == _GPS_TEXT_REMINDER

    def test_free_text_with_an_llm_gets_an_adaptive_note_before_the_reminder(self, monkeypatch):
        async def _fake_deviation(mc_runtime, user_text, context):
            return "Je comprends ta question — répondons-y juste après."
        monkeypatch.setattr(f"{_MODULE}.llm_deviation_reply", _fake_deviation)

        result = run(resolve_gps_stage(
            _RuntimeNoLLM(), "+22670000001",
            location_shared=False, is_yes=False, gps_default=None,
            user_text="c'est pour les moisissures na ?",
        ))

        assert result.resolved is False
        assert result.message.startswith("Je comprends ta question")
        assert _GPS_TEXT_REMINDER in result.message
