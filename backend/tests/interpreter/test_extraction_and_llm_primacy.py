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

from ladini.graphs.agents.market_coach.interpreter.routing import (
    _interpret_fast_path,
    make_input_interpreter,
)
from tests.conftest import ForbiddenLLM, ScriptedLLM, StubRuntime, make_state, run


def fast(text, expected_input, goal="SALES_PUBLISH_PRODUCT", skip=False, payload=None):
    # `make_state` traduit `expected_input=` en écriture RÉELLE de
    # `pending_interaction` (source canonique — voir tests/conftest.py) ;
    # `_interpret_fast_path` ne lit plus `expected_input` directement depuis
    # 2026-09-02 (refonte "no legacy shim").
    state = make_state(
        expected_input=expected_input,
        current_goal=goal,
        working_memory={"active_goal": goal},
        transaction_payload=payload if payload is not None else {"product": "tomates"},
        user_role="PRODUCER",
    )
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
        r = fast("14 chevres et l unite coute 34500 fcfa", "QUANTITY", goal="PRODUCTION_DECLARE_FUTURE")
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
        "disposition": "NEW_TASK",
        "intent": "SALES_PUBLISH_PRODUCT",
        "confidence": 0.9,
        "entities": {"product": "tomates", "quantity": 200.0, "unit": "TONNE"},
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

    def test_python_computes_missing_unit_status_when_no_literal_unit_is_reported(self):
        """Incrément F (2026-09-13) : `validation_status` n'est plus un champ
        demandé au LLM (spec §9, économie de tokens) — Python le déduit
        lui-même de `quantity`/`unit` déjà normalisés (voir
        `new_task_micro.py::_validation_status_for`)."""
        payload = {
            "disposition": "NEW_TASK",
            "intent": "SALES_PUBLISH_PRODUCT",
            "confidence": 0.9,
            "entities": {"product": "tomates", "quantity": 200.0, "unit": None},
        }
        r = self._interpret("je vends 200 de tomates", payload)
        assert r["extracted_entities"].get("unit") is None
        assert r.get("validation_status") == "INVALID_MISSING_UNIT"

    def test_a_bare_unit_word_hallucinated_as_the_product_is_rejected(self):
        """Bug réel confirmé (2026-08-17) : en réponse à une question de
        QUANTITÉ, "je veux 42 kg" a fait extraire "kg" comme PRODUIT (le
        panier cherchait ensuite le produit "kg", inexistant, et rejetait la
        précommande) — écrasant le vrai produit ("tomates") déjà connu de la
        conversation. Rien ne rejetait un mot d'unité (kg/tonne/sac/panier...)
        accepté comme nom de produit — même défaut que les mots de TYPE de
        production ci-dessus, corrigé par le même principe dans
        `_sanitize_product_candidate`."""
        payload = {
            "disposition": "NEW_TASK",
            "intent": "BUYER_ADD_TO_CART",
            "confidence": 0.9,
            "entities": {"product": "kg", "quantity": 42.0, "unit": "KG"},
        }
        r = self._interpret("je veux 42 kg", payload)
        assert "product" not in r["extracted_entities"]
        assert r["extracted_entities"].get("quantity") == 42.0
        assert r["extracted_entities"].get("unit") == "KG"


# =====================================================================
# PRIMAUTÉ DU LLM — une quantité composée écrite par l'utilisateur fait foi
# =====================================================================

