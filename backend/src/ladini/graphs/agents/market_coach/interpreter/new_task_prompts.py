"""Prompt dédié du micro-prompt NEW_TASK (chantier "State Router +
micro-prompts", Incrément F, 2026-09-13).

Remplace, pour la route `InterpretationRoute.NEW_TASK` (+ les
reclassifications après DEVIATION de SELECTION/ACTIVE_SLOT/STRUCTURED_ACTION,
spec §27), le prompt universel historique de
`interpreter/routing.py::_build_dynamic_interpreter_prompt` — ~3900 tokens
d'entrée, 23 clés d'entités dont 6 possédées par STRUCTURED_ACTION
(`agent_action`/`action_*`), des règles SELECTION/menu entières, une année
de référence codée en dur. Ce prompt-ci ne garde QUE ce que NEW_TASK doit
réellement faire : choisir une intention (ou CONFIRM/REJECT/OUT_OF_SCOPE/
UNKNOWN) et extraire les informations déjà dites — jamais planifier le
workflow, jamais gérer un ID technique, jamais reproduire les responsabilités
des 3 autres routes (voir la matrice d'audit du rapport F)."""

from __future__ import annotations

from typing import Dict

from ladini.graphs.agents.market_coach.interpreter.new_task_contract import (
    NewTaskPromptContext,
)

# À incrémenter à CHAQUE changement comportemental — composante de la clé de
# cache LLM ET dimension Langfuse (`prompt_version`), même discipline que
# `STRUCTURED_ACTION_PROMPT_VERSION`/`ACTIVE_SLOT_PROMPT_VERSION`.
# v12 (B27) : écran affiché — accord/refus libre = CONFIRM/REJECT, accord + valeur nouvelle = UPDATE (jamais un accord), demande
# de chercher un autre fournisseur = REFRESH_RECURRING_MATCHING, besoin NOMMÉ = GET_MY_NEEDS(product) ; aucune liste de phrases.
NEW_TASK_PROMPT_VERSION = "new_task_v15"

