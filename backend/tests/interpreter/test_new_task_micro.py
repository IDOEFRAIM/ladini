"""Chantier "State Router + micro-prompts" — Incrément F (2026-09-13) :
micro-prompt NEW_TASK (`new_task_v2`), dernier gros chantier de réduction du
prompt d'interprétation.

Couvre : audit-driven design (contrat spécialisé, anti-ID structurel, aucun
filtrage par rôle — spec §2/§7/§8/§11), extraction (produit/multi-produits,
quantité/unité sans hallucination, prix vs quantité, dates dynamiques —
spec §12/§13/§14/§16), UNKNOWN valide (spec §23), garde de taille de prompt
(spec §50), paires contrastives d'intentions proches (spec §41/42), et le
dispatch réel par route (spec §61 : NEW_TASK/DEVIATION reclassification
utilisent ce micro-prompt, les 3 autres routes ne l'utilisent jamais)."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ladini.graphs.agents.market_coach.interpreter.intent import INTENT_CONFIG
from ladini.graphs.agents.market_coach.interpreter.new_task_contract import (
    NewTaskInterpretation,
    NewTaskPromptContext,
)
from ladini.graphs.agents.market_coach.interpreter.new_task_micro import (
    NewTaskOutcome,
    run_new_task_microprompt,
)
from ladini.graphs.agents.market_coach.interpreter.new_task_prompts import (
    build_new_task_system_prompt,
    build_new_task_user_prompt,
)
from ladini.graphs.agents.market_coach.interpreter.routing import (
    _classifiable_intents,
    make_input_interpreter,
)
from tests.conftest import ScriptedLLM, StubRuntime, make_state, run


class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Msg(content)


class _Completion:
    def __init__(self, content: str, model: Optional[str] = None) -> None:
        self.choices = [_Choice(content)]
        self.model = model


class _CapturingGateway:
    def __init__(self, payloads: List[Any]) -> None:
        self._payloads = list(payloads)
        self.calls: List[Dict[str, Any]] = []

    def primary_model_name(self, profile: Any) -> str:
        return "test-model"

    async def complete(self, **kwargs: Any):
        self.calls.append(kwargs)
        idx = min(len(self.calls) - 1, len(self._payloads) - 1)
        payload = self._payloads[idx]
        content = payload if isinstance(payload, str) else json.dumps(payload)
        return _Completion(content, model="test-model")


def _runtime(*payloads: Any) -> tuple:
    gateway = _CapturingGateway(list(payloads))
    rt = type("RT", (), {"llm": object(), "llm_gateway": gateway})()
    return rt, gateway


CATALOG = {
    key: INTENT_CONFIG[key].get("label", key)
    for key in INTENT_CONFIG
    if key in _classifiable_intents()
}


def _ctx(**overrides: Any) -> NewTaskPromptContext:
    base = dict(reference_date="2026-09-13")
    base.update(overrides)
    return NewTaskPromptContext(**base)


# =====================================================================
# A. CONTRAT SPÉCIALISÉ / ANTI-ID (spec §8/§11)
# =====================================================================


class TestSpecializedContractHasNoTechnicalIds:
    def test_contract_has_no_agent_action_or_ids(self):
        fields = set(NewTaskInterpretation.model_fields.keys())
        assert fields == {
            "disposition", "intent", "confidence", "entities", "candidate_goals",
        }

    def test_entities_reject_a_smuggled_technical_id(self):
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            NewTaskInterpretation.model_validate({
                "disposition": "NEW_TASK",
                "intent": "SALES_PUBLISH_PRODUCT",
                "confidence": 0.9,
                "entities": {"product": "mais", "producer_id": "P1"},
            })

    def test_a_new_task_without_intent_is_rejected(self):
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            NewTaskInterpretation.model_validate({"disposition": "NEW_TASK", "confidence": 0.9})


# =====================================================================
# B. RÔLE-AGNOSTIQUE (spec §2/§7) — AUCUN filtrage par user_role
# =====================================================================


class TestCatalogIsRoleAgnostic:
    def test_classifiable_intents_has_no_role_parameter(self):
        """Garde structurelle légère : `_classifiable_intents` — source
        UNIQUE du catalogue new_task_v2 (spec §3) — ne prend aucun
        paramètre `role`, rendant IMPOSSIBLE toute réintroduction d'un
        filtrage par rôle dans ce catalogue (pas seulement une convention,
        une contrainte de signature)."""
        import inspect

        sig = inspect.signature(_classifiable_intents)
        assert "role" not in sig.parameters
        assert len(sig.parameters) == 0

    def test_system_prompt_is_byte_identical_for_the_same_catalog_regardless_of_caller_role(self):
        """Preuve directe (spec §7) : le PRODUCTEUR ET l'ACHETEUR reçoivent
        EXACTEMENT le même prompt système new_task_v2 — construit une seule
        fois à partir du même catalogue, jamais reconstruit différemment
        selon `user_role`."""
        prompt_for_producer_call = build_new_task_system_prompt(CATALOG)
        prompt_for_buyer_call = build_new_task_system_prompt(CATALOG)
        assert prompt_for_producer_call == prompt_for_buyer_call

    def test_same_message_same_state_different_role_yields_the_same_classification(self):
        """Bout en bout : même message, même état, seul `user_role` change
        — la classification produite (même LLM scripté identique) doit être
        strictement identique des deux côtés."""
        payload = {
            "disposition": "NEW_TASK",
            "intent": "SALES_PUBLISH_PRODUCT",
            "confidence": 0.9,
            "entities": {"product": "mais"},
        }
        for role in ("PRODUCER", "BUYER"):
            interp = make_input_interpreter(role)
            state = make_state(
                normalized_text="je veux vendre du mais",
                expected_input="NONE",
                user_role=role,
            )
            result = run(interp(state, StubRuntime(llm=ScriptedLLM(dict(payload)))))
            assert result["detected_intent"] == "SALES_PUBLISH_PRODUCT"
            assert result["interpreted_event"] == "NEW_TASK"


# =====================================================================
# C. EXTRACTION — PAS D'HALLUCINATION (spec §12/§13/§14/§46)
# =====================================================================


class TestEntityExtractionNeverHallucinates:
    async def _run(self, payload, text="peu importe le texte ici"):
        rt, gateway = _runtime(payload)
        outcome, result = await run_new_task_microprompt(
            {"message_sid": None}, rt, text, _ctx(), None, CATALOG
        )
        return outcome, result, gateway

    def test_missing_fields_stay_null_never_invented(self):
        outcome, result, _ = run(self._run({
            "disposition": "NEW_TASK", "intent": "SALES_PUBLISH_PRODUCT",
            "confidence": 0.9, "entities": {"product": "mais"},
        }))
        assert outcome == NewTaskOutcome.RESULT
        ents = result["extracted_entities"]
        assert "quantity" not in ents
        assert "price" not in ents
        assert ents["product"] == "mais"

    def test_multiple_products_are_never_merged_into_one_string(self):
        outcome, result, _ = run(self._run(
            {
                "disposition": "NEW_TASK", "intent": "BUYER_REQUEST", "confidence": 0.9,
                "entities": {"product": "œufs", "additional_products": ["laitue"]},
            },
            text="je cherche des œufs et de la laitue",
        ))
        ents = result["extracted_entities"]
        assert ents["product"] == "œufs"
        assert ents["additional_products"] == ["laitue"]
        assert "," not in ents["product"] and " et " not in ents["product"]

    def test_multiple_recurring_products_each_keep_their_own_quantity_and_unit(self):
        """Chantier multi-produits CREATE_RECURRING_NEED (2026-09-23, suite du
        correctif state-leak) : contrairement à `additional_products` (noms nus),
        `additional_items` porte quantité+unité PAR produit — plus aucune perte
        silencieuse de l'oignon."""
        outcome, result, _ = run(self._run(
            {
                "disposition": "NEW_TASK", "intent": "CREATE_RECURRING_NEED", "confidence": 0.9,
                "entities": {
                    "product": "tomate", "quantity": 10.0, "unit": "kg",
                    "additional_items": [{"product": "oignon", "quantity": 20.0, "unit": "kg"}],
                    "recurrence_type": "DAILY", "excluded_weekdays": [7],
                },
            },
            text="j ai besoin de 10 kg de tomate et 20 kg d oignon tous les jours sauf dimanche",
        ))
        ents = result["extracted_entities"]
        assert ents["product"] == "tomate"
        assert ents["quantity"] == 10.0
        assert ents["additional_items"] == [{"product": "oignon", "quantity": 20.0, "unit": "kg"}]

    def test_price_and_quantity_are_never_swapped(self):
        # "892 kg de maïs à 250 FCFA/kg" -> quantity=892/KG, price=250/KG —
        # texte réaliste requis ici : la garde anti-ancrage (même principe
        # que le chemin legacy) écarte toute unité que le texte ne confirme
        # pas littéralement, donc "KG" doit réellement apparaître dans `text`.
        outcome, result, _ = run(self._run(
            {
                "disposition": "NEW_TASK", "intent": "SALES_PUBLISH_PRODUCT", "confidence": 0.9,
                "entities": {
                    "product": "mais", "quantity": 892.0, "unit": "KG",
                    "price": 250.0, "price_unit": "KG",
                },
            },
            text="892 kg de mais a 250 fcfa le kg",
        ))
        ents = result["extracted_entities"]
        assert ents["quantity"] == 892.0
        assert ents["price"] == 250.0
        assert ents["unit"] == "KG"
        assert ents["price_unit"] == "KG"

    def test_quantity_without_a_literal_unit_yields_null_unit_and_missing_status(self):
        outcome, result, _ = run(self._run(
            {
                "disposition": "NEW_TASK", "intent": "SALES_PUBLISH_PRODUCT", "confidence": 0.9,
                "entities": {"product": "tomates", "quantity": 20.0, "unit": None},
            },
            text="je vends 20 tomates",
        ))
        ents = result["extracted_entities"]
        assert ents.get("unit") is None
        assert result.get("validation_status") == "INVALID_MISSING_UNIT"


