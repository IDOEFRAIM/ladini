"""Pagination de listes WhatsApp volumineuses — extrait de
``nodes/rendering/success.py`` pour être réutilisable par les couches
domaine (ex: ``services/domain/cart_service.py``) sans dépendre du module
de rendu (pure logique de formatage texte, aucune dépendance lourde).

Incident 2026-08-27 : une liste (stock, catalogue producteur, recherche
produit) produisait un texte non borné, découpé à l'aveugle par caractère
par ``api/tasks.py::_chunk_whatsapp_body`` — coupant parfois un élément en
plein milieu, et au-delà de 4 messages TRONQUAIT le reste avec un
disclaimer générique (perte silencieuse de données). ``PAGE_BREAK`` force
un découpage NET entre pages (jamais au milieu d'un élément) ; consommé et
retiré par ``_chunk_whatsapp_body``.
"""

from __future__ import annotations

from typing import List, Optional

MAX_ITEMS_PER_PAGE = 5
MAX_CHARS_PER_PAGE = 1200
PAGE_BREAK = "\f"


def paginate_item_blocks(
    header: str,
    item_blocks: List[str],
    *,
    max_items: int = MAX_ITEMS_PER_PAGE,
    max_chars: int = MAX_CHARS_PER_PAGE,
    footer_blocks: Optional[List[str]] = None,
) -> str:
    """Regroupe `item_blocks` en pages d'au plus `max_items` éléments,
    chacune bornée à `max_chars` caractères (header + éléments compris).
    Les pages sont jointes par `PAGE_BREAK`. Sans éléments, renvoie
    simplement `header`.

    `footer_blocks` (invite « répondez avec le numéro », astuce photos, conseil
    d'appel d'offres…) ne sont PAS des éléments : ils s'attachent à la DERNIÈRE
    page sans compter dans `max_items`. Incident réel (2026-09-28) : 3 offres +
    2 pieds de page remplissaient la page 1 (5 « éléments »), et le 3ᵉ pied
    atterrissait seul sur une « Page 2/2 » sans aucune option. Les index des
    options sont écrits dans chaque bloc élément (`*3.* …`) et ne dépendent donc
    jamais de la pagination."""
    footers = [f for f in (footer_blocks or []) if f]
    if not item_blocks:
        return "\n".join([header.strip(), *footers]).strip()

    pages: List[List[str]] = [[]]
    for block in item_blocks:
        current = pages[-1]
        projected = current + [block]
        projected_len = len(header) + 1 + sum(len(b) + 1 for b in projected)
        if current and (len(current) >= max_items or projected_len > max_chars):
            pages.append([block])
        else:
            pages[-1] = projected

    total = len(pages)
    rendered: List[str] = []
    for i, items in enumerate(pages, start=1):
        parts = [header, *items]
        if i == total:
            parts.extend(footers)
        if total > 1:
            suffix = " ➡️ *(suite ci-dessous)*" if i < total else ""
            parts.append(f"_Page {i}/{total}_{suffix}")
        rendered.append("\n".join(parts))
    return PAGE_BREAK.join(rendered)


__all__ = ["PAGE_BREAK", "MAX_ITEMS_PER_PAGE", "MAX_CHARS_PER_PAGE", "paginate_item_blocks"]
