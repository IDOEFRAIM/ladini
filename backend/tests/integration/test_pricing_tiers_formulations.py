"""Formulations tarifaires par conditionnement SANS « FCFA » — source contractuelle = extraction LLM.

Décision d'architecture (audit hardening 2026-09-29, `interpreter/routing.py` ~l.1008-1021 et
`domain/quantity_unit.py::parse_packaging_message`) : le parseur déterministe est un FILET DE FIABILITÉ à
motifs explicites, qui ne DEVINE JAMAIS (« pas de résultat partiel », « la correction n'est PAS d'empiler
une regex par tournure ») ; les formulations libres relèvent de l'extraction sémantique LLM (règle 5ter du
prompt). Donc ici :

1. le parseur déterministe ne doit jamais affirmer un résultat FAUX ou PARTIEL sur ces formulations (soit les
   deux bons paliers, soit rien — et alors le LLM tranche) ;
2. le contrat d'extraction (prompts + schéma) documente ces formulations et la règle `packaging=null` ;
3. le VRAI graphe, avec une extraction LLM scriptée conforme à ce contrat, aboutit aux mêmes valeurs
   commerciales pour les quatre formulations. Aucun appel LLM réseau : `HarnessLLM` est déterministe.
"""
from __future__ import annotations

import pytest

from ladini.domain.quantity_unit import parse_packaging_message
from ladini.graphs.agents.market_coach.interpreter.new_task_contract import (
    NewTaskPricingTier,
)
from tests.integration.test_sales_publish_packaging_tiers import (  # noqa: F401  (fixture `conv`)
    B5,
    B9,
    _ans,
    _draft,
    _miel_60,
    conv,
)

pytestmark = pytest.mark.integration

FORMULATIONS = [
    "700 le bidon de 5 L et 1000 le bidon de 9 L",
    "bidon 5 L 700, bidon 9 L 1000",
    "le bidon de 5 L coûte 700 et celui de 9 L coûte 1000",
    "Je vends le bidon de 5 l a 700 FCFA et celui de 9 l a 1000 FCFA",
]
EXPECTED = [(5.0, 700.0, "bidon"), (9.0, 1000.0, "bidon")]


def _key(tiers):
    return [(float(t["quantity"]), float(t["price"]), t["packaging"]) for t in tiers]


class TestDeterministicParserNeverClaimsAWrongResult:
    @pytest.mark.parametrize("text", FORMULATIONS + ["5 L à 700 et 9 L à 1000"])
    def test_either_the_two_right_tiers_or_nothing(self, text):
        tiers = parse_packaging_message(text).pricing_tiers
        if tiers:
            got = [(float(t["quantity"]), float(t["price"])) for t in tiers]
            assert got == [(5.0, 700.0), (9.0, 1000.0)], (text, tiers)
            if text.startswith("5 L à"):
                assert all(t["packaging"] is None for t in tiers), "packaging jamais inventé"

    def test_the_incident_message_is_resolved_deterministically(self):
        tiers = parse_packaging_message(FORMULATIONS[-1]).pricing_tiers
        assert _key(tiers) == EXPECTED


class TestExtractionContractIsDocumented:
    def test_both_prompts_carry_the_formulations_and_the_packaging_null_rule(self):
        from ladini.graphs.agents.market_coach.interpreter import (
            active_slot_prompts,
            new_task_prompts,
        )

        # NEW_TASK : budget de tokens gardé (TestPromptSizeGuard) -> seule la règle `packaging=null`.
        assert "`packaging` : mot DIT, sinon `null`." in new_task_prompts._SYSTEM_PROMPT_HEADER
        # ACTIVE_SLOT (chemin de l'incident : prix attendu) : la règle ET les formulations.
        src = active_slot_prompts._USER_PROMPT_TEMPLATE
        assert "700 le bidon de 5 L et 1000 le bidon de 9 L" in src
        assert "bidon 5 L 700, bidon 9 L 1000" in src
        assert "5 L à 700 et 9 L à 1000" in src and "packaging null" in src

    def test_schema_accepts_a_tier_without_packaging(self):
        tier = NewTaskPricingTier(quantity=5.0, unit="L", price=700.0, packaging=None)
        assert tier.packaging is None


class TestSameCommercialValuesEndToEnd:
    @pytest.mark.parametrize("text", FORMULATIONS)
    def test_named_packaging_formulations(self, conv, text):
        _miel_60(conv)
        t = conv.send(text, llm=_ans(pricing_tiers=[B5, B9]))
        assert "Confirmez-vous" in t.response and "Je n'ai pas bien saisi" not in t.response
        d = _draft(conv)
        assert _key(d["pricing_tiers"]) == EXPECTED and d["quantity"] == 60.0

    def test_unnamed_packaging_stays_unnamed(self, conv):
        _miel_60(conv)
        t = conv.send(
            "5 L à 700 et 9 L à 1000",
            llm=_ans(pricing_tiers=[{**B5, "packaging": None}, {**B9, "packaging": None}]),
        )
        assert "Confirmez-vous" in t.response
        assert _key(_draft(conv)["pricing_tiers"]) == [(5.0, 700.0, None), (9.0, 1000.0, None)]
