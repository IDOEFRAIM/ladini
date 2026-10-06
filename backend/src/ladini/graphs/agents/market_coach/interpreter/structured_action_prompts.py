"""Prompt dédié du micro-prompt STRUCTURED_ACTION (chantier "State Router +
micro-prompts", Incrément D, 2026-09-12).

Volontairement PETIT et SPÉCIALISÉ, même principe que `selection_prompts.py`/
`active_slot_prompts.py` : jamais le catalogue des ~60 intentions, jamais de
règles NEW_TASK/ACTIVE_SLOT génériques, JAMAIS d'identifiant technique
(`producer_id`/`pricing_tier_id`) — seulement des labels numérotés. Reprend
la règle de priorité DEVIATION apprise en C.1 (une nouvelle demande
explicite prévaut sur une simple ressemblance avec l'action courante)."""

from __future__ import annotations

from typing import Optional

from ladini.graphs.agents.market_coach.domain.selection_actions import ActionType
from ladini.graphs.agents.market_coach.interpreter.structured_action_contract import (
    StructuredActionPromptContext,
)

# À incrémenter à CHAQUE changement comportemental — composante de la clé de
# cache LLM ET dimension Langfuse (`prompt_version`), même discipline que
# `SELECTION_PROMPT_VERSION`/`ACTIVE_SLOT_PROMPT_VERSION`.
#
# v2 (2026-09-13, Incrément D.1) : corrige un cas réel raté en validation
# live — "finalement le bidon de 10 L" pendant SET_PACKAGE_COUNT (palier
# déjà choisi = 5 L). Diagnostic (3 reproductions déterministes) : le
# modèle identifie CORRECTEMENT qu'il s'agit d'un changement de
# conditionnement, mais hésite entre deux erreurs — soit `disposition=
# ACTION` sans jamais poser le champ `action` (rejeté par le validator :
# "ACTION requiert un champ 'action' explicite"), soit `disposition=
# DEVIATION` tout en posant quand même `selection_index`/`selected_value`
# (rejeté car DEVIATION doit être vide). Root cause : la règle de priorité
# DEVIATION ("une nouvelle demande explicite prévaut") et l'exemple
# "finalement/plutôt X" qui l'illustre se lisaient comme s'appliquant AUSSI
# à un changement de conditionnement POUR LE MÊME ACHAT — alors que
# `validate_action` autorise déjà explicitement ce cas comme une ACTION
# (SELECT_PRICING_TIER), jamais une déviation. v2 ajoute une clarification
# explicite (achat identique ≠ nouvelle demande) + un exemple contrastif
# dédié, sans transformer la règle en liste de synonymes.
STRUCTURED_ACTION_PROMPT_VERSION = "structured_action_v4"

_SYSTEM_PROMPT = (
    "Tu interprètes, dans une conversation WhatsApp au Burkina Faso, la "
    "réponse d'un utilisateur À L'INTÉRIEUR d'un tunnel d'achat déjà engagé "
    "(choix d'un producteur, d'un conditionnement, ou d'une quantité). Tu "
    "ne classifies AUCUNE nouvelle intention métier — seulement si ce "
    "message répond à l'action attendue, la refuse, ou concerne clairement "
    "autre chose. Réponds UNIQUEMENT avec un objet JSON valide, sans aucun "
    "texte autour."
)

_ACTION_LABELS = {
    ActionType.SELECT_PRODUCER: "choisir un producteur parmi les options ci-dessous",
    ActionType.SELECT_PRICING_TIER: "choisir un conditionnement parmi les options ci-dessous",
    ActionType.SET_PACKAGE_COUNT: "indiquer le nombre de paquets du conditionnement déjà choisi",
    ActionType.SET_QUANTITY: "indiquer la quantité souhaitée (produit à tarif unique, sans conditionnement)",
}