# =====================================================================
# D. DATES DYNAMIQUES (spec §16) — jamais une année codée en dur
# =====================================================================


class TestReferenceDateIsInjectedDynamically:
    def test_user_prompt_carries_the_injected_reference_date_not_a_hardcoded_year(self):
        prompt_2026 = build_new_task_user_prompt(_ctx(reference_date="2026-09-13"), "demain")
        prompt_2031 = build_new_task_user_prompt(_ctx(reference_date="2031-01-01"), "demain")
        assert "2026-09-13" in prompt_2026
        assert "2031-01-01" in prompt_2031
        assert "2026-09-13" not in prompt_2031

    def test_system_prompt_never_hardcodes_a_year(self):
        system_prompt = build_new_task_system_prompt(CATALOG)
        for year in ("2025", "2026", "2027"):
            assert year not in system_prompt


# =====================================================================
# E. UNKNOWN EST UNE SORTIE VALIDE (spec §23)
# =====================================================================


class TestUnknownIsValidNeverForced:
    def test_unresolvable_message_yields_unknown_not_a_guess(self):
        rt, gateway = _runtime({"disposition": "UNKNOWN", "confidence": 0.1})
        outcome, result = run(
            run_new_task_microprompt(
                {"message_sid": None}, rt, "grbl grbl", _ctx(), None, CATALOG
            )
        )
        assert outcome == NewTaskOutcome.RESULT
        assert result["interpreted_event"] == "UNKNOWN"
        assert result["detected_intent"] == "UNKNOWN"

    def test_hallucinated_intent_outside_catalog_is_rejected_after_repair_then_unknown(self):
        # Le modèle persiste hors-catalogue même après le repair (scripté
        # identique aux 2 appels) — jamais routé, jamais exécuté.
        rt, gateway = _runtime({
            "disposition": "NEW_TASK", "intent": "INTENTION_INVENTEE", "confidence": 0.9,
            "entities": {},
        })
        outcome, result = run(
            run_new_task_microprompt(
                {"message_sid": None}, rt, "fais un truc bizarre", _ctx(), None, CATALOG
            )
        )
        assert result["interpreted_event"] == "UNKNOWN"
        assert len(gateway.calls) == 2  # 1 essai + 1 repair, jamais plus


