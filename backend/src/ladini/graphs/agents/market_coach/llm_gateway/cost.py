"""Cost accounting — Incrément G (2026-09-13, "Production LLM Hardening"),
spec §23/§24.

Formule : `estimated_cost_usd = prompt_tokens/1e6 * input_per_1m +
completion_tokens/1e6 * output_per_1m`. Les prix viennent EXCLUSIVEMENT de
`settings.LLM_PRICING_JSON` (spec §24 : "les prix peuvent être contractuels
— config/env, jamais codés en dur") — un modèle absent de cette table
retourne `None` (coût inconnu), jamais une estimation inventée à partir
d'un tarif "public" qui peut ne pas correspondre au plan payant réel
(spec §52)."""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any, Dict, Optional

logger = logging.getLogger("ladini.llm_gateway.cost")


@lru_cache(maxsize=1)
def _pricing_table_cached(raw_json: str) -> Dict[str, Dict[str, float]]:
    """Cache keyed par le contenu JSON lui-même — se recalcule automatiquement
    si `LLM_PRICING_JSON` change (ex: rechargement de settings en test),
    jamais besoin d'invalidation manuelle."""
    if not raw_json:
        return {}
    try:
        parsed = json.loads(raw_json)
    except Exception as exc:
        logger.warning(
            "[llm_gateway.cost] LLM_PRICING_JSON illisible (%s) — coût "
            "traité comme inconnu pour tous les modèles.",
            exc,
        )
        return {}
    if not isinstance(parsed, dict):
        return {}
    table: Dict[str, Dict[str, float]] = {}
    for key, value in parsed.items():
        if not isinstance(value, dict):
            continue
        try:
            table[key] = {
                "input_per_1m": float(value.get("input_per_1m", 0.0)),
                "output_per_1m": float(value.get("output_per_1m", 0.0)),
            }
        except (TypeError, ValueError):
            continue
    return table


def _pricing_table(settings: Any = None) -> Dict[str, Dict[str, float]]:
    if settings is None:
        from ladini.core.settings import settings as _settings

        settings = _settings
    return _pricing_table_cached(str(getattr(settings, "LLM_PRICING_JSON", "") or ""))


def estimate_cost_usd(
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    *,
    settings: Any = None,
) -> Optional[float]:
    """`None` si `provider:model` n'a pas de prix configuré — jamais une
    estimation inventée (spec §52). Précision non requise (spec §16) : une
    estimation raisonnable suffit, pas un calcul facturable exact."""
    table = _pricing_table(settings)
    prices = table.get(f"{provider}:{model}")
    if prices is None:
        return None
    cost = (prompt_tokens / 1_000_000.0) * prices["input_per_1m"] + (
        completion_tokens / 1_000_000.0
    ) * prices["output_per_1m"]
    return round(cost, 8)


__all__ = ["estimate_cost_usd"]
