"""`api/tasks.py::_send_via_twilio` — audit UX interactive 2026-08-27.

``list_menu`` interactif (Twilio Content API `list-picker`) a été retiré
le même jour (blocages de template récurrents, ex. erreur 21656) : toute
liste part désormais en texte brut chunké, quelle que soit la config
Twilio. Seul ``quick_reply``/``confirm`` reste envoyé via Content Template
(boutons de confirmation). Ces tests verrouillent ce comportement ainsi
que les replis silencieux."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _tasks_module(monkeypatch, *, interactive_enabled=True, list_sid="HXlist", confirm_sid="HXconfirm"):
    import ladini.api.response_dispatch as mod

    monkeypatch.setattr(mod, "Client", MagicMock(return_value=SimpleNamespace()))
    monkeypatch.setattr(mod.settings, "TWILIO_ACCOUNT_SID", "sid", raising=False)
    monkeypatch.setattr(mod.settings, "TWILIO_AUTH_TOKEN", "token", raising=False)
    monkeypatch.setattr(mod.settings, "TWILIO_WHATSAPP_NUMBER", "whatsapp:+1555", raising=False)
    monkeypatch.setattr(mod.settings, "TWILIO_INTERACTIVE_ENABLED", interactive_enabled, raising=False)
    monkeypatch.setattr(mod.settings, "TWILIO_LIST_PICKER_CONTENT_SID", list_sid, raising=False)
    monkeypatch.setattr(mod.settings, "TWILIO_CONFIRM_CONTENT_SID", confirm_sid, raising=False)

    send_spy = MagicMock(return_value=SimpleNamespace(sid="SM123"))
    monkeypatch.setattr(mod, "send_whatsapp_message", send_spy)
    return mod, send_spy


class TestSanitizeContentVariables:
    """Incident 21656 (2026-08-27) : Twilio rejette `content_variables` dès
    qu'une valeur n'est pas une string (int/float/None issus d'un champ DB
    non casté, ex. `producer_id` UUID ou `price` float)."""

    def test_ints_and_floats_are_stringified(self):
        import ladini.api.response_dispatch as mod

        out = mod.sanitize_content_variables({"1": 175.0, "2": 3, "3": "déjà str"})
        assert out == {"1": "175.0", "2": "3", "3": "déjà str"}
        assert all(isinstance(v, str) for v in out.values())

    def test_none_becomes_empty_string_not_dropped(self):
        import ladini.api.response_dispatch as mod

        out = mod.sanitize_content_variables({"1": None, "2": "x"})
        assert out == {"1": "", "2": "x"}

    def test_non_string_keys_are_stringified_too(self):
        import ladini.api.response_dispatch as mod

        out = mod.sanitize_content_variables({1: "a", 2: "b"})
        assert out == {"1": "a", "2": "b"}

    def test_send_whatsapp_message_sanitizes_before_json_dumps(self, monkeypatch):
        """Bout-en-bout : `send_whatsapp_message` ne doit jamais laisser une
        valeur non-string atteindre `json.dumps` pour `content_variables`."""
        import ladini.api.response_dispatch as mod

        create_spy = MagicMock(return_value=SimpleNamespace(sid="SM1"))
        client = SimpleNamespace(messages=SimpleNamespace(create=create_spy))

        mod.send_whatsapp_message(
            client=client,
            from_number="whatsapp:+1555",
            to_phone="whatsapp:+22670000001",
            content_sid="HXtest",
            content_vars={"1": 175.0, "2": None, "3": "ok"},
        )

        sent_vars = json.loads(create_spy.call_args.kwargs["content_variables"])
        assert sent_vars == {"1": "175.0", "2": "", "3": "ok"}


class TestSendViaTwilioListMenu:
    """`list_menu` interactif DÉSACTIVÉ (2026-08-27) : blocages de template
    récurrents côté Meta/Twilio (ex. erreur 21656). Toute liste part
    désormais en texte brut chunké, quelle que soit la config Twilio
    (SID configuré ou non, `TWILIO_INTERACTIVE_ENABLED` on/off)."""

    def test_a_list_menu_hint_always_falls_back_to_plain_text(self, monkeypatch):
        mod, send_spy = _tasks_module(monkeypatch)
        result = {
            "interactive": {
                "kind": "list_menu",
                "title": "Choix",
                "button_text": "Voir les options",
                "options": [
                    {"index": "1", "label": "Poussins allemand", "value": "offer-42", "description": "5900 tête"},
                    {"index": "2", "label": "Maïs", "value": "offer-17"},
                ],
            }
        }
        out = mod._send_via_twilio("+22670000001", "Voici les produits", result)

        send_spy.assert_called_once()
        kwargs = send_spy.call_args.kwargs
        assert kwargs.get("content_sid") is None
        assert kwargs["body"] == "Voici les produits"
        assert out["status"] == "message_sent"
        assert "chunks" in out

    def test_falls_back_to_plain_text_even_with_a_sid_configured(self, monkeypatch):
        mod, send_spy = _tasks_module(monkeypatch, list_sid="HXlist")
        result = {"interactive": {"kind": "list_menu", "options": [{"index": "1", "label": "x", "value": "1"}]}}
        out = mod._send_via_twilio("+22670000001", "texte de repli", result)

        send_spy.assert_called_once()
        assert send_spy.call_args.kwargs.get("content_sid") is None
        assert out["status"] == "message_sent"
        assert "chunks" in out

    def test_falls_back_to_plain_text_when_options_are_empty(self, monkeypatch):
        mod, send_spy = _tasks_module(monkeypatch)
        result = {"interactive": {"kind": "list_menu", "options": []}}
        mod._send_via_twilio("+22670000001", "texte de repli", result)

        assert send_spy.call_args.kwargs.get("content_sid") is None

    def test_falls_back_to_plain_text_when_interactive_enabled(self, monkeypatch):
        mod, send_spy = _tasks_module(monkeypatch, interactive_enabled=True)
        result = {"interactive": {"kind": "list_menu", "options": [{"index": "1", "label": "x", "value": "1"}]}}
        mod._send_via_twilio("+22670000001", "texte de repli", result)

        assert send_spy.call_args.kwargs.get("content_sid") is None


class TestChunkWhatsappBodyPageBreak:
    """Incident 2026-08-27 (listes de stock volumineuses) : le rendu pose des
    marqueurs `PAGE_BREAK` entre pages déjà propres (voir
    `nodes/rendering/success.py::_paginate_item_blocks`) — `_chunk_whatsapp_body`
    doit les privilégier sur le découpage aveugle par caractère."""

    def test_page_break_markers_produce_one_chunk_per_page(self, monkeypatch):
        import ladini.api.response_dispatch as mod
        from ladini.graphs.agents.market_coach.services.text_pagination import (
            PAGE_BREAK,
        )

        body = f"page un{PAGE_BREAK}page deux{PAGE_BREAK}page trois"
        chunks = mod._chunk_whatsapp_body(body)

        assert chunks == ["page un", "page deux", "page trois"]

    def test_a_page_break_marker_never_leaks_into_the_sent_text(self, monkeypatch):
        import ladini.api.response_dispatch as mod
        from ladini.graphs.agents.market_coach.services.text_pagination import (
            PAGE_BREAK,
        )

        body = f"a{PAGE_BREAK}b"
        for chunk in mod._chunk_whatsapp_body(body):
            assert PAGE_BREAK not in chunk

    def test_a_body_without_page_breaks_behaves_as_before(self, monkeypatch):
        import ladini.api.response_dispatch as mod

        chunks = mod._chunk_whatsapp_body("juste du texte")
        assert chunks == ["juste du texte"]

    def test_an_oversized_page_is_still_re_chunked_by_char_limit(self, monkeypatch):
        import ladini.api.response_dispatch as mod
        from ladini.graphs.agents.market_coach.services.text_pagination import (
            PAGE_BREAK,
        )

        oversized = "x" * 2000
        body = f"court{PAGE_BREAK}{oversized}"
        chunks = mod._chunk_whatsapp_body(body, limit=1400)

        assert chunks[0] == "court"
        assert all(len(c) <= 1400 for c in chunks)


class TestSendViaTwilioQuickReply:
    def test_confirm_kind_sends_the_quick_reply_content_template(self, monkeypatch):
        mod, send_spy = _tasks_module(monkeypatch)
        result = {"interactive": {"kind": "confirm"}}
        out = mod._send_via_twilio("+22670000001", "Confirmez-vous ?", result)

        send_spy.assert_called_once()
        kwargs = send_spy.call_args.kwargs
        assert kwargs["content_sid"] == "HXconfirm"
        assert kwargs["content_vars"] == {"1": "Confirmez-vous ?"}
        assert out == {"status": "message_sent", "sid": "SM123", "interactive": "confirm"}

    def test_quick_reply_kind_uses_the_same_content_template_as_confirm(self, monkeypatch):
        """`quick_reply` (ag_ui_component QuickReplies) et `confirm` (filet
        de sécurité) partagent le même template Content API — seul le body
        varie, les boutons sont figés côté template Twilio."""
        mod, send_spy = _tasks_module(monkeypatch)
        result = {"interactive": {"kind": "quick_reply", "body": "x", "buttons": []}}
        out = mod._send_via_twilio("+22670000001", "Confirmez-vous ?", result)

        assert send_spy.call_args.kwargs["content_sid"] == "HXconfirm"
        assert out["interactive"] == "quick_reply"

    def test_falls_back_to_plain_text_when_confirm_sid_is_not_configured(self, monkeypatch):
        mod, send_spy = _tasks_module(monkeypatch, confirm_sid="")
        result = {"interactive": {"kind": "confirm"}}
        out = mod._send_via_twilio("+22670000001", "texte de repli", result)

        assert send_spy.call_args.kwargs.get("content_sid") is None
        assert out["status"] == "message_sent"


class TestSendViaTwilioPlainText:
    def test_no_interactive_hint_sends_plain_chunked_text(self, monkeypatch):
        mod, send_spy = _tasks_module(monkeypatch)
        out = mod._send_via_twilio("+22670000001", "juste du texte", {})

        assert send_spy.call_args.kwargs.get("content_sid") is None
        assert send_spy.call_args.kwargs["body"] == "juste du texte"
        assert out["status"] == "message_sent"

    def test_unknown_kind_falls_back_to_plain_text(self, monkeypatch):
        mod, send_spy = _tasks_module(monkeypatch)
        result = {"interactive": {"kind": "something_unrecognized"}}
        mod._send_via_twilio("+22670000001", "texte", result)

        assert send_spy.call_args.kwargs.get("content_sid") is None

    def test_missing_twilio_config_raises_before_any_send_attempt(self, monkeypatch):
        import ladini.api.response_dispatch as mod

        monkeypatch.setattr(mod.settings, "TWILIO_ACCOUNT_SID", "", raising=False)
        with pytest.raises(RuntimeError, match="Twilio configuration incomplete"):
            mod._send_via_twilio("+22670000001", "texte", {})


# =====================================================================
# Incident 2026-08-27 : erreur Twilio 21617 — "The concatenated message
# body exceeds the 1600 character limit". `body` accompagne `content_sid`
# dans send_whatsapp_message ; il n'était borné nulle part (seul
# content_vars["1"] l'était), donc un final_text long (catalogue, ex.
# 2178 caractères) faisait échouer l'envoi ET déclenchait une boucle de
# retry Celery inutile (une erreur de validation client ne se résout
# jamais au retry).
# =====================================================================

class TestSendViaTwilioLongBodyIncident:
    def _long_text(self, n=2178):
        return "x" * n

    def test_a_long_body_alongside_a_list_menu_hint_is_chunked_like_plain_text(self, monkeypatch):
        """`list_menu` étant désactivé (2026-08-27), un texte long part par
        le chemin texte brut chunké standard — pas de content_sid, donc pas
        de risque 21617, mais toujours plusieurs messages de ≤1400c."""
        mod, send_spy = _tasks_module(monkeypatch)
        result = {
            "interactive": {
                "kind": "list_menu",
                "options": [{"index": "1", "label": "Poussins", "value": "o1"}],
            }
        }
        out = mod._send_via_twilio("+22670000001", self._long_text(), result)

        for call in send_spy.call_args_list:
            assert call.kwargs.get("content_sid") is None
            assert len(call.kwargs["body"]) <= 1400
        assert out["status"] == "message_sent"

    def test_a_long_body_alongside_a_confirm_content_sid_is_truncated(self, monkeypatch):
        mod, send_spy = _tasks_module(monkeypatch)
        result = {"interactive": {"kind": "confirm"}}
        mod._send_via_twilio("+22670000001", self._long_text(), result)

        body = send_spy.call_args.kwargs["body"]
        assert len(body) <= 1400

    def test_a_twilio_rest_exception_on_confirm_falls_back_to_plain_text(self, monkeypatch):
        from twilio.base.exceptions import TwilioRestException

        mod, _ = _tasks_module(monkeypatch)

        def _boom(**kwargs):
            if kwargs.get("content_sid"):
                raise TwilioRestException(400, "uri", msg="template error", code=63016)
            return SimpleNamespace(sid="SM-plain")

        monkeypatch.setattr(mod, "send_whatsapp_message", _boom)

        out = mod._send_via_twilio("+22670000001", "Confirmez-vous ?", {"interactive": {"kind": "confirm"}})
        assert out["status"] == "message_sent"
        assert out["sid"] == "SM-plain"
