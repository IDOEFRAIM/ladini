"""
Unit Normalisation MCP Server (FastMCP).

Converts local/traditional units used in Burkina Faso and West Africa
into standard SI units (kilograms) so downstream agents work with
consistent numbers.

Supported local units:
  tine, plat, sac, botte, boîte, panier, calebasse, bassine, charretée

These conversions depend on the cereal/crop type because a "tine de mil"
weighs differently from a "tine de sorgho".

Tools:
  - convert_to_kg(value, unit, crop)      → ConversionResult
  - list_known_units(crop)                → UnitCatalogPayload
  - normalize_market_quantity(text, crop) → NormalizedQuantity  (NLP helper)
"""

from __future__ import annotations

import json
import re
import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field
from fastmcp import FastMCP, Context

logger = logging.getLogger("MCP.UnitsServer")

# ────────────────────── FastMCP instance ──────────────────────────────────

mcp = FastMCP("AgriConnect Units MCP Server")

# ────────────────────── Conversion tables ─────────────────────────────────

# Unit → approximate weight in KG, per crop category.
# Source: SONAGESS/FAO-BF field studies + trader interviews.
# Keys are lowercase, normalised unit labels.
_GENERIC_KG: Dict[str, float] = {
    "kg":        1.00,
    "kilo":      1.00,
    "tonne":  1_000.00,
    "sac":       100.00,   # standard 100 kg sack
    "demi_sac":   50.00,
    "tine":       17.50,   # 20L fuel can ~17-18 kg for cereals (FAO-BF)
    "plat":        2.50,   # small enamel plate
    "plat_moyen":  5.00,
    "botte":       0.50,   # bundle of leaves / herbs
    "boite":       0.20,   # small tin box (tomato paste size)
    "panier":     12.00,   # medium wicker basket
    "calebasse":   3.50,   # gourd
    "bassine":    20.00,   # 20L basin
    "charrette": 500.00,   # ox-cart load (approx)
    "g":           0.001,
    "gramme":      0.001,
}

# Per-crop overrides (crop_key → unit → kg).  Named everything in lowercase.
_PER_CROP_OVERRIDES: Dict[str, Dict[str, float]] = {
    "mais":       {"tine": 18.0, "sac": 100.0},
    "maïs":       {"tine": 18.0, "sac": 100.0},
    "mil":        {"tine": 17.0, "sac": 100.0},
    "sorgho":     {"tine": 16.5, "sac": 100.0},
    "riz":        {"tine": 19.0, "sac": 50.0},   # white rice, smaller sack
    "niébé":      {"tine": 15.0, "plat": 2.0},
    "arachide":   {"tine": 14.0, "sac": 80.0},
    "coton":      {"sac": 50.0},
    "fonio":      {"tine": 14.0},
    "haricot":    {"tine": 15.0},
}


def _kg_for_unit(unit_raw: str, crop: Optional[str]) -> Optional[float]:
    """Return the kg conversion factor for a unit, with crop-specific overrides."""
    unit = unit_raw.lower().strip().replace(" ", "_").replace("-", "_")
    crop_key = (crop or "").lower().strip()

    crop_table = _PER_CROP_OVERRIDES.get(crop_key, {})
    if unit in crop_table:
        return crop_table[unit]
    return _GENERIC_KG.get(unit)


# ────────────────────── Pydantic models ───────────────────────────────────

class ConversionResult(BaseModel):
    original_value: float
    original_unit: str
    crop: Optional[str] = None
    kg: float
    kg_per_unit: float
    note: Optional[str] = None


class UnitEntry(BaseModel):
    unit: str
    kg_per_unit: float
    source: str = "SONAGESS/FAO-BF"


class UnitCatalogPayload(BaseModel):
    crop: Optional[str] = None
    units: List[UnitEntry] = Field(default_factory=list)


class NormalizedQuantity(BaseModel):
    raw_text: str
    value: Optional[float] = None
    unit: Optional[str] = None
    crop: Optional[str] = None
    kg: Optional[float] = None
    confidence: str = "LOW"  # LOW | MEDIUM | HIGH


# ────────────────────── Tools ─────────────────────────────────────────────

@mcp.tool()
async def convert_to_kg(
    value: float,
    unit: str,
    crop: str = "",
    ctx: Context = None,
) -> str:
    """Convertit une quantité locale (tine, plat, sac…) en kilogrammes.

    Args:
        value: Quantité (ex: 3)
        unit: Unité locale (ex: tine, sac, plat)
        crop: Culture pour affiner la conversion (ex: mil, maïs)
    """
    if ctx:
        await ctx.info(f"convert_to_kg {value} {unit} (crop={crop})")

    factor = _kg_for_unit(unit, crop or None)
    if factor is None:
        raise ValueError(
            f"Unité inconnue: '{unit}'. "
            f"Unités supportées: {sorted(_GENERIC_KG.keys())}"
        )

    kg = round(value * factor, 3)
    note = (
        f"Facteur spécifique à {crop}"
        if crop and _kg_for_unit(unit, crop) != _GENERIC_KG.get(unit.lower().strip())
        else None
    )
    result = ConversionResult(
        original_value=value,
        original_unit=unit,
        crop=crop or None,
        kg=kg,
        kg_per_unit=factor,
        note=note,
    )
    return result.model_dump_json(indent=2)


