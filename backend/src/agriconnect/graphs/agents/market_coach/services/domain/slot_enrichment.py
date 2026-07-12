"""Slot enrichment — text-based extraction of structured slot values.

Centralises all heuristic and LLM-based extraction that was previously
scattered across validator.py.  Called by the SlotResolver (memory_update)
as the single enrichment pass before validation.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from agriconnect.core.logging import get_logger
from agriconnect.graphs.agents.market_coach.services.domain.quantity_unit import (
    parse_quantity_unit_from_text as _parse_qty_unit,
    extract_unit_only_from_text as _extract_unit_only,
    is_livestock_product as _is_livestock_product,
)

logger = get_logger("AgriConnect.MarketCoach.SlotEnrichment")

_PRODUCTION_TYPE_SYNONYMS = {
    "livestock": "LIVESTOCK",
    "elevage": "LIVESTOCK",
    "élevage": "LIVESTOCK",
    "betail": "LIVESTOCK",
    "bétail": "LIVESTOCK",
    "volaille": "LIVESTOCK",
    "poulet": "LIVESTOCK",
    "poussins": "LIVESTOCK",
    "culture": "CROP",
    "cultures": "CROP",
    "agriculture": "CROP",
    "champ": "CROP",
    "plantation": "CROP",
}


def extract_quantity_unit_from_text(text: str) -> Optional[Dict[str, Any]]:
    parsed = _parse_qty_unit(text or "")
    if not parsed:
        return None
    result = parsed.as_dict()
    return result if result else None


def extract_unit_only(text: str) -> Optional[str]:
    return _extract_unit_only(text) if text else None


def extract_production_type_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    lowered = text.lower()
    for token, value in _PRODUCTION_TYPE_SYNONYMS.items():
        if token in lowered:
            return value
    stripped = lowered.strip().upper()
    if stripped in {"CROP", "LIVESTOCK"}:
        return stripped
    return None


def extract_surface_from_text(text: str) -> Optional[float]:
    if not text:
        return None
    match = re.search(r"(\d+[\d\s,.]*)\s*(ha|hectare|hectares|m2|m²)", text, re.IGNORECASE)
    if not match:
        return None
    raw_value = match.group(1).replace(" ", "").replace(",", ".")
    unit = match.group(2).lower()
    try:
        numeric = float(raw_value)
    except (TypeError, ValueError):
        return None
    if unit in {"m2", "m²"}:
        return numeric / 10000.0
    return numeric


def extract_future_datetime_from_text(text: str) -> Optional[str]:
    if not text:
        return None

    absolute = re.search(r"(\d{4}-\d{2}-\d{2}(?:[tT ]\d{2}:\d{2}(?::\d{2})?)?)", text)
    if absolute:
        candidate = absolute.group(1).replace(" ", "T")
        try:
            dt = datetime.fromisoformat(candidate)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except ValueError:
            pass

    now = datetime.utcnow()
    month_match = re.search(r"dans\s+(\d+)\s*mois", text, re.IGNORECASE)
    if month_match:
        months = int(month_match.group(1))
        return (now + timedelta(days=30 * months)).isoformat()

    week_match = re.search(r"dans\s+(\d+)\s*semaines?", text, re.IGNORECASE)
    if week_match:
        weeks = int(week_match.group(1))
        return (now + timedelta(weeks=weeks)).isoformat()

    day_match = re.search(r"dans\s+(\d+)\s*jours?", text, re.IGNORECASE)
    if day_match:
        days = int(day_match.group(1))
        return (now + timedelta(days=days)).isoformat()

    return None


def _contains_quantitative_hint(text: str) -> bool:
    return bool(re.search(r"\d", text or ""))


async def llm_extract_quantity_unit(
    mc_runtime: Any,
    user_text: str,
) -> Optional[Dict[str, Any]]:
    if not user_text:
        return None
    llm = getattr(mc_runtime, "llm", None)
    if llm is None:
        return None

    prompt = (
        "Tu extrais des entités de commande agricole. "
        "Réponds strictement un JSON avec les clés: "
        "quantity (number|null), unit (KG|TONNE|SAC|PANIER|null), "
        "product (string|null). "
        "Ne retourne rien d'autre."
    )

    try:
        completion = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: llm.chat.completions.create(
                    model=getattr(mc_runtime, "model_answer", "llama-3.3-70b-versatile"),
                    messages=[
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": user_text},
                    ],
                    response_format={"type": "json_object"},
                    temperature=0.0,
                    max_tokens=90,
                )
            ),
            timeout=10.0,
        )
        parsed = json.loads(completion.choices[0].message.content or "{}")
        out: Dict[str, Any] = {}
        quantity = parsed.get("quantity")
        unit = parsed.get("unit")
        product = parsed.get("product")

        if quantity not in (None, "", [], {}):
            try:
                out["quantity"] = float(quantity)
            except (TypeError, ValueError):
                pass

        if unit not in (None, "", [], {}):
            unit_norm = str(unit).upper().strip()
            if unit_norm in {"KG", "TONNE", "SAC", "PANIER"}:
                out["unit"] = unit_norm

        if product not in (None, "", [], {}):
            out["product"] = str(product).strip()

        return out or None
    except asyncio.TimeoutError:
        logger.warning("SLOT_ENRICHMENT_LLM_TIMEOUT | user_text=%r", (user_text or "")[:160])
        return None
    except Exception as exc:
        logger.warning("SLOT_ENRICHMENT_LLM_ERROR | error=%s", exc)
        return None


def _needs_structured_extraction(payload: Dict[str, Any], fields: tuple) -> bool:
    return any(payload.get(field) in (None, "", 0, [], {}) for field in fields)


async def enrich_payload_from_text(
    payload: Dict[str, Any],
    text: str,
    goal: Optional[str],
    mc_runtime: Any,
) -> Dict[str, Any]:
    """Single entry point for all text-based slot enrichment.

    Applies deterministic regex extraction first, then LLM extraction
    as fallback for critical goals. Mutates and returns ``payload``.
    """
    goal_upper = str(goal or "").upper()

    if goal_upper == "BUYER_ADD_TO_CART":
        extracted = extract_quantity_unit_from_text(text)
        if extracted:
            payload.update({k: v for k, v in extracted.items() if v not in (None, "", 0, [], {})})

        product_str = str(payload.get("product") or "")
        product_is_dirty = _contains_quantitative_hint(product_str) or any(
            u in product_str.lower() for u in ["kg", "tonne", "sac", "panier"]
        )

        if _needs_structured_extraction(payload, ("quantity", "unit")) or product_is_dirty:
            llm_extracted = await llm_extract_quantity_unit(mc_runtime, text)
            if llm_extracted:
                payload.update({k: v for k, v in llm_extracted.items() if v not in (None, "", 0, [], {})})

    if payload.get("unit") in (None, "", [], {}) and text:
        unit_from_text = extract_unit_only(text)
        if unit_from_text:
            payload["unit"] = unit_from_text

    # Livestock (poussins, moutons, bœufs…) are counted per head, never weighed.
    # Default their unit to TÊTE so it isn't silently coerced to KG downstream.
    if payload.get("unit") in (None, "", [], {}) and _is_livestock_product(payload.get("product")):
        payload["unit"] = "TETE"

    if payload.get("surface") in (None, "", [], {}) and text:
        surface_value = extract_surface_from_text(text)
        if surface_value is not None:
            payload["surface"] = surface_value

    if payload.get("estimated_available_at") in (None, "", [], {}) and text:
        estimated = extract_future_datetime_from_text(text)
        if estimated:
            payload["estimated_available_at"] = estimated

    production_type_value = payload.get("production_type")
    if isinstance(production_type_value, str) and production_type_value.strip():
        candidate = production_type_value.strip().upper()
        if candidate in {"CROP", "LIVESTOCK"}:
            payload["production_type"] = candidate
        else:
            payload["production_type"] = None
    if payload.get("production_type") in (None, "", [], {}) and text:
        extracted_type = extract_production_type_from_text(text)
        if extracted_type:
            payload["production_type"] = extracted_type

    return payload


__all__ = [
    "enrich_payload_from_text",
    "extract_quantity_unit_from_text",
    "extract_unit_only",
    "extract_production_type_from_text",
    "extract_surface_from_text",
    "extract_future_datetime_from_text",
    "llm_extract_quantity_unit",
]
