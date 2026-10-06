"""Nœuds du graphe : validation, mémoire, désambiguïsation, nettoyage.

Chaque test encode une règle d'architecture ou un bug de production réel.
"""
from __future__ import annotations

import pytest

from ladini.agents.reducers import merge_dict
from ladini.graphs.agents.market_coach.core.pending_interaction import (
    get_pending_interaction,
    to_tunnel_category,
)
from ladini.graphs.agents.market_coach.interpreter.intent import INTENT_CONFIG
from ladini.graphs.agents.market_coach.nodes.validation import validator
from ladini.graphs.agents.market_coach.nodes.memory import memory_update
from ladini.graphs.agents.market_coach.nodes.cleanup import post_response_cleanup
from ladini.graphs.agents.market_coach.nodes.cognitive import cognitive_guard
from ladini.graphs.agents.market_coach.flows.buyer.helpers import (
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

    def test_missing_technical_id_yields_clarification_not_uuid_prompt(self, monkeypatch):
        """(2026-09-13, Deep Intent Architecture Cleanup) : les intents
        historiquement utilisés ici (STOCK_GET_MOVEMENTS/STOCK_UPDATE_LEVEL/
        CROP_RECORD_INTERVENTION/CROP_UPDATE_STAGE/AGRO_GET_ECONOMICS/
        SYSTEM_REPORT_ANOMALY) ont tous été supprimés d'INTENT_CONFIG — et par
        construction, plus aucun intent classifiable ne peut aujourd'hui
        déclarer un `*_id` requis sans résolveur (voir
        tests/architecture/test_no_broken_user_goals.py::
        test_technical_id_has_a_resolver_passthrough). On isole donc ce test
        du catalogue métier avec un goal synthétique qui reproduit exactement
        la situation visée (id technique requis, aucun résolveur) plutôt que
        de dépendre d'un intent réel qui ne devrait plus jamais exister."""
        goal = "TEST_SYNTHETIC_ORPHAN_ID_GOAL"
        monkeypatch.setitem(
            INTENT_CONFIG,
            goal,
            {"tool_name": "noop", "required": ["widget_id"], "action_type": "READ"},
        )
        st = make_state(current_goal=goal)
        r = run(validator(st, StubRuntime()))
        assert r.get("last_missing_field") is None, f"{goal} réclame encore un identifiant"
        assert r.get("response_strategy") == "CLARIFICATION"
        assert r.get("status") == "COMPLETED", "le tour doit se terminer proprement"

    @pytest.mark.parametrize("goal", [
        "MARKET_GET_REQUEST_DETAIL", "PROCUREMENT_SELECT_WINNER",
        # (2026-09-04, Product Completeness Phase 2) : mêmes impasses,
        # réintroduites par des chantiers récents. `PRODUCER_CONFIRM_DELIVERY_PAYMENT`
        # (F1) et `SALES_UNPUBLISH_PRODUCT` déclarent un `required` d'UUID
        # (`order_id`/`product_id`) résolu par un résolveur dédié
        # (`_resolve_order_for_delivery_payment`/`_resolve_product_for_unpublish`) —
        # sans l'entrée `_RESOLVER_PASSTHROUGH`, `missing_fields` non vide
        # faisait retomber le routeur sur `to_strategy` et le résolveur
        # n'était JAMAIS atteint : le producteur se voyait réclamer un UUID.
        "PRODUCER_CONFIRM_DELIVERY_PAYMENT", "SALES_UNPUBLISH_PRODUCT",
    ])
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
        assert to_tunnel_category(get_pending_interaction(r)) == "PRODUCT"

    def test_invalid_price_is_rejected_by_contract(self):
        st = make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            transaction_payload={"product": "maïs", "quantity": 10, "price": -5},
        )
        r = run(validator(st, StubRuntime()))
        assert r["response_strategy"] == "ASK_MISSING_FIELD"
        assert any("prix" in e.lower() for e in r.get("validation_errors") or [])


