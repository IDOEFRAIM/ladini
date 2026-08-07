"""Interpréteur : extraction déterministe + PRIMAUTÉ DU LLM.

Règle d'architecture verrouillée ici (exigée par le porteur du projet) :
  * le LLM dit ce que veut l'utilisateur — il est PRIMAIRE ;
  * le déterministe n'intervient que pour (a) le structurel (index de menu,
    clic), (b) une garde contre un échec LLM PROUVÉ (ancrage d'unité),
    (c) un repli quand le LLM est indisponible.

Chaque test correspond à un bug réellement observé en production.
"""
from __future__ import annotations

import pytest

from agriconnect.graphs.agents.market_coach.interpreter.routing import (
    _interpret_fast_path,
    make_input_interpreter,
)
from tests.conftest import ForbiddenLLM, ScriptedLLM, StubRuntime, make_state, run


def fast(text, expected_input, goal="SALES_PUBLISH_PRODUCT", skip=False, payload=None):
    state = {
        "expected_input": expected_input,
        "current_goal": goal,
        "working_memory": {"active_goal": goal},
        "transaction_payload": payload if payload is not None else {"product": "tomates"},
        "user_role": "PRODUCER",
    }
    return _interpret_fast_path(state, text, skip_numeric_shortcut=skip)


# =====================================================================
# EXTRACTION DÉTERMINISTE — cas NON AMBIGUS
# =====================================================================

class TestFastPathExtraction:
    @pytest.mark.parametrize("text,qty,unit", [
        ("200 tonnes", 200.0, "TONNE"),
        ("60kg", 60.0, "KG"),            # collé — saisie mobile
        ("50 sacs", 50.0, "SAC"),
        ("12,5 kg", 12.5, "KG"),
        ("1 000 kg", 1000.0, "KG"),
    ])
    def test_quantity_answer(self, text, qty, unit):
        r = fast(text, "QUANTITY")
        assert r["interpreted_event"] == "ANSWER"
        assert r["extracted_entities"]["quantity"] == qty
        assert r["extracted_entities"]["unit"] == unit

    def test_price_keeps_its_own_unit(self):
        """« 10000fcfa/kg » : price_unit=KG, SANS écraser l'unité de la quantité."""
        r = fast("10000fcfa/kg", "PRICE")
        ents = r["extracted_entities"]
        assert ents["price"] == 10000.0
        assert ents["price_unit"] == "KG"
        assert "unit" not in ents, "l'unité du PRIX ne doit jamais devenir celle de la QUANTITÉ"

    def test_compound_quantity_and_price_in_one_message(self):
        """Régression : « 775 kg ... 175 fcfa » remplit les DEUX slots.

        Avant, un seul était pris et l'agent redemandait l'autre en boucle.
        """
        r = fast("j ai 775 kg d oignon et le kg coute 175 fcfa", "QUANTITY")
        ents = r["extracted_entities"]
        assert ents["quantity"] == 775.0 and ents["unit"] == "KG"
        assert ents["price"] == 175.0
        assert r["raw_analysis"]["path"] == "fast_path_slot_numeric_compound_answer"

    def test_currency_number_is_never_mistaken_for_quantity(self):
        """« 14 chèvres et l'unité coûte 34500 fcfa » -> qty=14, pas 34500."""
        r = fast("14 chevres et l unite coute 34500 fcfa", "QUANTITY", goal="DECLARE_CROP_CYCLE")
        assert r["extracted_entities"]["quantity"] == 14.0
        assert r["extracted_entities"]["price"] == 34500.0

    @pytest.mark.parametrize("text,field,value", [
        ("j ai plutot 795 kg", "quantity", 795.0),
        ("nonnn j ai 795 kg", "quantity", 795.0),
        ("le prix c est 250 fcfa", "price", 250.0),
    ])
    def test_correction_during_confirmation_is_an_update(self, text, field, value):
        """Régression : les corrections chiffrées pendant le récap étaient ignorées."""
        r = fast(text, "CONFIRMATION")
        assert r["interpreted_event"] == "UPDATE"
        assert r["extracted_entities"][field] == value

    def test_confirmation_without_number_is_not_hijacked(self):
        assert fast("oui c est bon", "CONFIRMATION") is None

    def test_menu_index_is_structural_and_always_deterministic(self):
        r = fast("2", "SELECTION", goal="BUYER_REQUEST")
        assert r["interpreted_event"] == "SELECTION"
        assert r["extracted_entities"]["selection_index"] == 2

    def test_ambiguous_bare_number_is_deferred_to_llm(self):
        """Nombre nu SANS unité ni devise : le LLM doit décider."""
        assert fast("environ 300", "PRICE", skip=True) is None

    def test_ambiguous_bare_number_resolved_when_llm_unavailable(self):
        """…mais si le LLM est indisponible, le repli déterministe s'applique."""
        r = fast("environ 300", "PRICE", skip=False)
        assert r["extracted_entities"]["price"] == 300.0


# =====================================================================
# PRIMAUTÉ DU LLM — l'unité écrite par l'utilisateur fait foi
# =====================================================================