# =====================================================================
# F. REPAIR RETRY — MAXIMUM 1 (spec §36)
# =====================================================================


class TestRepairRetryIsCappedAtOne:
    def test_invalid_json_twice_in_a_row_stops_after_one_repair(self):
        rt, gateway = _runtime("ceci n'est pas du JSON", "toujours pas du JSON")
        outcome, result = run(
            run_new_task_microprompt(
                {"message_sid": None}, rt, "peu importe", _ctx(), None, CATALOG
            )
        )
        assert result["interpreted_event"] == "UNKNOWN"
        assert len(gateway.calls) == 2


# =====================================================================
# G. GARDE ANTI-RÉGRESSION TAILLE DE PROMPT (spec §50/§51)
# =====================================================================


class TestPromptSizeGuard:
    # Seuils relevés de 2200 à 2260 (2026-09-23) : `recurrence_type`/
    # `weekly_days`/`excluded_weekdays`/`max_price_per_unit` ajoutés au
    # schéma (bug réel — CREATE_RECURRING_NEED ne pouvait structurellement
    # jamais faire transiter une récurrence, voir TestRecurringNeedWith
    # ExcludedWeekday plus bas). +60 tokens pour un champ `required` du
    # catalogue reste très loin de l'ancien prompt à 5000 tokens que cette
    # garde vise réellement à empêcher (spec §50) — jamais une dérive non
    # justifiée.
    # Seuils relevés de 2260 à 2340 (2026-09-24) : `ambiguous_groups` ajouté
    # au schéma (bug réel production — "14 coq et 57 moutons chèvres chaque
    # semaine" : sans ce champ, le micro-prompt devait soit inventer une
    # répartition (57 de chaque), soit fusionner en un produit incohérent —
    # les deux interdits par la spec §4 "ne jamais inventer une information
    # absente"). +~50 tokens pour éviter une invention de données reste très
    # loin de l'ancien prompt à 5000 tokens que cette garde vise réellement à
    # empêcher (spec §50).
    # Seuils relevés de 2340 à 2430 (2026-09-26) : `orphan_quantities` ajouté
    # au schéma (bug réel production — "150 kg tomate et 200 kg chaque
    # semaine" : sans ce champ, une garde Python anti-troncature refusionnait
    # la deuxième quantité, sans produit, dans celle du produit déjà connu —
    # 150+200 devenait 350, jamais dit par l'utilisateur). +~90 tokens pour
    # donner au LLM un endroit où mettre une quantité SANS produit reste très
    # loin de l'ancien prompt à 5000 tokens que cette garde vise réellement à
    # empêcher (spec §50).
    # Seuils relevés de 2430 à 2460 (2026-09-29, mandat B2c.5) : consigne
    # ajoutée pour `max_price_per_unit` (CREATE_RECURRING_NEED) — sans elle,
    # un buyer disant "budget 500000" (sans dire "par kg") pouvait voir ce
    # montant TOTAL capturé tel quel dans un champ structurellement PAR
    # UNITÉ, un plafond de prix silencieusement faux d'un facteur = à la
    # quantité (même classe de bug que le mandat de certification des prix,
    # phases B1/B2c.3/B2c.5). +~20 tokens pour fermer cette ambiguïté reste
    # très loin de l'ancien prompt à 5000 tokens que cette garde vise
    # réellement à empêcher (spec §50).
    # Seuils relevés de 2460 à 2650 (2026-10-01, Étape 9A/9B) : nouvelle
    # disposition AMBIGUOUS + champ `candidate_goals` — "j'ai 90 L de miel"
    # (aucun tunnel actif) forçait jusqu'ici le LLM à choisir UNE intention
    # arbitraire (SALES_PUBLISH_PRODUCT ou STOCK_REGISTER_HARVEST — "max
    # confidence wins" explicitement interdit par le mandat §9) alors que le
    # texte ne contient structurellement aucun signal d'action qui tranche
    # entre les deux. Pas un champ optionnel de plus : la différence entre
    # exécuter une action métier arbitraire et demander une clarification
    # honnête. +~190 tokens (prose condensée au maximum, un seul exemple
    # contrastif) reste très loin de l'ancien prompt à 5000 tokens que cette
    # garde vise réellement à empêcher (spec §50).
    # Seuils relevés de 2650 à 2700 et de 2700 à 2750 (2026-10-05) : champ `starts_at` (date de début explicite d'un
    # besoin récurrent, « à partir du 20 octobre ») — sans lui l'interpréteur ne pouvait structurellement jamais
    # transmettre une date voulue, et le délai minimal avant première livraison (réglage admin) ne pouvait pas être
    # confronté à ce que l'utilisateur demande. ~+35 tokens, très loin des 5000 de l'ancien prompt (spec §50).
    def test_system_prompt_never_regresses_towards_the_old_5000_token_prompt(self):
        system_prompt = build_new_task_system_prompt(CATALOG)
        estimated_tokens = int(len(system_prompt.split()) * 1.3)
        # Flow compression (2026-10) : +~45 tokens (max_price_per_unit/conditionnement VOULU d'un BUYER_REQUEST) — loin des 5000.
        assert estimated_tokens < 2750, (
            f"system_prompt new_task_v2 ~{estimated_tokens} tokens — "
            "seuil de garde anti-régression dépassé (spec §50)"
        )

    def test_full_input_lands_in_the_targeted_range(self):
        system_prompt = build_new_task_system_prompt(CATALOG)
        user_prompt = build_new_task_user_prompt(
            _ctx(), "je veux vendre 20 sacs de mais a 250 le kilo"
        )
        total_tokens = int((len(system_prompt.split()) + len(user_prompt.split())) * 1.3)
        # B24 : +~30 tokens (champ `update_action` du schéma JSON) ; le garde anti-5000 tokens ci-dessus reste à 2650.
        assert 800 <= total_tokens <= 2800


