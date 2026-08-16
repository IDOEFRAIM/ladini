"""Nœuds du graphe : validation, mémoire, désambiguïsation, nettoyage.

Chaque test encode une règle d'architecture ou un bug de production réel.
"""
from __future__ import annotations

import pytest

from agriconnect.agents.reducers import merge_dict
from agriconnect.graphs.agents.market_coach.nodes.validation import validator
from agriconnect.graphs.agents.market_coach.nodes.memory import memory_update
from agriconnect.graphs.agents.market_coach.nodes.cleanup import post_response_cleanup
from agriconnect.graphs.agents.market_coach.nodes.semantic_disambiguation import (
    semantic_disambiguation,
)
from agriconnect.graphs.agents.market_coach.flows.buyer.helpers import (
    clear_active_goal,
    detect_cart_action,
)
from tests.conftest import StubRuntime, make_state, run


# =====================================================================
# VALIDATOR — jamais réclamer un identifiant technique
# =====================================================================

class TestValidatorNeverAsksForIds:
    """Bug : l'agent affichait « Étape 1/1 : identifiant enchère » avec
    expected_input=NONE — l'utilisateur ne peut pas connaître un UUID et sa
    réponse n'était rattachable à aucun champ : impasse conversationnelle."""

    @pytest.mark.parametrize("goal", [
        "STOCK_GET_MOVEMENTS", "STOCK_UPDATE_LEVEL",
        "CROP_RECORD_INTERVENTION", "CROP_UPDATE_STAGE",
        "AGRO_GET_ECONOMICS", "SYSTEM_REPORT_ANOMALY",
    ])
    def test_missing_technical_id_yields_clarification_not_uuid_prompt(self, goal):
        st = make_state(current_goal=goal)
        r = run(validator(st, StubRuntime()))
        assert r.get("last_missing_field") is None, f"{goal} réclame encore un identifiant"
        assert r.get("response_strategy") == "CLARIFICATION"
        assert r.get("status") == "COMPLETED", "le tour doit se terminer proprement"

    @pytest.mark.parametrize("goal", ["MARKET_GET_REQUEST_DETAIL", "PROCUREMENT_SELECT_WINNER"])
    def test_goals_with_resolver_pass_through_to_it(self, goal):
        """Ces goals ONT un résolveur (menu de sélection) : le validateur ne
        doit PAS bloquer avant de l'atteindre."""
        st = make_state(current_goal=goal)
        r = run(validator(st, StubRuntime()))
        assert r.get("status") == "PLANNING"
        assert r.get("last_missing_field") is None

    def test_ordinary_business_slot_is_still_requested(self):
        """Non-régression : un vrai champ métier reste demandé normalement."""
        st = make_state(current_goal="SALES_PUBLISH_PRODUCT")
        r = run(validator(st, StubRuntime()))
        assert r["response_strategy"] == "ASK_MISSING_FIELD"
        assert r["last_missing_field"] == "product"
        assert r["expected_input"] == "PRODUCT"

    def test_invalid_price_is_rejected_by_contract(self):
        st = make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            transaction_payload={"product": "maïs", "quantity": 10, "price": -5},
        )
        r = run(validator(st, StubRuntime()))
        assert r["response_strategy"] == "ASK_MISSING_FIELD"
        assert any("prix" in e.lower() for e in r.get("validation_errors") or [])


# =====================================================================
# MEMORY — enrichissement et gardes anti-hallucination
# =====================================================================