class TestUnitAnchoringGuard:
    """Bug vécu : le LLM « s'ancrait » sur TONNE et forçait tout en tonnes,
    malgré des corrections explicites en kg. Le TEXTE de l'utilisateur prime."""

    ANCHORED = {
        "interpreted_event": "NEW_TASK",
        "detected_intent": "SALES_PUBLISH_PRODUCT",
        "interpreter_confidence": 0.9,
        "validation_status": "VALID",
        "extracted_entities": {"product": "tomates", "quantity": 200.0, "unit": "TONNE"},
    }

    def _interpret(self, text, llm_payload=None):
        interp = make_input_interpreter("PRODUCER")
        rt = StubRuntime(llm=ScriptedLLM(llm_payload or self.ANCHORED))
        st = make_state(normalized_text=text, expected_input="NONE", user_role="PRODUCER")
        return run(interp(st, rt))

    @pytest.mark.parametrize("text,expected_unit", [
        ("je veux vendre 200kg de tomates", "KG"),      # collé
        ("j ai 500 kg de riz a vendre", "KG"),          # espacé
        ("je vends 30 sacs de mais", "SAC"),
        ("je vends deux cents kilos", "KG"),            # nombre en toutes lettres
        ("je vends 200 en sacs", "SAC"),                # unité non adjacente
    ])
    def test_literal_unit_in_text_beats_anchored_llm(self, text, expected_unit):
        r = self._interpret(text)
        assert r["extracted_entities"].get("unit") == expected_unit

    def test_genuine_tonnes_are_preserved(self):
        """Ne pas sur-corriger : si l'utilisateur dit tonnes, c'est TONNE."""
        r = self._interpret("je vends 3 tonnes de mais")
        assert r["extracted_entities"].get("unit") == "TONNE"

    def test_unit_absent_from_text_is_dropped_not_invented(self):
        """Aucune unité écrite -> le LLM ne peut pas la connaître : on l'écarte
        pour laisser le défaut du registre (KG) / élevage (TETE) s'appliquer."""
        r = self._interpret("je vends 200 de tomates")
        assert r["extracted_entities"].get("unit") is None

    def test_llm_self_reported_missing_unit_is_honoured(self):
        """`validation_status` est un signal DU LLM : on le consomme."""
        payload = dict(self.ANCHORED)
        payload["validation_status"] = "INVALID_MISSING_UNIT"
        payload["extracted_entities"] = {"product": "tomates", "quantity": 200.0, "unit": None}
        r = self._interpret("je vends 200 de tomates", payload)
        assert r["extracted_entities"].get("unit") is None


# =====================================================================
# AUCUNE LISTE FIGÉE NE DOIT DÉTOURNER LE LLM
# =====================================================================

class TestNoFrozenListHijack:
    """Régression majeure : une liste de tournures françaises (« je veux »,
    « je cherche »…) FORÇAIT detected_intent=BUYER_REQUEST, écrasant le LLM."""

    @pytest.mark.parametrize("text,llm_intent", [
        ("je veux voir mon panier", "BUYER_VIEW_CART"),
        ("je veux payer maintenant", "BUYER_PREORDER_CONFIRM"),
        ("il me faut le suivi de ma commande", "BUYER_CHECK_ORDER_STATUS"),
        ("je cherche a annuler ma commande", "BUYER_CANCEL_ORDER"),
        ("j aimerais lancer un appel d offres", "PROCUREMENT_CREATE_REQUEST"),
    ])
    def test_llm_classification_is_not_overridden(self, text, llm_intent):
        interp = make_input_interpreter("BUYER")
        rt = StubRuntime(llm=ScriptedLLM({
            "interpreted_event": "NEW_TASK",
            "detected_intent": llm_intent,
            "interpreter_confidence": 0.92,
            "extracted_entities": {},
        }))
        st = make_state(normalized_text=text, expected_input="NONE", user_role="BUYER")
        r = run(interp(st, rt))
        assert r["detected_intent"] == llm_intent, (
            "une heuristique lexicale a détourné la classification du LLM"
        )

    def test_hallucinated_intent_outside_catalog_is_rejected(self):
        """Contrepartie SAINE : une intention INVENTÉE par le LLM (absente du
        catalogue) est ramenée à UNKNOWN — jamais routée ni exécutée."""
        interp = make_input_interpreter("BUYER")
        rt = StubRuntime(llm=ScriptedLLM({
            "interpreted_event": "NEW_TASK",
            "detected_intent": "INTENTION_QUI_N_EXISTE_PAS",
            "interpreter_confidence": 0.99,
            "extracted_entities": {},
        }))
        st = make_state(normalized_text="fais un truc bizarre", expected_input="NONE", user_role="BUYER")
        r = run(interp(st, rt))
        assert r["detected_intent"] == "UNKNOWN"

    def test_dual_role_allows_selling_intent_for_a_buyer(self):
        """Refonte double-rôle : un même utilisateur peut vendre ET acheter —
        une intention de vente ne doit PAS être filtrée pour un acheteur."""
        interp = make_input_interpreter("BUYER")
        rt = StubRuntime(llm=ScriptedLLM({
            "interpreted_event": "NEW_TASK",
            "detected_intent": "SALES_PUBLISH_PRODUCT",
            "interpreter_confidence": 0.95,
            "extracted_entities": {},
        }))
        st = make_state(normalized_text="je veux vendre du mais", expected_input="NONE", user_role="BUYER")
        r = run(interp(st, rt))
        assert r["detected_intent"] == "SALES_PUBLISH_PRODUCT"
