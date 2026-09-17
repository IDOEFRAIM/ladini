"""`api/tasks.py::_send_via_whatsapp_cloud` — `list_menu` interactif
DÉSACTIVÉ (2026-08-27) : blocages de template récurrents côté Meta/Twilio
(ex. erreur 21656 sur le transport Twilio, schéma figé côté Console). Toute
liste part désormais en texte brut (chunké), même via l'API Cloud Meta qui
supporterait pourtant nativement les listes dynamiques — cohérence
volontaire entre les deux transports plutôt qu'un comportement différent
par provider. Seul ``confirm``/``quick_reply`` (boutons) reste interactif."""
from __future__ import annotations

from unittest.mock import AsyncMock

from tests.conftest import run


class TestSendViaWhatsAppCloudListMenu:
    def _patched(self, monkeypatch, *, native_enabled: bool = True):
        import ladini.api.response_dispatch as mod
        from ladini.services.whatsapp import cloud_api_client as wa

        # CI exporte MOCK_EXTERNAL_APIS=true (voir .github/workflows/cicd.yml)
        # — sans ce garde, `_send_via_whatsapp_cloud` retourne son
        # court-circuit "SIMULÉ" avant d'atteindre wa.is_configured()/send_text
        # /send_interactive_buttons, ce que ces tests verrouillent explicitement.
        monkeypatch.setattr(mod.settings, "MOCK_EXTERNAL_APIS", False, raising=False)
        monkeypatch.setattr(wa, "is_configured", lambda: True)
        monkeypatch.setattr(mod.settings, "WHATSAPP_NATIVE_INTERACTIVE_ENABLED", native_enabled)
        send_list = AsyncMock(return_value="wamid.list1")
        send_text = AsyncMock(return_value=["wamid.text1"])
        monkeypatch.setattr(wa, "send_interactive_list", send_list)
        monkeypatch.setattr(wa, "send_text", send_text)
        return mod, send_list, send_text

    def test_a_list_menu_hint_always_falls_back_to_plain_text(self, monkeypatch):
        mod, send_list, send_text = self._patched(monkeypatch)
        result = {
            "interactive": {
                "kind": "list_menu",
                "title": "Quel produit ?",
                "options": [
                    {"index": 1, "label": "Tomates"},
                    {"index": 2, "label": "Oignons"},
                ],
            }
        }
        out = run(mod._send_via_whatsapp_cloud("+22670000001", "Choisis un produit", result))

        send_list.assert_not_awaited()
        send_text.assert_awaited_once()
        assert out["status"] == "message_sent"

    def test_a_list_menu_hint_with_no_options_falls_back_to_plain_text(self, monkeypatch):
        mod, send_list, send_text = self._patched(monkeypatch)
        result = {"interactive": {"kind": "list_menu", "title": "x", "options": []}}
        out = run(mod._send_via_whatsapp_cloud("+22670000001", "Texte de repli", result))

        send_list.assert_not_awaited()
        send_text.assert_awaited_once()
        assert out["status"] == "message_sent"

    def test_confirm_kind_still_takes_the_buttons_path(self, monkeypatch):
        """Non-régression : la désactivation de `list_menu` ne doit pas
        affecter le chemin de confirmation (boutons), qui reste interactif."""
        mod, send_list, _send_text = self._patched(monkeypatch)
        from ladini.services.whatsapp import cloud_api_client as wa

        send_buttons = AsyncMock(return_value="wamid.btn1")
        monkeypatch.setattr(wa, "send_interactive_buttons", send_buttons)

        result = {"interactive": {"kind": "confirm"}}
        out = run(mod._send_via_whatsapp_cloud("+22670000001", "Confirmer ?", result))

        send_buttons.assert_awaited_once()
        send_list.assert_not_awaited()
        assert out["interactive"] == "confirm"