class TestMemoryUpdate:
    def test_livestock_gets_head_unit_and_type(self):
        """« 200 poussins » ne doit JAMAIS devenir 200 KG."""
        st = make_state(
            interpreted_event="NEW_TASK",
            current_goal="DECLARE_CROP_CYCLE",
            detected_intent="DECLARE_CROP_CYCLE",
            normalized_text="je vends 200 poussins",
            extracted_entities={"product": "poussins", "quantity": 200.0},
        )
        p = run(memory_update(st, StubRuntime()))["transaction_payload"]
        assert p["unit"] == "TETE"
        assert p["production_type"] == "LIVESTOCK"

    def test_compound_answer_keeps_both_quantity_and_price(self):
        """Le garde anti-hallucination ne doit PAS jeter le prix extrait
        de façon déterministe pendant la collecte de la quantité."""
        st = make_state(
            interpreted_event="ANSWER",
            expected_input="QUANTITY",
            current_goal="SALES_PUBLISH_PRODUCT",
            detected_intent="SALES_PUBLISH_PRODUCT",
            normalized_text="j ai 775 kg d oignon et le kg coute 175 fcfa",
            transaction_payload={"product": "Oignons"},
            extracted_entities={"quantity": 775.0, "unit": "KG", "price": 175.0, "price_unit": "KG"},
            raw_analysis={"path": "fast_path_slot_numeric_compound_answer"},
        )
        p = run(memory_update(st, StubRuntime()))["transaction_payload"]
        assert p["quantity"] == 775.0 and p["price"] == 175.0

    def test_off_topic_field_is_dropped_when_the_degraded_model_answered(self):
        """Le LLM de repli dégradé (Groq 429 → llama-3.1-8b, voir
        `interpreter/routing.py::_degraded_model_used`) hallucine tout le
        schéma : pendant la collecte d'un slot précis, ses champs hors-sujet
        sont ignorés. Ce garde ne mord QUE quand `raw_analysis.degraded_model`
        est vrai — voir la classe `TestPrimaryModelMultiSlotFilling`
        ci-dessous pour le chemin normal (modèle principal), qui ne doit
        JAMAIS être amputé de la même façon."""
        st = make_state(
            interpreted_event="ANSWER",
            expected_input="DATE",
            current_goal="DECLARE_CROP_CYCLE",
            detected_intent="DECLARE_CROP_CYCLE",
            normalized_text="d ici le 21 decembre",
            transaction_payload={"product": "boeufs", "quantity": 78, "price": 425000},
            extracted_entities={"product": "poussins allemand", "quantity": 5900, "price": 2400},
            raw_analysis={"path": "llm", "degraded_model": True, "model_used": "llama-3.1-8b-instant"},
        )
        p = run(memory_update(st, StubRuntime()))["transaction_payload"]
        assert p["product"] == "boeufs", "un champ hors-sujet a écrasé le produit validé"
        assert p["quantity"] == 78


# =====================================================================
# REMPLISSAGE MULTI-SLOTS — le modèle PRINCIPAL ne doit jamais être bridé
# =====================================================================

class TestPrimaryModelMultiSlotFilling:
    """Régression production (2026-08) : le garde anti-hallucination
    s'appliquait à TOUTE réponse ANSWER/UPDATE, quel que soit le modèle Groq
    ayant répondu — pas seulement le repli dégradé pour lequel il avait été
    conçu (voir les 2 incidents documentés dans `nodes/memory.py`). Un
    utilisateur qui répondait au tout premier slot (PRODUCT — jamais couvert
    par le carve-out `fast_path_slot_numeric_compound_answer`, qui ne
    reconnaît que quantité+prix) avec plusieurs infos à la fois se faisait
    amputer de tout sauf `product`, forçant l'agent à redemander la
    quantité/le prix au tour suivant — l'agent restait "linéaire" malgré une
    extraction LLM correcte en amont."""

    def test_primary_model_multi_field_answer_to_the_first_slot_is_preserved(self):
        st = make_state(
            interpreted_event="ANSWER",
            expected_input="PRODUCT",
            current_goal="SALES_PUBLISH_PRODUCT",
            detected_intent="SALES_PUBLISH_PRODUCT",
            normalized_text="tomates, 500kg a 200fcfa/kg",
            transaction_payload={},
            extracted_entities={"product": "tomates", "quantity": 500, "unit": "KG", "price": 200, "price_unit": "KG"},
            raw_analysis={"path": "llm", "degraded_model": False, "model_used": "llama-3.3-70b-versatile"},
        )
        p = run(memory_update(st, StubRuntime()))["transaction_payload"]
        assert p["product"] == "tomates"
        assert p["quantity"] == 500
        assert p["price"] == 200

    def test_missing_degraded_model_flag_means_a_non_llm_path_so_no_narrowing(self):
        """`degraded_model` n'est posé QUE par le chemin LLM complet
        (`interpreter/routing.py`, `raw_analysis.path == "llm"`) — les
        chemins déterministes (fast-path, repli sans LLM, bypass interactif)
        n'ont pas ce risque d'hallucination et n'ont donc pas besoin du
        garde : son absence signifie "pas de risque connu", pas "modèle
        principal supposé". Ici, un chemin sans LLM (`path == "no_llm"`) ne
        doit pas être bridé."""
        st = make_state(
            interpreted_event="ANSWER",
            expected_input="DATE",
            current_goal="DECLARE_CROP_CYCLE",
            detected_intent="DECLARE_CROP_CYCLE",
            normalized_text="d ici le 21 decembre, 78 boeufs",
            transaction_payload={"product": "boeufs"},
            extracted_entities={"estimated_available_at": "2026-12-21", "quantity": 78},
            raw_analysis={"path": "no_llm"},  # pas de clé degraded_model
        )
        p = run(memory_update(st, StubRuntime()))["transaction_payload"]
        assert p["quantity"] == 78, "un champ légitime issu d'un chemin déterministe ne doit pas être bridé"

    def test_locked_off_topic_field_is_still_dropped_from_the_primary_model_too(self):
        """Le fix élargit ce que le modèle PRINCIPAL peut remplir en une
        fois ; il ne supprime pas la protection pour autant quand le flag
        `degraded_model` est explicitement vrai, même sur un slot autre que
        PRICE/QUANTITY (ex: DATE, comme dans l'incident réel)."""
        st = make_state(
            interpreted_event="UPDATE",
            expected_input="DATE",
            current_goal="DECLARE_CROP_CYCLE",
            detected_intent="DECLARE_CROP_CYCLE",
            normalized_text="l unite coute plutot 36500",
            transaction_payload={"product": "chevres", "quantity": 14},
            extracted_entities={"product": "poussins allemands", "price": 36500},
            raw_analysis={"path": "llm", "degraded_model": True},
        )
        p = run(memory_update(st, StubRuntime()))["transaction_payload"]
        assert p["product"] == "chevres", "correction de PRIX pendant DATE : le produit ne doit pas être écrasé"