class TestCompoundQuantityAntiTruncationGuard:
    """Bug réel (2026-09-03, incident +22601479800) : « je veux 2 tonnes et
    250 kg » → le LLM a répondu quantity=2/unit=TONNE, tronquant le "et 250
    kg" — récap et confirmation figés sur "2 TONNE" à travers plusieurs
    tours, y compris après la correction explicite de l'utilisateur ("non
    j'ai dit 2 tonnes et 250 kg"). Même famille de garde que
    TestUnitAnchoringGuard : le texte de l'utilisateur prime sur une
    extraction LLM possiblement partielle."""

    # Schéma new_task_v2 (disposition/intent/confidence/entities) — pas l'ancien
    # format legacy (interpreted_event/detected_intent/extracted_entities) : depuis
    # Phase 2.5 (fermeture H5), `_interpret_fast_path` s'abstient (`return None`)
    # pour une correction chiffrée pendant une CONFIRMATION dès qu'un classifieur
    # réel existe — le message est donc RÉELLEMENT classifié par le micro-prompt
    # NEW_TASK (`interpreter/new_task_micro.py`), plus jamais court-circuité par le
    # fast-path avant lui. Le comportement testé ici (le texte prime sur une
    # extraction LLM tronquée) doit donc être garanti là où l'extraction a
    # RÉELLEMENT lieu désormais — voir `new_task_micro.py::_finalize`.
    TRUNCATED = {
        "disposition": "NEW_TASK",
        "intent": "PROCUREMENT_CREATE_REQUEST",
        "confidence": 0.9,
        "entities": {"quantity": 2.0, "unit": "TONNE"},
    }

    def _interpret(self, text, llm_payload=None):
        interp = make_input_interpreter("BUYER")
        rt = StubRuntime(llm=ScriptedLLM(llm_payload or self.TRUNCATED))
        st = make_state(
            normalized_text=text,
            expected_input="CONFIRMATION",
            current_goal="PROCUREMENT_CREATE_REQUEST",
            user_role="BUYER",
        )
        return run(interp(st, rt))

    def test_compound_quantity_in_text_overrides_a_truncated_llm_value(self):
        r = self._interpret(
            "je suis pret a payer maximum 250 fcfa le kg et je veux 2 tonnes et 250 kg"
        )
        ents = r["extracted_entities"]
        assert ents.get("quantity") == pytest.approx(2250.0)
        assert ents.get("unit") == "KG"

    def test_the_correction_turn_also_applies_the_compound_quantity(self):
        """La 2e formulation (correction explicite de l'utilisateur) doit
        elle aussi appliquer la somme — pas seulement la 1ère."""
        r = self._interpret("non j ai dit 2 tonnes et 250 kg")
        ents = r["extracted_entities"]
        assert ents.get("quantity") == pytest.approx(2250.0)
        assert ents.get("unit") == "KG"

    def test_a_single_quantity_pair_is_left_untouched(self):
        """Neutre quand le texte ne porte qu'UNE SEULE paire — pas de faux
        déclenchement sur une quantité simple correctement extraite."""
        payload = dict(self.TRUNCATED)
        payload["entities"] = {"quantity": 3.0, "unit": "TONNE"}
        r = self._interpret("je veux 3 tonnes de tomates", payload)
        ents = r["extracted_entities"]
        assert ents.get("quantity") == 3.0
        assert ents.get("unit") == "TONNE"

    def test_a_non_convertible_unit_pair_is_left_untouched(self):
        """« 2 tonnes et 3 sacs » n'est pas sommable (SAC n'a pas d'équivalent
        KG universel) — aucune correction, on laisse le LLM/simple parse."""
        payload = dict(self.TRUNCATED)
        payload["entities"] = {"quantity": 2.0, "unit": "TONNE"}
        r = self._interpret("je veux 2 tonnes et 3 sacs", payload)
        ents = r["extracted_entities"]
        assert ents.get("quantity") == 2.0
        assert ents.get("unit") == "TONNE"


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
            "disposition": "NEW_TASK",
            "intent": llm_intent,
            "confidence": 0.92,
            "entities": {},
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
            "disposition": "NEW_TASK",
            "intent": "INTENTION_QUI_N_EXISTE_PAS",
            "confidence": 0.99,
            "entities": {},
        }))
        st = make_state(normalized_text="fais un truc bizarre", expected_input="NONE", user_role="BUYER")
        r = run(interp(st, rt))
        assert r["detected_intent"] == "UNKNOWN"

    def test_dual_role_allows_selling_intent_for_a_buyer(self):
        """Refonte double-rôle : un même utilisateur peut vendre ET acheter —
        une intention de vente ne doit PAS être filtrée pour un acheteur."""
        interp = make_input_interpreter("BUYER")
        rt = StubRuntime(llm=ScriptedLLM({
            "disposition": "NEW_TASK",
            "intent": "SALES_PUBLISH_PRODUCT",
            "confidence": 0.95,
            "entities": {},
        }))
        st = make_state(normalized_text="je veux vendre du mais", expected_input="NONE", user_role="BUYER")
        r = run(interp(st, rt))
        assert r["detected_intent"] == "SALES_PUBLISH_PRODUCT"

    def test_dual_role_allows_buying_intent_for_a_producer(self):
        """(revue de validation, 2026-09-08) : symétrique du test
        ci-dessus — un utilisateur PRODUCER (donc le graphe compilé
        PRODUCER, `make_input_interpreter("PRODUCER")`) doit pouvoir
        déclencher une intention BUYER sans filtrage. Preuve DIRECTE de
        l'exemple obligatoire du mandat de revue : "user_role=PRODUCER,
        message='je veux acheter 20 kg de tomates' → BUYER_REQUEST"."""
        interp = make_input_interpreter("PRODUCER")
        rt = StubRuntime(llm=ScriptedLLM({
            "disposition": "NEW_TASK",
            "intent": "BUYER_REQUEST",
            "confidence": 0.93,
            "entities": {"product": "tomates", "quantity": 20.0, "unit": "KG"},
        }))
        st = make_state(
            normalized_text="je veux acheter 20 kg de tomates",
            expected_input="NONE",
            user_role="PRODUCER",
        )
        r = run(interp(st, rt))
        assert r["detected_intent"] == "BUYER_REQUEST"

    def test_interpreter_prompt_catalog_is_role_independent(self):
        """Preuve statique complémentaire : le catalogue d'intentions
        injecté dans le prompt LLM est identique quel que soit le rôle
        (plus de filtrage `allowed_intents_for_role` dans
        `_build_dynamic_interpreter_prompt`)."""
        from ladini.graphs.agents.market_coach.interpreter.routing import (
            _build_dynamic_interpreter_prompt,
        )
        producer_prompt = _build_dynamic_interpreter_prompt("PRODUCER")
        buyer_prompt = _build_dynamic_interpreter_prompt("BUYER")
        assert producer_prompt == buyer_prompt
        assert "BUYER_REQUEST" in producer_prompt
        assert "SALES_PUBLISH_PRODUCT" in producer_prompt


