"""Product validation — catalog lookup and sanitisation.

Extracted from ``interpreter/routing.py`` to isolate domain-level product
validation from the interpreter orchestration layer.
"""
from __future__ import annotations

import logging
import re as _re
from typing import Any, Dict, Optional

from agriconnect.graphs.agents.market_coach.interpreter.entities import (
    _GENERIC_PRODUCT_STOPWORDS,
    _KNOWN_PRODUCT_KEYWORDS,
    _SUSPICIOUS_PRODUCT_TOKENS,
)
from agriconnect.graphs.agents.market_coach.utils import (
    MarketRuntime,
    _clean_candidate_text,
)

logger = logging.getLogger("AgriConnect.Market.ProductValidation")


async def _catalog_has_product(name: str, mc_runtime: Optional[MarketRuntime]) -> bool:
    if not mc_runtime:
        return False
    try:
        db_service = mc_runtime.ensure_db()
    except Exception:
        return False
    if not db_service or not hasattr(db_service, "get_public_products"):
        return False
    try:
        result = await db_service.get_public_products(search=name, limit=1)
    except Exception as exc:
        logger.debug("Product catalog lookup failed: %s", exc)
        return False
    if not isinstance(result, dict):
        return False
    for key in ("items", "data", "results"):
        items = result.get(key)
        if isinstance(items, list) and items:
            return True
    return False


async def _validate_and_sanitize_product(
    product_value: Optional[str], mc_runtime: Optional[MarketRuntime]
) -> Optional[str]:
    if not product_value:
        return None
    candidate = _clean_candidate_text(product_value)
    if not candidate:
        return None
    lowered = candidate.lower()
    if lowered in _GENERIC_PRODUCT_STOPWORDS:
        return None
    if len(candidate) < 2 or len(candidate) > 40:
        return None
    if any(token in lowered for token in _SUSPICIOUS_PRODUCT_TOKENS):
        return None
    if _re.search(r"http[s]?://|www\\.|@|#", lowered):
        return None
    words = lowered.split()
    if any(tok in lowered for tok in _KNOWN_PRODUCT_KEYWORDS):
        return candidate
    if len(words) <= 3:
        return candidate
    has_catalog_match = await _catalog_has_product(candidate, mc_runtime)
    return candidate if has_catalog_match else None


__all__ = [
    "_catalog_has_product",
    "_validate_and_sanitize_product",
]
