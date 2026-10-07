"""FIELD CORPUS — langage réel du terrain (buyer). Un cas = un contexte métier + UNE phrase + les INVARIANTS attendus.

Catégories : A ellipse · B anaphore · C correction · D rejet partiel · E comparaison · F question de contexte · G changement de sujet ·
H localité · I français informel / fautes · J réponses courtes / nombres ambigus · K rejet+alternative / multi-action.

`expect` (invariants, jamais un texte exact) :
  chosen            producteur choisi APRÈS le tour (None = aucun)
  cart              panier attendu [(vendeur, quantité)]
  slots             sous-ensemble du payload métier
  preserved         l'état métier est IDENTIQUE avant/après (une question ne détruit rien)
  vendors           nombre d'offres encore proposées (affinage)
  has               sous-chaînes (insensible à la casse) que la réponse DOIT contenir (toutes)
  any_of            au moins UNE de ces sous-chaînes
  not_has           sous-chaînes interdites
  no_redisplay      la réponse n'est pas le menu complet rejoué à l'aveugle
`scripted` : ce que le MODÈLE doit comprendre de la phrase (contrat structuré) — utilisé par le mode scripté (CI) ; le vrai modèle l'ignore.
"""
from __future__ import annotations

from typing import Any, Dict, List

REF = lambda **kw: {"reference": kw}  # noqa: E731
ACTION = lambda action, **kw: {"disposition": "ACTION", "action": action, **kw}  # noqa: E731