@mcp.tool()
async def list_known_units(crop: str = "", ctx: Context = None) -> str:
    """Liste toutes les unités connues avec leurs équivalences en kg.

    Args:
        crop: Culture pour afficher les surcharges spécifiques (optionnel)
    """
    if ctx:
        await ctx.info(f"list_known_units crop={crop}")

    crop_key = (crop or "").lower().strip()
    override = _PER_CROP_OVERRIDES.get(crop_key, {})
    merged = {**_GENERIC_KG, **override}
    units = [UnitEntry(unit=u, kg_per_unit=kg) for u, kg in sorted(merged.items())]
    payload = UnitCatalogPayload(crop=crop or None, units=units)
    return payload.model_dump_json(indent=2)


@mcp.tool()
async def normalize_market_quantity(
    text: str,
    crop: str = "",
    ctx: Context = None,
) -> str:
    """Détecte et normalise une quantité depuis du texte libre.

    Ex: '3 tines de mil' → 51 kg

    Args:
        text: Texte libre contenant une quantité locale
        crop: Culture (optionnel, détectée si absente)
    """
    if ctx:
        await ctx.info(f"normalize_market_quantity text='{text[:40]}'")

    text_lower = text.lower()

    # Pattern: <number> <unit> (optionally: de <crop>)
    pattern = re.compile(
        r"(\d+(?:[.,]\d+)?)\s*"
        r"(tine|plat|sac|botte|boite|panier|calebasse|bassine|charrette|kg|kilo|tonne|g|gramme|demi.?sac)",
        re.IGNORECASE,
    )
    match = pattern.search(text_lower)
    if not match:
        return NormalizedQuantity(raw_text=text, confidence="LOW").model_dump_json(indent=2)

    raw_val = float(match.group(1).replace(",", "."))
    raw_unit = match.group(2).lower().replace(" ", "_").replace("-", "_")

    # Extract crop from text if not provided
    detected_crop = crop or None
    if not detected_crop:
        for c_key in _PER_CROP_OVERRIDES:
            if c_key in text_lower:
                detected_crop = c_key
                break

    factor = _kg_for_unit(raw_unit, detected_crop)
    if factor is None:
        return NormalizedQuantity(
            raw_text=text, value=raw_val, unit=raw_unit, crop=detected_crop, confidence="LOW"
        ).model_dump_json(indent=2)

    kg = round(raw_val * factor, 3)
    confidence = "HIGH" if detected_crop and detected_crop in _PER_CROP_OVERRIDES else "MEDIUM"
    return NormalizedQuantity(
        raw_text=text,
        value=raw_val,
        unit=raw_unit,
        crop=detected_crop,
        kg=kg,
        confidence=confidence,
    ).model_dump_json(indent=2)


# ────────────────────── Resources ─────────────────────────────────────────

@mcp.resource("units://catalog")
async def units_catalog_resource() -> str:
    """Full generic unit catalog as an MCP resource."""
    entries = [{"unit": u, "kg": kg} for u, kg in sorted(_GENERIC_KG.items())]
    return json.dumps(entries, ensure_ascii=False, indent=2)


# ────────────────────── Backward-compatible class wrapper ─────────────────

class UnitsMCPServer:
    """Compat wrapper for in-process callers."""

    name = "units"

    @staticmethod
    def list_tools():
        return [
            {"name": "convert_to_kg", "description": "Convertit une quantité locale en kg"},
            {"name": "list_known_units", "description": "Liste les unités connues et leurs équivalences"},
            {"name": "normalize_market_quantity", "description": "Normalise une quantité depuis du texte libre"},
        ]

    @staticmethod
    async def _dispatch(name: str, args: Dict[str, Any]) -> str:
        handlers = {
            "convert_to_kg": convert_to_kg,
            "list_known_units": list_known_units,
            "normalize_market_quantity": normalize_market_quantity,
        }
        fn = handlers.get(name)
        if not fn:
            raise ValueError(f"Unknown units tool: {name}")
        return await fn(**args)

    def call_tool_sync(self, name: str, arguments: dict) -> dict:
        import asyncio
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        raw = loop.run_until_complete(self._dispatch(name, arguments))
        return {"ok": True, "data": json.loads(raw) if isinstance(raw, str) else raw}

    def call_tool(self, name: str, arguments: dict):
        """Alias for call_tool_sync to match old interface."""
        return self.call_tool_sync(name, arguments)


# ────────────────────── Entry point ───────────────────────────────────────

if __name__ == "__main__":
    logger.info("Starting AgriConnect Units MCP Server")
    mcp.run()