# =====================================================================
# DÉSAMBIGUÏSATION — le LLM décide en premier
# =====================================================================

class TestDisambiguation:
    """Bug : le garde de confiance était inopérant (`conf >= s AND
    len(candidats) < 2`, or toute entrée a >= 2 candidats) — des indices
    lexicaux figés détournaient TOUJOURS une classification LLM sûre."""

    def _run(self, text, intent, conf):
        st = make_state(
            interpreted_event="NEW_TASK", interpreter_confidence=conf,
            expected_input="NONE", normalized_text=text,
            detected_intent=intent, user_role="PRODUCER",
        )
        return run(semantic_disambiguation(st, None))

    @pytest.mark.parametrize("text,intent", [
        ("j ai 200 kg de tomates a vendre", "SALES_PUBLISH_PRODUCT"),
        ("ou est ma commande", "BUYER_LIST_ORDERS"),
    ])
    def test_confident_llm_is_not_overridden_by_menu(self, text, intent):
        r = self._run(text, intent, 0.90)
        assert r == {} or r.get("current_goal") != "DISAMBIGUATION_PENDING"

    def test_uncertain_llm_still_gets_a_menu(self):
        r = self._run("j ai 200 kg de tomates", "SALES_PUBLISH_PRODUCT", 0.55)
        assert r.get("current_goal") == "DISAMBIGUATION_PENDING"

    def test_unknown_intent_still_gets_a_menu(self):
        r = self._run("j ai des trucs", "UNKNOWN", 0.30)
        assert r.get("current_goal") == "DISAMBIGUATION_PENDING"


# =====================================================================
# ÉTAT INTER-TOUR — merge_dict : effacer = None, JAMAIS pop
# =====================================================================

class TestMergeDictClearing:
    """`working_memory`/`transaction_payload` sont réduits par `merge_dict` :
    retirer une clé du patch ne la supprime PAS de l'état."""

    def test_cart_snapshot_is_really_cleared(self):
        """Bug : un panier abandonné pouvait RESSUSCITER (clé relue par preorder)."""
        old = {"active_goal": "X", "last_active_cart": [{"p": 1}], "autre": "garde"}
        patch = clear_active_goal({"working_memory": old}, clear_cart_snapshot=True)
        merged = merge_dict(old, patch)
        assert merged.get("last_active_cart") is None
        assert merged.get("autre") == "garde", "les clés voisines doivent survivre"

    def test_clearing_via_pop_would_not_work(self):
        """Documente POURQUOI on n'utilise jamais pop() (garde-fou pédagogique)."""
        old = {"k": "valeur"}
        patch = dict(old)
        patch.pop("k", None)
        assert merge_dict(old, patch).get("k") == "valeur", (
            "merge_dict conserve l'ancienne valeur : pop est inopérant"
        )


# =====================================================================
# CLEANUP — préservation ciblée des caches de menu
# =====================================================================

