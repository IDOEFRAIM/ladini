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

from typing import List

MAX_ITEMS_PER_PAGE = 5
MAX_CHARS_PER_PAGE = 1200
PAGE_BREAK = "\f"


def paginate_item_blocks(
    header: str,
    item_blocks: List[str],
    *,
    max_items: int = MAX_ITEMS_PER_PAGE,
    max_chars: int = MAX_CHARS_PER_PAGE,
) -> str:
    """Regroupe `item_blocks` en pages d'au plus `max_items` éléments,
    chacune bornée à `max_chars` caractères (header + éléments compris).
    Les pages sont jointes par `PAGE_BREAK`. Sans éléments, renvoie
    simplement `header`."""
    if not item_blocks:
        return header.strip()

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
        if total > 1:
            suffix = " ➡️ *(suite ci-dessous)*" if i < total else ""
            parts.append(f"_Page {i}/{total}_{suffix}")
        rendered.append("\n".join(parts))
    return PAGE_BREAK.join(rendered)


__all__ = ["PAGE_BREAK", "MAX_ITEMS_PER_PAGE", "MAX_CHARS_PER_PAGE", "paginate_item_blocks"]
