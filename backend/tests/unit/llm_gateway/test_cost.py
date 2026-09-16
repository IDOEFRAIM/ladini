"""Incrément G (2026-09-13, "Production LLM Hardening") — cost accounting
(spec §23/§24/§52).

Le prix vient EXCLUSIVEMENT de `settings.LLM_PRICING_JSON` — jamais une
valeur codée en dur ici (spec §52 : "ne mets pas des valeurs arbitraires
prétendues Groq production")."""
from __future__ import annotations

import json
from types import SimpleNamespace

from ladini.graphs.agents.market_coach.llm_gateway.cost import estimate_cost_usd


def _settings(pricing: dict) -> SimpleNamespace:
    return SimpleNamespace(LLM_PRICING_JSON=json.dumps(pricing))


class TestUnknownModelReturnsNoneNeverAnInventedPrice:
    def test_a_model_absent_from_the_pricing_table_returns_none(self):
        settings = _settings({})
        assert estimate_cost_usd("groq", "unknown-model", 1000, 500, settings=settings) is None

    def test_empty_pricing_json_returns_none_for_everything(self):
        settings = SimpleNamespace(LLM_PRICING_JSON="")
        assert estimate_cost_usd("groq", "openai/gpt-oss-20b", 1000, 500, settings=settings) is None


class TestCostCalculation:
    def test_cost_is_computed_from_configured_per_million_prices(self):
        settings = _settings(
            {"groq:openai/gpt-oss-20b": {"input_per_1m": 0.10, "output_per_1m": 0.20}}
        )
        cost = estimate_cost_usd(
            "groq", "openai/gpt-oss-20b", 1_000_000, 1_000_000, settings=settings
        )
        assert cost == 0.30  # 1M*0.10/1M + 1M*0.20/1M

    def test_zero_tokens_costs_zero(self):
        settings = _settings(
            {"groq:openai/gpt-oss-20b": {"input_per_1m": 0.10, "output_per_1m": 0.20}}
        )
        assert estimate_cost_usd("groq", "openai/gpt-oss-20b", 0, 0, settings=settings) == 0.0

    def test_different_provider_same_model_name_is_a_distinct_price_entry(self):
        settings = _settings(
            {
                "groq:qwen.qwen3-32b": {"input_per_1m": 1.0, "output_per_1m": 1.0},
                "bedrock_gateway:qwen.qwen3-32b": {"input_per_1m": 2.0, "output_per_1m": 2.0},
            }
        )
        cost_groq = estimate_cost_usd("groq", "qwen.qwen3-32b", 1_000_000, 0, settings=settings)
        cost_bedrock = estimate_cost_usd(
            "bedrock_gateway", "qwen.qwen3-32b", 1_000_000, 0, settings=settings
        )
        assert cost_groq == 1.0
        assert cost_bedrock == 2.0


class TestMalformedPricingJsonNeverCrashes:
    def test_invalid_json_is_treated_as_an_empty_table(self):
        settings = SimpleNamespace(LLM_PRICING_JSON="{not valid json")
        assert estimate_cost_usd("groq", "any-model", 100, 100, settings=settings) is None

    def test_non_dict_json_is_treated_as_an_empty_table(self):
        settings = SimpleNamespace(LLM_PRICING_JSON="[1, 2, 3]")
        assert estimate_cost_usd("groq", "any-model", 100, 100, settings=settings) is None

    def test_malformed_entry_values_are_skipped_not_crashing(self):
        settings = SimpleNamespace(
            LLM_PRICING_JSON=json.dumps(
                {"groq:model-x": {"input_per_1m": "not-a-number", "output_per_1m": 1.0}}
            )
        )
        assert estimate_cost_usd("groq", "model-x", 100, 100, settings=settings) is None