_DISPOSITION_RULES = (
    "Détermine UNE seule disposition :\n"
    "- ACTION : le message répond à l'action attendue ci-dessus.\n"
    "- REJECT : le message refuse/annule l'étape courante, SANS introduire "
    "clairement une nouvelle demande métier.\n"
    "- DEVIATION : le message exprime clairement une AUTRE demande métier, "
    "sans rapport avec ce tunnel d'achat.\n"
    "- QUESTION : l'utilisateur POSE UNE QUESTION sur ce qui est affiché ou choisi (« il livre ? », « c'est combien au total ? », "
    "« il reste combien ? », « lequel est moins cher ? », « c'est certifié ? ») — ce n'est PAS un choix : ne désigne rien. Tu "
    "COMPRENDS la question, tu ne la RÉPONDS jamais (renseigne \"question\").\n"
    "- UNKNOWN : impossible de déterminer de façon fiable ce que veut faire "
    "l'utilisateur.\n"
    "\n"
    "RÈGLE DE PRIORITÉ (critique) : une nouvelle demande métier EXPLICITE "
    "prévaut TOUJOURS sur une simple ressemblance avec l'action en cours. "
    "Si le message abandonne, suspend ou détourne clairement la question "
    "actuelle ET exprime une nouvelle action/demande, c'est DEVIATION, "
    "jamais ACTION ni REJECT.\n"
    "IMPORTANT — à ne PAS confondre avec une déviation : changer d'AVIS sur "
    "un DÉTAIL du MÊME achat en cours (autre conditionnement, autre "
    "quantité, autre prix proposé) N'EST PAS une nouvelle demande métier — "
    "c'est une ACTION (voir la règle spécifique ci-dessous pour ce type de "
    "changement). Une déviation change de PRODUIT ou de TÂCHE, jamais "
    "seulement de détail sur ce qui est déjà en cours d'achat/vente.\n"
    "Exemples (la frontière conceptuelle à généraliser, pas des expressions "
    "à mémoriser) :\n"
    '  "laisse tomber" (rien d\'autre exprimé) → REJECT.\n'
    '  "laisse tomber, je veux acheter du riz" (nouvelle demande '
    "explicite, autre produit) → DEVIATION.\n"
    '  "finalement le bidon de 10 L" (même achat, autre conditionnement) '
    "→ ACTION (jamais DEVIATION — voir règle SET_PACKAGE_COUNT ci-dessous).\n"
)

_SELECTION_RULES = (
    "Pour ACTION sur ce type de choix, l'utilisateur peut répondre avec SES mots, pas seulement un numéro. "
    "Désigne l'option avec UN SEUL des trois moyens :\n"
    '- "selection_index" (entier 1-based) : un numéro dit tel quel (« 4 », « le quatrième », « option 4 ») ;\n'
    '- "reference" : une désignation par les FAITS visibles, SANS choisir toi-même (le système compare aux options) :\n'
    '    {"reference_type": "ORDINAL", "ordinal": <n>} ou {"reference_type": "ORDINAL", "position": "LAST"|"PENULTIMATE"} '
    "(« le dernier », « l'avant-dernier ») ;\n"
    '    {"reference_type": "ATTRIBUTE", "producer_name": "<nom dit>", "price": <nombre>, "availability": <nombre>, '
    '"region": "<lieu dit>", "packaging": "<conditionnement dit>", "volume": <nombre>} (« Gilbert », « celui à 500 », '
    "« celui de Ouaga », « le sachet de 500 ml » -> volume 0.5) — ne renseigne QUE ce que l'utilisateur a dit ;\n"
    '    {"reference_type": "PREFERENCE", "criterion": "CHEAPEST"|"HIGHEST_AVAILABILITY"|"SUBJECTIVE"} '
    "(« le moins cher », « celui qui a le plus de stock » ; « le plus intéressant » = SUBJECTIVE) — ne calcule JAMAIS toi-même "
    "quel est le moins cher : le système le fait ;\n"
    '    {"reference_type": "REFINEMENT", "objection": "PRICE|DISTANCE|PACKAGE|OTHER", "region": "<lieu>", "packaging": "<conditionnement>", "max_price": <nombre>, '
    '"criterion": "CHEAPEST"|"HIGHEST_AVAILABILITY"} si l\'utilisateur AJOUTE une contrainte au lieu de choisir '
    "(« je préfère quelqu'un à Ouaga », « pas plus de 600 », « moins cher », « en sachet ») — ne renseigne que ce qui est dit ;\n"
    '    {"reference_type": "NONE_OF_THESE"} si aucune offre ne lui convient (« aucun », « rien ne me convient ») ;\n'
    '    ATTENTION : « pas Gilbert, Moussa » / « pas lui, prends Moussa » NOMME un remplaçant -> ATTRIBUTE {"producer_name": "Moussa"} '
    '(jamais OTHER) ; OTHER seulement quand AUCUN remplaçant n\'est nommé ;\n'
    '    {"reference_type": "OTHER", "producer_name": "<producteur écarté, s\'il est nommé>"} pour « pas celui-là, l\'autre » / « pas '
    'Gilbert, l\'autre » (sans producteur nommé : laisse producer_name vide — le système demandera lequel) ;\n'
    '    {"reference_type": "PAGINATION"} si l\'utilisateur demande de VOIR LA SUITE de la liste (« montre les autres », « voir plus », '
    "« suite ») : ce n'est pas un choix.\n"
    "Une OBJECTION (« c'est trop cher », « trop loin », « pas en bidon ») n'est PAS un abandon (REJECT) : c'est un REFINEMENT avec "
    "\"objection\" (et max_price/region/packaging si dits). REJECT est réservé à « annule », « laisse tomber » sans autre demande.\n"
    '- "selected_value" (texte court) seulement si aucun des moyens ci-dessus ne convient.\n'
    "Un seul des trois par réponse ; n'invente aucun identifiant. Un NOMBRE accompagné d'une unité ou d'un mot de quantité "
    "(« 4 litres », « mets-en 4 », « 500 francs max ») n'est JAMAIS une option : ce n'est pas une désignation. "
    'Si le message désigne une option ET donne une quantité/un nombre de paquets (« je prends Gilbert, 10 litres »), utilise '
    '"reference" ET renseigne "quantity"/"unit" (ou "package_count") — avec "selection_index" ou "selected_value", jamais.\n'
)