class TestUnitDefaultIsFlaggedNotSilent:
    """Incident réel (2026-09-15) : « Vente de 25 LITRE de boeufs » — l'unité
    était comblée silencieusement (`slot_enrichment.py`, via l'autorité
    unique `resolve_product_unit`), sans JAMAIS distinguer un défaut deviné
    d'une unité que le producteur a réellement écrite. `unit_was_assumed`
    doit apparaître dans `transaction_payload` chaque fois que ce défaut
    s'applique, pour que le récapitulatif
    (`services/ui/confirmation_summary.py`) puisse avertir explicitement au
    lieu de laisser passer une supposition invisible — voir aussi
    `tests/services/test_services_and_tools.py::TestSlotEnrichment` pour le
    test au niveau de la fonction d'enrichissement elle-même."""

    def test_livestock_with_no_stated_unit_is_flagged_as_assumed(self):
        st = make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            normalized_text="je veux vendre mes 25 boeufs",
            transaction_payload={"product": "boeufs", "quantity": 25, "price": 425000},
        )
        r = run(memory_update(st, StubRuntime()))
        payload = r["transaction_payload"]
        assert payload["unit"] == "TETE"
        assert payload.get("unit_was_assumed") is True

    def test_crop_with_no_stated_unit_is_also_flagged_not_just_livestock(self):
        """Le défaut aveugle « KG » pour une culture est le MÊME genre de
        supposition non prouvée que celui déjà corrigé pour l'élevage
        (incident 2026-09-08) — il doit être signalé de la même façon,
        plutôt que de descendre jusqu'à `create_product` (`unit=None` ->
        "KG" silencieux, hors de portée de tout récapitulatif)."""
        st = make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            normalized_text="j en ai 23",
            transaction_payload={"product": "haricot", "quantity": 23, "price": 500},
        )
        r = run(memory_update(st, StubRuntime()))
        payload = r["transaction_payload"]
        assert payload["unit"] == "KG"
        assert payload.get("unit_was_assumed") is True

    def test_a_stated_unit_is_never_flagged(self):
        st = make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            normalized_text="je vends 25 tetes de boeufs a 425000",
            transaction_payload={
                "product": "boeufs", "quantity": 25, "unit": "TETE", "price": 425000,
            },
        )
        r = run(memory_update(st, StubRuntime()))
        payload = r["transaction_payload"]
        assert "unit_was_assumed" not in payload