CASES: List[Dict[str, Any]] = [
    # ── A. ellipse ────────────────────────────────────────────────────────────────────────────────────────────────────
    {"id": "A1_quantity_only", "ctx": "quantity_slot", "utt": "10 seulement", "relation": "ANSWER",
     "expect": {"cart": [("Moussa", 10.0)]}},
    {"id": "A2_package_only", "ctx": "producer_menu", "utt": "en sachet", "relation": "REFINEMENT",
     "expect": {"vendors": 1, "cart": [], "chosen": None}},
    {"id": "A3_region_only", "ctx": "producer_menu", "utt": "à Ouaga", "relation": "REFINEMENT",
     "expect": {"vendors": 3, "cart": [], "chosen": None}},
    {"id": "A4_cheapest", "ctx": "producer_menu", "utt": "le moins cher", "relation": "SELECTION",
     "expect": {"chosen": "Moussa"}},
    # ── B. anaphore ───────────────────────────────────────────────────────────────────────────────────────────────────
    {"id": "B1_him_without_focus", "ctx": "producer_menu", "utt": "lui", "relation": "AMBIGUOUS",
     "expect": {"chosen": None, "cart": [], "no_redisplay": True}},
    {"id": "B2_that_one_without_focus", "ctx": "producer_menu", "utt": "celui-là", "relation": "AMBIGUOUS",
     "expect": {"chosen": None, "cart": [], "no_redisplay": True}},
    {"id": "B3_the_other_with_many", "ctx": "producer_menu", "utt": "pas celui-là, l'autre", "relation": "AMBIGUOUS",
     "expect": {"chosen": None, "cart": [], "no_redisplay": True}},
    # ── C. corrections ────────────────────────────────────────────────────────────────────────────────────────────────
    {"id": "C1_quantity_correction", "ctx": "cart_has_item", "utt": "je voulais dire 20 pas 10", "relation": "CORRECTION",
     "expect": {"cart": [("Moussa", 20.0)]}},
    {"id": "C2_short_correction", "ctx": "cart_has_item", "utt": "non 5", "relation": "CORRECTION",
     "expect": {"cart": [("Moussa", 5.0)]}},
    {"id": "C3_not_gilbert_moussa", "ctx": "tier_menu", "utt": "pas Gilbert, Moussa", "relation": "REJECTION+ALTERNATIVE",
     "expect": {"chosen": "Moussa"}},
    # ── C bis. BUSINESS EDITS : corrections de la ligne du panier (le modèle interprète, le DOMAINE exécute) ───────────────
    {"id": "C4_make_it_20", "ctx": "cart_has_item", "utt": "mets 20 plutôt", "relation": "CORRECTION",
     "expect": {"cart": [("Moussa", 20.0)]}},
    {"id": "C5_actually_8", "ctx": "cart_has_item", "utt": "finalement 8 litres", "relation": "CORRECTION",
     "expect": {"cart": [("Moussa", 8.0)]}},
    {"id": "C6_not_10_but_15", "ctx": "cart_has_item", "utt": "c pas 10 c 15", "relation": "CORRECTION",
     "expect": {"cart": [("Moussa", 15.0)]}},
    {"id": "C7_remove_line", "ctx": "cart_has_item", "utt": "enlève ça du panier", "relation": "CORRECTION",
     "expect": {"cart": []}},
    {"id": "C8_zero_is_not_a_quantity", "ctx": "cart_has_item", "utt": "mets 0", "relation": "CORRECTION",
     "expect": {"cart": [("Moussa", 10.0)]}},
    {"id": "C9_negative_is_refused", "ctx": "cart_has_item", "utt": "mets -5", "relation": "CORRECTION",
     "expect": {"cart": [("Moussa", 10.0)]}},
    {"id": "D4_price_no_longer_matters", "ctx": "producer_menu", "pre": ["c'est trop cher", "500"], "utt": "finalement le prix n'importe plus",
     "relation": "REFINEMENT", "expect": {"vendors": 4, "cart": [], "chosen": None}},
    # ── D. rejet partiel ──────────────────────────────────────────────────────────────────────────────────────────────
    {"id": "D1_too_expensive", "ctx": "producer_menu", "utt": "c'est trop cher", "relation": "REFINEMENT",
     "expect": {"preserved": True, "no_redisplay": True, "any_of": ["prix", "combien", "budget", "plafond"]}},
    {"id": "D3_price_ceiling_answer", "ctx": "producer_menu", "pre": ["c'est trop cher"], "utt": "500", "relation": "REFINEMENT",
     "expect": {"vendors": 2, "cart": [], "chosen": None}},
    {"id": "D2_not_in_bidon", "ctx": "tier_menu", "utt": "pas en bidon", "relation": "REFINEMENT",
     "expect": {"cart": [], "no_redisplay": True}},
    # ── E. comparaison ────────────────────────────────────────────────────────────────────────────────────────────────
    {"id": "E1_which_cheaper", "ctx": "producer_menu", "utt": "lequel est moins cher ?", "relation": "QUESTION",
     "expect": {"preserved": True, "has": ["Moussa"], "no_redisplay": True}},
    {"id": "E2_most_stock", "ctx": "producer_menu", "utt": "qui a le plus de stock ?", "relation": "QUESTION",
     "expect": {"preserved": True, "has": ["Gilbert"], "no_redisplay": True}},
    {"id": "E3_which_is_better", "ctx": "producer_menu", "utt": "entre Gilbert et Moussa lequel est mieux ?", "relation": "QUESTION",
     "expect": {"preserved": True, "has": ["Gilbert", "Moussa"], "no_redisplay": True}},
    # ── F. questions de contexte ──────────────────────────────────────────────────────────────────────────────────────
    {"id": "F1_delivers", "ctx": "quantity_slot", "utt": "il livre ?", "relation": "QUESTION",
     "expect": {"preserved": True, "has": ["pas cette information"], "no_redisplay": True}},
    {"id": "F2_total", "ctx": "cart_has_item", "utt": "c'est combien au total ?", "relation": "QUESTION",
     "expect": {"preserved": True, "any_of": ["4500", "4 500"], "no_redisplay": True}},
    {"id": "F3_remaining", "ctx": "quantity_slot", "utt": "il reste combien ?", "relation": "QUESTION",
     "expect": {"preserved": True, "has": ["300"], "no_redisplay": True}},
    {"id": "F4_certified", "ctx": "quantity_slot", "utt": "c'est certifié ?", "relation": "QUESTION",
     "expect": {"preserved": True, "has": ["pas cette information"], "no_redisplay": True}},
    # ── G. changement de sujet ────────────────────────────────────────────────────────────────────────────────────────
    {"id": "G1_forget_search_other", "ctx": "producer_menu", "utt": "bon laisse cherche tomate", "relation": "NEW_TASK",
     "expect": {"chosen": None, "cart": [], "has": ["tomate"]}},
    # ── H. localité ───────────────────────────────────────────────────────────────────────────────────────────────────
    {"id": "H1_anyone_in_ouaga", "ctx": "producer_menu", "utt": "ya pas quelqu'un à Ouaga ?", "relation": "REFINEMENT",
     "expect": {"vendors": 3, "cart": []}},
    # ── I. français informel / fautes ─────────────────────────────────────────────────────────────────────────────────
    {"id": "I1_jveu", "ctx": "quantity_slot", "utt": "jveu 10 litres", "relation": "ANSWER", "expect": {"cart": [("Moussa", 10.0)]}},
    {"id": "I2_met", "ctx": "quantity_slot", "utt": "met 10", "relation": "ANSWER", "expect": {"cart": [("Moussa", 10.0)]}},
    {"id": "I3_c_combien", "ctx": "cart_has_item", "utt": "c combien", "relation": "QUESTION",
     "expect": {"preserved": True, "any_of": ["4500", "4 500"], "no_redisplay": True}},
    {"id": "I4_moi_je_veux_sachet", "ctx": "producer_menu", "utt": "moi je veux sachet", "relation": "REFINEMENT",
     "expect": {"vendors": 1, "cart": []}},
    {"id": "I5_typo_ordinal", "ctx": "producer_menu", "utt": "le quatrieme m'interresse", "relation": "SELECTION",
     "expect": {"chosen": "Gilbert-prod"}},
    {"id": "I6_typo_region", "ctx": "producer_menu", "utt": "quelqu'un a ouagadogou", "relation": "REFINEMENT",
     "expect": {"vendors": 3, "cart": []}},
    {"id": "I7_lui_la_me_plait", "ctx": "producer_menu", "utt": "lui la me plait", "relation": "AMBIGUOUS",
     "expect": {"chosen": None, "cart": [], "no_redisplay": True}},
    # ── J. réponses courtes / nombres ambigus ─────────────────────────────────────────────────────────────────────────
    {"id": "J1_bare_5_quantity", "ctx": "quantity_slot", "utt": "5", "relation": "ANSWER", "expect": {"cart": [("Moussa", 5.0)]}},
    {"id": "J2_bare_500_in_menu", "ctx": "producer_menu", "utt": "500", "relation": "AMBIGUOUS",
     "expect": {"chosen": None, "cart": [], "no_redisplay": True}},
    # ── K. rejet + alternative / multi-action ─────────────────────────────────────────────────────────────────────────
    {"id": "K1_not_gilbert_take_moussa", "ctx": "producer_menu", "utt": "pas Gilbert, prends Moussa", "relation": "REJECTION+ALTERNATIVE",
     "expect": {"chosen": "Moussa"}},
    {"id": "K2_take_and_quantity", "ctx": "producer_menu", "utt": "prends Moussa et mets 10 litres", "relation": "SELECTION+ANSWER",
     "expect": {"cart": [("Moussa", 10.0)]}},
]

