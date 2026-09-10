"""`core/get_llm.py::_BedrockAdapter` and `get_llm()` provider dispatch
(2026-08-31, ajout du provider AWS Bedrock). Suit le même pattern que
`test_get_llm_think_stripping.py` : un client boto3 `bedrock-runtime` factice
(pas de réseau, pas d'identifiants AWS réels), et vérifie que
`_BedrockAdapter` expose EXACTEMENT la même interface normalisée que
`_GroqAdapter` (`client.chat.completions.create(...)` ->
`.choices[0].message.content`) attendue par les 5 sites d'appel de
production."""
from __future__ import annotations

import importlib

# `import ladini.core.get_llm as get_llm_mod` is unreliable here:
# `ladini/core/__init__.py` does `from .llm import get_llm` (a thin
# re-export shim), which — because the package attribute name "get_llm"
# collides with the submodule name "get_llm" — overwrites
# `ladini.core.get_llm` (normally auto-bound to the submodule on
# import) with the FUNCTION instead. `importlib.import_module` bypasses the
# package attribute entirely and returns the real module from `sys.modules`.
get_llm_mod = importlib.import_module("ladini.core.get_llm")
from ladini.core.get_llm import (
    _BedrockAdapter,
    _is_bedrock_throttling_error,
    get_llm,
)


def _converse_response(text: str, input_tokens: int = 10, output_tokens: int = 5):
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": text}],
            }
        },
        "usage": {"inputTokens": input_tokens, "outputTokens": output_tokens},
    }


class _FakeThrottlingError(Exception):
    """Stand-in for botocore.exceptions.ClientError with a ThrottlingException
    code — avoids a hard dependency on botocore's exact class in this test;
    `_is_bedrock_throttling_error` is exercised directly and separately."""


class _FakeBedrockClient:
    def __init__(self, response=None, raise_first=None):
        self._response = response
        self._raise_first = raise_first
        self.calls: list[dict] = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        if self._raise_first is not None and len(self.calls) == 1:
            raise self._raise_first
        return self._response


class TestBedrockAdapterMessageTranslation:
    def test_system_message_is_split_into_top_level_system_block(self):
        client = _FakeBedrockClient(_converse_response("ok"))
        adapter = _BedrockAdapter(client)

        adapter.chat.completions.create(
            model="anthropic.claude-3-5-sonnet-20241022-v2:0",
            messages=[
                {"role": "system", "content": "Tu es un assistant."},
                {"role": "user", "content": "Bonjour"},
            ],
        )

        call = client.calls[0]
        assert call["system"] == [{"text": "Tu es un assistant."}]
        assert call["messages"] == [{"role": "user", "content": [{"text": "Bonjour"}]}]
        assert call["modelId"] == "anthropic.claude-3-5-sonnet-20241022-v2:0"

    def test_temperature_maps_to_inference_config(self):
        client = _FakeBedrockClient(_converse_response("ok"))
        adapter = _BedrockAdapter(client)

        adapter.chat.completions.create(
            model="m", messages=[{"role": "user", "content": "hi"}], temperature=0.0
        )

        assert client.calls[0]["inferenceConfig"] == {"temperature": 0.0}

    def test_response_format_is_dropped_without_error(self):
        client = _FakeBedrockClient(_converse_response("{}"))
        adapter = _BedrockAdapter(client)

        out = adapter.chat.completions.create(
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            response_format={"type": "json_object"},
        )

        assert "response_format" not in client.calls[0]
        assert out.choices[0].message.content == "{}"

    def test_extracts_content_from_the_nested_converse_response(self):
        client = _FakeBedrockClient(_converse_response("La réponse du modèle."))
        adapter = _BedrockAdapter(client)

        out = adapter.chat.completions.create(
            model="m", messages=[{"role": "user", "content": "hi"}]
        )

        assert out.choices[0].message.content == "La réponse du modèle."

    def test_strips_a_think_block_from_the_response(self):
        client = _FakeBedrockClient(
            _converse_response("<think>\nplan...\n</think>\nLa vraie réponse.")
        )
        adapter = _BedrockAdapter(client)

        out = adapter.chat.completions.create(
            model="m", messages=[{"role": "user", "content": "hi"}]
        )

        assert out.choices[0].message.content == "La vraie réponse."

    def test_malformed_response_does_not_crash(self):
        client = _FakeBedrockClient({"output": {}})
        adapter = _BedrockAdapter(client)

        out = adapter.chat.completions.create(
            model="m", messages=[{"role": "user", "content": "hi"}]
        )

        assert "llm-error" in out.choices[0].message.content


