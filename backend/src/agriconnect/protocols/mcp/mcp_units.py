"""Unit Normalisation MCP Tool (async).

Converts local/traditional units used in Burkina Faso and West Africa
into standard SI units (kilograms) so downstream agents work with
consistent numbers.

Supported local units:
  tine, plat, sac, botte, boîte, panier, calebasse, bassine, charretée

These conversions depend on the cereal/crop type because a "tine de mil"
weighs differently from a "tine de sorgho".

Tools exposed:
  - convert_to_kg(value, unit, crop)      → ConversionResult
  - list_known_units(crop)                → UnitCatalogPayload
  - normalize_market_quantity(text, crop) → NormalizedQuantity  (NLP hint helper)
"""

from __future__ import annotations

import re
import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

from .servers.base import AsyncMCPServer

logger = logging.getLogger("MCP.UnitsServer")


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


# ────────────────────── Server ────────────────────────────────────────────

class UnitsMCPServer(AsyncMCPServer):
    """Async MCP server for West-African agricultural unit conversions."""

    name = "units"

    def _register_tools(self) -> None:
        self.register(
            name="convert_to_kg",
            description="Convertit une quantité locale (tine, plat, sac…) en kilogrammes",
            input_schema={
                "type": "object",
                "properties": {
                    "value": {"type": "number", "description": "Quantité (ex: 3)"},
                    "unit": {"type": "string", "description": "Unité locale (ex: tine, sac, plat)"},
                    "crop": {"type": "string", "description": "Culture pour affiner la conversion (ex: mil, maïs)"},
                },
                "required": ["value", "unit"],
            },
            handler=self._convert_to_kg,
        )
        self.register(
            name="list_known_units",
            description="Liste toutes les unités connues avec leurs équivalences en kg",
            input_schema={
                "type": "object",
                "properties": {"crop": {"type": "string"}},
            },
            handler=self._list_known_units,
        )
        self.register(
            name="normalize_market_quantity",
            description=(
                "Détecte et normalise une quantité depuis du texte libre "
                "(ex: '3 tines de mil' → 51 kg)"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "crop": {"type": "string"},
                },
                "required": ["text"],
            },
            handler=self._normalize_market_quantity,
        )

    # ── Handlers ──────────────────────────────────────────────────────────

    async def _convert_to_kg(self, args: Dict[str, Any]) -> ConversionResult:
        value = float(args["value"])
        unit = args["unit"]
        crop = args.get("crop")
        factor = _kg_for_unit(unit, crop)
        if factor is None:
            raise ValueError(
                f"Unité inconnue: '{unit}'. "
                f"Unités supportées: {sorted(_GENERIC_KG.keys())}"
            )
        kg = round(value * factor, 3)
        note = f"Facteur spécifique à {crop}" if crop and _kg_for_unit(unit, crop) != _GENERIC_KG.get(unit) else None
        return ConversionResult(
            original_value=value,
            original_unit=unit,
            crop=crop,
            kg=kg,
            kg_per_unit=factor,
            note=note,
        )

    async def _list_known_units(self, args: Dict[str, Any]) -> UnitCatalogPayload:
        crop = args.get("crop")
        crop_key = (crop or "").lower().strip()
        override = _PER_CROP_OVERRIDES.get(crop_key, {})
        merged = {**_GENERIC_KG, **override}
        units = [UnitEntry(unit=u, kg_per_unit=kg) for u, kg in sorted(merged.items())]
        return UnitCatalogPayload(crop=crop, units=units)

    async def _normalize_market_quantity(self, args: Dict[str, Any]) -> NormalizedQuantity:
        text: str = args["text"]
        crop: Optional[str] = args.get("crop")
        text_lower = text.lower()

        # Pattern: <number> <unit> (optionally: de <crop>)
        pattern = re.compile(
            r"(\d+(?:[.,]\d+)?)\s*"
            r"(tine|plat|sac|botte|boite|panier|calebasse|bassine|charrette|kg|kilo|tonne|g|gramme|demi.?sac)",
            re.IGNORECASE,
        )
        match = pattern.search(text_lower)
        if not match:
            return NormalizedQuantity(raw_text=text, confidence="LOW")

        raw_val = float(match.group(1).replace(",", "."))
        raw_unit = match.group(2).lower().replace(" ", "_").replace("-", "_")

        # Extract crop from text if not provided
        detected_crop = crop
        if not detected_crop:
            for c_key in _PER_CROP_OVERRIDES:
                if c_key in text_lower:
                    detected_crop = c_key
                    break

        factor = _kg_for_unit(raw_unit, detected_crop)
        if factor is None:
            return NormalizedQuantity(raw_text=text, value=raw_val, unit=raw_unit, crop=detected_crop, confidence="LOW")

        kg = round(raw_val * factor, 3)
        confidence = "HIGH" if detected_crop and detected_crop in _PER_CROP_OVERRIDES else "MEDIUM"
        return NormalizedQuantity(
            raw_text=text,
            value=raw_val,
            unit=raw_unit,
            crop=detected_crop,
            kg=kg,
            confidence=confidence,
        )


if __name__ == "__main__":
    import asyncio, json

    server = UnitsMCPServer()
    print("Tools:", [t["name"] for t in server.list_tools()])
    result = asyncio.run(server.call_tool("convert_to_kg", {"value": 3, "unit": "tine", "crop": "mil"}))
    print(json.dumps(result.model_dump(), indent=2, ensure_ascii=False))

    result2 = asyncio.run(server.call_tool("normalize_market_quantity", {"text": "j'ai 10 sacs de maïs", "crop": "maïs"}))
    print(json.dumps(result2.model_dump(), indent=2, ensure_ascii=False))
