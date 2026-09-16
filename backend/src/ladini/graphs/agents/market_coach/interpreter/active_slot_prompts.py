"""Prompt dédié du micro-prompt ACTIVE_SLOT (chantier "State Router +
micro-prompts", Phase C, 2026-09-12).

Volontairement PETIT et SPÉCIALISÉ, même principe que
`selection_prompts.py` : jamais le catalogue des ~60 intentions, jamais les
règles NEW_TASK/SELECTION/dates génériques — seulement ce qui est
nécessaire pour comprendre une réponse dans un tunnel déjà ouvert. Le LLM
garde l'entière responsabilité de la compréhension linguistique libre
(reformulations, corrections, réponses composées) ; ce module ne fait
AUCUNE énumération de formulations possibles."""

from __future__ import annotations

from typing import Dict

# À incrémenter à CHAQUE changement comportemental de ce prompt — composante
# de la clé de cache LLM (`active_slot_micro.py::_cache_key`) ET dimension
# Langfuse (`prompt_version`). Distincte de `SELECTION_PROMPT_VERSION` et de
# `INTERPRETER_PROMPT_VERSION` — chaque famille de micro-prompt a la sienne
# (spec §19 de l'Incrément B, même principe reconduit ici).
#
# v2 (2026-09-12, Phase C.1) : corrige un cas réel raté en validation live —
# "laisse ça je veux vendre du riz" (slot QUANTITY actif, product=mais déjà
# connu) était classé UPDATE{product: riz} au lieu de DEVIATION. Diagnostic
# (3/3 reproductions identiques à temperature=0) : le modèle appliquait
# mécaniquement la règle "corrige un champ déjà connu" dès qu'un nom de champ
# connu (product) réapparaissait dans le message — sans jamais peser le
# signal d'abandon ("laisse ça") + la nouvelle action métier explicite
# ("vendre du riz" ≠ correction du produit en cours d'achat/vente). v2
# ajoute une règle sémantique explicite (une nouvelle demande explicite
# prévaut sur une correction de champ de même nom) + 2 paires d'exemples
# contrastifs courts — jamais une liste de déclencheurs lexicaux.
ACTIVE_SLOT_PROMPT_VERSION = "active_slot_v3"

ACTIVE_SLOT_SYSTEM_PROMPT = (
    "Tu interprètes, dans une conversation WhatsApp au Burkina Faso, la "
    "réponse d'un utilisateur À L'INTÉRIEUR d'une transaction déjà en "
    "cours, alors qu'une information précise est attendue. Tu ne "
    "classifies AUCUNE nouvelle intention métier — seulement si ce message "
    "répond au champ attendu, le corrige, l'abandonne, ou concerne "
    "clairement autre chose. Réponds UNIQUEMENT avec un objet JSON valide, "
    "sans aucun texte autour."
)

