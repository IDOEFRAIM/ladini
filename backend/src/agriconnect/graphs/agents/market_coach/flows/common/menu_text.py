"""Menu text rendering — pure functions for WhatsApp/AG-UI menu formatting.

All menu string assembly lives here so that flows and services only
build ``MenuOption`` lists and call these helpers for the text output.
No state access, no MCP calls — pure string functions.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from agriconnect.graphs.agents.market_coach.flows.common.menu_contracts import (
    MenuOption,
    MenuRequest,
)


def render_numbered_menu(
    options: Sequence[MenuOption],
    header: Optional[str] = None,
) -> str:
    """Build a numbered menu string for WhatsApp / AG-UI.

    Example output::

        Choisissez une option :
        [1] ✅ Confirmer
        [2] ❌ Annuler
    """
    lines: List[str] = []
    if header:
        lines.append(header)
    for opt in options:
        lines.append(f"[{opt.index}] {opt.label}")
    return "\n".join(lines)


def render_selection_list(
    header: str,
    items: Sequence[Dict[str, Any]],
    line_fn: Any,
) -> str:
    """Build a numbered list from items using a per-item formatting function.

    ``line_fn(index: int, item: dict) -> str`` produces one menu line.
    """
    lines: List[str] = [header]
    for i, item in enumerate(items, start=1):
        lines.append(line_fn(i, item))
    return "\n".join(lines)


def build_selection_menu(
    title: str,
    items: Sequence[Dict[str, Any]],
    kind: str,
    label_fn: Any,
    value_key: str = "id",
    header: Optional[str] = None,
    line_fn: Any = None,
) -> Dict[str, Any]:
    """Build a full selection response dict with mapping + MenuRequest.

    Parameters
    ----------
    title : Menu title for the MenuRequest.
    items : List of dicts to render as options.
    kind : MenuRequest kind (``"farm"``, ``"auction"``, ``"bid"``, …).
    label_fn : ``(item) -> str`` producing the option label.
    value_key : Key in each item dict to use as the mapping value.
    header : Optional WhatsApp header line.
    line_fn : Optional ``(index, item) -> str`` for custom line rendering.
              If omitted, uses ``"{index}. *{label}*"`` format.
    """
    mapping: Dict[str, str] = {}
    candidates: List[str] = []
    lines: List[str] = [header] if header else [f"*{title}*"]

    for i, item in enumerate(items, start=1):
        label = label_fn(item)
        candidates.append(label)
        item_id = item.get(value_key) or item.get("id") or ""
        mapping[str(i)] = str(item_id)
        if line_fn:
            lines.append(line_fn(i, item))
        else:
            lines.append(f"{i}. *{label}*")

    menu_text = "\n".join(lines)
    return {
        "menu_text": menu_text,
        "mapping": mapping,
        "candidates": candidates,
        "menu_request": MenuRequest(
            title=title,
            options=[
                MenuOption(index=str(i), label=c, value=mapping.get(str(i)))
                for i, c in enumerate(candidates, start=1)
            ],
            kind=kind,
            preformatted_text=menu_text,
        ),
    }


def render_cart_actions_hint(has_auction_items: bool = False) -> str:
    """Standard cart actions footer."""
    hint = "\n_Répondez *précommander* pour valider, ou ajoutez un autre produit._"
    if has_auction_items:
        hint += (
            "\n_Pour les articles en enchère : *négocier* pour faire une contre-offre._"
        )
    return hint


# =====================================================================
# HARMONISED UX HELPERS — one consistent voice across every buyer menu
# =====================================================================


def render_selection_prompt(
    *,
    noun: str = "choix",
    allow_cancel: bool = True,
) -> str:
    """Consistent 'reply with a number' prompt used by every selection menu.

    Keeps a single wording everywhere (vendor picker, order list, auction list,
    bids…) so the buyer always knows exactly how to answer.
    """
    tail = ", ou *annuler* pour quitter" if allow_cancel else ""
    return f"\n_Répondez avec le *numéro* de votre {noun}{tail}._"


def render_quick_actions(actions: Sequence[str]) -> str:
    """Render a compact 'what can I do next' footer from short action phrases.

    Example::

        💡 _Vous pouvez :_ *mon panier* · *précommander* · *mes commandes*
    """
    cleaned = [str(a).strip() for a in actions if str(a).strip()]
    if not cleaned:
        return ""
    bullets = " · ".join(f"*{a}*" for a in cleaned)
    return f"\n💡 _Vous pouvez :_ {bullets}"


__all__ = [
    "render_numbered_menu",
    "render_selection_list",
    "build_selection_menu",
    "render_cart_actions_hint",
    "render_selection_prompt",
    "render_quick_actions",
]
