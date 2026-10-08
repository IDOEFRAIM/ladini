from __future__ import annotations

from ._builders import edit

CASES = [
    {"id": "R1_enleve_ca", "domain": "cart", "ctx": "cart_has_item", "utt": "enlève ça du panier", "critical": True,
     "expect": {"cart": []}, "scripted": edit({"field": "REMOVE"})},
    {"id": "R2_retire_le_lait", "domain": "cart", "ctx": "cart_has_item", "utt": "retire le lait", "critical": True,
     "expect": {"cart": []}, "scripted": edit({"field": "REMOVE", "product": "lait"})},
    {"id": "R3_supprime_ce_produit", "domain": "cart", "ctx": "cart_has_item", "utt": "supprime ce produit", "critical": False,
     "expect": {"cart": []}, "scripted": edit({"field": "REMOVE"})},
]