# Ce que le MODÈLE doit comprendre (mode scripté, CI). Une phrase absente = UNKNOWN. Le résolveur, le domaine et le graphe sont RÉELS.
def _q(topic: str, **kw: Any) -> Dict[str, Any]:
    return {"disposition": "QUESTION", "question": {"topic": topic, **kw}}


def _ref_action(**kw: Any) -> Dict[str, Any]:
    return {"disposition": "ACTION", "action": "SELECT_PRODUCER", "reference": kw}


def _refine(**kw: Any) -> Dict[str, Any]:
    return _ref_action(reference_type="REFINEMENT", **kw)


def _edit(spec: Dict[str, Any]) -> Dict[str, Any]:
    return {"new_task": {"disposition": "NEW_TASK", "intent": "BUYER_EDIT_CART", "confidence": 0.9, "entities": {"cart_edit": spec}}}


def _qty(n: float, unit: str | None = None) -> Dict[str, Any]:
    return {"disposition": "ACTION", "action": "SET_QUANTITY", "quantity": n, "unit": unit}


SCRIPTED: Dict[str, Dict[str, Any]] = {
    # mises en place des contextes
    "10 litres": _qty(10, "L"),
    # A. ellipse
    "10 seulement": _qty(10),
    "en sachet": _refine(packaging="sachet"),
    "à ouaga": _refine(region="Ouaga"),
    "le moins cher": _ref_action(reference_type="PREFERENCE", criterion="CHEAPEST"),
    # B. anaphore : aucun repère -> jamais une cible devinée (garde d'étayage)
    "lui": {"disposition": "ACTION", "action": "SELECT_PRODUCER", "selection_index": 2},
    "celui-là": _ref_action(reference_type="ORDINAL", position="LAST"),
    "pas celui-là, l'autre": _ref_action(reference_type="OTHER"),
    # C. correction / K. rejet + alternative
    "pas gilbert, moussa": _ref_action(reference_type="ATTRIBUTE", producer_name="Moussa"),
    "pas gilbert, prends moussa": _ref_action(reference_type="ATTRIBUTE", producer_name="Moussa"),
    # D. rejet partiel
    "c'est trop cher": _refine(objection="PRICE"),
    "500": _refine(max_price=500),
    "pas en bidon": {"disposition": "ACTION", "action": "SELECT_PRICING_TIER", "reference": {"reference_type": "ATTRIBUTE", "packaging": "sachet"}},
    # E. comparaison  /  F. questions de contexte (le modèle comprend, ne répond jamais)
    "lequel est moins cher ?": _q("COMPARISON", criterion="CHEAPEST"),
    "qui a le plus de stock ?": _q("COMPARISON", criterion="HIGHEST_AVAILABILITY"),
    "entre gilbert et moussa lequel est mieux ?": _q("COMPARISON", names=["Gilbert", "Moussa"]),
    "il livre ?": _q("DELIVERY"),
    "il reste combien ?": _q("STOCK"),
    "c'est certifié ?": _q("CERTIFICATION"),
    "c'est combien au total ?": {"new_task": {"disposition": "NEW_TASK", "intent": "BUYER_VIEW_CART", "confidence": 0.9, "entities": {}}},
    "c combien": {"new_task": {"disposition": "NEW_TASK", "intent": "BUYER_VIEW_CART", "confidence": 0.9, "entities": {}}},
    # C. correction d'une LIGNE du panier : le modèle comprend (champ + valeur), le DOMAINE exécute
    "je voulais dire 20 pas 10": _edit({"field": "QUANTITY", "value": 20}),
    "non 5": _edit({"field": "QUANTITY", "value": 5}),
    "mets 20 plutôt": _edit({"field": "QUANTITY", "value": 20}),
    "finalement 8 litres": _edit({"field": "QUANTITY", "value": 8, "unit": "L"}),
    "c pas 10 c 15": _edit({"field": "QUANTITY", "value": 15}),
    "enlève ça du panier": _edit({"field": "REMOVE"}),
    "mets 0": _edit({"field": "QUANTITY", "value": 0}),
    "mets -5": _edit({"field": "QUANTITY", "value": -5}),
    "finalement le prix n'importe plus": _refine(remove=["max_price"]),
    # G. changement de sujet
    "bon laisse cherche tomate": {"disposition": "DEVIATION", "new_task": {
        "disposition": "NEW_TASK", "intent": "BUYER_REQUEST", "confidence": 0.9, "entities": {"product": "tomate"}}},
    # H / I. localité, informel, fautes
    "ya pas quelqu'un à ouaga ?": _refine(region="Ouaga"),
    "jveu 10 litres": _qty(10, "L"),
    "met 10": _qty(10),
    "moi je veux sachet": _refine(packaging="sachet"),
    "le quatrieme m'interresse": _ref_action(reference_type="ORDINAL", ordinal=4),
    "quelqu'un a ouagadogou": _refine(region="ouagadogou"),
    "lui la me plait": _ref_action(reference_type="PREFERENCE", criterion="SUBJECTIVE"),
    # K. multi-action
    "prends moussa et mets 10 litres": {**_ref_action(reference_type="ATTRIBUTE", producer_name="Moussa"), "quantity": 10, "unit": "L"},
}

