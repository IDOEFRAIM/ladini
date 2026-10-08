"""Catalogue du harnais : Sawadogo 700, Moussa 450, GARIKO 1200, Gilbert 500 (+ paliers sachet)."""
from __future__ import annotations

from ._builders import objection_price, refine

CASES = [
    {"id": "K1_max_500", "domain": "search", "ctx": "producer_menu", "utt": "max 500", "critical": True,
     "expect": {"n_vendors": 2}, "scripted": refine(max_price=500)},
    {"id": "K2_finalement_800", "domain": "search", "ctx": "producer_menu", "pre": ["max 500"], "utt": "finalement 800", "critical": True,
     "expect": {"n_vendors": 3}, "scripted": refine(max_price=800)},
    {"id": "K3_prix_nimporte_plus", "domain": "search", "ctx": "producer_menu", "pre": ["c'est trop cher", "500"],
     "utt": "finalement le prix n'importe plus", "critical": True,
     "expect": {"n_vendors": 4}, "scripted": refine(remove=["max_price"])},
    {"id": "K4_trop_cher_valueless", "domain": "search", "ctx": "producer_menu", "utt": "c'est trop cher", "critical": True,
     "unchanged": True, "scripted": objection_price()},
    {"id": "K5_pending_ceiling_number", "domain": "search", "ctx": "producer_menu", "pre": ["c'est trop cher"], "utt": "500", "critical": True,
     "expect": {"n_vendors": 2}, "scripted": refine(max_price=500)},
    {"id": "K6_plutot_en_sachet", "domain": "search", "ctx": "producer_menu", "utt": "plutôt en sachet", "critical": False,
     "expect": {"n_vendors": 1}, "scripted": refine(packaging="sachet")},
]