_USER_PROMPT_TEMPLATE = (
    "But métier en cours : {goal}\n"
    "Catégorie de champ attendue : {category}\n"
    "Champ exact attendu : {field_name}\n"
    "Informations déjà connues : {known_entities}\n"
    'Message utilisateur : "{normalized_text}"\n'
    "\n"
    "L'utilisateur peut :\n"
    "1. répondre au champ attendu ;\n"
    "2. donner PLUSIEURS informations explicites à la fois ;\n"
    "3. corriger une information déjà connue (même une AUTRE que celle "
    "attendue) ;\n"
    "4. rejeter/annuler ;\n"
    "5. parler d'autre chose (une tâche différente, sans rapport).\n"
    "\n"
    "Détermine UNE seule disposition :\n"
    "- ANSWER : le message répond à l'information actuellement demandée.\n"
    "- UPDATE : le message corrige/modifie une information qui appartient "
    "ENCORE à la tâche active en cours (même si ce n'est pas le champ "
    "actuellement attendu).\n"
    "- REJECT : le message refuse/annule ce qui est explicitement demandé, "
    "SANS introduire clairement une nouvelle tâche métier.\n"
    "- DEVIATION : le message exprime clairement une AUTRE demande/action "
    "métier qui ne constitue pas une réponse au slot attendu — n'invente "
    "RIEN pour la transaction courante dans ce cas.\n"
    "- UNKNOWN : impossible de déterminer de façon fiable ce que veut faire "
    "l'utilisateur.\n"
    "\n"
    "RÈGLE DE PRIORITÉ (critique) : une nouvelle demande métier EXPLICITE "
    "prévaut TOUJOURS sur une simple correction de champ ou un simple rejet "
    "— même si le message réemploie le nom d'un champ déjà connu (ex: "
    "\"product\"). Si le message abandonne, suspend ou détourne clairement "
    "la question actuelle ET exprime une nouvelle action/demande, c'est "
    "DEVIATION, jamais UPDATE ni REJECT. Ne classe UPDATE que si le message "
    "reste À L'INTÉRIEUR de la même tâche (une correction, pas une nouvelle "
    "demande) ; ne classe REJECT que si l'utilisateur abandonne SANS "
    "introduire clairement autre chose.\n"
    "Exemples (la frontière conceptuelle à généraliser, pas des expressions "
    "à mémoriser) :\n"
    '  "laisse tomber" (rien d\'autre exprimé) → REJECT.\n'
    '  "laisse tomber, je veux acheter des œufs" (nouvelle demande '
    "explicite) → DEVIATION.\n"
    '  "non, mets plutôt 300" (corrige le prix de LA MÊME vente) → UPDATE.\n'
    '  "non, je veux plutôt vendre du riz" (nouvelle vente, pas une '
    "correction du produit en cours) → DEVIATION.\n"
    "\n"
    "Règles d'extraction :\n"
    "- N'extrais QUE ce qui est explicitement énoncé ou normalisable sans "
    "ambiguïté (ex: \"vingt sacs\" → 20, unit=SAC) — n'invente JAMAIS une "
    "valeur absente du message (pas d'unité si aucune n'est écrite).\n"
    "- Le champ attendu est un point de repère, PAS un filtre : si "
    "l'utilisateur donne plusieurs informations explicites en même temps "
    "(ex: produit ET quantité ET prix), retourne-les TOUTES dans "
    '"extracted_entities" — n\'en retiens jamais une seule au détriment '
    "des autres.\n"
    "- Pour UPDATE, retourne uniquement le(s) champ(s) corrigé(s) — "
    "n'invente jamais le champ actuellement attendu s'il n'est pas "
    "mentionné.\n"
    "- Pour DEVIATION et UNKNOWN, \"extracted_entities\" doit être vide.\n"
    "- Utilise exclusivement ces noms de champs canoniques quand ils "
    "s'appliquent : product, additional_products, quantity, unit, price, "
    "price_unit, pricing_tiers, farm_name, zone, movement_type, value "
    "(pour une date).\n"
    "- \"quantity\" : si le stock est décrit en PLUSIEURS groupes de "
    "conditionnements (\"60 bidons de 5 L et 30 bidons de 20 L\"), c'est la "
    "SOMME de chaque groupe (paquets × contenu), jamais le premier nombre "
    "lu seul (60×5 + 30×20 = 900, pas 60).\n"
    "- \"pricing_tiers\" : si PLUSIEURS couples quantité+unité+prix sont "
    "donnés pour le même produit (\"1 bidon de 5 L à 10000 FCFA, 1 bidon de "
    "20 L à 50000 FCFA\"), liste CHAQUE déclinaison comme un objet distinct "
    "(quantity/unit/price/packaging) — un prix \"par unité de base\" SANS "
    "conditionnement précis (\"3000 FCFA le litre\") va dans price/"
    "price_unit, jamais dans pricing_tiers. Le quantity/unit D'UN TARIF "
    "décrit le CONTENU d'un seul paquet, jamais le nombre de paquets en "
    "stock.\n"
    "- \"confidence\" : ta certitude entre 0.0 et 1.0 sur la disposition "
    "choisie — sois prudent (valeur basse) en cas de doute réel entre "
    "ANSWER et DEVIATION.\n"
    "\n"
    "Réponds strictement avec cet objet JSON, sans aucun autre texte :\n"
    '{{"disposition": "ANSWER|UPDATE|REJECT|DEVIATION|UNKNOWN", '
    '"extracted_entities": {{}}, "confidence": <0.0 à 1.0>}}'
)

_REPAIR_PROMPT_TEMPLATE = (
    "Ta réponse précédente est invalide : {reason}\n"
    "\n"
    "Contraintes :\n"
    '- "disposition" doit être ANSWER, UPDATE, REJECT, DEVIATION ou '
    "UNKNOWN.\n"
    '- "extracted_entities" doit être un objet JSON (vide pour DEVIATION '
    "et UNKNOWN).\n"
    '- "confidence" doit être un nombre entre 0.0 et 1.0.\n'
    "\n"
    "Corrige uniquement le JSON, sans texte autour."
)


def _format_known_entities(known_entities: Dict[str, object]) -> str:
    if not known_entities:
        return "(aucune)"
    return ", ".join(f"{k}={v}" for k, v in known_entities.items())


def build_active_slot_user_prompt(
    *,
    goal: str,
    category: str,
    field_name: str,
    known_entities: Dict[str, object],
    normalized_text: str,
) -> str:
    return _USER_PROMPT_TEMPLATE.format(
        goal=goal or "AUCUN",
        category=category or "NONE",
        field_name=field_name or "(non spécifié)",
        known_entities=_format_known_entities(known_entities),
        normalized_text=normalized_text,
    )


def build_active_slot_repair_prompt(*, reason: str) -> str:
    return _REPAIR_PROMPT_TEMPLATE.format(reason=reason)


__all__ = [
    "ACTIVE_SLOT_PROMPT_VERSION",
    "ACTIVE_SLOT_SYSTEM_PROMPT",
    "build_active_slot_user_prompt",
    "build_active_slot_repair_prompt",
]