#: Cas qui DÉPENDENT d'une capacité de domaine absente (pas de correction de ligne de panier) : mesurés avec le vrai modèle, jamais scriptés.
DOMAIN_LIMITS: set = set()


# ── vendeur (graphe PRODUCER) ────────────────────────────────────────────────────────────────────────────────────────────
#   slots   sous-ensemble du payload du brouillon APRÈS le tour   ·   pending  type d'attente   ·   missing  champs encore demandés
#   no_publish  aucun outil de publication appelé (« pas maintenant » ne publie rien)
SELLER_CASES: List[Dict[str, Any]] = [
    {"id": "S1_quantity_fragment", "ctx": "seller_missing_quantity", "utt": "j'ai 300 kg", "relation": "ANSWER",
     "expect": {"slots": {"quantity": 300.0, "price": 250.0}, "no_publish": True}},
    {"id": "S2_price_correction", "ctx": "seller_recap", "utt": "à 300 francs", "relation": "CORRECTION",
     "expect": {"slots": {"price": 300.0, "quantity": 300.0, "product": "tomate"}, "no_publish": True}},
    {"id": "S3_product_correction", "ctx": "seller_recap", "utt": "en fait c'est oignon pas tomate", "relation": "CORRECTION",
     "expect": {"slots": {"product": "oignon", "quantity": 300.0}, "no_publish": True}},
    {"id": "S4_not_now", "ctx": "seller_recap", "utt": "pas maintenant", "relation": "REJECTION", "expect": {"no_publish": True}},
    {"id": "S5_short_no_number", "ctx": "seller_recap", "utt": "non 250", "relation": "CORRECTION",
     "expect": {"slots": {"price": 250.0}, "no_publish": True}},
    {"id": "S6_quantity_correction", "ctx": "seller_recap", "utt": "non 250 kg", "relation": "CORRECTION",
     "expect": {"slots": {"quantity": 250.0, "product": "tomate"}, "no_publish": True}},
    {"id": "S7_slash_price", "ctx": "seller_recap", "utt": "plutôt 280/kg", "relation": "CORRECTION",
     "expect": {"slots": {"price": 280.0, "quantity": 300.0}, "no_publish": True}},
    {"id": "S8_yes_but", "ctx": "seller_recap", "utt": "oui mais 280/kg", "relation": "CORRECTION",
     "expect": {"slots": {"price": 280.0}, "no_publish": True}},
    {"id": "S9_not_300_but_250", "ctx": "seller_recap", "utt": "c'est pas 300 c'est 250", "relation": "CORRECTION",
     "expect": {"slots": {"quantity": 250.0}, "no_publish": True}},
    {"id": "S10_wrong_product", "ctx": "seller_recap", "utt": "non pas des tomates, des mangues", "relation": "CORRECTION",
     "expect": {"slots": {"product": "mangue", "quantity": 300.0}, "no_publish": True}},
    {"id": "S11_price_typo", "ctx": "seller_recap", "utt": "ah non c 350 le prix", "relation": "CORRECTION",
     "expect": {"slots": {"price": 350.0}, "no_publish": True}},
    {"id": "S12_quantity_wrong", "ctx": "seller_recap", "utt": "jai dit 500 kg pas 300", "relation": "CORRECTION",
     "expect": {"slots": {"quantity": 500.0}, "no_publish": True}},
    {"id": "S13_ok_but_price", "ctx": "seller_recap", "utt": "ok mais mets 275", "relation": "CORRECTION",
     "expect": {"slots": {"price": 275.0}, "no_publish": True}},
]
