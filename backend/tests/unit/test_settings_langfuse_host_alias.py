"""`core/settings.py::Settings.LANGFUSE_HOST` — incident 2026-08-27.

`.env` déclare `LANGFUSE_BASE_URL=https://cloud.langfuse.com`, mais le champ
Pydantic s'appelait `LANGFUSE_HOST` — jamais lu depuis cette variable, donc
toujours retombé sur le défaut Docker interne `http://langfuse-web:3000`,
injoignable en dehors de docker-compose. Le thread consommateur de la SDK
Langfuse tourne en arrière-plan (hors de tout try/except applicatif) :
l'échec de connexion sur chaque batch loggait
« Unexpected error occurred... » sur CHAQUE tâche Celery, sans jamais
impacter l'envoi du message WhatsApp — silencieux mais total sur
l'observabilité LLM. `AliasChoices` permet de lire soit l'ancien nom
(`LANGFUSE_HOST`), soit celui réellement présent dans `.env`
(`LANGFUSE_BASE_URL`)."""
from __future__ import annotations

import pytest


@pytest.fixture()
def _clean_langfuse_env(monkeypatch):
    for key in ("LANGFUSE_HOST", "LANGFUSE_BASE_URL"):
        monkeypatch.delenv(key, raising=False)


class TestLangfuseHostAlias:
    def test_langfuse_base_url_is_read_into_langfuse_host(self, monkeypatch, _clean_langfuse_env):
        monkeypatch.setenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.com")
        from agriconnect.core.settings import Settings

        assert Settings().LANGFUSE_HOST == "https://cloud.langfuse.com"

    def test_legacy_langfuse_host_still_works(self, monkeypatch, _clean_langfuse_env):
        monkeypatch.setenv("LANGFUSE_HOST", "https://self-hosted.example.com")
        from agriconnect.core.settings import Settings

        assert Settings().LANGFUSE_HOST == "https://self-hosted.example.com"

    def test_default_is_the_docker_internal_host_when_neither_is_set(self, monkeypatch, _clean_langfuse_env):
        from agriconnect.core.settings import Settings

        # `_env_file=None` : ignore le `.env` du dépôt (qui déclare
        # LANGFUSE_BASE_URL) pour isoler le comportement par défaut du champ.
        assert Settings(_env_file=None).LANGFUSE_HOST == "http://langfuse-web:3000"
