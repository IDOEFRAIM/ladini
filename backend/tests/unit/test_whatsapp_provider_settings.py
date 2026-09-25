"""Alias d'environnement WhatsApp Meta/Twilio (mandat "adaptation webhook Meta/
Twilio", 2026-09-25) — verrouille que les noms d'env EXACTS demandés par le mandat
(`WHATSAPP_PROVIDER`, `META_WHATSAPP_TOKEN`, `META_WHATSAPP_PHONE_NUMBER_ID`,
`META_WHATSAPP_VERIFY_TOKEN`, `META_GRAPH_API_VERSION`, `TWILIO_PHONE_NUMBER`)
peuplent bien les MÊMES champs `Settings` que les noms canoniques préexistants
(`MESSAGING_PROVIDER`, `WHATSAPP_CLOUD_API_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`,
`WHATSAPP_WEBHOOK_VERIFY_TOKEN`, `WHATSAPP_GRAPH_API_VERSION`,
`TWILIO_WHATSAPP_NUMBER`) — jamais un second champ concurrent. `Settings` est un
`BaseSettings` (pydantic-settings) : chaque test construit une instance FRAÎCHE via
`Settings(_env_file=None)` pour ne dépendre ni d'un `.env` local ni de l'ordre des
tests (même discipline que `test_sandbox_mode.py::_no_ambient_sandbox_env`)."""
from __future__ import annotations

import pytest

from ladini.core.settings import Settings


@pytest.fixture(autouse=True)
def _clean_whatsapp_env(monkeypatch):
    """Assainit l'environnement de CE test des deux jeux de noms — un test qui ne
    pose QUE l'alias ne doit jamais accidentellement lire une vraie valeur laissée
    par le shell/CI sous le nom canonique, et réciproquement."""
    for key in (
        "MESSAGING_PROVIDER",
        "WHATSAPP_PROVIDER",
        "WHATSAPP_CLOUD_API_TOKEN",
        "META_WHATSAPP_TOKEN",
        "WHATSAPP_PHONE_NUMBER_ID",
        "META_WHATSAPP_PHONE_NUMBER_ID",
        "WHATSAPP_WEBHOOK_VERIFY_TOKEN",
        "META_WHATSAPP_VERIFY_TOKEN",
        "WHATSAPP_GRAPH_API_VERSION",
        "META_GRAPH_API_VERSION",
        "TWILIO_WHATSAPP_NUMBER",
        "TWILIO_PHONE_NUMBER",
    ):
        monkeypatch.delenv(key, raising=False)


class TestMessagingProviderAlias:
    def test_whatsapp_provider_alias_populates_messaging_provider(self, monkeypatch):
        monkeypatch.setenv("WHATSAPP_PROVIDER", "twilio")
        assert Settings(_env_file=None).MESSAGING_PROVIDER == "twilio"

    def test_whatsapp_provider_meta_is_normalized_to_whatsapp_cloud(self, monkeypatch):
        """"meta" est le vocabulaire du mandat — le reste du code (`response_
        dispatch.py`, `whatsapp_webhook.py`, `workers/outbox/channels/whatsapp.py`)
        ne teste que `== "twilio"` ; "meta" resterait fonctionnellement correct même
        sans cette normalisation (tout ce qui n'est pas "twilio" prend le chemin
        Cloud API), mais ce test verrouille l'intention explicitement plutôt que de
        dépendre d'un raisonnement "ça marche par accident"."""
        monkeypatch.setenv("WHATSAPP_PROVIDER", "meta")
        assert Settings(_env_file=None).MESSAGING_PROVIDER == "whatsapp_cloud"

    def test_whatsapp_provider_meta_is_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("WHATSAPP_PROVIDER", "META")
        assert Settings(_env_file=None).MESSAGING_PROVIDER == "whatsapp_cloud"

    def test_canonical_messaging_provider_still_wins_when_both_names_are_set(
        self, monkeypatch
    ):
        """Rétrocompatibilité : un déploiement existant qui pose déjà
        `MESSAGING_PROVIDER` ne doit jamais être court-circuité silencieusement
        par un `WHATSAPP_PROVIDER` introduit ailleurs (ex: un outil externe, un
        template `.env` partiellement mis à jour)."""
        monkeypatch.setenv("MESSAGING_PROVIDER", "twilio")
        monkeypatch.setenv("WHATSAPP_PROVIDER", "whatsapp_cloud")
        assert Settings(_env_file=None).MESSAGING_PROVIDER == "twilio"

    def test_default_is_unchanged_when_neither_name_is_set(self):
        assert Settings(_env_file=None).MESSAGING_PROVIDER == "whatsapp_cloud"

    def test_a_blank_canonical_provider_never_shadows_a_filled_alias(self, monkeypatch):
        monkeypatch.setenv("MESSAGING_PROVIDER", "")
        monkeypatch.setenv("WHATSAPP_PROVIDER", "meta")
        assert Settings(_env_file=None).MESSAGING_PROVIDER == "whatsapp_cloud"


