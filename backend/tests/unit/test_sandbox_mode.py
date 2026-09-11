"""`core/settings.py::Settings._apply_sandbox_mode` et
`core/get_llm.py::_MockGroqClient` — bascule sandbox explicite (2026-08-27) :
préserver les ressources de production (jamais de vrai paiement Paydunya en
sandbox, jamais de mock silencieux d'un sender WhatsApp de production)."""
from __future__ import annotations

import importlib
import json
import logging

import pytest

from ladini.core.settings import Settings


def _get_llm_module():
    """`ladini.core.get_llm` (le module) est shadowé par la fonction
    `get_llm` ré-exportée dans `ladini/core/__init__.py` (`from .llm
    import get_llm` — même nom que le module) : `import
    ladini.core.get_llm as x` peut résoudre `x` vers la FONCTION plutôt
    que le module. `importlib.import_module` contourne ce piège en lisant
    directement `sys.modules`."""
    return importlib.import_module("ladini.core.get_llm")


@pytest.fixture(autouse=True)
def _no_ambient_sandbox_env(monkeypatch):
    """Isole ce fichier de l'environnement RÉEL du process.

    Corrigé (2026-09-11, CI cassée) : `Settings()` (pydantic-settings) lit
    `os.environ` avec une priorité PLUS HAUTE que la valeur par défaut d'un
    champ. Le job CI exporte `SANDBOX_MODE=true` / `MOCK_EXTERNAL_APIS=true`
    pour TOUTE l'étape pytest (`.github/workflows/cicd.yml` — délibéré,
    "environnement hermétique, aucun appel réseau réel" pour le RESTE de la
    suite). Les tests `*_off_by_default` ci-dessous veulent vérifier le
    DÉFAUT DU CHAMP pydantic, pas la valeur héritée de l'environnement du
    job — sans cette garde ils passaient par hasard en local (aucune de ces
    deux variables n'y est exportée) et échouaient à coup sûr en CI.
    N'affecte PAS les tests qui passent `SANDBOX_MODE=True`/`False`
    explicitement à `_settings()` : un kwarg de constructeur a toujours
    priorité sur l'environnement, avec ou sans cette fixture.
    """
    monkeypatch.delenv("SANDBOX_MODE", raising=False)
    monkeypatch.delenv("MOCK_EXTERNAL_APIS", raising=False)


def _settings(**overrides):
    base = {
        "_env_file": None,
        "GROQ_API_KEY": "x",
        "DATABASE_URL": "postgresql://u:p@localhost/db",
    }
    base.update(overrides)
    return Settings(**base)


class TestSandboxModeValidator:
    def test_sandbox_mode_off_by_default(self):
        s = _settings()
        assert s.SANDBOX_MODE is False

    def test_sandbox_mode_forces_paydunya_test_mode(self, caplog):
        with caplog.at_level(logging.WARNING):
            s = _settings(SANDBOX_MODE=True, PAYDUNYA_MODE="live")
        assert s.PAYDUNYA_MODE == "test"
        assert any("PAYDUNYA_MODE forcé" in r.message for r in caplog.records)

    def test_sandbox_mode_with_a_production_twilio_number_warns(self, caplog):
        with caplog.at_level(logging.WARNING):
            s = _settings(SANDBOX_MODE=True, TWILIO_WHATSAPP_NUMBER="whatsapp:+22657114780")
        assert any(
            "n'est PAS le" in r.message and "numéro sandbox" in r.message
            for r in caplog.records
        )
        # Jamais de correction SILENCIEUSE d'un numéro explicitement configuré.
        assert s.TWILIO_WHATSAPP_NUMBER == "whatsapp:+22657114780"

    def test_sandbox_mode_with_the_real_sandbox_number_does_not_warn(self, caplog):
        with caplog.at_level(logging.WARNING):
            s = _settings(
                SANDBOX_MODE=True,
                TWILIO_WHATSAPP_NUMBER="whatsapp:+14155238886",
            )
        assert not any("numéro sandbox" in r.message for r in caplog.records)

    def test_sandbox_mode_off_never_touches_paydunya_mode(self):
        s = _settings(SANDBOX_MODE=False, PAYDUNYA_MODE="live")
        assert s.PAYDUNYA_MODE == "live"


class TestMockExternalApis:
    def test_mock_flag_off_by_default(self):
        assert _settings().MOCK_EXTERNAL_APIS is False

    def test_get_groq_sdk_returns_a_mock_client_when_enabled(self, monkeypatch):
        get_llm_mod = _get_llm_module()
        from ladini.core.settings import settings as live_settings

        monkeypatch.setattr(live_settings, "MOCK_EXTERNAL_APIS", True, raising=False)
        monkeypatch.setattr(get_llm_mod, "_GROQ_SDK_SINGLETON", None, raising=False)

        sdk = get_llm_mod.get_groq_sdk(force_refresh=True)
        assert isinstance(sdk, get_llm_mod._MockGroqClient)

    def test_mock_client_returns_empty_json_object_in_json_mode(self, monkeypatch):
        get_llm_mod = _get_llm_module()
        from ladini.core.settings import settings as live_settings

        monkeypatch.setattr(live_settings, "MOCK_EXTERNAL_APIS", True, raising=False)
        monkeypatch.setattr(get_llm_mod, "_GROQ_SDK_SINGLETON", None, raising=False)
        monkeypatch.setattr(get_llm_mod, "_LLM_SINGLETON", None, raising=False)
        # (2026-08-31, ajout du provider Bedrock) : `get_llm()` peut aussi
        # dispatcher vers ces deux singletons selon `settings.LLM_PROVIDER` —
        # sans les réinitialiser ici aussi, un client RÉEL déjà mis en cache
        # ailleurs (process partagé entre tests) survivrait au monkeypatch de
        # `MOCK_EXTERNAL_APIS` (le check mock ne s'exécute que si le
        # singleton est encore `None`), ce qui a fait échouer ce test en
        # pratique avec `LLM_PROVIDER=bedrock` configuré.
        monkeypatch.setattr(get_llm_mod, "_BEDROCK_CLIENT_SINGLETON", None, raising=False)
        monkeypatch.setattr(
            get_llm_mod, "_OPENAI_COMPATIBLE_SDK_SINGLETON", None, raising=False
        )

        llm = get_llm_mod.get_llm()
        resp = llm.chat.completions.create(
            model="x", messages=[], response_format={"type": "json_object"}
        )
        content = resp.choices[0].message.content
        assert json.loads(content) == {}

    def test_mock_client_never_requires_a_real_api_key(self, monkeypatch):
        get_llm_mod = _get_llm_module()
        from ladini.core.settings import settings as live_settings

        monkeypatch.setattr(live_settings, "MOCK_EXTERNAL_APIS", True, raising=False)
        monkeypatch.setattr(live_settings, "LADINI_APIKEY", "", raising=False)
        monkeypatch.setattr(live_settings, "GROQ_API_KEY", "", raising=False)
        monkeypatch.setattr(get_llm_mod, "_GROQ_SDK_SINGLETON", None, raising=False)

        # Must NOT raise, unlike the real-key path.
        sdk = get_llm_mod.get_groq_sdk(force_refresh=True)
        assert sdk is not None