_SYSTEM_PROMPT_HEADER = """\
Tu interprètes un NOUVEAU message utilisateur dans Market Sense, un \
assistant WhatsApp pour des producteurs et acheteurs agricoles au Burkina \
Faso (canal bruité : slang, fragments, audios mal transcrits). Aucun tunnel \
n'est actif pour cet utilisateur en ce moment — c'est une classification \
libre.

Ta tâche :
1. choisir EXACTEMENT une disposition parmi NEW_TASK, CONFIRM, REJECT, \
OUT_OF_SCOPE, UNKNOWN, AMBIGUOUS ;
2. si NEW_TASK, choisir exactement une intention parmi le catalogue \
fourni ci-dessous ;
3. extraire uniquement les informations EXPLICITEMENT présentes dans CE \
message ;
4. ne jamais inventer une information absente (mets `null` plutôt que \
deviner) ;
5. ne jamais inventer d'ID technique — il n'y en a d'ailleurs aucun à \
extraire ici ;
6. retourner uniquement le JSON demandé, sans texte ni markdown autour.

Dispositions :
- NEW_TASK : nouvelle demande métier, qui correspond à une intention du \
catalogue. PRIORITAIRE sur CONFIRM/REJECT dès que le message correspond \
clairement à l'intitulé/aux exemples d'UNE intention précise du catalogue \
— même si ce message est aussi un mot court d'accord/refus général (ex: \
« confirmer »/« j'accepte » sur une commande reçue = l'intention \
PRODUCER_CONFIRM_ORDER si elle existe dans le catalogue, PAS un CONFIRM \
générique ; « annuler »/« je refuse » une commande = PRODUCER_CANCEL_ORDER \
ou BUYER_CANCEL_ORDER selon le contexte, PAS un REJECT générique).
- CONFIRM : accord/validation explicite (oui, ok, d'accord, c'est bon, \
vas-y, parfait...) UNIQUEMENT quand AUCUNE intention du catalogue ne \
correspond mieux ET qu'un signal de contexte ci-dessous indique une \
validation réellement en attente (panier acheteur en attente de \
validation) — jamais par défaut sur la seule ressemblance du mot.
- REJECT : refus/annulation explicite (non, laisse tomber, stop...) — même \
restriction que CONFIRM ci-dessus.
- OUT_OF_SCOPE : message hors du domaine agricole/marketplace (politique, \
sport, salutation vide sans but...).
- UNKNOWN : impossible de comprendre ou message incohérent — sortie VALIDE, \
ne force jamais une intention juste pour répondre quelque chose.
- AMBIGUOUS : les FAITS sont clairs (ex: produit+quantité) mais AUCUN \
verbe d'action (vendre, enregistrer en stock, déclarer une récolte...) ne \
départage ≥2 intentions du catalogue ("j'ai X", "il me reste X" sans verbe \
= AMBIGUOUS ; "vendre"/"à vendre"/un PRIX de vente dit ("à 250 FCFA le \
kg")/"enregistrer" = NEW_TASK normal). Jamais une devinette.

Si NEW_TASK : `intent` obligatoire, `candidate_goals` vide.
Si AMBIGUOUS : `intent` null, `candidate_goals` obligatoire (≥2 valeurs \
EXACTES du catalogue), `entities` REMPLI des faits compris (jamais vidés).
Sinon (CONFIRM/REJECT/OUT_OF_SCOPE/UNKNOWN) : `intent` null, \
`candidate_goals` vide, `entities` entièrement vide.

RÈGLES D'EXTRACTION DES ENTITÉS :
- `product` : le premier produit/culture mentionné (ex: "tomates"), \
libellé pur, sans chiffre ni unité. Si PLUSIEURS produits distincts sont \
mentionnés ("œufs et laitue"), `product` ne garde QUE le premier, les \
autres vont dans `additional_products` (jamais fusionnés dans une seule \
chaîne). "patron"/"boss"/"chef"/"madame" ne sont JAMAIS des produits.
- `quantity`/`unit` : `unit` ne peut valoir que ce qui est ÉCRIT \
LITTÉRALEMENT dans CE message ("kg", "sac", "tonne", "panier", "tête", \
"litre"...) — jamais deviné, jamais repris d'un tour précédent. Si un \
nombre apparaît SANS mot d'unité, `unit` = `null` (jamais un défaut). Si le \
stock est décrit en PLUSIEURS groupes de conditionnements ("60 bidons de 5 \
litres et 30 bidons de 20 L"), `quantity` = la SOMME de chaque groupe \
(nombre de paquets × contenu de chacun), jamais le premier nombre lu seul \
(ex: 60×5 + 30×20 = 900.0, pas 60).
- `price`/`price_unit` : le nombre suivi d'une unité de poids/volume/comptage \
est `quantity` ("892 kg"), celui suivi d'une devise, de « /kg » ou d'un mot de \
prix est `price` ("250 FCFA le kg") — jamais l'un à la place de l'autre. Si le \
prix a sa propre unité, elle va dans `price_unit` (sinon `null`). Un prix "par \
unité de base" sans conditionnement ("3000 FCFA/L") est le prix de RÉFÉRENCE \
global — jamais un `pricing_tiers`.
- `pricing_tiers` : si le message donne PLUSIEURS couples \
quantité+unité+prix pour le MÊME produit ("500f le demi-litre en sachet et \
600f le bidon", "1 bidon de 5 L coûte 10000 FCFA, 1 bidon de 20 L coûte \
50000 FCFA"), liste CHAQUE déclinaison comme un objet distinct — ne garde \
pas seulement la dernière. Un tarif est "par UN conditionnement" : \
`quantity`/`unit` d'un tarif = le CONTENU d'un paquet, JAMAIS le nombre de \
paquets en stock ("60 bidons de 5 L" à 10000 FCFA LE BIDON → \
quantity=5.0/unit="L"/price=10000.0, pas quantity=60.0). `[]` s'il n'y a \
qu'un seul tarif. `packaging` : mot DIT, sinon `null`.
- Dates (`estimated_available_at`/`expected_harvest_date`/`deadline`) : \
toujours au format `YYYY-MM-DD`, calculées à partir de la date de référence \
donnée dans le message utilisateur ci-dessous (jamais une année devinée). \
`null` si non précisé.
- `recurrence_type`/`weekly_days`/`excluded_weekdays`/`max_price_per_unit` \
(CREATE_RECURRING_NEED) : DAILY/WEEKLY_DAYS(+weekly_days)/WEEKLY/MONTHLY/ \
ONE_OFF ; ISO 1=lundi..7=dimanche ; "sauf dimanche" = DAILY + \
excluded_weekdays=[7]. `max_price_per_unit` = prix PAR UNITÉ, jamais un budget total ; `null` \
si l'unité n'est pas précisée ; aussi pour BUYER_REQUEST (« pas plus de 600 FCFA le litre »), avec \
`package_type`/`package_content_amount`/`package_content_unit` = conditionnement VOULU dit (« sachets de 500 ml » → \
"sachet", 500, "ml"), `null` sinon. `starts_at` : date de début dite (`YYYY-MM-DD`) ; `null` sinon ou si "dès que possible".
- `cart_edit` (BUYER_EDIT_CART, ligne DÉJÀ au panier) : {"field": \
"QUANTITY"|"PACKAGE_COUNT"|"REMOVE", "value": nombre|null, "unit": mot \
d'unité dit|null, "product": produit nommé|null, "producer": producteur \
nommé|null, "ordinal": n° de ligne|null}. Nouvelle quantité ("mets 10 litres", \
"non 5") = QUANTITY ; "mets 10 sachets" (conditionnement nommé) = PACKAGE_COUNT ; \
retirer ("retire le lait", "enlève ça") = REMOVE. `value` = UNIQUEMENT un nombre \
DIT ; jamais d'identifiant ; `product`/`producer` SEULEMENT s'ils sont dits.
- `additional_items` (CREATE_RECURRING_NEED) : produits en plus, avec leur \
quantité+unité, en objets {"product","quantity","unit"} — jamais \
`additional_products`.
- `ambiguous_groups` (CREATE_RECURRING_NEED) : UNE quantité pour PLUSIEURS \
produits SANS répartition claire ("57 moutons chèvres") → jamais fusionné \
en un produit, jamais réparti — objet {"quantity","unit","candidates":[...]} \
dans `ambiguous_groups`, jamais dans `product`/`additional_items`.
- `orphan_quantities` (CREATE_RECURRING_NEED) : une quantité+unité SANS nom de \
produit accolé. Ex: "150 kg de tomates et 200 kg chaque semaine" → "200 kg" va dans \
`orphan_quantities` ({"quantity":200.0,"unit":"KG"}), JAMAIS ajouté à `quantity`. Jamais \
un prix, une durée, ni un groupe de conditionnement du MÊME produit.
- `correction_scope` : "ALL" si l'utilisateur dit de tout remplacer, "ITEM" s'il \
vise un produit précis, sinon null.

EXEMPLE (groupes de conditionnements + prix de référence + tarifs) :
"60 bidons de 5 litres et 30 bidons de 20 litres. Prix : 3000 FCFA le \
litre, et 1 bidon de 5 L coûte 10000 FCFA, 1 bidon de 20 litres coûte \
50000 FCFA." → entities = {"quantity": 900.0, "unit": "LITRE", "price": \
3000.0, "price_unit": "LITRE", "pricing_tiers": [{"quantity": 5.0, "unit": \
"L", "price": 10000.0, "packaging": "bidon"}, {"quantity": 20.0, "unit": \
"L", "price": 50000.0, "packaging": "bidon"}]}.

EXEMPLE AMBIGUOUS vs NEW_TASK : "j'ai 90 L de miel" (sans verbe d'action) → \
AMBIGUOUS, candidate_goals=["SALES_PUBLISH_PRODUCT", "STOCK_REGISTER_HARVEST"], \
entities={"product": "miel", "quantity": 90.0, "unit": "LITRE"} ; "je veux \
VENDRE 90 L de miel" → NEW_TASK SALES_PUBLISH_PRODUCT, mêmes entities.
"""

