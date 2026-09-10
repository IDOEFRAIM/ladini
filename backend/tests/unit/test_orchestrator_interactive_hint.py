"""`orchestrator/orchestrator.py::Orchestrator._interactive_hint` — audit
MCP/AGUI 2026-08-26 : unifie le rendu génératif sur ``ag_ui_component``.

Avant : ``ag_ui_component`` (construit par ``nodes/ui_engine.py`` à partir
d'un ``MenuRequest``) était écrit dans l'état du graphe mais jamais lu par
la couche d'envoi — la couche d'envoi ne connaissait qu'un champ
``interactive`` séparé, dérivé de ``response_strategy``/``status``, alimenté
uniquement pour la confirmation. Tout menu construit via le chemin
``MenuRequest``/``ui_engine`` restait donc invisible pour l'utilisateur
WhatsApp (texte brut sans les boutons/liste). Ces tests verrouillent que
``_interactive_hint`` traduit désormais ``ag_ui_component`` en premier."""
from __future__ import annotations

from ladini.orchestrator.orchestrator import Orchestrator


class TestInteractiveHintReadsAgUiComponent:
    def test_a_list_menu_ag_ui_component_is_no_longer_translated(self):
        """`list_menu` interactif désactivé (2026-08-27, blocages de
        template récurrents type erreur 21656) : un ag_ui_component
        ``ListMenu`` ne doit plus jamais produire d'indice interactif — le
        tour retombe sur `final_response` (texte brut, déjà paginé par le
        renderer)."""
        final = {
            "ag_ui_component": {
                "lc_type": "constructor",
                "id": ["ag_ui", "ListMenu"],
                "kwargs": {
                    "title": "Quel produit ?",
                    "options": [
                        {"index": 1, "label": "Tomates"},
                        {"index": 2, "label": "Oignons"},
                    ],
                    "metadata": {"kind": "product_disambiguation"},
                },
            },
        }
        assert Orchestrator._interactive_hint(final) is None

    def test_an_unrelated_ag_ui_component_id_is_ignored(self):
        """Seul le type ``ListMenu`` est traduit — un futur type ag_ui
        différent ne doit pas être mal interprété comme une liste."""
        final = {
            "ag_ui_component": {
                "id": ["ag_ui", "SomeOtherWidget"],
                "kwargs": {"title": "x", "options": []},
            },
        }
        assert Orchestrator._interactive_hint(final) is None

    def test_confirm_still_works_without_an_ag_ui_component(self):
        """Non-régression : le chemin de confirmation existant (indépendant
        d'ag_ui_component) doit continuer de fonctionner à l'identique."""
        final = {"status": "WAITING_CONFIRMATION"}
        assert Orchestrator._interactive_hint(final) == {"kind": "confirm"}

    def test_a_list_menu_ag_ui_component_does_not_shadow_the_confirm_fallback(self):
        """Un ``ListMenu`` n'étant plus traduit, le filet de sécurité
        `confirm` (statut WAITING_CONFIRMATION) reste actif même en sa
        présence — plus de branche `list_menu` pour le court-circuiter."""
        final = {
            "status": "WAITING_CONFIRMATION",
            "ag_ui_component": {
                "id": ["ag_ui", "ListMenu"],
                "kwargs": {"title": "Choix", "options": [{"index": 1, "label": "A"}]},
            },
        }
        hint = Orchestrator._interactive_hint(final)
        assert hint["kind"] == "confirm"

    def test_no_hint_when_nothing_matches(self):
        assert Orchestrator._interactive_hint({"status": "COMPLETED"}) is None

    def test_non_dict_input_is_handled_gracefully(self):
        assert Orchestrator._interactive_hint(None) is None  # type: ignore[arg-type]


class TestInteractiveHintReadsQuickReplies:
    """Audit UX interactive 2026-08-27 : QuickReplies remplace
    FormConfirmation, un composant que ce hint ne lisait jamais (voir
    nodes/rendering/confirm.py et nodes/confirmation_gate.py)."""

    def test_a_quick_replies_ag_ui_component_is_translated(self):
        final = {
            "ag_ui_component": {
                "id": ["ag_ui", "QuickReplies"],
                "kwargs": {
                    "body": "Confirmez-vous la vente de 50kg de mais ?",
                    "buttons": [
                        {"id": "CONFIRM", "title": "✅ Confirmer"},
                        {"id": "REJECT", "title": "❌ Annuler"},
                    ],
                    "metadata": {"goal": "SALES_PUBLISH_PRODUCT"},
                },
            },
        }
        hint = Orchestrator._interactive_hint(final)
        assert hint == {
            "kind": "quick_reply",
            "body": "Confirmez-vous la vente de 50kg de mais ?",
            "buttons": [
                {"id": "CONFIRM", "title": "✅ Confirmer"},
                {"id": "REJECT", "title": "❌ Annuler"},
            ],
        }

    def test_quick_replies_are_capped_at_three_buttons(self):
        final = {
            "ag_ui_component": {
                "id": ["ag_ui", "QuickReplies"],
                "kwargs": {
                    "body": "x",
                    "buttons": [{"id": str(i), "title": str(i)} for i in range(5)],
                },
            },
        }
        hint = Orchestrator._interactive_hint(final)
        assert len(hint["buttons"]) == 3

    def test_quick_replies_takes_priority_over_the_confirm_flag_fallback(self):
        final = {
            "status": "WAITING_CONFIRMATION",
            "ag_ui_component": {
                "id": ["ag_ui", "QuickReplies"],
                "kwargs": {"body": "x", "buttons": []},
            },
        }
        hint = Orchestrator._interactive_hint(final)
        assert hint["kind"] == "quick_reply"