class TestMaxTokensIsExplicitAndLargeEnough:
    """(2026-09-14, incident WhatsApp — root cause de la journée) : ce
    plafond n'avait JAMAIS reçu le même correctif que `selection_micro.py`/
    `active_slot_micro.py` (Phase B.1, 2026-09-12) — les modèles Groq
    RÉELLEMENT disponibles pour le profil INTERPRETER sont des modèles DE
    RAISONNEMENT (tokens de "réflexion" décomptés de `max_tokens` avant le
    JSON final). À 300, `openai/gpt-oss-20b` échouait SYSTÉMATIQUEMENT en
    prod — forçant un repli permanent sur le modèle de secours, moins
    fiable en jugement, responsable de la quasi-totalité des mauvaises
    classifications "confirmer"/"annuler" chassées ce jour-là."""

    def test_max_tokens_is_explicit_and_at_least_as_generous_as_structured_action(self):
        rt, gateway = _runtime(
            {"disposition": "NEW_TASK", "intent": "SALES_PUBLISH_PRODUCT", "confidence": 0.9, "entities": {}}
        )
        run(
            run_new_task_microprompt(
                {"message_sid": None}, rt, "peu importe", _ctx(), None, CATALOG
            )
        )
        assert len(gateway.calls) == 1
        assert gateway.calls[0]["max_tokens"] is not None
        # `structured_action_micro.py` (800) porte le 2e schéma de sortie le
        # plus large des 4 micro-prompts — celui-ci (disposition + intent +
        # jusqu'à ~15 champs d'entités, `pricing_tiers` en tableau) est le
        # PLUS large des quatre et ne doit donc jamais être plus bas.
        assert gateway.calls[0]["max_tokens"] >= 800