class TestWhatsAppCloudApiFieldAliases:
    def test_meta_whatsapp_token_alias(self, monkeypatch):
        monkeypatch.setenv("META_WHATSAPP_TOKEN", "EAAG_test_token")
        assert Settings(_env_file=None).WHATSAPP_CLOUD_API_TOKEN == "EAAG_test_token"

    def test_meta_whatsapp_phone_number_id_alias(self, monkeypatch):
        monkeypatch.setenv("META_WHATSAPP_PHONE_NUMBER_ID", "123456789012345")
        assert Settings(_env_file=None).WHATSAPP_PHONE_NUMBER_ID == "123456789012345"

    def test_meta_whatsapp_verify_token_alias(self, monkeypatch):
        monkeypatch.setenv("META_WHATSAPP_VERIFY_TOKEN", "ladini_webhook_verify_token_2026")
        assert (
            Settings(_env_file=None).WHATSAPP_WEBHOOK_VERIFY_TOKEN
            == "ladini_webhook_verify_token_2026"
        )

    def test_meta_graph_api_version_alias(self, monkeypatch):
        monkeypatch.setenv("META_GRAPH_API_VERSION", "v20.0")
        assert Settings(_env_file=None).WHATSAPP_GRAPH_API_VERSION == "v20.0"

    def test_canonical_names_still_work_unchanged(self, monkeypatch):
        monkeypatch.setenv("WHATSAPP_CLOUD_API_TOKEN", "canonical_token")
        monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "canonical_id")
        monkeypatch.setenv("WHATSAPP_WEBHOOK_VERIFY_TOKEN", "canonical_verify")
        monkeypatch.setenv("WHATSAPP_GRAPH_API_VERSION", "v99.0")
        s = Settings(_env_file=None)
        assert s.WHATSAPP_CLOUD_API_TOKEN == "canonical_token"
        assert s.WHATSAPP_PHONE_NUMBER_ID == "canonical_id"
        assert s.WHATSAPP_WEBHOOK_VERIFY_TOKEN == "canonical_verify"
        assert s.WHATSAPP_GRAPH_API_VERSION == "v99.0"

    def test_a_blank_canonical_token_never_shadows_a_filled_alias(self, monkeypatch):
        """Incident réel 2026-09-25 : `AliasChoices("WHATSAPP_CLOUD_API_TOKEN",
        "META_WHATSAPP_TOKEN")` seul fait gagner le premier nom PRÉSENT dans
        l'environnement, même vide — pas le premier NON VIDE. Un `.env` qui
        déclare encore `WHATSAPP_CLOUD_API_TOKEN=` (héritage de `.env.example`)
        à côté d'un `META_WHATSAPP_TOKEN=EAAG...` fraîchement rempli lisait donc
        silencieusement la chaîne vide. `_prefer_non_empty_alias_over_blank_
        canonical` doit fermer ce trou : une valeur canonique VIDE ne doit
        jamais l'emporter sur un alias REMPLI."""
        monkeypatch.setenv("WHATSAPP_CLOUD_API_TOKEN", "")
        monkeypatch.setenv("META_WHATSAPP_TOKEN", "EAAG_fresh_token")
        assert Settings(_env_file=None).WHATSAPP_CLOUD_API_TOKEN == "EAAG_fresh_token"

    def test_a_filled_canonical_token_still_wins_over_a_filled_alias(self, monkeypatch):
        """Symétrique du test précédent : quand les DEUX portent une vraie
        valeur, le nom canonique reste prioritaire (jamais un flip-flop selon
        l'ordre d'écriture du .env)."""
        monkeypatch.setenv("WHATSAPP_CLOUD_API_TOKEN", "canonical_wins")
        monkeypatch.setenv("META_WHATSAPP_TOKEN", "alias_loses")
        assert Settings(_env_file=None).WHATSAPP_CLOUD_API_TOKEN == "canonical_wins"


class TestTwilioPhoneNumberAlias:
    def test_twilio_phone_number_alias(self, monkeypatch):
        monkeypatch.setenv("TWILIO_PHONE_NUMBER", "whatsapp:+15551234567")
        assert Settings(_env_file=None).TWILIO_WHATSAPP_NUMBER == "whatsapp:+15551234567"

    def test_canonical_twilio_whatsapp_number_still_works(self, monkeypatch):
        monkeypatch.setenv("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")
        assert Settings(_env_file=None).TWILIO_WHATSAPP_NUMBER == "whatsapp:+14155238886"