_PACKAGE_COUNT_RULES = (
    "Deux cas possibles pour ACTION — ne les mélange JAMAIS :\n"
    "\n"
    '1. Le message donne un NOMBRE DE PAQUETS (le cas normal) : utilise '
    '"action": "SET_PACKAGE_COUNT" et "package_count" — un chiffre nu ici '
    "signifie TOUJOURS un nombre de paquets, jamais une option de menu.\n"
    "2. Le message NOMME un AUTRE conditionnement que celui déjà choisi "
    '(ex: "finalement le bidon de 10 litres") : c\'est un changement de '
    "palier pour le MÊME achat, PAS une déviation, PAS un rejet — utilise "
    '"action": "SELECT_PRICING_TIER" ET "selection_index"/"selected_value" '
    "(jamais package_count/quantity dans ce cas). Pose OBLIGATOIREMENT le "
    'champ "action" — sans lui, ta réponse est invalide même si '
    "l'intention est claire.\n"
    "\n"
    "Exemple :\n"
    '  conditionnement déjà choisi = 5 L, message = "finalement le bidon '
    'de 10 L"\n'
    '  → {"disposition": "ACTION", "action": "SELECT_PRICING_TIER", '
    '"selection_index": <index du bidon 10 L>}\n'
    "  (PAS disposition=DEVIATION : c'est le même achat, juste un autre "
    "conditionnement.)\n"
)

_QUANTITY_RULES = (
    "Pour ACTION :\n"
    "- « mets 10 », « jveux 10 litres », « 10 seulement », « j'en veux 10 » donnent la quantité 10 (un verbe d'ajout + un nombre est "
    "la quantité, jamais une nouvelle demande) ;\n"
    '- utilise "quantity" (nombre) et "unit" si explicitement énoncée — un '
    "chiffre nu ici signifie TOUJOURS la quantité elle-même, jamais un "
    "index de menu ;\n"
    "- n'invente JAMAIS une unité absente du message (unit=null si "
    "aucune n'est écrite).\n"
)