# =====================================================================
# H. PAIRES CONTRASTIVES D'INTENTIONS PROCHES (spec §41/42)
# =====================================================================


class TestContrastiveIntentPairs:
    """Le catalogue lui-même (labels d'`INTENT_CONFIG`, réutilisés tels
    quels — source unique, spec §3) porte déjà la distinction conceptuelle
    entre intentions proches — ces tests prouvent que le WIRING la respecte
    une fois le LLM (scripté) tranché, pas que le LLM "comprend" (validé à
    part par le replay live)."""

    def test_buyer_request_vs_buyer_list_orders(self):
        # "je veux acheter du riz" (nouvelle envie) vs "où est ma commande"
        # (historique déjà passé) — intentions proches, jamais confondues.
        for text, intent in (
            ("je veux acheter du riz", "BUYER_REQUEST"),
            ("où est ma commande", "BUYER_LIST_ORDERS"),
        ):
            interp = make_input_interpreter("BUYER")
            payload = {
                "disposition": "NEW_TASK", "intent": intent, "confidence": 0.9,
                "entities": {},
            }
            state = make_state(normalized_text=text, expected_input="NONE", user_role="BUYER")
            result = run(interp(state, StubRuntime(llm=ScriptedLLM(payload))))
            assert result["detected_intent"] == intent

    def test_sales_list_orders_vs_buyer_list_orders(self):
        # "mes ventes" (commandes REÇUES sur mes produits, producteur) vs
        # "mes commandes" (achats déjà passés, acheteur) — même utilisateur
        # double-rôle, deux intentions distinctes selon ce qu'il dit.
        for text, intent in (
            ("mes ventes reçues", "SALES_LIST_ORDERS"),
            ("mes commandes passées", "BUYER_LIST_ORDERS"),
        ):
            interp = make_input_interpreter("PRODUCER")
            payload = {
                "disposition": "NEW_TASK", "intent": intent, "confidence": 0.9,
                "entities": {},
            }
            state = make_state(normalized_text=text, expected_input="NONE", user_role="PRODUCER")
            result = run(interp(state, StubRuntime(llm=ScriptedLLM(payload))))
            assert result["detected_intent"] == intent


