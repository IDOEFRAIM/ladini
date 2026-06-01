from typing import Tuple, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession

from .common import clean_text, positive_float


class UtilsMixin:
    async def normalize_unit(self, session: AsyncSession, quantity: float, unit: str) -> Tuple[float, str, float]:
        quantity = positive_float(quantity, "quantity", allow_zero=True)
        key = (unit).lower().strip()
        UNIT_TO_KG = {
            "sac": 100, "sacs": 100,
            "tine": 18, "tines": 18,
            "plat": 2.5, "plats": 2.5,
            "kg": 1, "kilo": 1, "kilos": 1,
            "tonne": 1000, "tonnes": 1000,
        }
        multiplier = UNIT_TO_KG.get(key, 1.0)
        unit_final = "KG" if key in UNIT_TO_KG else (unit or "").upper()
        qty_kg = quantity * multiplier
        return multiplier, unit_final, qty_kg

   

    async def check_price_anomaly(self, session: AsyncSession, product_name: str, proposed_price: float, zone_id: str) -> Dict[str, Any]:
        product_name = clean_text(product_name, "product_name", required=True)
        proposed_price = positive_float(proposed_price, "proposed_price", allow_zero=True)
        # If a more specific 'get_standard_price' exists on the composed service, use it.
        getter = getattr(self, "get_standard_price", None)
        if callable(getter):
            # The composed implementation expects (session, product_name, zone_id)
            try:
                ref = await getter(session, product_name, zone_id)
            except TypeError:
                # Fallback: the method may be the wrapper that doesn't accept session
                ref = None
        else:
            ref = None
        if not ref:
            return {"is_anomaly": False, "reason": "Pas de prix de référence disponible."}

        ref_price = ref["price_per_unit"]
        ratio = proposed_price / ref_price if ref_price > 0 else 0

        if ratio > 3.0:
            return {
                "is_anomaly": True,
                "level": "HIGH",
                "reason": f"Prix proposé ({proposed_price} FCFA) est {ratio:.1f}x le prix référence ({ref_price} FCFA).",
                "reference_price": ref_price,
            }
        elif ratio < 0.3:
            return {
                "is_anomaly": True,
                "level": "LOW",
                "reason": f"Prix proposé ({proposed_price} FCFA) est anormalement bas vs. référence ({ref_price} FCFA).",
                "reference_price": ref_price,
            }
        return {"is_anomaly": False, "reference_price": ref_price}