_JSON_CONTRACT = (
    "\n"
    "Réponds strictement avec cet objet JSON, sans aucun autre texte :\n"
    '{{"disposition": "ACTION|REJECT|DEVIATION|QUESTION|UNKNOWN", "action": '
    '"{action_type}|null", "selection_index": <entier ou null>, '
    '"selected_value": "<texte ou null>", "reference": <objet ou null>, "package_count": <nombre ou '
    'null>, "quantity": <nombre ou null>, "unit": "<texte ou null>", '
    '"question": <null ou {{"topic": "PRICE|STOCK|DELIVERY|CERTIFICATION|LOCATION|TOTAL|DISTANCE|COMPARISON|OTHER", '
    '"criterion": "CHEAPEST|HIGHEST_AVAILABILITY|null", "names": ["<producteurs nommés>"], "target": <référence ou null>}}>, '
    '"confidence": <0.0 à 1.0>}}'
)


def _format_options(prompt_context: StructuredActionPromptContext) -> str:
    if not prompt_context.options:
        return "(aucune option)"
    return "\n".join(f"{o.index}. {o.label}" for o in prompt_context.options)


def build_structured_action_user_prompt(
    *,
    prompt_context: StructuredActionPromptContext,
    goal: Optional[str],
    normalized_text: str,
) -> str:
    action = prompt_context.expected_action
    lines = [
        f"But métier en cours : {goal or 'AUCUN'}",
        f"Action attendue : {_ACTION_LABELS[action]}",
    ]
    if action in (ActionType.SELECT_PRODUCER, ActionType.SELECT_PRICING_TIER):
        lines.append(f"Options :\n{_format_options(prompt_context)}")
        type_rules = _SELECTION_RULES
    elif action == ActionType.SET_PACKAGE_COUNT:
        if prompt_context.active_tier_label:
            lines.append(f"Conditionnement déjà choisi : {prompt_context.active_tier_label}")
        if prompt_context.options:
            lines.append(
                "Conditionnements disponibles pour un changement explicite "
                f"seulement :\n{_format_options(prompt_context)}"
            )
        type_rules = _PACKAGE_COUNT_RULES
    else:  # SET_QUANTITY
        type_rules = _QUANTITY_RULES

    if prompt_context.awaiting == "PRICE_CEILING":
        lines.append(
            "Question posée juste avant à l'utilisateur : « Tu veux rester sous quel prix ? » — un NOMBRE (avec ou sans « FCFA ») est ce "
            "plafond : REFINEMENT avec max_price (jamais une option du menu, jamais une quantité)."
        )
    if prompt_context.producer_options and action != ActionType.SELECT_PRODUCER:
        lines.append(
            "Producteurs (l'utilisateur peut en CHANGER s'il le dit, ex. « pas Gilbert, Moussa » : action SELECT_PRODUCER) :\n"
            + "\n".join(f"{o.index}. {o.label}" for o in prompt_context.producer_options)
        )
    lines.append(f'Message utilisateur : "{normalized_text}"')
    lines.append("")
    lines.append(_DISPOSITION_RULES)
    lines.append(type_rules)
    lines.append(_JSON_CONTRACT.format(action_type=action.value))
    return "\n".join(lines)


def build_structured_action_system_prompt() -> str:
    return _SYSTEM_PROMPT


_REPAIR_PROMPT_TEMPLATE = (
    "Ta réponse précédente est invalide : {reason}\n"
    "\n"
    "Contraintes :\n"
    '- "disposition" doit être ACTION, REJECT, DEVIATION ou UNKNOWN.\n'
    "- pour ACTION : remplis UNIQUEMENT les champs correspondant à une "
    "seule action à la fois (jamais un mélange selection_index/"
    "package_count/quantity).\n"
    '- pour REJECT/DEVIATION/UNKNOWN : tous les champs d\'action doivent '
    "être null.\n"
    "\n"
    "Corrige uniquement le JSON, sans texte autour."
)


def build_structured_action_repair_prompt(*, reason: str) -> str:
    return _REPAIR_PROMPT_TEMPLATE.format(reason=reason)


__all__ = [
    "STRUCTURED_ACTION_PROMPT_VERSION",
    "build_structured_action_system_prompt",
    "build_structured_action_user_prompt",
    "build_structured_action_repair_prompt",
]