# =====================================================================
# I. DISPATCH RÉEL PAR ROUTE (spec §61) — seule NEW_TASK/DEVIATION
#    l'utilisent, jamais les 3 autres routes en fonctionnement normal.
# =====================================================================


class TestOnlyNewTaskRouteUsesTheMicroprompt:
    def test_new_task_route_calls_the_microprompt(self, monkeypatch):
        import ladini.graphs.agents.market_coach.interpreter.new_task_micro as new_task_micro_module

        called = {"value": False}

        async def _spy(*args, **kwargs):
            called["value"] = True
            return NewTaskOutcome.RESULT, {
                "interpreted_event": "NEW_TASK",
                "detected_intent": "BUYER_REQUEST",
                "interpreter_confidence": 0.9,
                "extracted_entities": {},
                "raw_analysis": {"path": "new_task_micro"},
            }

        monkeypatch.setattr(new_task_micro_module, "run_new_task_microprompt", _spy)
        interp = make_input_interpreter("BUYER")
        state = make_state(normalized_text="je veux acheter du riz", expected_input="NONE", user_role="BUYER")
        run(interp(state, StubRuntime(llm=ScriptedLLM({"disposition": "NEW_TASK", "intent": "BUYER_REQUEST", "confidence": 0.9, "entities": {}}))))
        assert called["value"] is True

    def test_feature_flag_disabled_falls_back_to_legacy_immediately(self, monkeypatch):
        """Spec §56 : rollback sans redéploiement — flag à False doit
        empêcher TOUT appel au micro-prompt new_task_v2, même pour la route
        NEW_TASK, et retomber directement sur l'interpréteur unifié legacy."""
        import ladini.graphs.agents.market_coach.interpreter.new_task_micro as new_task_micro_module
        from ladini.core.settings import settings as mc_settings

        def _boom(*args, **kwargs):
            raise AssertionError("new_task_micro ne doit pas être appelé, flag désactivé")

        monkeypatch.setattr(new_task_micro_module, "run_new_task_microprompt", _boom)
        monkeypatch.setattr(mc_settings, "MARKET_COACH_NEW_TASK_V2_ENABLED", False)
        interp = make_input_interpreter("BUYER")
        legacy_payload = {
            "interpreted_event": "NEW_TASK",
            "detected_intent": "BUYER_REQUEST",
            "interpreter_confidence": 0.9,
            "validation_status": "VALID",
            "extracted_entities": {"product": "riz"},
        }
        state = make_state(normalized_text="je veux acheter du riz", expected_input="NONE", user_role="BUYER")
        result = run(interp(state, StubRuntime(llm=ScriptedLLM(legacy_payload))))
        assert result["interpreted_event"] == "NEW_TASK"
        assert result["detected_intent"] == "BUYER_REQUEST"


