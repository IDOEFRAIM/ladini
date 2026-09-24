"""Identité canonique d'un nom de produit — UNE seule fonction pour tout le backend.

Deux libellés désignent le même produit si leurs clés sont égales : casse, accents,
ligatures (bœuf/boeuf, chèvre/chevre), pluriel simple (chèvres/chèvre) et ponctuation
sont neutralisés. Extrait tel quel de `services/database/auction.py::_normalize_product_label`
(comportement inchangé) pour être partagé par le service ET le moteur conversationnel
(ex. désigner l'item corrigé dans un draft multi-produits) au lieu de listes d'alias
dispersées dans les flows.
"""
from __future__ import annotations

import unicodedata
from typing import Any, List


def canonical_product_key(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip().lower()
    if not text:
        return ""
    text = text.replace("œ", "oe").replace("æ", "ae")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    tokens: List[str] = []
    for token in text.split():
        cleaned = "".join(ch for ch in token if ch.isalnum())
        if len(cleaned) > 3 and cleaned.endswith("s"):
            cleaned = cleaned[:-1]
        tokens.append(cleaned)
    return " ".join(t for t in tokens if t)


def same_product(a: Any, b: Any) -> bool:
    key_a, key_b = canonical_product_key(a), canonical_product_key(b)
    return bool(key_a) and key_a == key_b


__all__ = ["canonical_product_key", "same_product"]