class TestBedrockThrottlingFallback:
    def test_falls_back_to_the_fast_model_on_throttling(self, monkeypatch):
        from ladini.core.settings import settings

        monkeypatch.setattr(settings, "LLM_MODEL", "fast-model")

        class _ThrottleExc(Exception):
            pass

        monkeypatch.setattr(
            get_llm_mod, "_is_bedrock_throttling_error", lambda exc: isinstance(exc, _ThrottleExc)
        )

        client = _FakeBedrockClient(
            response=_converse_response("réponse dégradée"),
            raise_first=_ThrottleExc("throttled"),
        )
        adapter = _BedrockAdapter(client)

        out = adapter.chat.completions.create(
            model="slow-reasoning-model", messages=[{"role": "user", "content": "hi"}]
        )

        assert len(client.calls) == 2
        assert client.calls[1]["modelId"] == "fast-model"
        assert out.choices[0].message.content == "réponse dégradée"

    def test_does_not_fall_back_on_a_non_throttling_error(self):
        client = _FakeBedrockClient(raise_first=RuntimeError("boom, not throttling"))
        adapter = _BedrockAdapter(client)

        try:
            adapter.chat.completions.create(
                model="m", messages=[{"role": "user", "content": "hi"}]
            )
            assert False, "expected the original exception to propagate"
        except RuntimeError as exc:
            assert "boom" in str(exc)
        assert len(client.calls) == 1


class TestIsBedrockThrottlingError:
    def test_non_client_error_is_never_throttling(self):
        assert _is_bedrock_throttling_error(ValueError("nope")) is False

    def test_a_real_botocore_throttling_client_error_is_detected(self):
        try:
            from botocore.exceptions import ClientError
        except ImportError:
            return  # boto3/botocore not installed in this environment
        exc = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "rate limited"}},
            "Converse",
        )
        assert _is_bedrock_throttling_error(exc) is True

    def test_a_different_client_error_code_is_not_throttling(self):
        try:
            from botocore.exceptions import ClientError
        except ImportError:
            return
        exc = ClientError(
            {"Error": {"Code": "ValidationException", "Message": "bad request"}},
            "Converse",
        )
        assert _is_bedrock_throttling_error(exc) is False


class TestGetLlmProviderDispatch:
    def _reset_singletons(self, monkeypatch):
        monkeypatch.setattr(get_llm_mod, "_LLM_SINGLETON", None)
        monkeypatch.setattr(get_llm_mod, "_GROQ_SDK_SINGLETON", None)
        monkeypatch.setattr(get_llm_mod, "_BEDROCK_CLIENT_SINGLETON", None)
        monkeypatch.setattr(get_llm_mod, "_OPENAI_COMPATIBLE_SDK_SINGLETON", None)

    def test_bedrock_without_openai_base_url_uses_native_boto3_adapter(
        self, monkeypatch
    ):
        from ladini.core.settings import settings

        self._reset_singletons(monkeypatch)
        monkeypatch.setattr(settings, "LLM_PROVIDER", "bedrock")
        monkeypatch.setattr(settings, "OPENAI_BASE_URL", "")
        monkeypatch.setattr(settings, "MOCK_EXTERNAL_APIS", True)

        def _groq_sdk_must_not_be_called(*a, **k):
            raise AssertionError("get_groq_sdk() must not be called for LLM_PROVIDER=bedrock")

        monkeypatch.setattr(get_llm_mod, "get_groq_sdk", _groq_sdk_must_not_be_called)

        client = get_llm()

        assert isinstance(client, _BedrockAdapter)

    def test_bedrock_with_openai_base_url_prefers_the_openai_compatible_gateway(
        self, monkeypatch
    ):
        from ladini.core.settings import settings

        self._reset_singletons(monkeypatch)
        monkeypatch.setattr(settings, "LLM_PROVIDER", "bedrock")
        monkeypatch.setattr(settings, "OPENAI_BASE_URL", "https://example.aws/v1")
        monkeypatch.setattr(settings, "OPENAI_API_KEY", "fake-key")
        monkeypatch.setattr(settings, "MOCK_EXTERNAL_APIS", True)

        def _bedrock_client_must_not_be_called(*a, **k):
            raise AssertionError(
                "get_bedrock_client() must not be called when OPENAI_BASE_URL is set"
            )

        monkeypatch.setattr(
            get_llm_mod, "get_bedrock_client", _bedrock_client_must_not_be_called
        )

        client = get_llm()

        from ladini.core.get_llm import _GroqAdapter

        assert isinstance(client, _GroqAdapter)

    def test_groq_provider_is_the_default_and_unchanged(self, monkeypatch):
        from ladini.core.settings import settings

        self._reset_singletons(monkeypatch)
        monkeypatch.setattr(settings, "LLM_PROVIDER", "groq")
        monkeypatch.setattr(settings, "MOCK_EXTERNAL_APIS", True)

        client = get_llm()

        from ladini.core.get_llm import _GroqAdapter

        assert isinstance(client, _GroqAdapter)

    def test_mock_external_apis_short_circuits_bedrock_to_the_mock_client(
        self, monkeypatch
    ):
        from ladini.core.settings import settings

        self._reset_singletons(monkeypatch)
        monkeypatch.setattr(settings, "LLM_PROVIDER", "bedrock")
        monkeypatch.setattr(settings, "MOCK_EXTERNAL_APIS", True)

        client = get_llm_mod.get_bedrock_client()

        out = client.chat.completions.create(
            response_format={"type": "json_object"}, messages=[]
        )
        assert out.choices[0].message.content == "{}"
