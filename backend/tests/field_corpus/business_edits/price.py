from __future__ import annotations

from ._builders import sale

CASES = [
    {"id": "P1_cest_350_le_prix", "domain": "seller", "ctx": "seller_recap", "utt": "ah non c 350 le prix", "critical": True,
     "expect": {"price": 350.0}, "scripted": sale({"price": 350, "price_unit": "kg"})},
    {"id": "P2_plutot_280_francs", "domain": "seller", "ctx": "seller_recap", "utt": "plutôt 280 francs", "critical": True,
     "expect": {"price": 280.0}, "scripted": sale({"price": 280})},
    {"id": "P3_plutot_280_kg", "domain": "seller", "ctx": "seller_recap", "utt": "plutôt 280/kg", "critical": True,
     "expect": {"price": 280.0}, "scripted": sale({"price": 280, "price_unit": "kg"})},
    {"id": "P4_model_says_quantity_for_francs", "domain": "seller", "ctx": "seller_recap", "utt": "plutôt 280 francs le kg", "critical": True,
     "expect": {"price": 280.0}, "scripted": sale({"quantity": 280}), "adversarial": True},
    {"id": "P5_product_correction", "domain": "seller", "ctx": "seller_recap", "utt": "en fait oignon pas tomate", "critical": True,
     "expect": {"product": "oignon", "price": None}, "scripted": sale({"product": "oignon"})},
]
