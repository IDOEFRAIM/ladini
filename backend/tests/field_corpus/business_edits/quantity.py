"""domain : cart | seller | search. `expect` = champs du snapshot qui changent (le reste doit rester IDENTIQUE) ; `unchanged` = ne RIEN muter est le bon comportement."""
from __future__ import annotations

from ._builders import edit, sale

CASES = [
    {"id": "Q1_mets_20", "domain": "cart", "ctx": "cart_has_item", "utt": "mets 20", "critical": True,
     "expect": {"cart": [("Moussa", 20.0)]}, "scripted": edit({"field": "QUANTITY", "value": 20})},
    {"id": "Q2_mets_20_litres", "domain": "cart", "ctx": "cart_has_item", "utt": "mets 20 litres", "critical": True,
     "expect": {"cart": [("Moussa", 20.0)]}, "scripted": edit({"field": "QUANTITY", "value": 20, "unit": "L"})},
    {"id": "Q3_finalement_8_litres", "domain": "cart", "ctx": "cart_has_item", "utt": "finalement 8 litres", "critical": True,
     "expect": {"cart": [("Moussa", 8.0)]}, "scripted": edit({"field": "QUANTITY", "value": 8, "unit": "L"})},
    {"id": "Q4_non_5", "domain": "cart", "ctx": "cart_has_item", "utt": "non 5", "critical": True,
     "expect": {"cart": [("Moussa", 5.0)]}, "scripted": edit({"field": "QUANTITY", "value": 5}, correction=True)},
    {"id": "Q5_pas_10_cest_15", "domain": "cart", "ctx": "cart_has_item", "utt": "c'est pas 10 c'est 15", "critical": True,
     "expect": {"cart": [("Moussa", 15.0)]}, "scripted": edit({"field": "QUANTITY", "value": 15}, correction=True)},
    {"id": "SQ1_non_250_kg", "domain": "seller", "ctx": "seller_recap", "utt": "non 250 kg", "critical": True,
     "expect": {"quantity": 250.0, "unit": "KG"}, "scripted": sale({"quantity": 250, "unit": "kg"})},
    # lecture FAUSSE du modèle (prix) : l'ancienne valeur nommée (300) désigne la quantité dans l'état courant
    {"id": "SQ2_pas_300_cest_250", "domain": "seller", "ctx": "seller_recap", "utt": "c'est pas 300 c'est 250", "critical": True,
     "expect": {"quantity": 250.0}, "scripted": sale({"price": 250}), "adversarial": True},
    {"id": "SQ3_jai_dit_500_pas_300", "domain": "seller", "ctx": "seller_recap", "utt": "jai dit 500 kg pas 300", "critical": False,
     "expect": {"quantity": 500.0}, "scripted": sale({"quantity": 500, "unit": "kg"})},
    {"id": "SQ4_model_says_price_for_kg", "domain": "seller", "ctx": "seller_recap", "utt": "non 250 kilos", "critical": True,
     "expect": {"quantity": 250.0}, "scripted": sale({"price": 250, "price_unit": "kg"}), "adversarial": True},
]
