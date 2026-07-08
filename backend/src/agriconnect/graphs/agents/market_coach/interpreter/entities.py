"""Entity normalisation — key remapping, unit synonyms, product sanitisation.

Extracted from ``interpreter/routing.py`` so that entity-handling logic
lives in its own module and routing.py stays focused on LLM interpretation
and the goal planner state machine.
"""
from __future__ import annotations

import logging
import re as _re
import unicodedata as _unicodedata
from typing import Any, Dict, Optional

from agriconnect.graphs.agents.market_coach.core.slots import build_remap_dict
from agriconnect.graphs.agents.market_coach.services.domain.quantity_unit import (
    UNIT_SYNONYMS as _CANONICAL_UNIT_SYNONYMS,
    normalize_unit_token as _normalize_unit_token_impl,
    parse_quantity_unit_from_text,
)
from agriconnect.graphs.agents.market_coach.utils import (
    canonical_unit_label,
    normalize_slot_keys,
    slot_has_value,
    _clean_candidate_text,
)

logger = logging.getLogger("AgriConnect.Market.Entities")

# _ENTITY_KEY_REMAP is derived from core/slots.py (single source of truth).
_ENTITY_KEY_REMAP: Dict[str, str] = build_remap_dict()


_UNIT_SYNONYMS: Dict[str, str] = _CANONICAL_UNIT_SYNONYMS

_normalize_unit_token = _normalize_unit_token_impl


_GENERIC_PRODUCT_STOPWORDS = {
    "merci",
    "bonjour",
    "bonsoir",
    "salut",
    "d'accord",
    "ok",
    "c'est bon",
    "aucun",
    "nothing",
}

_KNOWN_PRODUCT_KEYWORDS = {
    "maïs",
    "mais",
    "riz",
    "sorgho",
    "soja",
    "arachide",
    "oignon",
    "tomate",
    "piment",
    "gombo",
    "banane",
    "igname",
    "coton",
    "manioc",
    "mil",
    "niébé",
    "engrais",
    "urée",
    "npk",
    "intrant",
    "semence",
    "fertilisant",
    "poivron",
    "carotte",
    "chou",
}

_SUSPICIOUS_PRODUCT_TOKENS = {
    "commande",
    "commandes",
    "precommande",
    "précommande",
    "précommander",
    "precommander",
    "prix",
    "payer",
    "paiement",
    "client",
    "livraison",
    "acheteur",
    "achete",
    "acheté",
    "acheter",
    "vendeur",
    "vendre",
    "bonjour",
    "bonsoir",
    "merci",
    "urgent",
}


def _sanitize_product_candidate(value: Any) -> Optional[str]:
    candidate = _clean_candidate_text(str(value)) if isinstance(value, str) else None
    if not candidate:
        return None
    lowered = candidate.lower()
    if lowered in _GENERIC_PRODUCT_STOPWORDS:
        return None
    if any(tok in lowered for tok in _KNOWN_PRODUCT_KEYWORDS):
        return candidate
    words = lowered.split()
    if len(words) > 6:
        return None
    if _re.search(r"http[s]?://|www\.|@|#", lowered):
        return None
    if not _re.search(r"[a-zàâçéèêëîïôûùüÿñæœ]", lowered):
        return None
    return candidate


def _remap_entities(raw_entities: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise les clés d'entités extraites par le LLM vers les clés canoniques."""

    canonicalized = normalize_slot_keys({
        _ENTITY_KEY_REMAP.get(str(k).lower().strip(), k): v
        for k, v in (raw_entities or {}).items()
    })
    normalized: Dict[str, Any] = {}
    for key, value in canonicalized.items():
        if not slot_has_value(value):
            continue
        if key in {"price", "quantity"}:
            try:
                clean_str = str(value).replace(",", ".").replace(" ", "").replace("\xa0", "")
                normalized[key] = float(clean_str)
            except (ValueError, TypeError):
                logger.warning("Entity '%s' non numérique: %r — ignoré", key, value)
                continue
        elif key == "selection_index":
            try:
                normalized[key] = int(value)
            except (ValueError, TypeError):
                logger.warning("selection_index non entier: %r — ignoré", value)
                continue
        elif key == "product":
            sanitized = _sanitize_product_candidate(value)
            if sanitized:
                normalized[key] = sanitized
        else:
            normalized[key] = str(value).strip() if isinstance(value, str) else value
    return normalized


def _fallback_quantity_unit_from_text(text: str) -> Optional[Dict[str, Any]]:
    """Capture déterministe d'un motif numérique suivi d'une unité standard."""
    result = parse_quantity_unit_from_text(text)
    return result.as_dict() if result else None


__all__ = [
    "_ENTITY_KEY_REMAP",
    "_UNIT_SYNONYMS",
    "_normalize_unit_token",
    "_QUANTITY_UNIT_PATTERN",
    "_GENERIC_PRODUCT_STOPWORDS",
    "_KNOWN_PRODUCT_KEYWORDS",
    "_SUSPICIOUS_PRODUCT_TOKENS",
    "_sanitize_product_candidate",
    "_remap_entities",
    "_fallback_quantity_unit_from_text",
]
