"""Faits VISIBLES d'un menu générique (enchères, offres reçues, stocks…) -> options résolubles par `selection_reference`.

Chaque menu migré publie, par option, les faits que l'utilisateur a LUS (`MenuOption.facts`) ; `ui_engine` les fige dans
`working_memory["menu_facts"]` avec l'horodatage d'affichage. Ce module les convertit en `VisibleOption` (alias de clés tolérés :
un flow dit `product`, un autre `product_name`) et dit si l'instantané est PÉRIMÉ — une référence naturelle ne se résout jamais
contre un menu périmé (même règle de péremption que les menus récurrents, `context_arbitration._RECURRING_MENU_TTL_SECONDS`).
"""

from __future__ import annotations

from typing import Any, List, Mapping, Optional

from ladini.graphs.agents.market_coach.domain.selection_reference import VisibleOption

#: Au-delà, la liste affichée n'est plus fiable : on ne devine pas sur un menu ancien (la valeur est volontairement alignée sur
#: la péremption des menus récurrents : 10 minutes).
MENU_FACTS_TTL_SECONDS = 600.0

_NAME_KEYS = ("name", "producer", "producer_name", "vendor_name", "buyer_name", "product", "product_name")
_PRICE_KEYS = ("price", "offer_price", "unit_price", "max_price", "amount")
_QTY_KEYS = ("quantity", "available_qty", "stock", "qty")
_REGION_KEYS = ("region", "zone", "zone_name")
_DAY_KEYS = ("day", "date", "created_at", "launched_at", "starts_at", "delivery_date")


def _first(facts: Mapping[str, Any], keys: tuple) -> Any:
    for key in keys:
        value = facts.get(key)
        if value not in (None, ""):
            return value
    return None


def _float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def is_stale(menu_facts: Mapping[str, Any], now: float) -> bool:
    created = _float(menu_facts.get("created_at"))
    return created is None or (now - created) > MENU_FACTS_TTL_SECONDS


def visible_options(menu_facts: Mapping[str, Any]) -> List[VisibleOption]:
    """Options visibles d'un instantané `menu_facts` (ordre et index tels qu'AFFICHÉS)."""
    out: List[VisibleOption] = []
    for position, option in enumerate(menu_facts.get("options") or [], start=1):
        if not isinstance(option, Mapping):
            continue
        raw_facts = option.get("facts")
        facts: Mapping[str, Any] = raw_facts if isinstance(raw_facts, Mapping) else {}
        try:
            index = int(str(option.get("index")))
        except (TypeError, ValueError):
            index = position
        day = _first(facts, _DAY_KEYS)
        out.append(
            VisibleOption(
                index=index,
                entity_id=str(option.get("entity_id") or ""),
                name=str(_first(facts, _NAME_KEYS) or option.get("label") or ""),
                region=str(_first(facts, _REGION_KEYS) or ""),
                price=_float(_first(facts, _PRICE_KEYS)),
                unit=str(facts.get("unit") or "").upper(),
                availability=_float(_first(facts, _QTY_KEYS)),
                price_label=str(facts.get("price_label") or ""),
                day=str(day)[:10] if day else None,
            )
        )
    return out


__all__ = ["MENU_FACTS_TTL_SECONDS", "is_stale", "visible_options"]