# =====================================================================
# J. BESOIN RÉCURRENT AVEC JOUR EXCLU (bug réel 2026-09-23, non-régression)
#
# "J'ai besoin de 20 kg de tomate tous les jours sauf les dimanches" était
# classé BUYER_REQUEST en production ("produit non disponible dans notre
# catalogue" — `flows/buyer/cart.py`) au lieu de CREATE_RECURRING_NEED.
# Cause racine, confirmée en lisant le code (jamais devinée) : le contrat
# `NewTaskEntities` (extra="forbid") ne portait AUCUN champ
# `recurrence_type`/`weekly_days`/`excluded_weekdays`/`max_price_per_unit`
# — alors que `INTENT_CONFIG["CREATE_RECURRING_NEED"]["required"]` EXIGE
# `recurrence_type`. Le micro-prompt NEW_TASK ne pouvait donc structurellement
# jamais faire transiter une récurrence, quel que soit le jugement du LLM.
# Ce test fige le CONTRAT (schéma + wiring), jamais le jugement du LLM lui-
# même — voir la philosophie de `tests/architecture/
# test_recurring_need_intent_wiring.py`. La résolution catalogue elle-même
# (`RecurringSupplyMixin._resolve_sub_category`, `SubCategory` — jamais un
# `Product` en stock) était déjà correcte avant ce correctif et reste
# inchangée ici.
# =====================================================================


class TestRecurringNeedWithExcludedWeekday:
    _RECURRING_TEXT = "J'ai besoin de 20 kg de tomate tous les jours sauf les dimanches"
    _RECURRING_PAYLOAD = {
        "disposition": "NEW_TASK",
        "intent": "CREATE_RECURRING_NEED",
        "confidence": 0.95,
        "entities": {
            "product": "tomate",
            "quantity": 20.0,
            "unit": "KG",
            "recurrence_type": "DAILY",
            "excluded_weekdays": [7],
        },
    }

    def test_recurrence_entities_no_longer_trigger_a_schema_validation_error(self):
        """Le coeur du bug : ce payload — exactement ce qu'un LLM bien
        informé doit produire pour ce message — levait une ValidationError
        `extra_forbidden` sur `recurrence_type`/`excluded_weekdays` avant ce
        correctif, jamais un défaut de jugement du LLM scripté ici."""
        rt, gateway = _runtime(self._RECURRING_PAYLOAD)
        outcome, result = run(
            run_new_task_microprompt(
                {"message_sid": None}, rt, self._RECURRING_TEXT, _ctx(), None, CATALOG
            )
        )
        assert outcome == NewTaskOutcome.RESULT
        assert len(gateway.calls) == 1  # schéma valide dès le 1er essai, aucun repair déclenché
        assert result["interpreted_event"] == "NEW_TASK"
        assert result["detected_intent"] == "CREATE_RECURRING_NEED"
        ents = result["extracted_entities"]
        assert ents["product"] == "tomate"
        assert ents["quantity"] == 20.0
        assert ents["unit"] == "KG"
        assert ents["recurrence_type"] == "DAILY"
        assert ents["excluded_weekdays"] == [7]

    def test_full_interpreter_routes_to_recurring_need_tunnel_never_cart(self):
        """Bout en bout via `make_input_interpreter` : le message doit
        atteindre le tunnel `recurring_need` (`flows/buyer/recurring_need.py`)
        — JAMAIS `cart` (`flows/buyer/cart.py`, propriétaire du message
        "produit non disponible dans notre catalogue" observé en prod) ni
        exiger un `Product` actuellement en stock."""
        interp = make_input_interpreter("BUYER")
        state = make_state(
            normalized_text=self._RECURRING_TEXT, expected_input="NONE", user_role="BUYER"
        )
        result = run(interp(state, StubRuntime(llm=ScriptedLLM(dict(self._RECURRING_PAYLOAD)))))
        assert result["detected_intent"] == "CREATE_RECURRING_NEED"
        assert result["interpreted_event"] == "NEW_TASK"
        assert INTENT_CONFIG["CREATE_RECURRING_NEED"]["tunnel"] == "recurring_need"
        assert INTENT_CONFIG["CREATE_RECURRING_NEED"]["tunnel"] != "cart"
        ents = result["extracted_entities"]
        assert ents["recurrence_type"] == "DAILY"
        assert ents["excluded_weekdays"] == [7]

    def test_punctual_purchase_of_the_same_product_still_uses_the_old_cart_flow(self):
        """Test miroir explicitement requis (non-régression) : "je cherche 20
        kg de tomate maintenant" reste BUYER_REQUEST -> tunnel `cart`, jamais
        absorbé par le nouveau tunnel `recurring_need`."""
        payload = {
            "disposition": "NEW_TASK",
            "intent": "BUYER_REQUEST",
            "confidence": 0.9,
            "entities": {"product": "tomate", "quantity": 20.0, "unit": "KG"},
        }
        interp = make_input_interpreter("BUYER")
        state = make_state(
            normalized_text="Je cherche 20 kg de tomate maintenant",
            expected_input="NONE",
            user_role="BUYER",
        )
        result = run(interp(state, StubRuntime(llm=ScriptedLLM(payload))))
        assert result["detected_intent"] == "BUYER_REQUEST"
        assert result["interpreted_event"] == "NEW_TASK"
        # BUYER_REQUEST reste hors du tunnel recurring_need (resté à l'écart
        # du tunnel dédié cart.py lui-même — `_TUNNELLESS_FLOW_INTENTS`,
        # core/goals.py — jamais absorbé par le nouveau tunnel).
        assert INTENT_CONFIG["BUYER_REQUEST"].get("tunnel") != "recurring_need"