# =====================================================================
# DÉTECTION DU MODÈLE DÉGRADÉ — signale à nodes/memory.py qu'un repli
# Groq (429 sur le modèle principal) a répondu ce tour-ci.
# =====================================================================

class TestDegradedModelDetection:
    """`raw_analysis.degraded_model` est le SEUL moyen après-coup de savoir
    si `get_llm.py::GROQ_RATE_LIMIT_FALLBACK` a substitué un modèle plus
    faible à l'appel courant — `nodes/memory.py` s'en sert pour ne durcir son
    garde anti-hallucination que dans ce cas précis (voir
    `tests/nodes/test_nodes_behaviour.py::TestPrimaryModelMultiSlotFilling`)."""

    PAYLOAD = {
        "disposition": "NEW_TASK",
        "intent": "SALES_PUBLISH_PRODUCT",
        "confidence": 0.9,
        "entities": {"product": "tomates"},
    }

    def test_model_matching_the_request_is_not_flagged_as_degraded(self):
        interp = make_input_interpreter("PRODUCER")
        rt = StubRuntime(llm=ScriptedLLM(self.PAYLOAD))  # échoue le modèle demandé par défaut
        st = make_state(normalized_text="tomates", expected_input="PRODUCT", user_role="PRODUCER")
        r = run(interp(st, rt))
        assert r["raw_analysis"]["degraded_model"] is False
        assert r["raw_analysis"]["model_used"] == rt.model_answer

    def test_model_different_from_the_request_is_flagged_as_degraded(self):
        interp = make_input_interpreter("PRODUCER")
        rt = StubRuntime(llm=ScriptedLLM(self.PAYLOAD, respond_as_model="llama-3.1-8b-instant"))
        st = make_state(normalized_text="tomates", expected_input="PRODUCT", user_role="PRODUCER")
        r = run(interp(st, rt))
        assert r["raw_analysis"]["degraded_model"] is True
        assert r["raw_analysis"]["model_used"] == "llama-3.1-8b-instant"

    def test_unresolvable_model_field_on_the_response_defaults_to_degraded(self):
        """Filet de sécurité : si `.model` ne correspond pas clairement au
        modèle demandé (vide, inattendu...), on ne peut PAS prouver que le
        modèle principal a répondu — on reste prudent plutôt que de
        désactiver le garde-fou."""
        interp = make_input_interpreter("PRODUCER")
        rt = StubRuntime(llm=ScriptedLLM(self.PAYLOAD, respond_as_model=""))
        st = make_state(normalized_text="tomates", expected_input="PRODUCT", user_role="PRODUCER")
        r = run(interp(st, rt))
        assert r["raw_analysis"]["degraded_model"] is True


# =====================================================================
# PLUSIEURS PRODUITS DANS LE MÊME MESSAGE — jamais fusionnés en une seule
# chaîne de recherche (incident réel 2026-08-14, voir
# [[buyer-search-fuzzy-match-safety-2026-08]] : "laitue, oeufs" fusionné en
# un seul terme a fuzzy-matché "Bœuf" au lieu des œufs demandés).
# =====================================================================

class TestMultipleProductsAreNeverMerged:
    def test_additional_products_from_the_llm_survive_extraction(self):
        interp = make_input_interpreter("BUYER")
        rt = StubRuntime(llm=ScriptedLLM({
            "disposition": "NEW_TASK",
            "intent": "BUYER_REQUEST",
            "confidence": 0.9,
            "entities": {
                "product": "œufs",
                "additional_products": ["laitue"],
            },
        }))
        st = make_state(
            normalized_text="je cherche des œufs et de la laitue",
            expected_input="NONE", user_role="BUYER",
        )
        r = run(interp(st, rt))
        assert r["extracted_entities"]["product"] == "œufs"
        assert r["extracted_entities"]["additional_products"] == ["laitue"]

    def test_a_single_product_yields_no_additional_products_key(self):
        interp = make_input_interpreter("BUYER")
        rt = StubRuntime(llm=ScriptedLLM({
            "disposition": "NEW_TASK",
            "intent": "BUYER_REQUEST",
            "confidence": 0.9,
            "entities": {"product": "tomates", "additional_products": []},
        }))
        st = make_state(normalized_text="je cherche des tomates", expected_input="NONE", user_role="BUYER")
        r = run(interp(st, rt))
        assert r["extracted_entities"].get("additional_products") in (None, [])
