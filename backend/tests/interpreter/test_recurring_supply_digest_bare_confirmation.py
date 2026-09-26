"""`_bare_confirmation_for_recurring_supply_digest` (interpreter/routing.py, VS4 pilote — mandat
digest). Même discipline que `_bare_confirmation_for_pending_producer_order` (son voisin côté
producteur) : réponse à un message PROACTIF (le digest quotidien
`RecurringSupplyDigestService`), sans `PendingInteraction`, sur un vocabulaire fermé et borné à la
disponibilité RÉELLE d'un besoin — vérification déterministe, jamais un résultat deviné.

Aucun nouvel intent LLM (budget du prompt `new_task_v2` déjà saturé, spec §50) : ce fast-path
émet toujours `UPDATE_RECURRING_NEED` (déjà au catalogue) avec un `action` sentinel interne —
`"CONFIRM_MATCH"`/`"REJECT_MATCH"` (inchangés), ou `"DIGEST_MODIFY_MENU"` pour "modifier"/
"changer" (mandat digest 2026-09-26, §5 : AVANT ce mandat, "modifier" émettait `GET_MY_NEEDS`
sans `action`, un simple listing en lecture seule sans jamais ouvrir de vraie modification
guidée — voir `flows/buyer/recurring_need.py::_digest_modify_flow_start` pour le mini-flow
que ce sentinel déclenche désormais)."""
from __future__ import annotations

from ladini.graphs.agents.market_coach.interpreter.routing import (
    _bare_confirmation_for_recurring_supply_digest,
)
from tests.conftest import StubRuntime, run


def rt(responses=None):
    return StubRuntime(responses=responses or {})


_WITH_NEEDS = {
    "list_my_recurring_needs": {
        "status": "success",
        "items": [{"recurring_need_id": "need-1", "product": "tomate", "matched_quantity": 40}],
    }
}
_NO_NEEDS = {"list_my_recurring_needs": {"status": "success", "items": []}}


class TestConfirmWords:
    def test_confirmer_with_a_recurring_need_resolves_to_update_confirm_match(self):
        result = run(_bare_confirmation_for_recurring_supply_digest(rt(_WITH_NEEDS), "+226700", "confirmer"))
        assert result is not None
        assert result["detected_intent"] == "UPDATE_RECURRING_NEED"
        assert result["extracted_entities"] == {"action": "CONFIRM_MATCH"}
        assert result["interpreted_event"] == "NEW_TASK"

    def test_oui_also_resolves_as_confirm(self):
        result = run(_bare_confirmation_for_recurring_supply_digest(rt(_WITH_NEEDS), "+226700", "oui"))
        assert result is not None and result["extracted_entities"]["action"] == "CONFIRM_MATCH"

    def test_no_recurring_need_at_all_defers_to_normal_classification(self):
        result = run(_bare_confirmation_for_recurring_supply_digest(rt(_NO_NEEDS), "+226700", "confirmer"))
        assert result is None


class TestRejectWords:
    def test_pas_cette_fois_resolves_to_update_reject_match(self):
        result = run(_bare_confirmation_for_recurring_supply_digest(rt(_WITH_NEEDS), "+226700", "pas cette fois"))
        assert result is not None
        assert result["detected_intent"] == "UPDATE_RECURRING_NEED"
        assert result["extracted_entities"] == {"action": "REJECT_MATCH"}

    def test_pas_demain_also_resolves_as_reject(self):
        result = run(_bare_confirmation_for_recurring_supply_digest(rt(_WITH_NEEDS), "+226700", "pas demain"))
        assert result is not None and result["extracted_entities"]["action"] == "REJECT_MATCH"

    def test_non_also_resolves_as_reject(self):
        result = run(_bare_confirmation_for_recurring_supply_digest(rt(_WITH_NEEDS), "+226700", "non"))
        assert result is not None and result["extracted_entities"]["action"] == "REJECT_MATCH"

    def test_no_recurring_need_at_all_defers_to_normal_classification(self):
        result = run(_bare_confirmation_for_recurring_supply_digest(rt(_NO_NEEDS), "+226700", "pas demain"))
        assert result is None


class TestModifyWord:
    def test_modifier_opens_the_digest_modify_menu(self):
        result = run(_bare_confirmation_for_recurring_supply_digest(rt(_WITH_NEEDS), "+226700", "modifier"))
        assert result is not None
        assert result["detected_intent"] == "UPDATE_RECURRING_NEED"
        assert result["extracted_entities"] == {"action": "DIGEST_MODIFY_MENU"}

    def test_changer_also_opens_the_digest_modify_menu(self):
        result = run(_bare_confirmation_for_recurring_supply_digest(rt(_WITH_NEEDS), "+226700", "changer"))
        assert result is not None and result["extracted_entities"]["action"] == "DIGEST_MODIFY_MENU"

    def test_no_recurring_need_at_all_defers_to_normal_classification(self):
        result = run(_bare_confirmation_for_recurring_supply_digest(rt(_NO_NEEDS), "+226700", "modifier"))
        assert result is None


class TestPasDemainPourNamedProduct:
    """Mandat digest §9 : "pas demain pour l'oignon" ne rejette QUE ce besoin nommé — distinct
    de "pas demain" bare (`TestRejectWords`), qui rejette TOUS les besoins actionnables."""

    def test_pas_demain_pour_extracts_the_product_hint(self):
        result = run(
            _bare_confirmation_for_recurring_supply_digest(rt(_WITH_NEEDS), "+226700", "pas demain pour l'oignon")
        )
        assert result is not None
        assert result["detected_intent"] == "UPDATE_RECURRING_NEED"
        assert result["extracted_entities"] == {"action": "DIGEST_SKIP_PRODUCT", "product": "oignon"}

    def test_pas_demain_pour_la_tomate_also_matches(self):
        result = run(
            _bare_confirmation_for_recurring_supply_digest(rt(_WITH_NEEDS), "+226700", "pas demain pour la tomate")
        )
        assert result is not None and result["extracted_entities"]["product"] == "tomate"


class TestProtections:
    def test_an_unrelated_word_never_triggers_a_db_lookup(self):
        # Aucune réponse stubée pour list_my_recurring_needs : si le code l'appelait quand même,
        # StubRuntime lèverait — la réussite du test prouve que l'appel n'a jamais eu lieu.
        result = run(_bare_confirmation_for_recurring_supply_digest(rt(), "+226700", "bonjour"))
        assert result is None

    def test_no_phone_defers_to_normal_classification(self):
        result = run(_bare_confirmation_for_recurring_supply_digest(rt(_WITH_NEEDS), "", "confirmer"))
        assert result is None

    def test_a_gateway_error_defers_to_normal_classification_rather_than_raising(self):
        class RaisingRuntime:
            llm = None

            def __getattr__(self, name):
                raise RuntimeError("boom")

        result = run(_bare_confirmation_for_recurring_supply_digest(RaisingRuntime(), "+226700", "confirmer"))
        assert result is None

    def test_a_typo_on_confirmer_is_still_recognized(self):
        result = run(_bare_confirmation_for_recurring_supply_digest(rt(_WITH_NEEDS), "+226700", "confimer"))
        assert result is not None and result["extracted_entities"]["action"] == "CONFIRM_MATCH"
