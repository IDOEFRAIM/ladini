from __future__ import annotations

from ._builders import edit, sale

CASES = [
    {"id": "CE1_oui_mais_mets_12", "domain": "cart", "ctx": "cart_has_item", "utt": "oui mais mets 12", "critical": True,
     "expect": {"cart": [("Moussa", 12.0)]}, "scripted": edit({"field": "QUANTITY", "value": 12}, correction=True)},
    {"id": "CE2_ok_mais_enleve_le_lait", "domain": "cart", "ctx": "cart_has_item", "utt": "ok mais enlève le lait", "critical": True,
     "expect": {"cart": []}, "scripted": edit({"field": "REMOVE", "product": "lait"}, correction=True)},
    {"id": "CE3_vas_y_mais_plutot_20", "domain": "cart", "ctx": "cart_has_item", "utt": "vas-y mais plutôt 20", "critical": True,
     "expect": {"cart": [("Moussa", 20.0)]}, "scripted": edit({"field": "QUANTITY", "value": 20}, correction=True)},
    {"id": "CE4_seller_oui_mais_280", "domain": "seller", "ctx": "seller_recap", "utt": "oui mais 280/kg", "critical": True,
     "expect": {"price": 280.0}, "scripted": sale({"price": 280, "price_unit": "kg"})},
]
