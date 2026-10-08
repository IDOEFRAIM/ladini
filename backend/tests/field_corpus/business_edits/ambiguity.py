from __future__ import annotations

from ._builders import sale

CASES = [
    {"id": "A1_bare_number_two_fields", "domain": "seller", "ctx": "seller_recap", "utt": "non 280", "critical": True,
     "unchanged": True, "scripted": sale({"price": 280})},
    {"id": "A2_equal_values_old_ambiguous", "domain": "seller", "ctx": "seller_recap_equal", "utt": "non pas 500, 300", "critical": True,
     "unchanged": True, "scripted": sale({"price": 300})},
    # signaux contradictoires proposés par le modèle (quantité ET prix pour « 250 kg ») : rien ne doit muter de façon incohérente
    {"id": "A3_conflicting_cues", "domain": "seller", "ctx": "seller_recap", "utt": "non 250 kg", "critical": True,
     "unchanged": True, "scripted": sale({"price": 250, "quantity": 300, "unit": "kg"}), "adversarial": True, "scripted_only": True},
]