# =====================================================================
# K. BESOIN MENSUEL (bug réel initial, mandat Phase 3 MONTHLY §30, non-régression)
#
# "j'ai besoin de 90 L de lait chaque mois" partait vers le catalogue puis le
# packaging puis l'appel d'offres : `recurrence_type` n'admettait pas MONTHLY
# — ni dans le prompt système du micro-prompt NEW_TASK (`new_task_prompts.py`,
# l'enum montré au LLM), ni dans le domaine (`RECURRENCE_TYPES`). Même famille
# de bug que J ci-dessus (une valeur de récurrence structurellement invisible
# du micro-prompt/domaine fait perdre CREATE_RECURRING_NEED), cause DIFFÉRENTE
# (une VALEUR manquante de l'enum, pas un CHAMP manquant du contrat).
# =====================================================================


class TestRecurringNeedMonthly:
    _RECURRING_TEXT = "j'ai besoin de 90 L de lait chaque mois"
    _RECURRING_PAYLOAD = {
        "disposition": "NEW_TASK",
        "intent": "CREATE_RECURRING_NEED",
        "confidence": 0.95,
        "entities": {
            "product": "lait",
            "quantity": 90.0,
            "unit": "L",
            "recurrence_type": "MONTHLY",
        },
    }

    def test_the_historical_bug_message_no_longer_falls_back_to_catalog(self):
        """LE test permanent exigé par le mandat (§30) : ce message précis doit
        désormais produire exactement le même type de transaction que son
        équivalent WEEKLY — jamais un repli catalogue/packaging/appel d'offres."""
        interp = make_input_interpreter("BUYER")
        state = make_state(
            normalized_text=self._RECURRING_TEXT, expected_input="NONE", user_role="BUYER"
        )
        result = run(interp(state, StubRuntime(llm=ScriptedLLM(dict(self._RECURRING_PAYLOAD)))))
        assert result["detected_intent"] == "CREATE_RECURRING_NEED"
        assert result["interpreted_event"] == "NEW_TASK"
        assert INTENT_CONFIG["CREATE_RECURRING_NEED"]["tunnel"] == "recurring_need"
        assert INTENT_CONFIG["CREATE_RECURRING_NEED"]["tunnel"] != "cart"
        ents = result["extracted_entities"]
        assert ents["product"] == "lait"
        assert ents["quantity"] == 90.0
        assert ents["unit"] == "LITRE"  # "L" canonicalisé par canonical_unit_label
        assert ents["recurrence_type"] == "MONTHLY"

    def test_monthly_entities_pass_schema_validation_on_the_first_attempt(self):
        rt, gateway = _runtime(self._RECURRING_PAYLOAD)
        outcome, result = run(
            run_new_task_microprompt(
                {"message_sid": None}, rt, self._RECURRING_TEXT, _ctx(), None, CATALOG
            )
        )
        assert outcome == NewTaskOutcome.RESULT
        assert len(gateway.calls) == 1  # schéma valide dès le 1er essai, aucun repair déclenché
        assert result["extracted_entities"]["recurrence_type"] == "MONTHLY"