class TestExplicitUnitClearsTheAssumedFlag:
    """Incident 2026-09-19 : « je propose maximum 375 fcfa par kg et je veux
    65 kg » — l'acheteur écrit « kg » deux fois, mais le récap disait « Unité
    non précisée par vous : KG supposée ». Un tour précédent avait posé
    KG par défaut (`unit_was_assumed`) ; l'unité explicite, IDENTIQUE, était
    ignorée par `_apply_slot` (valeur égale → retour) donc le drapeau restait."""

    _TEXT = "je propose maximum 375 fcfa par kg et je veux 65 kg"

    def _turn(self, text, entities):
        st = make_state(
            current_goal="BUYER_REQUEST",
            normalized_text=text,
            interpreted_event="UPDATE",
            extracted_entities=entities,
            transaction_payload={
                "product": "poivrons", "unit": "KG", "unit_was_assumed": True,
            },
        )
        return run(memory_update(st, StubRuntime()))["transaction_payload"]

    def test_llm_extracted_unit_equal_to_the_assumed_one_clears_the_flag(self):
        payload = self._turn(self._TEXT, {"unit": "KG", "quantity": 65, "price": 375})
        assert "unit_was_assumed" not in payload

    def test_unit_written_in_the_text_clears_the_flag_even_without_llm_unit(self):
        payload = self._turn(self._TEXT, {"quantity": 65, "price": 375})
        assert "unit_was_assumed" not in payload

    def test_a_text_without_any_unit_keeps_the_flag(self):
        payload = self._turn("je veux du poivron pas cher", {"unit": "KG"})
        assert payload.get("unit_was_assumed") is True


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

    def test_pricing_tiers_survives_the_degraded_model_guard_on_price_slot(self):
        """Incident réel (2026-08-29) : un producteur répondant à la
        question PRIX avec plusieurs tarifs ("25 L à 500 FCFA et 40 L à
        900 FCFA de lait") pendant un repli modèle dégradé (fréquent, voir
        GROQ_RATE_LIMIT_FALLBACK) se faisait amputer de `pricing_tiers` —
        absent de l'ancien allowlist `{"price", "price_unit"}` du slot PRICE
        — la confirmation retombait sur un gabarit à tarif unique avec des
        valeurs incohérentes (quantité/prix d'un tour précédent)."""
        st = make_state(
            interpreted_event="ANSWER",
            expected_input="PRICE",
            current_goal="SALES_PUBLISH_PRODUCT",
            detected_intent="SALES_PUBLISH_PRODUCT",
            normalized_text="je vends les bidon de 25 L a 500 fcfa et le bidon de 40 l a 900 fcfa",
            transaction_payload={"product": "lait", "quantity": 250, "unit": "KG"},
            extracted_entities={
                "price": 25.0,
                "pricing_tiers": [
                    {"quantity": 25, "unit": "L", "price": 500, "packaging": "bidon"},
                    {"quantity": 40, "unit": "L", "price": 900, "packaging": "bidon"},
                ],
            },
            raw_analysis={"path": "llm", "degraded_model": True, "model_used": "llama-3.1-8b-instant"},
        )
        p = run(memory_update(st, StubRuntime()))["transaction_payload"]
        assert p.get("pricing_tiers") == [
            {"quantity": 25, "unit": "L", "price": 500, "packaging": "bidon"},
            {"quantity": 40, "unit": "L", "price": 900, "packaging": "bidon"},
        ], "pricing_tiers a été jeté par le garde anti-hallucination du slot PRICE"

    def test_pricing_tiers_survives_the_degraded_model_guard_on_quantity_slot(self):
        st = make_state(
            interpreted_event="ANSWER",
            expected_input="QUANTITY",
            current_goal="SALES_PUBLISH_PRODUCT",
            detected_intent="SALES_PUBLISH_PRODUCT",
            normalized_text="j'ai des bidons de 25 L et 40 L",
            transaction_payload={"product": "lait"},
            extracted_entities={
                "pricing_tiers": [
                    {"quantity": 25, "unit": "L", "price": None, "packaging": "bidon"},
                    {"quantity": 40, "unit": "L", "price": None, "packaging": "bidon"},
                ],
            },
            raw_analysis={"path": "llm", "degraded_model": True},
        )
        p = run(memory_update(st, StubRuntime()))["transaction_payload"]
        assert p.get("pricing_tiers"), "pricing_tiers a été jeté par le garde anti-hallucination du slot QUANTITY"

    def test_a_correction_during_confirmation_refreshes_the_displayed_quantity(self):
        """Bug réel (2026-09-03, incident PROCUREMENT_CREATE_REQUEST) : après
        un 1er tour établissant "2 tonnes" (quantity_display="2 TONNE" figé
        par `_normalize_quantity_to_kg`), une VRAIE correction ("non, plutôt
        1 tonne et 125 kg") mettait bien à jour `quantity` (contrat
        d'exécution) mais JAMAIS `quantity_display`/`unit_display` — le
        récapitulatif affiché à l'utilisateur restait figé sur "2 TONNE" tour
        après tour, quel que soit le nombre de corrections. Voir
        `services/ui/confirmation_summary.py::_format_quantity`, qui préfère
        `quantity_display` à `quantity`."""
        # Tour 1 : établit 2 tonnes (= 2000 KG), quantity_display="2"/TONNE.
        st1 = make_state(
            interpreted_event="UPDATE",
            expected_input="CONFIRMATION",
            current_goal="PROCUREMENT_CREATE_REQUEST",
            detected_intent="PROCUREMENT_CREATE_REQUEST",
            normalized_text="2 tonnes",
            transaction_payload={"product": "tomates", "price": 250.0},
            extracted_entities={"quantity": 2.0, "unit": "TONNE"},
        )
        payload1 = run(memory_update(st1, StubRuntime()))["transaction_payload"]
        assert payload1["quantity"] == 2000.0
        assert payload1["unit_display"] == "TONNE"
        assert payload1["quantity_display"] == 2.0

        # Tour 2 : correction explicite vers 1 tonne (= 1000 KG) — DOIT
        # rafraîchir l'affichage, pas seulement le contrat d'exécution.
        st2 = make_state(
            interpreted_event="UPDATE",
            expected_input="CONFIRMATION",
            current_goal="PROCUREMENT_CREATE_REQUEST",
            detected_intent="PROCUREMENT_CREATE_REQUEST",
            normalized_text="non, plutot 1 tonne",
            transaction_payload=payload1,
            extracted_entities={"quantity": 1.0, "unit": "TONNE"},
        )
        payload2 = run(memory_update(st2, StubRuntime()))["transaction_payload"]
        assert payload2["quantity"] == 1000.0
        assert payload2["unit_display"] == "TONNE"
        assert payload2["quantity_display"] == 1.0, (
            "quantity_display est resté figé sur l'ancienne valeur (2) — "
            "le récapitulatif affichera l'ancienne quantité malgré la "
            "correction"
        )

        # Tour 3 : un tour qui NE touche PAS la quantité (juste la date) ne
        # doit PAS, lui, faire dériver l'affichage — non-régression du
        # comportement `setdefault` historique.
        st3 = make_state(
            interpreted_event="UPDATE",
            expected_input="CONFIRMATION",
            current_goal="PROCUREMENT_CREATE_REQUEST",
            detected_intent="PROCUREMENT_CREATE_REQUEST",
            normalized_text="la date limite c'est le 30 septembre",
            transaction_payload=payload2,
            extracted_entities={"estimated_available_at": "2026-09-30T00:00:00+00:00"},
        )
        payload3 = run(memory_update(st3, StubRuntime()))["transaction_payload"]
        assert payload3["quantity_display"] == 1.0
        assert payload3["unit_display"] == "TONNE"


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
    lexicaux figés détournaient TOUJOURS une classification LLM sûre.

    (2026-09-08, correction topologique du bloc conversationnel) : cette
    décision (« faut-il désambiguïser ? ») a été DÉPLACÉE de
    `semantic_disambiguation` (devenu un pur exécuteur, voir
    `tests/nodes/test_semantic_disambiguation.py`) vers `cognitive_guard`,
    son nouveau et unique propriétaire (`nodes/cognitive.py::
    _classify_nominal_action`). Ces 3 scénarios — préservés à l'identique,
    seule la cible de `_run` a changé — vérifient maintenant
    `cognitive_decision.action` plutôt qu'un menu déjà construit."""

    def _run(self, text, intent, conf):
        st = make_state(
            interpreted_event="NEW_TASK", interpreter_confidence=conf,
            expected_input="NONE", normalized_text=text,
            detected_intent=intent, user_role="PRODUCER",
        )
        return run(cognitive_guard(st, None))

    @pytest.mark.parametrize("text,intent", [
        ("j ai 200 kg de tomates a vendre", "SALES_PUBLISH_PRODUCT"),
        ("ou est ma commande", "BUYER_LIST_ORDERS"),
    ])
    def test_confident_llm_is_not_overridden_by_menu(self, text, intent):
        r = self._run(text, intent, 0.90)
        assert r["cognitive_decision"]["action"] != "DISAMBIGUATE"

    def test_uncertain_llm_still_gets_a_menu(self):
        r = self._run("j ai 200 kg de tomates", "SALES_PUBLISH_PRODUCT", 0.55)
        assert r["cognitive_decision"]["action"] == "DISAMBIGUATE"

    def test_unknown_intent_still_gets_a_menu(self):
        r = self._run("j ai des trucs", "UNKNOWN", 0.30)
        assert r["cognitive_decision"]["action"] == "DISAMBIGUATE"


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

    def test_menu_cache_of_the_owning_goal_survives(self, monkeypatch):
        """(2026-09-13, Deep Intent Architecture Cleanup) : `SALES_ACCEPT_CONTRACT`
        et son ownership de `bids_menu` ont été supprimés — plus aucun goal
        classifiable ne possède aujourd'hui une entrée dans
        `_MENU_CACHE_OWNERS` (les seuls écrivains de `bids_menu`/`stocks_menu`,
        `resolve_received_bids`/`_resolve_stock`, sont morts avec les intents
        qui les déclenchaient). On teste donc le mécanisme générique
        (préservation d'un cache pour son propriétaire déclaré) avec un
        propriétaire synthétique plutôt que de dépendre d'un intent réel."""
        import ladini.graphs.agents.market_coach.nodes.cleanup as cleanup_mod

        monkeypatch.setitem(
            cleanup_mod._MENU_CACHE_OWNERS, "bids_menu", frozenset({"SYNTHETIC_GOAL"})
        )
        st = make_state(
            status="WAITING_INPUT", expected_input="SELECTION",
            current_goal="SYNTHETIC_GOAL",
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
