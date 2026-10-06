"""Messages PROACTIFS par template WhatsApp approuvé (relances commerciales, confirmations de commande, Outbox).

Un message libre n'est livré que dans les 24 h suivant le dernier message de l'utilisateur ; un template approuvé
l'est toujours. Template : « Bonjour: / {{1}} / Merci pour votre confiance. » — `{{1}}` porte le message, aplati
(WhatsApp refuse retours à la ligne, tabulations et plus de 4 espaces consécutifs dans une variable).
"""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from ladini.core.settings import settings
from ladini.workers.outbox.channels.whatsapp import (
    WhatsAppChannel,
    flatten_for_template,
    template_chunks,
)

SID = "HX3ed61f2342542cc21aa97e3e5f3f3aec"


class _Calls:
    def __init__(self, fail_on: int | None = None, fail_template_always: bool = False):
        self.calls: list[dict] = []
        self.fail_on = fail_on
        self.fail_template_always = fail_template_always

    def create(self, **kw):
        self.calls.append(kw)
        is_template = "content_sid" in kw
        if is_template and self.fail_template_always:
            raise RuntimeError("20404 template introuvable")
        if is_template and self.fail_on is not None and len([c for c in self.calls if "content_sid" in c]) == self.fail_on:
            raise RuntimeError("63016 échec")
        return SimpleNamespace(sid=f"SM-{len(self.calls)}")


@pytest.fixture
def twilio(monkeypatch):
    holder = SimpleNamespace(messages=_Calls())

    class _Client:
        def __init__(self, sid, token, http_client=None):
            self.messages = holder.messages

    class _Http:
        def __init__(self, timeout=None):
            self.timeout = timeout

    http_client = SimpleNamespace(TwilioHttpClient=_Http)
    fake = SimpleNamespace(rest=SimpleNamespace(Client=_Client), http=SimpleNamespace(http_client=http_client))
    monkeypatch.setitem(sys.modules, "twilio", fake)
    monkeypatch.setitem(sys.modules, "twilio.rest", fake.rest)
    monkeypatch.setitem(sys.modules, "twilio.http", fake.http)
    monkeypatch.setitem(sys.modules, "twilio.http.http_client", http_client)
    for key, value in (("TWILIO_ACCOUNT_SID", "sid"), ("TWILIO_AUTH_TOKEN", "tok"), ("TWILIO_WHATSAPP_NUMBER", "+1555")):
        monkeypatch.setattr(settings, key, value, raising=False)
    return holder


def _send(body: str):
    return WhatsAppChannel()._send_sync_twilio("+2260", body)


def test_flatten_removes_newlines_tabs_and_long_space_runs():
    assert flatten_for_template("Commande confirmée ✅\n\nProduit : oignon   x 50 kg\n\tTotal : 8750 FCFA") == (
        "Commande confirmée ✅ | Produit : oignon x 50 kg | Total : 8750 FCFA"
    )
    flat = flatten_for_template("a\r\n\r\n\r\nb      c")
    assert "\n" not in flat and "\t" not in flat and "     " not in flat


def test_chunks_stay_under_the_variable_limit_and_cut_on_spaces():
    chunks = template_chunks("mot " * 600)
    assert 1 < len(chunks) <= 4 and all(len(c) <= 900 for c in chunks)
    assert all(not c.startswith(" ") and not c.endswith(" ") for c in chunks)


def test_with_a_template_the_message_goes_through_the_content_api(twilio, monkeypatch):
    monkeypatch.setattr(settings, "TWILIO_PROACTIVE_TEMPLATE_CONTENT_SID", SID, raising=False)
    result = _send("Votre commande est confirmée.\nTotal : 8750 FCFA")
    (call,) = twilio.messages.calls
    assert call["content_sid"] == SID and call["to"] == "whatsapp:+2260" and call["from_"] == "+1555"
    assert "body" not in call  # jamais de corps libre : c'est le template qui porte le message
    assert json.loads(call["content_variables"]) == {"1": "Votre commande est confirmée. | Total : 8750 FCFA"}
    assert result.ok and result.provider_ref == "SM-1"


def test_a_long_message_is_split_into_several_template_messages(twilio, monkeypatch):
    monkeypatch.setattr(settings, "TWILIO_PROACTIVE_TEMPLATE_CONTENT_SID", SID, raising=False)
    result = _send("mot " * 600)
    assert 1 < len(twilio.messages.calls) <= 4 and all("content_sid" in c for c in twilio.messages.calls)
    assert result.provider_ref == f"SM-{len(twilio.messages.calls)}"


def test_without_a_template_the_historic_free_form_send_is_unchanged(twilio, monkeypatch):
    monkeypatch.setattr(settings, "TWILIO_PROACTIVE_TEMPLATE_CONTENT_SID", "", raising=False)
    _send("bonjour\nmerci")
    (call,) = twilio.messages.calls
    assert call["body"] == "bonjour\nmerci" and "content_sid" not in call


def test_a_refused_template_falls_back_to_free_form_instead_of_losing_the_message(twilio, monkeypatch):
    monkeypatch.setattr(settings, "TWILIO_PROACTIVE_TEMPLATE_CONTENT_SID", SID, raising=False)
    twilio.messages.fail_template_always = True
    result = _send("relance importante")
    assert [("content_sid" in c) for c in twilio.messages.calls] == [True, False]
    assert twilio.messages.calls[1]["body"] == "relance importante"
    assert result.ok


def test_a_failure_after_a_partial_template_send_is_reported_not_duplicated(twilio, monkeypatch):
    monkeypatch.setattr(settings, "TWILIO_PROACTIVE_TEMPLATE_CONTENT_SID", SID, raising=False)
    twilio.messages.fail_on = 2  # le 2e morceau échoue après que le 1er est parti
    result = _send("mot " * 600)
    assert result.ok is False
    assert all("content_sid" in c for c in twilio.messages.calls), "aucun renvoi libre qui doublonnerait le début"


def test_commercial_follow_up_uses_the_same_channel(monkeypatch):
    """La relance du compte commercial passe par `WhatsAppChannel().send` : elle hérite donc du template."""
    import inspect

    import ladini.services.commercial.admin_api as api

    assert "WhatsAppChannel().send(" in inspect.getsource(api)