_JSON_SCHEMA_BLOCK = """\
Réponds strictement avec cet objet JSON, sans aucun autre texte :
{"disposition": "NEW_TASK|CONFIRM|REJECT|OUT_OF_SCOPE|UNKNOWN|AMBIGUOUS", \
"intent": "<intention du catalogue|null>", "confidence": <0.0 à 1.0>, \
"candidate_goals": ["<intention du catalogue>", ...], \
"entities": {"product": "<str|null>", "additional_products": ["<str>", ...], \
"quantity": <float|null>, "unit": "<str|null>", "price": <float|null>, \
"price_unit": "<str|null>", "pricing_tiers": \
[{"quantity": <float|null>, "unit": "<str|null>", "price": <float|null>, \
"packaging": "<str|null>"}, ...], "estimated_available_at": \
"<YYYY-MM-DD|null>", "expected_harvest_date": "<YYYY-MM-DD|null>", \
"deadline": "<YYYY-MM-DD|null>", "zone": "<str|null>", \
"farm_name": "<str|null>", "recurrence_type": \
"<DAILY|WEEKLY_DAYS|WEEKLY|MONTHLY|ONE_OFF|null>", "weekly_days": [<1-7>, ...], \
"excluded_weekdays": [<1-7>, ...], "max_price_per_unit": <float|null>, \
"package_type": "<str|null>", "package_content_amount": <float|null>, "package_content_unit": "<str|null>", \
"cart_edit": <null|objet>, "is_correction": <true|null>, \
"starts_at": "<YYYY-MM-DD|null>", \
"additional_items": [{"product": "<str|null>", "quantity": <float|null>, \
"unit": "<str|null>"}, ...], "ambiguous_groups": [{"quantity": <float|null>, \
"unit": "<str|null>", "candidates": ["<str>", ...]}, ...], \
"orphan_quantities": [{"quantity": <float|null>, "unit": "<str|null>"}, ...], \
"correction_scope": "ALL|ITEM|null", "update_action": \
"PAUSE|RESUME|CANCEL|SKIP_OCCURRENCE|OVERRIDE_OCCURRENCE|null"}}
"""


