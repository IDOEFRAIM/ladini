"""`flows/buyer/gps_delivery_gate.py` — implémentation UNIQUE du gate GPS
2 étapes, partagée par `order_tracking.py::finalize_winner` et
`preorder.py::create_preorder`. Avant cette consolidation, chaque flow
portait sa propre copie légèrement différente — exactement la fragilité
dénoncée par l'utilisateur (2026-08-14) : ajouter/corriger la logique de
localisation demandait de toucher plusieurs endroits, et il était facile
d'en oublier un. Voir [[precommande-architecture-consolidation-2026-08]]."""
from __future__ import annotations

from ladini.graphs.agents.market_coach.flows.buyer.gps_delivery_gate import (
    _GPS_FIRST_TIME_PROMPT,
    _GPS_HABITUAL_PROMPT,
    _GPS_TEXT_REMINDER,
    enter_gps_stage,
    resolve_gps_stage,
)
from tests.conftest import run

_MODULE = "ladini.graphs.agents.market_coach.flows.buyer.gps_delivery_gate"


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
    def test_a_shared_location_uses_the_outcome_already_resolved_by_the_webhook(self):
        """(2026-09-02, refonte GPS) : plus de relecture DB (`_get_stored_
        location`) pour ce cas — l'issue exacte (`location_outcome`) et les
        coordonnées (`location_lat`/`location_lon`) sont déjà résolues,
        SYNCHRONE, côté webhook AVANT l'enqueue Celery. Élimine la course
        webhook/tâche que l'ancienne relecture pouvait exposer."""
        result = run(resolve_gps_stage(
            _RuntimeNoLLM(), "+22670000001",
            location_shared=True, is_yes=False, gps_default=None,
            location_outcome="NEW_LOCATION_ACCEPTED",
            location_lat=13.0, location_lon=-1.0,
        ))

        assert result.resolved is True
        assert result.lat == 13.0
        assert result.lon == -1.0

    def test_a_persistence_error_asks_to_reshare_distinctly_from_out_of_zone(self):
        result = run(resolve_gps_stage(
            _RuntimeNoLLM(), "+22670000001",
            location_shared=True, is_yes=False, gps_default=None,
            location_outcome="LOCATION_PERSISTENCE_ERROR",
        ))

        assert result.resolved is False
        assert "repartager" in result.message

    def test_a_point_rejected_by_geofencing_is_never_silently_replaced_by_an_old_one(self):
        """(2026-09-02, mandat §31 "no silent fallback") : un point rejeté
        (hors zone) ne doit JAMAIS retomber silencieusement sur une position
        déjà stockée — le message doit être EXPLICITE sur le rejet, jamais
        un point lat/lon renvoyé comme si tout allait bien."""
        result = run(resolve_gps_stage(
            _RuntimeNoLLM(), "+22670000001",
            location_shared=True, is_yes=False, gps_default={"lat": 12.0, "lon": -1.0},
            location_outcome="LOCATION_OUT_OF_ZONE",
        ))

        assert result.resolved is False
        assert result.lat is None
        assert result.lon is None
        assert "hors de notre zone" in result.message

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


class TestGoogleMapsLinkPastedAsText:
    """Incident : « 📍 Ma position actuelle : https://www.google.com/maps?q=..
    (précision ±58 m) » envoyé en TEXTE → GPS redemandé en boucle."""

    _TEXT = (
        "📍 Ma position actuelle : https://www.google.com/maps?q=12.371400,-1.519700 "
        "(précision ±58 m)"
    )

    def test_parser_handles_the_common_google_maps_forms(self):
        from ladini.core.location import parse_google_maps_coordinates as p

        assert p(self._TEXT) == (12.3714, -1.5197)
        assert p("https://www.google.com/maps/@12.37,-1.51,15z") == (12.37, -1.51)
        assert p("https://www.google.com/maps/place/X/data=!3d12.37!4d-1.51") == (12.37, -1.51)
        assert p("https://www.google.com/maps?q=95.0,-1.5") is None
        assert p("salut, voici mon adresse") is None

    def test_parser_accepts_bare_coordinates_but_not_prices(self):
        from ladini.core.location import parse_google_maps_coordinates as p

        assert p("33.264381,-7.586925") == (33.264381, -7.586925)
        assert p("ma position 12.3714, -1.5197 merci") == (12.3714, -1.5197)
        assert p("12.5, 3.2") is None
        assert p("prix 495000.50 quantite 95") is None

    def test_accepted_point_resolves_even_when_the_interpreter_read_it_as_yes(self, monkeypatch):
        async def _fake_persist(phone, lat, lon):
            from ladini.core.location import LocationOutcome
            return LocationOutcome.NEW_LOCATION_ACCEPTED, None
        monkeypatch.setattr(f"{_MODULE}.persist_shared_location", _fake_persist)

        result = run(resolve_gps_stage(
            _RuntimeNoLLM(), "+22670000001",
            location_shared=False, is_yes=True, gps_default=None, user_text=self._TEXT,
        ))

        assert result.resolved is True
        assert (result.lat, result.lon) == (12.3714, -1.5197)

    def test_out_of_zone_link_is_rejected_explicitly_not_looped(self, monkeypatch):
        async def _fake_persist(phone, lat, lon):
            from ladini.core.location import LocationOutcome
            return LocationOutcome.LOCATION_OUT_OF_ZONE, "hors zone"
        monkeypatch.setattr(f"{_MODULE}.persist_shared_location", _fake_persist)

        result = run(resolve_gps_stage(
            _RuntimeNoLLM(), "+22670000001",
            location_shared=False, is_yes=False, gps_default=None,
            user_text="https://www.google.com/maps?q=33.264381,-7.586925",
        ))

        assert result.resolved is False
        assert "hors de notre zone" in result.message
