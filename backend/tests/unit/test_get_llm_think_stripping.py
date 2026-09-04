"""`core/get_llm.py::_GroqAdapter` — incident réel (2026-08-27) : un repli
automatique vers un modèle de raisonnement (`GROQ_RATE_LIMIT_FALLBACK`,
ex. qwen/qwen3.6-27b sur rate-limit du modèle principal) a laissé fuir tel
quel un bloc `<think>...</think>` dans un message WhatsApp
("[3/3] <think>\nHere's a thinking process..."). Le point d'interception
UNIQUE de tous les appels Groq (`_GroqAdapter.create`) doit désormais
nettoyer ce raisonnement avant de renvoyer la réponse, quel que soit le
site d'appel (routing.py, response_handlers.py, clarification.py,
slot_enrichment.py, nodes/rendering/ask.py)."""
from __future__ import annotations

import pytest

from agriconnect.core.get_llm import _strip_think_block, _GroqAdapter


class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)


class _Completion:
    def __init__(self, content, usage=None):
        self.choices = [_Choice(content)]
        self.usage = usage


class _FakeCompletions:
    def __init__(self, response):
        self._response = response

    def create(self, **kwargs):
        return self._response


class _FakeChat:
    def __init__(self, response):
        self.completions = _FakeCompletions(response)


class _FakeClient:
    def __init__(self, response):
        self.chat = _FakeChat(response)


class TestStripThinkBlock:
    def test_a_closed_think_block_is_removed(self):
        text = "<think>\nreasoning here\n</think>\nLa vraie réponse."
        assert _strip_think_block(text) == "La vraie réponse."

    def test_an_unclosed_think_block_drops_everything_after_it(self):
        """Modèle qui épuise son budget de tokens EN PLEIN raisonnement —
        exposer un raisonnement partiel n'est jamais préférable à une
        réponse vide (les appelants ont leur propre repli sur contenu
        vide)."""
        text = "[3/3] <think>\nHere's a thinking process:\n\n1. Analyze..."
        assert _strip_think_block(text) == "[3/3]"

    def test_text_without_a_think_block_is_untouched(self):
        text = "Quelle quantité de mais souhaitez-vous ?"
        assert _strip_think_block(text) == text

    def test_case_insensitive(self):
        text = "<THINK>reasoning</THINK>La réponse."
        assert _strip_think_block(text) == "La réponse."

    def test_empty_and_none_are_passed_through(self):
        assert _strip_think_block("") == ""
        assert _strip_think_block(None) is None


class TestGroqAdapterStripsThinkFromResponses:
    def test_a_response_with_a_think_block_is_sanitized(self):
        response = _Completion("<think>\nplan...\n</think>\nLa vraie réponse.")
        adapter = _GroqAdapter(_FakeClient(response))

        out = adapter.chat.completions.create(model="qwen/qwen3.6-27b", messages=[])

        assert out.choices[0].message.content == "La vraie réponse."

    def test_a_response_without_a_think_block_is_unmodified(self):
        response = _Completion("Quelle quantité souhaitez-vous ?")
        adapter = _GroqAdapter(_FakeClient(response))

        out = adapter.chat.completions.create(model="llama-3.3-70b-versatile", messages=[])

        assert out.choices[0].message.content == "Quelle quantité souhaitez-vous ?"

    def test_an_unclosed_think_block_still_gets_stripped_end_to_end(self):
        """Reproduit l'incident réel : "[3/3] <think>\nHere's a thinking
        process..." envoyé tel quel à l'utilisateur."""
        response = _Completion(
            "[3/3] <think>\nHere's a thinking process:\n\n1. Analyze User Input..."
        )
        adapter = _GroqAdapter(_FakeClient(response))

        out = adapter.chat.completions.create(model="qwen/qwen3.6-27b", messages=[])

        assert "<think>" not in out.choices[0].message.content
        assert "thinking process" not in out.choices[0].message.content