class TestPostResponseCleanup:
    def test_stale_menu_cache_from_other_goal_is_dropped(self):
        st = make_state(
            status="WAITING_INPUT", expected_input="SELECTION",
            current_goal="BUYER_PREORDER_INIT",
            working_memory={"bids_menu": "PERIME", "stocks_menu": "PERIME",
                            "available_mapping_kind": "preorder_action"},
        )
        wm = run(post_response_cleanup(st, None))["working_memory"]
        assert wm["bids_menu"] is None and wm["stocks_menu"] is None

    def test_menu_cache_of_the_owning_goal_survives(self):
        st = make_state(
            status="WAITING_INPUT", expected_input="SELECTION",
            current_goal="SALES_ACCEPT_CONTRACT",
            working_memory={"bids_menu": "menu courant", "stocks_menu": "PERIME"},
        )
        wm = run(post_response_cleanup(st, None))["working_memory"]
        assert wm["bids_menu"] == "menu courant"
        assert wm["stocks_menu"] is None


class TestPostResponseCleanupPreservesCurrentGoal:
    """Bug réel, confirmé par logs serveur (2026-08-15) : ce garde-fou ne
    couvrait QUE les canaux SELECTION et CONFIRMATION — pas le cas, bien
    plus courant, d'un formulaire générique en attente d'UN CHAMP précis
    (ASK_MISSING_FIELD : expected_input="PRICE"/"QUANTITY"/"DEADLINE"/etc.,
    status="WAITING_INPUT"). `current_goal` était effacé après CHAQUE tour
    d'appel d'offres/vente/déclaration de culture — le tour suivant
    démarrait avec `current_goal=None` en pleine opération, empêchant
    goal_planner de jamais la faire progresser jusqu'à l'exécution (boucle
    infinie entre les mêmes questions). Voir
    [[precommande-architecture-consolidation-2026-08]]."""

    def test_a_field_specific_wait_now_preserves_current_goal(self):
        st = make_state(
            status="WAITING_INPUT", expected_input="PRICE",
            current_goal="PROCUREMENT_CREATE_REQUEST",
        )
        result = run(post_response_cleanup(st, None))
        assert "current_goal" not in result, "ne doit pas être touché — préservé"

    @pytest.mark.parametrize("expected_input", ["PRICE", "QUANTITY", "DEADLINE", "UNIT", "PRODUCT"])
    def test_preserved_for_every_generic_field_type(self, expected_input):
        st = make_state(
            status="WAITING_INPUT", expected_input=expected_input,
            current_goal="SALES_PUBLISH_PRODUCT",
        )
        result = run(post_response_cleanup(st, None))
        assert "current_goal" not in result

    def test_selection_and_confirmation_channels_still_work_as_before(self):
        """Non-régression : les deux canaux déjà couverts avant ce fix."""
        st_selection = make_state(
            status="WAITING_INPUT", expected_input="SELECTION", current_goal="BUYER_LIST_AUCTIONS",
        )
        st_confirmation = make_state(
            status="WAITING_CONFIRMATION", expected_input="CONFIRMATION", current_goal="SALES_PUBLISH_PRODUCT",
        )
        assert "current_goal" not in run(post_response_cleanup(st_selection, None))
        assert "current_goal" not in run(post_response_cleanup(st_confirmation, None))

    def test_a_genuinely_completed_operation_still_clears_current_goal(self):
        """Non-régression : une opération VRAIMENT terminée (rien en
        attente) doit toujours repartir de zéro au tour suivant."""
        st = make_state(status="COMPLETED", expected_input="NONE", current_goal="SALES_PUBLISH_PRODUCT")
        result = run(post_response_cleanup(st, None))
        assert result["current_goal"] is None

    def test_no_expected_input_still_clears_current_goal_even_if_waiting_input(self):
        """Un status WAITING_INPUT sans expected_input concret (NONE) reste
        un canal générique, pas un formulaire actif — ne doit pas préserver
        le goal indéfiniment."""
        st = make_state(status="WAITING_INPUT", expected_input="NONE", current_goal="SALES_PUBLISH_PRODUCT")
        result = run(post_response_cleanup(st, None))
        assert result["current_goal"] is None


# =====================================================================
# ACTION PANIER — pilotée par le verdict LLM, pas par mots-clés
# =====================================================================

class TestCartActionUsesLlmVerdict:
    """Bug : scan par sous-chaîne — « je ne veux pas annuler » déclenchait
    ANNULER (la négation était ignorée)."""

    BASE = {"expected_input": "SELECTION", "active_cart": [{"x": 1}],
            "working_memory": {"available_mapping_kind": "cart"}}

    @pytest.mark.parametrize("event,expected", [
        ("CONFIRM", "PREORDER"),
        ("REJECT", "CANCEL"),
        ("ANSWER", None),
        ("SELECTION", None),
    ])
    def test_action_follows_interpreted_event(self, event, expected):
        assert detect_cart_action({**self.BASE, "interpreted_event": event}) == expected
