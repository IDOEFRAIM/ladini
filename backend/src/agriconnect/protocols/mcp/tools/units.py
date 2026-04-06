from __future__ import annotations

import json
from typing import Any, Dict, List

from agriconnect.infrastructure.mcp.base import MCPToolSpec

_GENERIC_KG: Dict[str, float] = {
    "kg": 1.0,
    "kilo": 1.0,
    "tonne": 1000.0,
    "sac": 100.0,
    "demi_sac": 50.0,
    "tine": 17.5,
    "plat": 2.5,
    "plat_moyen": 5.0,
    "botte": 0.5,
    "boite": 0.2,
    "panier": 12.0,
    "calebasse": 3.5,
    "bassine": 20.0,
    "charrette": 500.0,
    "g": 0.001,
    "gramme": 0.001,
}


class UnitsTools:
    name = "units"

    @staticmethod
    def _norm(unit: str) -> str:
        return (unit or "").strip().lower().replace("-", "_").replace(" ", "_")

    async def convert_to_kg(self, value: float, unit: str, crop: str = "", ctx=None) -> str:
        if ctx:
            await ctx.info(f"convert_to_kg {value} {unit}")
        factor = _GENERIC_KG.get(self._norm(unit))
        if factor is None:
            raise ValueError(f"Unite inconnue: {unit}")
        return json.dumps(
            {
                "original_value": value,
                "original_unit": unit,
                "crop": crop or None,
                "kg": round(value * factor, 3),
                "kg_per_unit": factor,
            },
            ensure_ascii=False,
        )

    async def list_known_units(self, crop: str = "", ctx=None) -> str:
        if ctx:
            await ctx.info("list_known_units")
        return json.dumps({"crop": crop or None, "units": _GENERIC_KG}, ensure_ascii=False)

    def get_tools(self) -> List[MCPToolSpec]:
        return [
            MCPToolSpec(name="convert_to_kg", handler=self.convert_to_kg),
            MCPToolSpec(name="list_known_units", handler=self.list_known_units),
        ]

    async def ping(self) -> Dict[str, Any]:
        return {"status": "ready"}
