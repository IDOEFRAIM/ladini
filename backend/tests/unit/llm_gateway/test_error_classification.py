"""`llm_gateway/error_classification.py::classify_llm_error` — §15 du brief.

TRANSIENT compte pour le disjoncteur ; CONFIG désactive le candidat sans le
compter comme une panne transitoire ; APPLICATION ne touche JAMAIS la santé
(§15 : "un bug applicatif ne doit jamais faire croire que le provider est
down"). Le cas `anthropic.claude-haiku-4-5` ci-dessous est le message
d'erreur RÉEL observé en direct contre la passerelle bedrock-mantle cette
session (pas un exemple inventé)."""

from __future__ import annotations

import asyncio
import json

from agriconnect.graphs.agents.market_coach.llm_gateway.error_classification import (
    classify_llm_error,
)
from agriconnect.graphs.agents.market_coach.llm_gateway.types import ErrorClass


class TestTransientErrors:
    def test_asyncio_timeout_is_transient(self):
        assert classify_llm_error(asyncio.TimeoutError()) == ErrorClass.TRANSIENT

    def test_plain_timeout_error_is_transient(self):
        assert classify_llm_error(TimeoutError()) == ErrorClass.TRANSIENT

    def test_connection_error_is_transient(self):
        assert classify_llm_error(ConnectionError("connection reset")) == ErrorClass.TRANSIENT

    def test_status_code_429_is_transient(self):
        exc = Exception("rate limited")
        exc.status_code = 429
        assert classify_llm_error(exc) == ErrorClass.TRANSIENT

    def test_status_code_503_is_transient(self):
        exc = Exception("service unavailable")
        exc.status_code = 503
        assert classify_llm_error(exc) == ErrorClass.TRANSIENT


class TestConfigErrors:
    def test_status_code_401_is_config(self):
        exc = Exception("unauthorized")
        exc.status_code = 401
        assert classify_llm_error(exc) == ErrorClass.CONFIG

    def test_status_code_403_is_config(self):
        exc = Exception("forbidden")
        exc.status_code = 403
        assert classify_llm_error(exc) == ErrorClass.CONFIG

    def test_the_real_bedrock_gateway_unsupported_model_message_is_config(self):
        # Message RÉEL observé (2026-09-02) : anthropic.claude-haiku-4-5 sur
        # la passerelle bedrock-mantle, HTTP 400.
        exc = Exception(
            "Error code: 400 - {'error': {'code': 'validation_error', "
            "'message': \"The model 'anthropic.claude-haiku-4-5' does not "
            "support the '/v1/chat/completions' API\", 'param': None, "
            "'type': 'invalid_request_error'}}"
        )
        exc.status_code = 400
        assert classify_llm_error(exc) == ErrorClass.CONFIG

    def test_model_not_found_message_is_config_even_without_status_code(self):
        assert classify_llm_error(Exception("model not found")) == ErrorClass.CONFIG


class TestApplicationErrors:
    def test_json_decode_error_is_application_never_transient(self):
        try:
            json.loads("not json")
        except json.JSONDecodeError as exc:
            assert classify_llm_error(exc) == ErrorClass.APPLICATION
        else:
            raise AssertionError("json.loads devait lever JSONDecodeError")

    def test_a_plain_value_error_from_our_own_code_is_application(self):
        assert classify_llm_error(ValueError("bug de code appelant")) == ErrorClass.APPLICATION

    def test_a_key_error_is_application(self):
        assert classify_llm_error(KeyError("champ_manquant")) == ErrorClass.APPLICATION

    def test_an_unrecognized_generic_exception_defaults_to_application_not_transient(self):
        """§15 : par prudence, une erreur non reconnue N'OUVRE PAS le
        disjoncteur — mieux vaut la rendre visible en log qu'accuser à tort
        un provider sain."""
        assert classify_llm_error(RuntimeError("quelque chose d'inattendu")) == ErrorClass.APPLICATION