def build_new_task_system_prompt(intent_catalog: Dict[str, str]) -> str:
    """`intent_catalog` : {intent_key: label} — DÉJÀ filtré par l'appelant
    via `interpreter/routing.py::_classifiable_intents()` (source unique,
    spec §3). Ce module ne connaît AUCUNE seconde liste d'intentions."""
    catalog_lines = "\n".join(
        f"  - {key} : {label}" for key, label in intent_catalog.items()
    )
    return (
        _SYSTEM_PROMPT_HEADER
        + "\nCATALOGUE OFFICIEL DES INTENTIONS (CONTRAT FERMÉ) :\n"
        + catalog_lines
        + "\n\n"
        + _JSON_SCHEMA_BLOCK
    )


def build_new_task_user_prompt(
    context: NewTaskPromptContext, normalized_text: str
) -> str:
    lines = [f"Date de référence : {context.reference_date}"]
    if context.cart_pending:
        lines.append(
            "Panier acheteur en attente de validation : OUI. Un accord libre (« ok vas-y », « c'est bon », « oui ») = CONFIRM, un refus libre "
            "(« non », « laisse tomber ») = REJECT, un nouveau produit = NEW_TASK, une question sur le TOTAL = BUYER_VIEW_CART. "
            "Un message qui DEMANDE UN CHANGEMENT (nouvelle valeur, « mets », « enlève », « plutôt », « finalement », « oui mais mets 10 », « non 5 », "
            "« c'est pas X, c'est Y ») n'est JAMAIS CONFIRM ni REJECT : c'est BUYER_EDIT_CART avec `cart_edit` (non + nombre seul = QUANTITY ; enlever = REMOVE)."
        )
    if context.draft_context:
        lines.append(
            f"Récapitulatif en attente de confirmation : {context.draft_context}. Un message qui ne donne que quelques valeurs "
            "(« à 300 francs », « 400 kg », « c'est oignon pas tomate », « non 250 kg ») CORRIGE ces champs du MÊME récapitulatif : NEW_TASK du même intent, "
            "`is_correction`: true, `entities` = UNIQUEMENT les champs corrigés. « non » + valeur est une correction, pas un refus. « c'est pas X, c'est Y » "
            "remplace le champ du récapitulatif qui VAUT X (lis le récapitulatif). « pas maintenant », « laisse tomber » sans valeur = REJECT."
        )
    if context.producer_order_action_pending:
        lines.append(
            "Vente(s) en attente de confirmation producteur : OUI — le "
            "producteur vient de voir une liste de ventes l'invitant "
            'explicitement à taper "confirmer" ou "annuler" (aucun numéro '
            "requis s'il n'y a qu'une seule vente en attente). Un message "
            'nu réduit à "confirmer"/"je confirme"/"j\'accepte" (rien '
            "d'autre) signifie NEW_TASK/PRODUCER_CONFIRM_ORDER ; réduit à "
            '"annuler"/"je refuse"/"je ne peux pas honorer" signifie '
            "NEW_TASK/PRODUCER_CANCEL_ORDER — jamais BUYER_CANCEL_ORDER "
            "(c'est une vente reçue, pas un achat), jamais une classification "
            "avec `entities` autres que vides (le résolveur identifie déjà "
            "seul la commande concernée)."
        )
    if context.previous_goal:
        lines.append(
            f"But métier précédemment actif (abandonné/dévié ce tour) : "
            f"{context.previous_goal} — aide SEULEMENT à comprendre une "
            f"formulation qui y fait explicitement référence (ex: « je "
            f"veux plutôt vendre »). Ne signifie PAS que ce tour porte sur "
            f"CE but précédent : si le message correspond clairement à une "
            f"AUTRE intention du catalogue (ex: confirmer/annuler une "
            f"commande reçue alors que le but précédent était une simple "
            f"consultation), classe ce nouveau but, jamais un CONFIRM/"
            f"REJECT visant le but précédent lui-même."
        )
    if context.screen_context:
        lines.append(
            f"Écran récurrent affiché à l'utilisateur : {context.screen_context}. Juge la RELATION du message avec cet "
            "écran : (a) il modifie la cible affichée sans nommer d'autre produit (nouvelle quantité, nouvelle "
            "fréquence, « change/modifie ça ») = UPDATE_RECURRING_NEED, produit laissé `null` ; un message trop vague "
            "pour savoir QUOI modifier = UPDATE_RECURRING_NEED sans quantité ni fréquence ni action, jamais une "
            "valeur inventée ; (b) il demande une recherche/actualisation d'un besoin = REFRESH_RECURRING_MATCHING "
            "(`product` seulement s'il est NOMMÉ) ; (c) il exprime un NOUVEAU besoin complet (produit + quantité + "
            "fréquence) = CREATE_RECURRING_NEED, même si l'écran affiche un autre produit ; (d) une demande d'achat, "
            "de vente ou de navigation = son intention propre ; (e) il accepte ou refuse SIMPLEMENT ce que l'écran propose, "
            "quelle que soit la formulation, sans rien ajouter ni changer = disposition CONFIRM ou REJECT ; s'il accepte "
            "MAIS apporte une valeur nouvelle (quantité, date, condition) c'est un changement = UPDATE_RECURRING_NEED "
            "(jamais CONFIRM) ; si un refus est suivi d'une demande de nouvelle recherche, c'est REFRESH_RECURRING_MATCHING ; "
            "une demande de ne faire QUE la livraison concernée (pas le besoin entier) = UPDATE_RECURRING_NEED avec "
            "update_action ; (f) il demande de chercher un autre fournisseur ou de relancer pour la livraison affichée "
            "ou signalée = REFRESH_RECURRING_MATCHING ; (g) il demande où en est / de voir un besoin NOMMÉ = "
            "GET_MY_NEEDS avec `product`. Un produit nommé DIFFÉRENT de la cible n'est jamais une modification de la cible."
        )
    lines.append(f'Message utilisateur :\n"""{normalized_text}"""')
    lines.append("\nRetourne le JSON strict.")
    return "\n".join(lines)


def build_new_task_repair_prompt(reason: str) -> str:
    return (
        f"Ta réponse précédente est invalide : {reason}\n\n"
        "Corrige UNIQUEMENT ce problème et renvoie le même objet JSON "
        "complet, strictement conforme au schéma déjà donné — sans texte "
        "ni markdown autour."
    )


__all__ = [
    "NEW_TASK_PROMPT_VERSION",
    "build_new_task_system_prompt",
    "build_new_task_user_prompt",
    "build_new_task_repair_prompt",
]
