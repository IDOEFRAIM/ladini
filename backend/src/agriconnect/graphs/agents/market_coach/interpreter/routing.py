"""Market — Interpreter & Routing.

Centralise l'appel LLM d'interprétation et le routage post-validator.
Le prompt système est construit dynamiquement à partir du catalogue
COMPLET de `INTENT_CONFIG` — PAS filtré par rôle (PRODUCER/BUYER).

(2026-09-08, refonte responsabilités des nœuds d'entrée, mandat §6) : ce
module filtrait auparavant le catalogue par rôle (compile-time, via
`allowed_intents_for_role`), empêchant par construction qu'un utilisateur
PRODUCER déclenche une intention BUYER (et réciproquement) — violation
directe du principe double-rôle (un même utilisateur peut acheter ET
vendre dans la même conversation). Le filtrage a été retiré du prompt LLM
ET du clamp post-LLM ; `role`/`user_role` ne reste qu'un signal SECONDAIRE
(repli dégradé sans LLM, `cart_pending`, logs) — jamais une restriction
structurelle de ce que le LLM peut reconnaître.

Entity normalisation lives in ``interpreter/entities.py``.
Product validation lives in ``services/domain/product_validation.py``.
The goal planner state machine lives in ``interpreter/goal_planner.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re as _re
from string import Template
from typing import Any, Dict, List, Optional

from agriconnect.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    get_pending_interaction,
    to_tunnel_category,
)
from agriconnect.graphs.agents.market_coach.core.slots import (
    SLOT_FILLING_INPUTS,
    get_slot_hint,
)
from agriconnect.graphs.agents.market_coach.core.state import (
    MarketAgentState,
    resolve_current_goal,
)

# Source UNIQUE du seuil de confiance de rupture d'intention. L'interpréteur
# (ici) et tunnel_manager (goal_planner) DOIVENT utiliser exactement le même :
# sinon l'interpréteur promeut un message en INTERRUPTION à un seuil que
# tunnel_manager refuse ensuite → zone morte produisant un "je n'ai pas compris"
# confus au lieu d'une bascule propre OU d'une continuation propre.
from agriconnect.graphs.agents.market_coach.core.tunnel_manager import (
    INTERRUPTION_CONFIDENCE_THRESHOLD,
)
from agriconnect.graphs.agents.market_coach.domain.tier_interaction import (
    pending_pack_count_tier,
)
from agriconnect.graphs.agents.market_coach.domain.selection_actions import (
    ActionType,
    build_selection_context,
    fast_path_action,
    tier_menu_prompt_block,
)
from agriconnect.graphs.agents.market_coach.interpreter.entities import (
    _fallback_quantity_unit_from_text,
    _remap_entities,
)
from agriconnect.graphs.agents.market_coach.interpreter.goal_planner import (
    _NAVIGATION_INTENTS,
    INTENT_TO_GOAL_MAP,
    _extract_buyer_product,
    _init_intent_to_goal_map,
    _looks_like_buyer_product_request,
    goal_planner,
)
from agriconnect.graphs.agents.market_coach.interpreter.intent import (
    INTENT_CONFIG,
    INTENT_ROLE,
)
from agriconnect.graphs.agents.market_coach.interpreter.interpreter_result import (
    InterpreterResult,
)
from agriconnect.graphs.agents.market_coach.interpreter.prompts import (
    INTERPRETER_USER_PROMPT,
)
from agriconnect.graphs.agents.market_coach.services.domain.product_validation import (
    _validate_and_sanitize_product,
)
from agriconnect.domain.quantity_unit import (
    extract_deterministic_pricing_tiers,
    extract_unit_only_from_text,
    parse_compound_quantity,
    parse_quantity_unit_from_text,
    scan_number_candidates,
)
from agriconnect.graphs.agents.market_coach.utils import (
    MarketRuntime,
    canonical_unit_label,
)

logger = logging.getLogger("AgriConnect.Market.InterpreterRouting")

# =====================================================================
# CONFIRMATION KEYWORDS — filet déterministe (voir _interpret_fast_path)
# Vocabulaire fermé, EXACT MATCH uniquement sur texte normalisé — reprend
# mot pour mot les exemples déjà documentés dans le prompt LLM ci-dessous
# (§ "CONFIRM"/"REJECT"), jamais une liste inventée séparément.
# =====================================================================
_CONFIRM_EXACT_PHRASES = frozenset(
    {
        "oui", "ok", "okay", "d'accord", "daccord", "je confirme",
        "je suis d'accord", "je suis daccord", "c'est bon", "cest bon",
        "ca va", "ça va", "parfait", "vas-y", "vasy", "valide", "confirmer",
        "confirme", "confirmé", "yes",
    }
)
_REJECT_EXACT_PHRASES = frozenset(
    {
        "non", "annule", "annuler", "stop", "pas d'accord", "pas daccord",
        "je ne confirme pas", "je refuse", "no",
    }
)

# =====================================================================
# ROLE-BASED INTENT FILTERING (anti-cross-pollution AG-UI)
# =====================================================================

# Sets DERIVED from intent.INTENT_ROLE — single source of truth.
# DO NOT hand-edit; modify intent.INTENT_ROLE in intent.py instead.
PRODUCER_INTENTS: frozenset = frozenset(
    k for k, role in INTENT_ROLE.items() if role == "PRODUCER"
)
BUYER_INTENTS: frozenset = frozenset(
    k for k, role in INTENT_ROLE.items() if role == "BUYER"
)
COMMON_INTENTS: frozenset = frozenset(
    k for k, role in INTENT_ROLE.items() if role == "BOTH"
)

# Populate the goal planner's INTENT_TO_GOAL_MAP now that role sets exist.
_init_intent_to_goal_map(PRODUCER_INTENTS, BUYER_INTENTS, COMMON_INTENTS)


# Intents confirmés non-utilisés en production (2026-08-30, revue coût LLM —
# voir [[pricing-tiers-litre-fastpath-bug-2026-08]] round 6 : le catalogue
# unifié ~1700 tokens contribuait à saturer le quota Groq 8000 TPM/modèle sur
# un seul appel interpréteur). Retirés du catalogue vu par le LLM — donc plus
# jamais CLASSABLES depuis un message utilisateur — mais INTENT_CONFIG /
# actions/agro.py /finance.py/system.py restent intacts (pas de suppression de
# code, juste de surface de classification) au cas où un appel programmatique
# interne les utiliserait encore ailleurs.
_DISABLED_INTENT_PREFIXES: frozenset = frozenset({"AGRO_", "FINANCE_", "SYSTEM_"})

# (2026-09-04, Phase 4 — alignement catalogue/produit) : goals DÉPRÉCIÉS
# individuellement. Même mécanisme que les préfixes ci-dessus (retirés du
# catalogue vu par le LLM, donc jamais classables depuis un message
# utilisateur) — `INTENT_CONFIG`, les handlers et les services restent
# INTACTS, aucun code métier n'est supprimé.
#
# Critère unique d'entrée dans cette liste : le goal est atteignable par un
# utilisateur mais ne peut PAS aboutir à un résultat métier réel — soit son
# `tool_name` ne correspond à aucun outil MCP existant (audit de
# reachability Phase 3), soit son handler est volontairement neutralisé.
# Autrement dit : ce sont des « faux boutons » conversationnels.
# Justification détaillée par goal : docs/PRODUCT_INTENT_SCOPE_2026-09-04.md
_DEPRECATED_INTENTS: frozenset = frozenset(
    {
        # — Agronomie de suivi (interventions, stades, sol) : AUCUNE méthode
        # DB correspondante (`log_intervention`, `add_growth_log`,
        # `add_crop_growth_stage`, `update_soil_profile`, `create_crop_cycle`
        # n'existent nulle part). La capacité « culture » réellement
        # supportée est `DECLARE_CROP_CYCLE` -> `declare_future_production`,
        # qui reste exposée.
        "CROP_START_CYCLE",
        "CROP_RECORD_INTERVENTION",
        "CROP_RECORD_OBSERVATION",
        "CROP_UPDATE_STAGE",
        "CROP_UPDATE_SOIL",
        # — Écritures d'inventaire : les vraies méthodes existent
        # (`adjust_stock`, `remove_stock`, `delete_stock`,
        # `add_stock_movement`) mais `intent.py` pointe des variantes
        # `*_by_id` inexistantes. NON recâblées : décision produit explicite
        # de ne pas exposer un ledger d'inventaire tant que son lien avec
        # les ventes (qui débitent `Product.quantity_for_sale`, jamais
        # `Stock`) n'est pas défini.
        "STOCK_RECORD_MOVEMENT",
        "STOCK_ADJUST",
        "STOCK_REMOVE_PARTIAL",
        "STOCK_DELETE",
        "STOCK_UPDATE_LEVEL",
        # — `get_farm_stocks` n'existe pas ; `STOCK_GET_SUMMARY`
        # (`get_stocks`) couvre déjà la consultation et reste exposé.
        "STOCK_GET_DETAIL",
        # — Seul cas de la famille dont l'outil FONCTIONNE
        # (`get_stock_movements`) : il ne manquait que le câblage
        # (`_RESOLVER_PASSTHROUGH` + branche `_resolve_stock`), soit ~2
        # lignes. Déprécié malgré tout par COHÉRENCE : un historique de
        # mouvements n'a de sens que si les mouvements sont enregistrables
        # et corrigeables, or `STOCK_RECORD_MOVEMENT`/`STOCK_ADJUST` sont
        # justement hors catalogue. À ré-exposer d'un bloc avec le reste du
        # ledger si la décision produit va dans ce sens.
        "STOCK_GET_MOVEMENTS",
        # — Lectures sans implémentation. `MARKET_SNAPSHOT_ZONAL` est un
        # doublon strict de `MARKET_SNAPSHOT` (`get_market_snapshot`, même
        # `required=['zone']`), qui reste exposé : aucune capacité perdue.
        "MARKET_SNAPSHOT_ZONAL",
        "SEARCH_NEARBY",  # `get_all_zone_market_overview` absent ; exige lat/lon jamais saisis
        "DASHBOARD_PRODUCER",  # `get_producer_dashboard` absent, aucun agrégat équivalent
        "PROFILE_GET_TRUST",  # `get_trust_score` absent
        "PROFILE_GET_CONTEXT",  # `get_user_context` absent — enrichissement interne, pas une action utilisateur
        # — Bascule de rôle : écrit une ligne `agent_actions` inerte que
        # RIEN ne consomme (prouvé : tests/evals/blocked/PROFILE_SWITCH_ROLE.md),
        # et son `create_agent_action` n'existe pas non plus comme outil.
        "PROFILE_SWITCH_ROLE",
        # — « Contrat verrouillé » : aucune entité Contract/StagedTransaction
        # n'existe dans le dépôt, `commit_staged_transaction` non plus.
        # Décision produit requise avant toute implémentation.
        "SALES_ACCEPT_CONTRACT",
        # — Winner-selection hors tunnel : handlers volontairement neutralisés
        # par F4 (anti-bypass). Ils restaient CLASSABLES, donc un utilisateur
        # pouvait encore les atteindre pour ne récolter qu'une erreur
        # technique — ils sortent maintenant aussi du catalogue.
        "PROCUREMENT_SELECT_WINNER",
        "PROCUREMENT_ACCEPT_OFFER",
    }
)


def allowed_intents_for_role(role: str) -> frozenset:
    """Retourne l'ensemble des intentions reconnaissables par l'interpréteur.

    Refonte double-rôle : tout utilisateur peut vendre ET acheter, message par
    message — l'interpréteur doit donc TOUJOURS voir le catalogue complet
    (PRODUCER ∪ BUYER ∪ BOTH), quel que soit le paramètre `role` reçu (conservé
    pour compat de signature / logs uniquement). Le filtrage par rôle n'existe
    plus en amont de la classification LLM ; la sécurité se fait désormais en
    aval, au niveau de l'action (voir `nodes/role_guard.py`).
    """
    missing = set(INTENT_CONFIG) - set(INTENT_ROLE)
    if missing:
        logger.warning(
            "Intents missing INTENT_ROLE entry (defaulting to PRODUCER): %s",
            sorted(missing),
        )
    all_intents = PRODUCER_INTENTS | BUYER_INTENTS | COMMON_INTENTS | frozenset(missing)
    return frozenset(
        intent
        for intent in all_intents
        if not any(intent.startswith(p) for p in _DISABLED_INTENT_PREFIXES)
        and intent not in _DEPRECATED_INTENTS
    )


# Goal de CRÉATION (pas encore persisté, en attente de CONFIRMATION) → intent
# "jumeau" décrit dans le catalogue comme une mise à jour d'une entité déjà
# EXISTANTE. Une correction en langage libre pendant la confirmation ("non
# c'est 200 tonnes") ressemble structurellement à ce jumeau pour le LLM — voir
# le garde-fou dans `make_input_interpreter` juste avant le forçage
# INTERRUPTION sur CONFIRMATION.
_PENDING_CREATE_UPDATE_SIBLINGS: Dict[str, str] = {
    "SALES_PUBLISH_PRODUCT": "SALES_UPDATE_PRODUCT",
    "DECLARE_CROP_CYCLE": "SALES_UPDATE_PRODUCTION",
    "FARM_CREATE": "FARM_UPDATE",
}


# =====================================================================
# DYNAMIC SYSTEM PROMPT BUILDER (Role-aware)
# =====================================================================

_PROMPT_CACHE: Dict[str, str] = {}


_SYSTEM_PROMPT_TEMPLATE = Template("""\
Tu es l'Interprète conversationnel de Market Sense, un assistant WhatsApp
pour des $role_label_plural agricoles au Burkina Faso. Le canal est bruité (slang,
fragments, audios mal transcrits).

Tu devez classer le message utilisateur et extraire des entités structurées.

Ta sortie OBLIGATOIRE est un JSON strict avec EXACTEMENT ces clés :
{
  "interpreted_event": "<NEW_TASK | ANSWER | CONFIRM | REJECT | SELECTION | UPDATE | INTERRUPTION | OUT_OF_SCOPE | UNKNOWN>",
  "detected_intent": "<une intention du catalogue ci-dessous OU 'UNKNOWN'>",
  "interpreter_confidence": <float entre 0.0 et 1.0>,
  "validation_status": "<VALID|INVALID_MISSING_UNIT|INVALID_AMBIGUOUS_UNIT>",
  "extracted_entities": {
      "product": "<str|null>",
      "additional_products": ["<str>", ...],
      "quantity": <float|null>,
      "unit": "<KG|TONNE|SAC|PANIER|null>",
      "price": <float|null>,
      "estimated_available_at": "<YYYY-MM-DD|null>",
      "expected_harvest_date": "<YYYY-MM-DD|null>",
      "deadline": "<YYYY-MM-DD|null>",
      "zone": "<str|null>",
      "farm_name": "<str|null>",
      "price_unit": "<KG|TONNE|SAC|PANIER|null>",
      "pricing_tiers": [{"quantity": <float>, "unit": "<str>", "price": <float>, "packaging": "<str|null>"}, ...],
      "selection_index": <int|null>,
      "selected_value": "<str|null>",
      "movement_type": "<IN|OUT|null>",
      "reason": "<str|null>",
      "agent_action": "<SELECT_PRODUCER|SELECT_PRICING_TIER|SET_PACKAGE_COUNT|SET_QUANTITY|null>",
      "action_producer_id": "<str|null>",
      "action_pricing_tier_id": "<str|null>",
      "action_package_count": <float|null>,
      "action_quantity": <float|null>,
      "action_unit": "<str|null>"
  }
}

Les 6 champs `agent_action`/`action_*` ne sont utilisés QUE quand le contexte
agent fournit un bloc `action_structuree_attendue` (voir règle 4bis) — sinon
laisse-les tous `null`.

═══════════════════════════════════════════════════════════════
CATALOGUE OFFICIEL DES INTENTIONS POUR $role_label (CONTRAT FERMÉ) :
═══════════════════════════════════════════════════════════════
$intent_catalog

═══════════════════════════════════════════════════════════════
RÈGLES STRICTES DE CLASSIFICATION :
═══════════════════════════════════════════════════════════════
1. **interpreted_event** qualifie le TYPE conversationnel du message :
   - NEW_TASK     : l'utilisateur initie une nouvelle action métier.
   - ANSWER       : réponse à une question de type saisie/formulaire (slot-filling).
   - CONFIRM      : validation explicite du récapitulatif (oui, ok, d'accord, c'est bon, confirmer...).
   - REJECT       : refus ou annulation explicite (non, annule, stop, pas d'accord, quitter...).
   - SELECTION    : choix d'un élément dans une liste ou un menu AG-UI (index ou nom).
   - UPDATE       : correction explicite d'une info déjà fournie (ex: "Non pas 10 sacs mais plutôt 15").
   - INTERRUPTION : changement brusque de sujet en plein milieu d'un tunnel actif.
   - OUT_OF_SCOPE : message hors-domaine (politique, sport, religion, salutations vides sans but).
   - UNKNOWN      : impossible de comprendre ou message totalement incohérent.

1bis. **PANIER EN ATTENTE DE VALIDATION** : si le contexte agent indique
   `panier_en_attente = OUI`, l'utilisateur vient de voir son panier et on lui a
   proposé de le valider (précommander). Interprète alors son message en langage
   LIBRE, sans exiger un mot précis :
   - Tout accord / envie d'aller au bout (« oui », « je suis d'accord », « ok
     vas-y », « c'est bon », « valide », « on y va », « parfait ») →
     interpreted_event = CONFIRM.
   - Tout refus / abandon (« non », « annule », « laisse tomber », « pas
     maintenant ») → interpreted_event = REJECT.
   - S'il ajoute un nouveau produit (« ajoute 10 kg de riz ») → NEW_TASK.
   Ne renvoie JAMAIS UNKNOWN pour un simple accord/refus dans ce contexte.

1ter. **RÉPONSE À UN MENU DE SÉLECTION ACTIF (CRITIQUE, PRIORITÉ MAXIMALE)** :
   si le contexte agent indique `expected_input = SELECTION`, l'acheteur
   vient de voir une liste numérotée (paliers, producteurs, commandes,
   options de menu...) et répond à CE choix précis — jamais une nouvelle
   recherche ni un message hors-sujet. Tout message qui exprime un choix, un
   ordre ou un chiffre (« le deuxième », « la seconde option », « le 2 »,
   « je prends le premier », « le gros bidon », un chiffre nu, une
   description qui correspond clairement à une des options listées) DOIT
   être classé :
   - `interpreted_event` = `SELECTION` (le cas général) ou `ANSWER` (si le
     choix se formule comme une réponse directe à la question posée, ex:
     une quantité rattachée au choix) — jamais `UNKNOWN`, jamais
     `NEW_TASK`, jamais `OUT_OF_SCOPE` pour ce genre de message.
   - `interpreter_confidence >= 0.90`.
   - `detected_intent` = l'intention ACTIVE en cours (`current_goal` fourni
     dans le contexte agent ci-dessus) — jamais `UNKNOWN` : le tunnel est
     déjà ouvert sur cette intention, la réponse au menu en fait partie
     intégrante, pas une nouvelle classification.
   - L'entité choisie va dans `extracted_entities.selection_index` (chiffre
     ou ordinal — position 1-indexée dans la liste) OU
     `extracted_entities.selected_value` (texte/description), jamais les
     deux, selon la règle 4 ci-dessous (anti-hallucination d'IDs).
   Ne bascule JAMAIS vers un intent générique de nouvelle demande (ex:
   BUYER_REQUEST) ni vers `NEW_TASK` tant que `expected_input = SELECTION` :
   une réponse à un menu de choix n'est structurellement PAS une nouvelle
   recherche, même si sa formulation ressemble à une phrase d'achat libre.

2. **Extraction des entités (OBLIGATOIRE)** :
   - Tu dois copier TOUT nom de culture/produit détecté (tomates, maïs, riz, oignons...) dans `extracted_entities.product`.
   - Si un mot suit "de", "du", "des", "d'" après une quantité ou une unité, considère-le comme un candidat produit et renseigne `product`.
   - N'utilise PAS d'autres clés (« product_name », « item_name »...) dans la sortie JSON : seule la clé `product` est contractuelle.
   - `product` ne doit être `null` que si aucun produit explicite n'est présent dans le texte.
   - **TERMES D'ADRESSE — jamais un produit (CRITIQUE)** : "patron", "boss",
     "chef", "monsieur", "madame" et autres formules pour s'adresser à
     quelqu'un ne sont JAMAIS des noms de produit, même juste après "vendre
     des produits" ou "je vends". Exemple : "vendre des produits boss" ->
     l'utilisateur t'appelle "boss", il ne vend PAS un produit qui s'appelle
     "boss" -> `product`: null (redemande le nom du produit). Idem si un nom
     de produit déjà donné est ensuite mal recopié comme "boss"/"patron" par
     erreur — ne le réutilise jamais comme candidat produit.
   - **PLUSIEURS PRODUITS DANS LE MÊME MESSAGE (CRITIQUE — jamais de fusion)** :
     si l'utilisateur mentionne PLUSIEURS produits distincts (ex: "j'ai besoin
     d'œufs et de laitue", "50kg de maïs et 30kg d'oignons"), `product` ne
     contient QUE le PREMIER produit mentionné, comme un nom UNIQUE et
     PROPRE (jamais une liste, jamais séparé par une virgule ou "et" —
     interdiction absolue d'écrire "laitue, œufs" ou "maïs et oignons" dans
     `product`). Mets chaque produit SUPPLÉMENTAIRE (nom seul, sans
     quantité/unité) dans le tableau `extracted_entities.additional_products`
     — vide (`[]`) s'il n'y en a qu'un seul ou aucun. Le système traite les
     produits UN PAR UN ; jamais simultanément dans une seule recherche.
   - **Nom de domaine/exploitation** : si `expected_input` vaut `FARM_NAME`, ou si `last_agent_question` demande le nom de la ferme/exploitation/domaine, tout texte libre fourni (même un seul mot, ex: "Matata", "Ferme du Soleil") EST ce nom — copie-le TEL QUEL dans `extracted_entities.farm_name` et mets `interpreted_event = ANSWER`. Ne renvoie JAMAIS OUT_OF_SCOPE/UNKNOWN dans ce contexte pour un mot ou une courte phrase qui ne correspond à aucune autre intention du catalogue : c'est un nom propre, pas un message hors-sujet.

3. **detected_intent** identifie l'intention métier PRÉCISE.

4. **ANTI-HALLUCINATION D'IDS** : Tu n'inventes jamais d'ID technique. Si choix de l'IHM :
   - chiffre pur ou ordinal ("le 2ème", "option 1") → `selection_index` (int).
   - nom propre ou texte ("l'offre de Diallo") → `selected_value` (str).

4bis. **CONTRAT D'ACTION STRUCTURÉE (CRITIQUE, PRIORITÉ ABSOLUE sur la règle 4
   ci-dessus)** : si le contexte agent fournit un bloc
   `action_structuree_attendue` (voir juste après ce message), le tunnel
   d'achat attend UNE action précise parmi `SELECT_PRODUCER`,
   `SELECT_PRICING_TIER`, `SET_PACKAGE_COUNT`, `SET_QUANTITY` — la ligne
   "ACTION ATTENDUE" du bloc te dit LAQUELLE. Tu n'inventes JAMAIS une autre
   action que celle-ci ou `SELECT_PRICING_TIER` (seule action toujours
   autorisée en plus, pour un changement d'avis explicite pendant
   `SET_PACKAGE_COUNT` — voir plus bas). Ceci REMPLACE `selection_index`/
   `selected_value` pour ce message : n'utilise PAS ces deux champs ici.
   - `extracted_entities.agent_action` = le nom EXACT de l'action choisie
     (string, une des 4 valeurs ci-dessus).
   - `SELECT_PRODUCER` → `extracted_entities.action_producer_id` = le
     `producer_id` EXACT listé dans le bloc, copié tel quel — jamais un id
     que tu inventes, jamais un id d'une AUTRE liste.
   - `SELECT_PRICING_TIER` → `extracted_entities.action_pricing_tier_id` =
     le `pricing_tier_id` EXACT listé. Un message comme "le premier, c'est
     à dire 5 L" ou "le bidon de 5 L" ou "celui à 450 FCFA" doit être mappé
     sémantiquement à EXACTEMENT UN palier de la liste fournie.
   - `SET_PACKAGE_COUNT` → `extracted_entities.action_package_count` = le
     NOMBRE DE PAQUETS (float). **Un chiffre nu à cette étape EST ce
     nombre** — ne le confonds JAMAIS avec un index de menu ni avec une
     quantité en unité de mesure. Exception : si l'acheteur désigne
     EXPLICITEMENT un AUTRE conditionnement de la liste ("finalement le
     bidon de 5 L"), retourne plutôt `SELECT_PRICING_TIER` avec le nouveau
     `pricing_tier_id`.
   - `SET_QUANTITY` → `extracted_entities.action_quantity` (float) et,
     seulement si littéralement écrite dans CE message,
     `extracted_entities.action_unit`.
   - Si le message ne correspond CLAIREMENT à aucune option listée (aucun
     `agent_action` fiable) → `interpreted_event` = `UNKNOWN`,
     `detected_intent` = `UNKNOWN`, n'invente rien. Ne classe JAMAIS ce
     message comme une nouvelle tâche (`NEW_TASK`) tant que ce bloc est
     actif, même si sa formulation ressemble à une nouvelle recherche
     produit.
   - **N'ajoute JAMAIS `quantity`/`unit`/`selection_index`/`selected_value`
     en plus d'un `agent_action`** — même si le message contient un nombre
     qui a servi à identifier l'option (ex: "5 L" dans "le premier, c'est à
     dire 5 L" identifie le PALIER, ce n'est pas une quantité d'achat
     séparée). Faire les deux à la fois a produit un incident réel
     (2026-09-01) : le palier ET une fausse quantité de 5 étaient tous deux
     remplis pour le même "5 L", créant une contradiction que le code
     devait ensuite arbitrer à l'aveugle. Un seul champ, jamais deux
     interprétations concurrentes du même message.

5. **Segmentation stricte des quantités** :
   - "product" doit être un libellé pur (ex: "tomates"), SANS chiffres ni unités.
   - "quantity" est un float (ex: 50.0).
   - **RÈGLE D'OR DE L'UNITÉ (LECTURE LITTÉRALE, JAMAIS DE DEVINETTE)** :
     `unit` ne peut valoir que ce qui est ÉCRIT LITTÉRALEMENT dans CE message
     ("kg", "kilo", "tonne", "sac", "panier", "tête", "unité"). Tu n'as PAS le
     droit de DÉDUIRE, DEVINER, ni de REPRENDRE une unité mentionnée à un tour
     PRÉCÉDENT. Exemple : si un tour précédent parlait de tonnes mais que CE
     message dit "200 kg", alors unit="KG" — jamais "TONNE". Si "200" apparaît
     SANS aucun mot d'unité dans ce message, alors `unit` = null OBLIGATOIREMENT
     (ne mets jamais "KG"/"TONNE" par défaut — c'est le rôle du système, pas le
     tien) ET validation_status = INVALID_MISSING_UNIT.
   - Si l'unité est ambiguë ou contradictoire → validation_status = INVALID_AMBIGUOUS_UNIT.
   - Sinon (unité littéralement présente) → validation_status = VALID.
   - **DÉSAMBIGUÏSATION QUANTITÉ vs PRIX (CRITIQUE)** : un message peut contenir
     PLUSIEURS nombres à la fois (ex: "892 kg de maïs, le prix minimum est 250
     FCFA par kg"). Ne JAMAIS assigner le nombre suivi d'une unité de poids/volume/
     comptage ("kg", "tonne", "sac", "panier", "tête", "unité"...) au champ
     `price`, et ne JAMAIS assigner le nombre suivi d'une devise/mot de prix
     ("FCFA", "franc", "prix", "par kg", "l'unité"...) au champ `quantity`. Si
     le message contient les deux, les DEUX champs doivent être mis à jour
     simultanément — jamais un seul au détriment de l'autre. Exemple : "892 kg
     de maïs, le prix minimum est 250 FCFA par kg" → quantity=892.0, unit="KG",
     price=250.0 (jamais price=892.0).
   - **UNITÉ DU PRIX DIFFÉRENTE DE CELLE DE LA QUANTITÉ** : la quantité TOTALE
     et le prix UNITAIRE peuvent légitimement porter des unités différentes
     (ex: vente de 200 TONNES au total, mais prix fixé à 10000 FCFA par KG).
     Si le message précise une unité pour le prix qui diffère de celle de la
     quantité, renseigne `unit` avec l'unité de la QUANTITÉ et `price_unit`
     avec l'unité du PRIX — ne fusionne JAMAIS les deux dans un seul champ
     `unit`. Si le prix ne précise aucune unité propre, laisse `price_unit`
     à `null` (il sera supposé identique à `unit`). Exemple : "je vends 200
     tonnes de produit au prix de 10000 FCFA/kg" → quantity=200.0, unit=
     "TONNE", price=10000.0, price_unit="KG".

5bis. **PLUSIEURS TARIFS/CONDITIONNEMENTS POUR UN MÊME PRODUIT (CRITIQUE — jamais
   d'écrasement)** : si l'utilisateur donne PLUSIEURS couples quantité+unité+prix
   pour le MÊME produit (ex: "500f le demi-litre en sachet et 600f le bidon",
   "5000 FCFA le sac de 50kg ou 600 FCFA le kg au détail"), NE GARDE PAS
   seulement le dernier ou le premier : liste CHAQUE déclinaison comme un objet
   distinct dans `pricing_tiers` — `quantity`/`unit`/`price` pour cette
   déclinaison précise, `packaging` pour le conditionnement s'il est mentionné
   ("sachet", "bidon", "sac"...) sinon `null`. `unit` y suit EXACTEMENT la même
   règle d'or que ci-dessus (lecture littérale du mot employé — "demi-litre"
   reste "demi-litre", "L" reste "L", jamais reformulé, arrondi ou converti
   vers une autre unité comme "SAC"/"KG"). Quand il n'y a qu'UN SEUL tarif
   dans le message, laisse `pricing_tiers` à `[]` (les champs `quantity`/
   `unit`/`price` racine suffisent, pas de duplication).

6. **NORMALISATION STRICTE DES DATES (OBLIGATOIRE)** :
   - L'année de référence est **2026**.
   - Toute date, estimation de disponibilité ou de récolte formulée en langage naturel (ex: "29 octobre", "fin octobre", "demain", "dans 3 jours") doit être **impérativement convertie au format ISO standard : YYYY-MM-DD**.
   - `estimated_available_at` : Date de disponibilité estimée pour l'acheteur (ex: "disponible le 29 octobre" ou "prêt le 29 oct" → "2026-10-29").
   - `expected_harvest_date` : Date prévue pour la récolte physique (ex: "récolte prévue en octobre" → "2026-10-15" (milieu de mois par défaut si imprécis)).
   - `deadline` : Date LIMITE d'un appel d'offres (jusqu'à quand les producteurs peuvent répondre, ex: "avant le 30 septembre", "réponses jusqu'au 15 oct" → "2026-09-30"/"2026-10-15").
   - N'envoie JAMAIS de texte libre ou de noms de mois écrits en toutes lettres dans ces champs. Si non spécifié ou impossible à déterminer, mets `null`.

7. Tu réponds UNIQUEMENT le JSON, sans markdown, sans explication.

EXEMPLES OBLIGATOIRES (FORMAT STRICT — champs omis ci-dessous = null/[],
le schéma complet est déjà donné plus haut, ne le redemande pas) :
"50kg de patates pour le 29 octobre" → {"interpreted_event":"NEW_TASK","detected_intent":"DECLARE_CROP_CYCLE","interpreter_confidence":0.95,"validation_status":"VALID","extracted_entities":{"product":"patates","quantity":50.0,"unit":"KG","estimated_available_at":"2026-10-29"}}
"20 tomates" → {"interpreted_event":"NEW_TASK","detected_intent":"DECLARE_CROP_CYCLE","interpreter_confidence":0.85,"validation_status":"INVALID_MISSING_UNIT","extracted_entities":{"product":"tomates","quantity":20.0,"unit":null}}
"je cherche des œufs et de la laitue dans ma région" → {"interpreted_event":"NEW_TASK","detected_intent":"BUYER_REQUEST","interpreter_confidence":0.9,"validation_status":"VALID","extracted_entities":{"product":"œufs","additional_products":["laitue"]}}
""")


_UNIFIED_PROMPT_CACHE_KEY = "UNIFIED"


def _build_dynamic_interpreter_prompt(role: str = "PRODUCER") -> str:
    """Construit le prompt système avec le catalogue COMPLET des intentions.

    (2026-09-08, mandat §6 "moteur de compréhension, pas orchestrateur") :
    le paramètre `role` n'a plus d'effet sur le contenu — CORRECTIF réel,
    pas seulement un commentaire : ce filtrage `allowed_intents_for_role`
    survivait encore ici malgré un commentaire déjà présent prétendant le
    contraire, empêchant par exemple un PRODUCTEUR de voir son message
    classé BUYER_REQUEST (catalogue LLM amputé des intents achat). Chaque
    utilisateur peut vendre ET acheter dans la même conversation ; le rôle
    de profil ne doit plus filtrer STRUCTURELLEMENT les intentions
    disponibles (`user_role` reste lisible comme contexte secondaire par
    ailleurs, ex. `cart_pending` plus bas dans ce module).
    """
    if _UNIFIED_PROMPT_CACHE_KEY in _PROMPT_CACHE:
        return _PROMPT_CACHE[_UNIFIED_PROMPT_CACHE_KEY]

    intent_lines: List[str] = []
    for intent_key, config in INTENT_CONFIG.items():
        label = config.get("label", intent_key)
        required = config.get("required") or []
        req_str = ", ".join(required) if required else "aucun"
        intent_lines.append(f"  - {intent_key} : {label} [requis: {req_str}]")

    intent_catalog = "\n".join(intent_lines)

    prompt = _SYSTEM_PROMPT_TEMPLATE.substitute(
        role_label="PRODUCTEUR OU ACHETEUR",
        role_label_plural="PRODUCTEURS ET ACHETEURS",
        intent_catalog=intent_catalog,
    )
    _PROMPT_CACHE[_UNIFIED_PROMPT_CACHE_KEY] = prompt
    return prompt


# =====================================================================
# FAST-PATH SÉCURISÉ (Zéro Heuristique de Token Floue)
# =====================================================================


def _interpret_fast_path(
    state: Dict[str, Any],
    text: str,
    *,
    skip_numeric_shortcut: bool = False,
    llm_available: bool = False,
) -> Optional[Dict[str, Any]]:
    """Court-circuite le LLM uniquement pour les actions structurelles pures d'AG-UI.

    `skip_numeric_shortcut` désactive le raccourci regex PRICE/QUANTITY quand
    un LLM est disponible sur le runtime : celui-ci est la source prioritaire
    pour extraire quantity+unit et price+price_unit ENSEMBLE (il voit tout le
    message en contexte) ; ce raccourci ne doit plus servir que de repli
    quand le LLM est indisponible.
    """
    # (2026-09-02, "no legacy shim") : source unique, dérivée de
    # `pending_interaction` — un seul point de traduction pour toutes les
    # comparaisons plus bas dans cette fonction.
    expected = to_tunnel_category(get_pending_interaction(state))
    clean = text.strip().lower()
    locked_goal = resolve_current_goal(state)

    if not clean:
        return None

    # (2026-09-02, validation réelle — mandat §16) : filet déterministe pour
    # un vocabulaire FERMÉ, court, quasi-universel d'accord/refus pendant une
    # CONFIRM_ACTION (`PendingInteraction`, source canonique — voir `expected`
    # ci-dessus, dérivé de `to_tunnel_category`). Le LLM reste PRIMAIRE et
    # documente déjà ce même vocabulaire dans son propre prompt (voir plus
    # haut : "oui, ok, d'accord, c'est bon, confirmer...") — ceci n'ajoute
    # AUCUN mot que le LLM ne devrait pas déjà reconnaître, c'est un filet de
    # fiabilité pour le non-déterminisme MoE connu de Groq (même phrase,
    # même température=0.0, classification qui varie d'un appel à l'autre —
    # observé en prod), PAS une résolution d'intention "compliquée" ni une
    # tentative de deviner une sélection en langage libre (§17 : "ne pas
    # ajouter de regex spécifiques" reste respecté — ce n'est pas une regex,
    # c'est une égalité stricte sur un texte NORMALISÉ, pas une correspondance
    # partielle risquant un faux positif sur une phrase plus longue).
    if expected == "CONFIRMATION":
        _bare = clean.strip(" .!?,;: ")
        if _bare in _CONFIRM_EXACT_PHRASES:
            return {
                "interpreted_event": "CONFIRM",
                "detected_intent": str(locked_goal or "UNKNOWN").upper(),
                "interpreter_confidence": 0.99,
                "extracted_entities": {},
                "raw_analysis": {"path": "fast_path_confirmation_keyword"},
            }
        if _bare in _REJECT_EXACT_PHRASES:
            return {
                "interpreted_event": "REJECT",
                "detected_intent": str(locked_goal or "UNKNOWN").upper(),
                "interpreter_confidence": 0.99,
                "extracted_entities": {},
                "raw_analysis": {"path": "fast_path_confirmation_keyword"},
            }

    # WAITING_FOR_PACKAGE_COUNT (audit 2026-09-01) : un palier est déjà résolu
    # et on attend son NOMBRE DE PAQUETS. Le même texte "2" ne veut pas dire la
    # même chose selon l'état — index de palier sous SELECTION, nombre de
    # paquets ici — et un nombre de paquets n'a JAMAIS d'unité. Voir
    # domain/pricing_tiers.py::pending_pack_count_tier (source unique).
    pack_count_tier = pending_pack_count_tier(state)

    # Correction chiffrée pendant la CONFIRMATION ("j'ai plutôt 795 kg", "non
    # j'ai 795 kg" sur un récap déjà affiché) : un nombre typé sans ambiguïté
    # (unité de poids/comptage OU devise à proximité) est une correction
    # directe du brouillon, pas une nouvelle tâche — à traiter ici en
    # UPDATE, avant même de risquer une reclassification LLM (le LLM a
    # laissé cette correction sans effet en prod : le récap restait figé sur
    # l'ancienne quantité malgré 2 corrections explicites successives).
    _confirmation_correction = expected == "CONFIRMATION" and bool(
        _re.search(r"\d", clean)
    )

    # Priorité slot-filling : en attente de quantité/prix, un nombre doit rester une ANSWER
    if expected in ("PRICE", "QUANTITY") or _confirmation_correction:
        # Balaye CHAQUE nombre du message et le type par son voisinage immédiat
        # (unité de poids/comptage ⇒ quantité ; devise fcfa/cfa à proximité ⇒
        # prix). Primitive déterministe currency-aware PARTAGÉE :
        # services/domain/quantity_unit.scan_number_candidates — même logique
        # exacte qu'avant (fenêtre 18 chars, pas de `\b` en tête pour capter
        # "60kg"/"10000fcfa", unité TOUJOURS conservée même près d'une devise
        # pour que "10000fcfa/kg" garde son price_unit). Un message composé
        # ("775 kg ... 175 fcfa") est ainsi désambiguïsé nombre par nombre. La
        # regex d'extraction ne vit plus qu'à UN endroit (voir tests de
        # caractérisation). Les branches ci-dessous décident quantité vs prix
        # vs compound à partir de `near_currency`/`unit`.
        candidates: List[Dict[str, Any]] = [
            {"value": c.value, "near_currency": c.near_currency, "unit": c.unit}
            for c in scan_number_candidates(clean)
        ]

        # Multi-tarifs ("25 L à 500 fcfa ET 40 L à 900 fcfa") : 2+ nombres
        # accolés à une devise. Ce fast-path déterministe ne sait produire
        # QU'UNE paire quantité/prix — le forcer ici pickait arbitrairement le
        # premier nombre "près d'une devise" comme LE prix (incident réel
        # 2026-08-29 : "25" pris pour le prix, écrasant la vraie valeur, le
        # 2e tarif disparaissant purement et simplement). Dès que 2+ candidats
        # portent une devise à proximité, c'est structurellement un cas
        # `pricing_tiers` — on laisse la main au LLM (qui sait les regrouper,
        # voir règle 5bis du prompt) plutôt que de deviner.
        # Cette garde vaut aussi bien pour une réponse directe (PRICE/QUANTITY,
        # `skip_numeric_shortcut`) que pour une CORRECTION pendant la
        # confirmation (`_confirmation_correction`, ex: le producteur répète
        # ses paliers tarifaires après un récap déjà faux) — dans les deux
        # cas, 2+ nombres près d'une devise ne peuvent être résolus qu'en
        # perdant de l'information si on force une seule paire ici.
        _currency_candidates = [c for c in candidates if c["near_currency"]]
        if (
            (skip_numeric_shortcut or (_confirmation_correction and llm_available))
            and len(_currency_candidates) >= 2
        ):
            # Avant de laisser la main au LLM (peu fiable ici — voir
            # `extract_deterministic_pricing_tiers`, incident 2026-08-30 : la
            # MÊME phrase a produit `pricing_tiers` correctement une fois puis
            # échoué la fois suivante, même modèle/température=0.0, non-
            # déterminisme MoE connu côté Groq) : tente le parseur
            # déterministe. S'il livre un résultat SANS AMBIGUÏTÉ, on n'a même
            # plus besoin d'appeler le LLM pour ce message — plus rapide ET
            # fiable à 100%. S'il échoue (structure non reconnue), on laisse
            # la main au LLM comme avant (`return None`).
            deterministic_tiers = extract_deterministic_pricing_tiers(text)
            if deterministic_tiers:
                return {
                    "interpreted_event": (
                        "UPDATE" if expected == "CONFIRMATION" else "ANSWER"
                    ),
                    "detected_intent": str(locked_goal or "UNKNOWN").upper(),
                    "interpreter_confidence": 0.98,
                    "extracted_entities": {"pricing_tiers": deterministic_tiers},
                    "raw_analysis": {"path": "fast_path_deterministic_pricing_tiers"},
                }
            return None

        # Réponse composée non-ambiguë ("775 kg d'oignon et le kg coûte 175
        # fcfa") : un nombre porte une unité de poids/comptage SANS devise à
        # proximité (quantité), un AUTRE porte une devise à proximité (prix) —
        # c'est de l'extraction structurée par correspondance exacte de
        # tokens, pas une supposition floue. On remplit les DEUX slots
        # d'un coup, TOUJOURS (même LLM disponible) : router un cas aussi
        # net vers le LLM ne fait que l'exposer à une mauvaise classification
        # d'intention (vécu en prod — un message quantité+prix composé s'est
        # fait détourner vers DECLARE_CROP_CYCLE alors que le tunnel actif
        # était SALES_PUBLISH_PRODUCT). Le LLM garde la priorité uniquement
        # pour le cas ambigu (un seul nombre, rôle incertain) juste en dessous.
        _event_type = "UPDATE" if expected == "CONFIRMATION" else "ANSWER"
        _qty_candidate = next(
            (c for c in candidates if c["unit"] and not c["near_currency"]), None
        )
        _price_candidate = next((c for c in candidates if c["near_currency"]), None)
        if (
            _qty_candidate is not None
            and _price_candidate is not None
            and _qty_candidate is not _price_candidate
        ):
            _qty_value: Any = _qty_candidate["value"]
            _qty_unit: Any = _qty_candidate["unit"]
            # Quantité COMPOSÉE ("2 tonnes et 250 kg") : incident réel
            # (2026-09-03) — `_qty_candidate` ci-dessus ne retient que la
            # PREMIÈRE paire quantité+unité rencontrée ; sur "je suis prêt à
            # payer 250 fcfa le kg et je veux 2 tonnes et 250 kg", la
            # quantité totale restait figée à "2 TONNE" à travers plusieurs
            # tours, y compris après une correction explicite de
            # l'utilisateur. `parse_compound_quantity` filtre déjà le nombre
            # du prix (son unité, "fcfa", n'est pas une unité reconnue) et ne
            # somme que les paires réellement convertibles en KG — neutre
            # (même résultat que `_qty_candidate`) quand le texte ne porte
            # qu'une seule quantité.
            _compound = parse_compound_quantity(text)
            if _compound.unit == "KG" and _compound.quantity not in (None, _qty_value):
                _qty_value, _qty_unit = _compound.quantity, _compound.unit
            compound_entities: Dict[str, Any] = {
                "quantity": _qty_value,
                "unit": _qty_unit,
                "price": _price_candidate["value"],
            }
            if _price_candidate["unit"]:
                compound_entities["price_unit"] = _price_candidate["unit"]
            return {
                "interpreted_event": _event_type,
                "detected_intent": str(locked_goal or "UNKNOWN").upper(),
                "interpreter_confidence": 0.98,
                "extracted_entities": compound_entities,
                "raw_analysis": {"path": "fast_path_slot_numeric_compound_answer"},
            }

        if _confirmation_correction:
            # Une seule valeur typée sans ambiguïté (quantité OU prix, pas les
            # deux) : correction ciblée d'un seul champ du brouillon. Si le
            # nombre n'est PAS typé (aucune unité/devise détectée), on ne
            # devine pas — on laisse la main au LLM (`return None` plus bas)
            # plutôt que de risquer d'écraser le mauvais champ.
            if _qty_candidate is not None and _price_candidate is None:
                # Même correctif de quantité composée que ci-dessus — une
                # correction pendant la confirmation ("non j'ai dit 2 tonnes
                # et 250 kg") passe par CETTE branche (pas de prix mentionné
                # dans le message de correction) et souffrait du même
                # tronquage à la première paire.
                _qty_value = _qty_candidate["value"]
                _qty_unit = _qty_candidate["unit"]
                _compound = parse_compound_quantity(text)
                if _compound.unit == "KG" and _compound.quantity not in (
                    None,
                    _qty_value,
                ):
                    _qty_value, _qty_unit = _compound.quantity, _compound.unit
                return {
                    "interpreted_event": _event_type,
                    "detected_intent": str(locked_goal or "UNKNOWN").upper(),
                    "interpreter_confidence": 0.98,
                    "extracted_entities": {
                        "quantity": _qty_value,
                        "unit": _qty_unit,
                    },
                    "raw_analysis": {
                        "path": "fast_path_confirmation_correction",
                        "slot": "quantity",
                    },
                }
            if _price_candidate is not None and _qty_candidate is None:
                price_entities: Dict[str, Any] = {"price": _price_candidate["value"]}
                if _price_candidate["unit"]:
                    price_entities["price_unit"] = _price_candidate["unit"]
                return {
                    "interpreted_event": _event_type,
                    "detected_intent": str(locked_goal or "UNKNOWN").upper(),
                    "interpreter_confidence": 0.98,
                    "extracted_entities": price_entities,
                    "raw_analysis": {
                        "path": "fast_path_confirmation_correction",
                        "slot": "price",
                    },
                }
            return None

        # Cas simple non-ambigu restant (un seul nombre, mais typé sans
        # équivoque : "775 kg" pour une QUANTITY, "175 fcfa" pour un PRICE) :
        # même sans second nombre pour former une paire composée, ce nombre
        # porte déjà sa réponse au slot en cours — le résoudre ici (jamais
        # via le LLM) ferme la MÊME faille que le cas composé ci-dessus :
        # tant qu'une réponse répond sans ambiguïté au slot demandé, elle ne
        # doit JAMAIS pouvoir déclencher une reclassification d'intention
        # (le LLM ne voit même pas le message). Seul un nombre GENUINEMENT
        # ambigu (aucune unité, aucune devise détectée à proximité — on ne
        # sait pas s'il répond au slot ou introduit autre chose) est laissé
        # au LLM quand celui-ci est disponible.
        if expected == "QUANTITY":
            _unambiguous_single = next(
                (c for c in candidates if c["unit"] and not c["near_currency"]), None
            )
        else:
            _unambiguous_single = next(
                (c for c in candidates if c["near_currency"]), None
            )

        if skip_numeric_shortcut and _unambiguous_single is None:
            candidates = []

        # WAITING_FOR_PACKAGE_COUNT (audit 2026-09-01) : un nombre PORTANT UNE
        # UNITÉ ne peut pas être un nombre de paquets ("30 litres", "le bidon
        # de 5 L", "3 bidons de 10 L"). Le résoudre ici en `quantity` était la
        # cause racine du cas reproduit "30 litres → 30 bidons = 27 000 FCFA",
        # et empêchait tout changement d'avis de palier ("finalement le bidon
        # de 5 L" devenait 5 paquets du palier 10 L). Ce chemin déterministe
        # n'a donc rien de valide à produire : on laisse le LLM trancher — lui
        # seul reçoit la liste des paliers (`tier_menu_context`) et sait
        # distinguer une re-sélection d'un nombre de paquets. Quand aucun LLM
        # n'est disponible, on garde l'extraction : le domaine
        # (cart_service/pricing_tiers) refuse alors explicitement l'unité au
        # lieu de la convertir en silence — jamais d'ajout au panier erroné.
        if (
            pack_count_tier is not None
            and expected == "QUANTITY"
            and llm_available
            and any(c["unit"] for c in candidates)
        ):
            return None

        if candidates:
            if expected == "QUANTITY":
                chosen = next(
                    (c for c in candidates if c["unit"] and not c["near_currency"]),
                    None,
                )
                if chosen is None:
                    chosen = next(
                        (c for c in candidates if not c["near_currency"]), None
                    )
            else:
                chosen = next((c for c in candidates if c["near_currency"]), None)
                if chosen is None:
                    chosen = next((c for c in candidates if not c["unit"]), None)
            if chosen is None:
                chosen = candidates[0]
            numeric_value = chosen["value"]
            mapped_unit = chosen["unit"]
            has_currency = chosen["near_currency"]
            number_match = True  # sentinel: garde le bloc ci-dessous actif
        else:
            numeric_value = None
            number_match = None

        if number_match:
            if numeric_value is not None:
                # Désambiguïsation par unité (extraction structurée déterministe,
                # PAS de la classification d'intention) : on classe le nombre
                # selon son UNITÉ réelle, pas selon ce que l'agent attendait.
                # Un nombre suivi d'une devise ("250 fcfa") = PRIX ; suivi d'une
                # unité de poids/comptage ("234 kg") = QUANTITÉ — même si on
                # demandait l'autre (l'utilisateur donne souvent la quantité
                # quand on attend le prix). Sans ça, "234 kg" en réponse à une
                # question de budget devenait price=234 (payload corrompu → perte
                # du tour puis fuite de 234 dans la demande suivante).
                if has_currency:
                    slot = "price"
                elif mapped_unit:
                    slot = "quantity"
                else:
                    slot = "price" if expected == "PRICE" else "quantity"

                entities: Dict[str, Any] = {slot: numeric_value}
                if slot == "quantity":
                    if mapped_unit:
                        entities["unit"] = mapped_unit
                    elif pack_count_tier is not None:
                        # WAITING_FOR_PACKAGE_COUNT : un nombre de paquets est
                        # SANS DIMENSION. Le repli d'unité ci-dessous relit le
                        # payload FUSIONNÉ, qui porte encore l'unité de la
                        # demande initiale ("30 L de lait", donnée AVANT même
                        # que les paliers soient affichés) — il fabriquait donc
                        # `unit="LITRE"` sur un simple "3", exactement l'héritage
                        # toxique que la séparation quantité/paquet doit
                        # empêcher. Voir domain/pricing_tiers.py.
                        pass
                    else:
                        payload = state.get("transaction_payload") or {}
                        fallback_unit = (
                            payload.get("unit_display")
                            or payload.get("original_unit")
                            or payload.get("unit")
                        )
                        if fallback_unit not in (None, "", [], {}):
                            entities["unit"] = canonical_unit_label(fallback_unit)
                elif slot == "price" and mapped_unit:
                    # Le prix a sa PROPRE base ("10000 FCFA/kg") qui peut différer
                    # de l'unité de la quantité totale ("200 tonnes") — les deux
                    # sont légitimement indépendants (ex: vente de 200 tonnes au
                    # prix de 10000 FCFA/kg). Ne JAMAIS écraser `unit` (celui de
                    # la quantité) avec l'unité du prix : ça renommait
                    # silencieusement "200 tonnes" en "200 kg" au tour suivant.
                    # `price_unit` est un champ dédié, lu par confirmation_summary
                    # pour afficher le prix avec SA vraie unité au lieu de
                    # toujours réutiliser celle de la quantité.
                    entities["price_unit"] = mapped_unit

                return {
                    "interpreted_event": "ANSWER",
                    "detected_intent": str(locked_goal or "UNKNOWN").upper(),
                    "interpreter_confidence": 0.98,
                    "extracted_entities": entities,
                    "raw_analysis": {
                        "path": "fast_path_slot_numeric_answer",
                        "slot": slot,
                    },
                }

    # Ex-fast-paths producteur (mes commandes / mes offres / appels d'offres /
    # modifier une production / modifier un produit) SUPPRIMÉS : listes de
    # tokens figées qui échouaient sur toute formulation non prévue. Le LLM
    # est déjà instruit dynamiquement (catalogue INTENT_CONFIG par rôle,
    # voir _build_dynamic_interpreter_prompt) pour classer ces intentions —
    # il n'y a plus besoin d'un pré-filtrage par mots-clés en amont.

    # Ex-Fast-path 0bis (annulation explicite) SUPPRIMÉ : couvert par la
    # règle "SELECTION + refus en langage libre → REJECT" du prompt LLM
    # (interpreter/prompts.py), qui ne se limite plus à une liste fixe.

    # Ex-Fast-path 0quater (précommande depuis un panier actif) SUPPRIMÉ : un
    # whitelist de tokens approximatifs ("ok", "vasy", "cbon"...) échouait sur
    # toute formulation libre non listée (ex: "Oui je veux") et forçait un
    # retour au début du tunnel — exactement ce qu'on veut éviter. Le LLM
    # (interpreter/prompts.py) est désormais explicitement instruit à
    # reconnaître une confirmation/annulation en langage libre face à un menu
    # d'action précommande, et flows/buyer/flow.py's "Natural confirm" +
    # "Phase-locked routing" (PREORDER_DRAFTED) absorbent le reste : même un
    # goal mal classé reste forcé dans le tunnel précommande tant qu'aucune
    # confirmation/rejet explicite n'a été résolu. Plus aucune énumération de
    # formulations à maintenir ici.

    # Ex-fast-path 0ter (escalade "appel" depuis un menu SELECTION) et
    # ex-fast-path buyer procurement tracking ("suivre mes appels d'offres"
    # / "voir mes enchères") SUPPRIMÉS : listes de tokens figées, redondantes
    # avec la classification LLM déjà instruite par le catalogue INTENT_CONFIG
    # (BUYER_REQUEST, BUYER_LIST_AUCTIONS y sont déjà déclarés).

    # Ex-Fast-path 0 (RESUME par mot-clé figé) SUPPRIMÉ : le LLM reçoit
    # désormais `suspended_task` dans son contexte (interpreter/prompts.py) et
    # est instruit à reconnaître une intention de reprise en langage libre.

    # Fast-path 1 : Choix d'un index numérique pur sur un composant Menu / Liste AG-UI
    if clean.isdigit():
        candidates = state.get("expected_candidates") or []
        has_active_mapping = bool(state.get("available_mapping")) or (
            str(
                (state.get("working_memory") or {}).get("available_mapping_kind") or ""
            ).lower()
            in {"intent_disambiguation", "order_list", "selection_menu"}
        )
        if expected == "SELECTION" or len(candidates) > 0 or has_active_mapping:
            return {
                "interpreted_event": "SELECTION",
                "detected_intent": "UNKNOWN",
                "interpreter_confidence": 1.0,
                "extracted_entities": {"selection_index": int(clean)},
                "raw_analysis": {"path": "fast_path_selection_index"},
            }

    # Ex-Fast-path 1bis (sélection de palier en texte libre, regex
    # ordinaux/quantité+unité) SUPPRIMÉ (2026-08-30, refonte "LLM pilote la
    # sélection de palier") : cassait à chaque variation de formulation.
    # Une réponse en texte libre pendant un menu de paliers actif retombe
    # maintenant vers le LLM comme n'importe quel autre message — mais
    # INFORMÉ cette fois : la liste des paliers actifs (avec leurs vrais
    # `tier_id`) est injectée dans son prompt (`tier_menu_context`, voir
    # plus bas dans ce fichier et `interpreter/prompts.py`), avec une règle
    # explicite de ne jamais inventer un id. `cart.py` reste le seul juge :
    # il valide que le `selected_value` renvoyé correspond bien à un
    # `tier_id` de la liste actuellement affichée avant de l'utiliser.

    # Ex-Fast-path 2 (extraction numérique PRICE/QUANTITY) SUPPRIMÉ : c'était un
    # DOUBLON, moins capable, du bloc numérique EN TÊTE de cette fonction (qui
    # gère déjà compound quantité+prix, valeur unique non ambiguë, correction
    # pendant CONFIRMATION, et l'unité collée au nombre). Surtout, il IGNORAIT
    # `skip_numeric_shortcut` : quand le LLM est disponible et qu'un nombre nu
    # AMBIGU ("300", sans unité ni devise) doit lui être délégué, le premier
    # bloc se retirait proprement (candidates vidées) mais l'exécution
    # retombait ICI, qui ré-attrapait le nombre en ANSWER — annulant
    # silencieusement la priorité LLM que le premier bloc venait d'établir. Un
    # SEUL chemin d'extraction numérique existe désormais (le bloc en tête).

    return None


# =====================================================================
# CONTRAT D'ACTION STRUCTURÉE — producteur / palier / nombre de paquets
# (2026-09-01, voir domain/selection_actions.py pour le pourquoi complet)
# =====================================================================

_ACTION_EVENT: Dict[ActionType, str] = {
    ActionType.SELECT_PRODUCER: "SELECTION",
    ActionType.SELECT_PRICING_TIER: "SELECTION",
    ActionType.SET_PACKAGE_COUNT: "ANSWER",
    ActionType.SET_QUANTITY: "ANSWER",
}


def _selection_action_output(
    raw: Dict[str, Any], locked_goal: Any, path: str
) -> Dict[str, Any]:
    """Traduit une action structurée BRUTE (pas encore validée — la
    validation contre le contexte courant est le travail de `cart.py`, pas
    de l'interpréteur, voir la séparation interprétation/exécution) en sortie
    `input_interpreter` standard. Une seule fonction pour les DEUX sources
    (FastPath déterministe et LLM) : elles produisent un contrat identique,
    jamais deux formats concurrents."""
    action: ActionType = raw["action"]
    entities: Dict[str, Any] = {"agent_action": action.value}
    if raw.get("producer_id"):
        entities["action_producer_id"] = raw["producer_id"]
    if raw.get("pricing_tier_id"):
        entities["action_pricing_tier_id"] = raw["pricing_tier_id"]
    if raw.get("package_count") is not None:
        entities["action_package_count"] = raw["package_count"]
    if raw.get("quantity") is not None:
        entities["action_quantity"] = raw["quantity"]
    if raw.get("unit"):
        entities["action_unit"] = raw["unit"]
    return {
        "interpreted_event": _ACTION_EVENT[action],
        "detected_intent": str(locked_goal or "UNKNOWN").upper(),
        "interpreter_confidence": 0.98,
        "extracted_entities": entities,
        "raw_analysis": {"path": path},
    }


# =====================================================================
# REPLI DÉTERMINISTE — LLM INDISPONIBLE
# =====================================================================


def _degraded_fallback(role_up: str, text: str) -> Optional[Dict[str, Any]]:
    """Classification minimale de secours quand le LLM est HORS-SERVICE.

    N'est PAS le chemin nominal : appelée uniquement quand Groq renvoie une
    erreur (429 quota / timeout) ou est absent. Sans elle, une demande d'achat
    évidente ("je veux des tomates") retombe sur UNKNOWN → récap vide ou menu
    de commandes hors-sujet. On ne couvre QUE le cas d'achat acheteur le plus
    fréquent — le reste reste UNKNOWN (clarification propre), jamais un état
    cassé. Dès que le LLM répond, ce repli n'est plus sollicité.
    """
    if role_up != "BUYER":
        return None
    clean = (text or "").strip().lower()
    if not _looks_like_buyer_product_request(clean):
        return None
    product = _extract_buyer_product(clean)
    entities: Dict[str, Any] = {"product": product} if product else {}
    return {
        "interpreted_event": "NEW_TASK",
        "detected_intent": "BUYER_REQUEST",
        "interpreter_confidence": 0.6,
        "extracted_entities": entities,
        "raw_analysis": {"path": "degraded_buyer_product_fallback"},
    }


# =====================================================================
# NODE 3 — INPUT INTERPRETER (Factory by role)
# =====================================================================


def make_input_interpreter(role: str = "PRODUCER"):
    """Crée un nœud `input_interpreter`.

    (2026-09-08, mandat §6) : `role` ne sert plus qu'à des usages
    SECONDAIRES (repli dégradé sans LLM, signal `cart_pending`, logs) —
    plus jamais à filtrer structurellement quelles intentions le LLM/le
    fast-path peuvent produire (voir `_build_dynamic_interpreter_prompt`
    ci-dessus, même correctif)."""
    role_up = str(role or "PRODUCER").upper().strip()

    async def _input_interpreter_impl(
        state: MarketAgentState, mc_runtime: MarketRuntime
    ) -> Dict[str, Any]:
        text = state.get("normalized_text") or state.get("user_query") or ""
        if not text:
            messages = state.get("messages") or []
            if isinstance(messages, list):
                for msg in reversed(messages):
                    if not isinstance(msg, dict):
                        continue
                    role2 = str(msg.get("role") or "").lower().strip()
                    if role2 != "user":
                        continue
                    content = msg.get("content")
                    if isinstance(content, str) and content.strip():
                        text = content.strip()
                        break
        # (2026-09-02, "no legacy shim") : source unique, dérivée de
        # `pending_interaction` — un seul point de traduction pour toutes les
        # comparaisons/le prompt LLM plus bas dans cette fonction.
        expected_input = to_tunnel_category(get_pending_interaction(state))
        onboarding_active = bool(state.get("is_onboarding"))
        locked_goal = resolve_current_goal(state)

        # ── 0. BYPASS INTERACTIF (zéro token) ──────────────────────────
        # Un message interactif WhatsApp (bouton quick-reply / ligne de liste)
        # a déjà été désambiguïsé côté client : le clic porte un id/valeur
        # sans équivoque. On résout DIRECTEMENT en événement structuré sans
        # jamais appeler Groq — c'est le cœur de l'optimisation de coût. La
        # boucle LLM d'interprétation n'a de sens que pour du texte libre.
        interactive = str(state.get("interactive_selection") or "").strip()
        if interactive and not onboarding_active:
            up = interactive.upper()
            if up in {"CONFIRM", "OUI", "YES", "VALIDER", "CONFIRMER"}:
                logger.info(
                    "[Interpreter InteractiveBypass] CONFIRM (payload=%s)", interactive
                )
                return {
                    "interpreted_event": "CONFIRM",
                    "detected_intent": str(locked_goal or "UNKNOWN").upper(),
                    "interpreter_confidence": 1.0,
                    "extracted_entities": {},
                    "raw_analysis": {"path": "interactive_bypass_confirm"},
                }
            if up in {"REJECT", "NON", "NO", "ANNULER", "CANCEL"}:
                logger.info(
                    "[Interpreter InteractiveBypass] REJECT (payload=%s)", interactive
                )
                return {
                    "interpreted_event": "REJECT",
                    "detected_intent": str(locked_goal or "UNKNOWN").upper(),
                    "interpreter_confidence": 1.0,
                    "extracted_entities": {},
                    "raw_analysis": {"path": "interactive_bypass_reject"},
                }
            # Sélection dans une liste : chiffre pur → selection_index,
            # sinon la valeur métier (UUID / slug / goal) → selected_value.
            # La machinerie SELECTION existante (memory_update + resolvers)
            # résout ensuite via `available_mapping`.
            entities = (
                {"selection_index": int(interactive)}
                if interactive.isdigit()
                else {"selected_value": interactive}
            )
            logger.info("[Interpreter InteractiveBypass] SELECTION (%s)", entities)
            return {
                "interpreted_event": "SELECTION",
                "detected_intent": "UNKNOWN",
                "interpreter_confidence": 1.0,
                "extracted_entities": entities,
                "raw_analysis": {"path": "interactive_bypass_selection"},
            }

        def _emit_onboarding(extracted: Dict[str, Any], source: str) -> Dict[str, Any]:
            return {
                "interpreted_event": "ONBOARDING_INPUT",
                "detected_intent": "ONBOARDING",
                "interpreter_confidence": 1.0,
                "extracted_entities": extracted,
                "raw_analysis": {"path": source, "role": role_up},
            }

        # 0.5 CONTRAT D'ACTION STRUCTURÉE (2026-09-01) : reconstruit à chaque
        # tour, JAMAIS depuis un canal générique périmé (voir
        # domain/selection_actions.py, incident "LE PREMIER, C'EST À DIRE
        # 5 L" — `expected_candidates` gardait la liste PRODUCTEUR alors que
        # le menu de paliers était déjà affiché). Tant que ce tunnel est
        # actif, c'est l'UNIQUE grille de lecture du message — avant même le
        # FastPath générique et l'appel LLM classique ci-dessous.
        selection_context = (
            None if onboarding_active else build_selection_context(state)
        )
        if selection_context is not None and selection_context.expected_action:
            fp_raw = fast_path_action(text, selection_context)
            if fp_raw is not None:
                logger.info(
                    "[Interpreter SelectionAction] fast-path %s",
                    fp_raw["action"].value,
                )
                return _selection_action_output(
                    fp_raw, locked_goal, "fast_path_selection_action"
                )

        # 1. Traitement prioritaire par Fast-path structurel rigide
        # Le LLM reste la source PRIORITAIRE pour extraire quantity+unit ET
        # price+price_unit ensemble (schéma JSON déjà instruit, voir
        # `_SYSTEM_PROMPT_TEMPLATE` plus haut) — il voit tout le message en
        # contexte, alors que le fast-path regex ci-dessous ne fait que
        # deviner une unité à partir d'une fenêtre de caractères voisins et se
        # trompe sur toute formulation non prévue. Le fast-path numérique
        # (PRICE/QUANTITY) ne doit donc s'exécuter QUE si le LLM est
        # indisponible sur ce runtime — sinon on laisse tomber jusqu'à l'appel
        # LLM (étape 4 ci-dessous), qui a la vraie priorité.
        llm = getattr(mc_runtime, "llm", None)
        _skip_numeric_shortcut = (
            expected_input in ("PRICE", "QUANTITY") and llm is not None
        )
        # (2026-09-08, P1-5 audit architectural) : `forced_role` supprimé —
        # `_interpret_fast_path` ne l'a JAMAIS lu (recherche exhaustive dans
        # ce module : aucune autre occurrence), et ce dict local n'était de
        # toute façon jamais retourné comme patch de nœud — `forced_role` ne
        # pouvait donc PAS non plus survivre comme canal d'état pour
        # `nodes/cognitive.py`/`nodes/semantic_disambiguation.py`, qui le
        # lisaient (`state.get("forced_role") or state.get("user_role")`,
        # branche gauche morte). Code mort des deux côtés — supprimé plutôt
        # que déclaré, faute de preuve qu'il soit nécessaire.
        fast = (
            None
            if onboarding_active
            else _interpret_fast_path(
                state,
                text,
                skip_numeric_shortcut=_skip_numeric_shortcut,
                llm_available=llm is not None,
            )
        )
        if fast is not None:
            logger.info(
                "[Interpreter FastPath] event=%s intent=%s expected=%s role=%s",
                fast.get("interpreted_event"),
                fast.get("detected_intent"),
                expected_input,
                role_up,
            )
            logger.debug(
                "[Interpreter FastPath] role=%s expected=%s event=%s intent=%s",
                role_up,
                expected_input,
                fast.get("interpreted_event"),
                fast.get("detected_intent"),
            )
            return fast

        # 2. Sécurité d'exécution de l'infrastructure
        if llm is None:
            logger.warning("No LLM on runtime — interpreter returns UNKNOWN")
            if onboarding_active:
                return _emit_onboarding({}, "onboarding_no_llm")
            degraded = _degraded_fallback(role_up, text)
            if degraded is not None:
                logger.info(
                    "[Interpreter] LLM absent — repli déterministe %s",
                    degraded["detected_intent"],
                )
                return degraded
            return {
                "interpreted_event": "UNKNOWN",
                "detected_intent": "UNKNOWN",
                "interpreter_confidence": 0.0,
                "extracted_entities": {},
                "raw_analysis": {"path": "no_llm"},
            }

        # 3. Résolution dynamique des contextes de prompts
        system_prompt = _build_dynamic_interpreter_prompt(role_up)
        _exp_input = expected_input or "NONE"
        _slot_hint = (
            get_slot_hint(_exp_input.lower())
            if _exp_input not in ("NONE", "CONFIRMATION", "SELECTION")
            else ""
        )
        _slot_hint_line = (
            f" → {_slot_hint}"
            if _slot_hint and _slot_hint != _exp_input.lower()
            else ""
        )
        _suspended_goal = state.get("suspended_goal")
        _goal_stack = state.get("goal_stack") or []
        _suspended_task = (
            str(_suspended_goal or (_goal_stack[-1] if _goal_stack else "")) or "aucune"
        )
        # Panier prêt à valider : signal d'état (phase CART + panier non vide),
        # PAS un mot-clé du texte. Le LLM s'en sert pour comprendre un accord
        # libre ("je suis d'accord") comme une validation de précommande.
        _cart_phase = str(
            (state.get("preorder_workflow") or {}).get("phase") or ""
        ).upper()
        cart_pending = (
            role_up == "BUYER"
            and _cart_phase == "CART"
            and bool(state.get("active_cart"))
        )
        # (2026-09-01, contrat d'action structurée) : `selection_context` a
        # déjà été reconstruit en tête de fonction (étape 0.5, JAMAIS depuis
        # un canal générique périmé — voir domain/selection_actions.py). On
        # ne fait que le rendre en texte pour le prompt ici.
        _selection_block = (
            tier_menu_prompt_block(selection_context) if selection_context else ""
        )
        selection_action_block = (
            f"- action_structuree_attendue :\n{_selection_block}\n"
            if _selection_block
            else ""
        )

        user_prompt = INTERPRETER_USER_PROMPT.format(
            current_goal=state.get("current_goal") or "AUCUN",
            expected_input=_exp_input,
            slot_hint_line=_slot_hint_line,
            last_agent_question=state.get("last_agent_question") or "—",
            expected_candidates=", ".join(state.get("expected_candidates") or [])
            or "—",
            suspended_task=_suspended_task,
            cart_pending="OUI" if cart_pending else "non",
            selection_action_block=selection_action_block,
            normalized_text=text,
        )

        # 4. Appel LLM d'analyse sémantique et contextuelle — via le LLM
        # Gateway (2026-09-02) : plus de nom de modèle en dur ni de
        # `asyncio.wait_for` local, le Gateway porte son propre budget par
        # profil (settings.LLM_REASONING_BUDGET_SECONDS) et gère lui-même le
        # repli inter-modèle/inter-provider + le disjoncteur partagé (Redis)
        # — voir `graphs/agents/market_coach/llm_gateway/`. Le comportement
        # visible (timeout → UNKNOWN dégradé) est inchangé, juste plus résilient.
        from agriconnect.graphs.agents.market_coach.llm_gateway import (
            resolve_gateway,
            resolve_profile,
        )

        _gateway = resolve_gateway(mc_runtime)
        _profile = resolve_profile(mc_runtime)
        _requested_model = _gateway.primary_model_name(_profile)
        try:
            from agriconnect.core.telemetry import get_trace_id

            completion = await _gateway.complete(
                profile=_profile,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.0,
                request_id=get_trace_id(),
                agent_node="input_interpreter",
            )
            parsed = json.loads(completion.choices[0].message.content or "{}")
            # Repli transparent (Gateway : fallback inter-candidat, OU
            # get_llm.py::GROQ_RATE_LIMIT_FALLBACK en interne au candidat
            # choisi) : `completion.model` (champ standard de la réponse) est
            # le SEUL moyen de détecter après coup qu'un modèle différent du
            # primaire a répondu. Utilisé plus bas pour ne durcir le
            # garde-fou anti-hallucination (`nodes/memory.py::
            # _EXPECTED_INPUT_ALLOWED_FIELDS`) QUE quand ce modèle dégradé a
            # effectivement répondu — le modèle principal, lui, suit déjà
            # l'instruction du prompt d'extraire plusieurs champs à la fois
            # sans halluciner (voir RÈGLE 5 du prompt système).
            _actual_model = getattr(completion, "model", None)
            # Inconnu (attribut absent) : on ne peut pas prouver que le modèle
            # principal a répondu — on reste prudent (comme avant ce fix).
            _degraded_model_used = (_actual_model is None) or (
                _actual_model != _requested_model
            )
        except Exception as exc:
            # Dégradation attendue (Gateway épuisé : tous les candidats du
            # profil ont échoué/étaient indisponibles dans le budget) :
            # WARNING, pas de traceback — bruit de log inutile pour un cas
            # déjà géré par le fallback UNKNOWN ci-dessous. Réserver
            # ERROR+exc_info aux échecs non anticipés (bug de parsing JSON,
            # exception hors du chemin LLM, etc.).
            from agriconnect.graphs.agents.market_coach.llm_gateway import (
                LLMGatewayExhausted,
            )

            is_expected = isinstance(
                exc, (LLMGatewayExhausted, asyncio.TimeoutError, TimeoutError)
            )
            if is_expected:
                logger.warning(
                    "Interpreter LLM indisponible (%s) — forcing UNKNOWN", exc
                )
            else:
                logger.error(
                    "Interpreter LLM CRASH : %s — forcing UNKNOWN", exc, exc_info=True
                )
            if onboarding_active:
                return _emit_onboarding({}, "onboarding_llm_crash")
            # Repli déterministe AVANT d'abandonner en UNKNOWN : une demande
            # d'achat évidente reste servie même si Groq est en panne/quota
            # épuisé (cause racine des récaps vides observés en prod).
            degraded = _degraded_fallback(role_up, text)
            if degraded is not None:
                logger.info(
                    "[Interpreter] LLM en panne — repli déterministe %s (produit=%s)",
                    degraded["detected_intent"],
                    degraded["extracted_entities"].get("product"),
                )
                return degraded
            # `gateway_reason` (2026-09-05, incident "je veux voir les
            # enchères" → UNKNOWN générique) : distingue POURQUOI la Gateway
            # a été épuisée — voir `LLMGatewayExhausted.reason`. N'affecte
            # PAS `unknown_reason` (déjà `TECHNICAL_FAILURE` pour ce `path`,
            # voir `interpreter_result.py::_UNKNOWN_REASON_BY_PATH`) — c'est
            # une information SUPPLÉMENTAIRE pour l'observabilité et pour
            # `clarification_node`, qui l'utilise pour ne jamais retenter un
            # second appel LLM voué au même échec (§11 du brief incident).
            gateway_reason = getattr(exc, "reason", None)
            return {
                "interpreted_event": "UNKNOWN",
                "detected_intent": "UNKNOWN",
                "interpreter_confidence": 0.0,
                "extracted_entities": {},
                "raw_analysis": {
                    "path": "llm_crash",
                    "error": str(exc),
                    "gateway_reason": gateway_reason,
                },
            }

        # 5. Normalisation sémantique et gardes-fous anti-dérive
        raw_event = str(parsed.get("interpreted_event") or "UNKNOWN").upper().strip()
        if raw_event == "PROVIDE_INFO":
            raw_event = "ANSWER"

        raw_intent = str(parsed.get("detected_intent") or "UNKNOWN").upper().strip()
        # (2026-09-08, mandat §6, correctif réel) : le clamp vérifiait AVANT
        # `raw_intent not in allowed_intents_for_role(role_up)` — un
        # sous-ensemble filtré PAR RÔLE de profil, forçant UNKNOWN toute
        # intention BUYER classée par un utilisateur sur le graphe compilé
        # PRODUCER (et réciproquement), même quand le LLM avait correctement
        # compris le message (mandat §6/§3 : le rôle ne doit plus décider
        # structurellement quelles intentions sont atteignables). La garde
        # anti-hallucination RESTE — mandat §6 : "conserver ... règles
        # anti-hallucination" — mais vérifie désormais contre le catalogue
        # COMPLET (`INTENT_CONFIG`), pas contre un sous-ensemble par rôle :
        # un intent qui n'existe nulle part dans le catalogue est toujours
        # rejeté, un intent valide d'un autre "rôle" ne l'est plus.
        if raw_intent != "UNKNOWN" and raw_intent not in INTENT_CONFIG:
            logger.warning(
                "LLM a halluciné une intention hors catalogue: %s — ignoré",
                raw_intent,
            )
            raw_intent = "UNKNOWN"

        try:
            confidence = max(
                0.0, min(1.0, float(parsed.get("interpreter_confidence") or 0.0))
            )
        except (TypeError, ValueError):
            confidence = 0.0

        # Ex-OVERRIDE LEXICAL ACHETEUR SUPPRIMÉ (dette dangereuse) : un test
        # `_looks_like_buyer_product_request()` sur une liste FIGÉE de tournures
        # ("je veux", "je cherche", "il me faut"…) FORÇAIT ici
        # `detected_intent = BUYER_REQUEST` (confiance relevée à 0.85), écrasant
        # la classification du LLM. Dégâts prouvés : « je veux voir mon panier »
        # (BUYER_VIEW_CART), « je veux payer maintenant », « il me faut de
        # l'aide », « je cherche à joindre le service client » étaient TOUS
        # détournés vers une recherche produit. Chaque nouveau contre-exemple
        # obligeait à rallonger la liste d'exclusions — course sans fin contre
        # les fautes de frappe et les tournures non prévues.
        # Le LLM classe déjà ces intentions (catalogue INTENT_CONFIG complet
        # dans son prompt) : c'est LUI qui décide. La même heuristique reste
        # utilisée UNIQUEMENT dans `_degraded_fallback` (LLM en panne/quota),
        # où deviner vaut mieux que ne rien comprendre.

        # ATTENTION — cause racine des blocages persistants ("ça marche sur un
        # numéro neuf, jamais sur celui qui a déjà bugué") : cette règle
        # rabaissait AVEUGLÉMENT tout NEW_TASK en ANSWER dès qu'un slot était
        # attendu, sans vérifier que le message répondait VRAIMENT à ce slot.
        # Un numéro resté coincé en plein slot-filling (ex: expected=PRICE
        # après une 1ère demande) voyait donc TOUTE nouvelle demande produit
        # ("je veux acheter des tomates") rabaissée en ANSWER, absorbée par
        # RÈGLE 1bis (goal_planner) qui ne fait QUE re-verrouiller l'ancien
        # goal SANS jamais purger — le nouveau produit se retrouvait mélangé
        # à l'ancienne quantité/prix, indéfiniment (event jamais NEW_TASK →
        # la purge de RÈGLE 5/1quater n'était jamais atteinte). Un numéro
        # frais (expected_input=NONE au 1er message) ne passait jamais par ce
        # rabaissement, donc semblait « corrigé » alors que le vrai bug
        # persistait pour quiconque était déjà en plein slot-filling.
        # Fix : ne rabaisser que si le produit extrait est ABSENT ou IDENTIQUE
        # au produit déjà suivi — une vraie réponse au slot ne change jamais
        # de produit. Un produit DIFFÉRENT est un signal structurel sans
        # ambiguïté qu'il s'agit d'une nouvelle demande, pas d'une réponse.
        _raw_entities_preview = parsed.get("extracted_entities") or {}
        _fresh_product_preview = (
            str(_raw_entities_preview.get("product") or "").strip().lower()
        )
        _cur_product_preview = (
            str((state.get("transaction_payload") or {}).get("product") or "")
            .strip()
            .lower()
        )
        _is_different_product = (
            bool(_fresh_product_preview)
            and _fresh_product_preview != _cur_product_preview
        )

        # Refonte double-rôle — 2e signal de nouveauté, indépendant du produit :
        # beaucoup d'intentions n'ont structurellement PAS de champ `product`
        # (BUYER_CHECK_ORDER_STATUS, SALES_LIST_ORDERS, PROFILE_SWITCH_ROLE...).
        # Le catalogue fusionné (interpréteur voit désormais TOUTES les
        # intentions, pas seulement celles du rôle courant) fait que le LLM les
        # reconnaît bien plus souvent en plein tunnel — sans ce garde-fou,
        # elles étaient ravalées en ANSWER par la seule règle "produit
        # identique/absent" et englouties dans le tunnel actif au lieu de
        # déclencher une interruption ("l'agent est perdu" quand l'intention
        # change brusquement). Un intent DIFFÉRENT du goal verrouillé, classé
        # avec une confiance suffisante, est un signal structurel de nouvelle
        # tâche tout aussi fort qu'un produit différent.
        _locked_goal_upper = str(locked_goal or "").upper().strip()
        _is_different_goal = (
            raw_intent not in {"UNKNOWN", _locked_goal_upper}
            and confidence >= INTERRUPTION_CONFIDENCE_THRESHOLD
        )
        # (2026-09-08, correction finale Bloc 1, mandat §13 "ANSWER contenant
        # changement de produit") : le garde ci-dessous ne couvrait que le
        # sens NEW_TASK -> ANSWER (rabaissement annulé si le produit/goal
        # diffère). Il ne couvrait PAS le sens inverse : un LLM peut classer
        # DIRECTEMENT un message comme ANSWER (le prompt ne distingue pas
        # "réponse au slot" de "nouveau produit glissé dans la même phrase")
        # alors que le produit/l'intention a changé. Par CONTRAT, ANSWER
        # signifie "continuation du même goal, sans arbitrage" — c'est ce que
        # `cognitive_guard`/`tunnel_manager` supposent en aval (ANSWER ne
        # déclenche jamais d'interruption/clarification/désambiguïsation, par
        # construction). Un ANSWER qui porte en réalité une bascule de
        # produit/intention doit donc être promu en NEW_TASK, exactement
        # comme si le LLM l'avait classé ainsi dès le départ — sinon la
        # bascule est silencieusement absorbée dans l'ancien goal (le vrai
        # bug que ce garde corrige, symétrique à celui déjà en place).
        if (
            expected_input in SLOT_FILLING_INPUTS
            and raw_event in {"NEW_TASK", "ANSWER"}
            and (_is_different_product or _is_different_goal)
        ):
            raw_event = "NEW_TASK"
            confidence = max(confidence, 0.9)
        elif expected_input in SLOT_FILLING_INPUTS and raw_event == "NEW_TASK":
            raw_event = "ANSWER"
        elif _is_different_product or _is_different_goal:
            # Preuve structurelle forte (produit différent OU intention
            # différente et confiante) : on garantit que tunnel_manager (seuil
            # de confiance) laissera passer le switch, plutôt que de dépendre
            # uniquement du score auto-déclaré du LLM.
            confidence = max(confidence, 0.9)

        # Correction d'un brouillon PAS ENCORE PERSISTÉ (attente de CONFIRMATION
        # sur SALES_PUBLISH_PRODUCT/DECLARE_CROP_CYCLE/FARM_CREATE) : "non c'est
        # 200 tonnes" ressemble fortement, pour le LLM, à l'intent catalogue
        # SALES_UPDATE_PRODUCT/SALES_UPDATE_PRODUCTION/FARM_UPDATE — mais ces
        # intents "mettent à jour un produit EXISTANT au catalogue", qui n'existe
        # pas encore puisque rien n'a été confirmé/publié. Sans ce garde-fou, le
        # bloc d'interruption ci-dessous abandonnait le brouillon en cours pour
        # basculer vers un lookup catalogue qui ne trouve rien ("Votre catalogue
        # de produits est actuellement vide.") — le brouillon (quantité, prix...)
        # est perdu. Une intention-jumelle "update" détectée ici doit rester une
        # correction DU MÊME goal en attente, pas une interruption vers un autre.
        _locked_goal_for_confirmation = str(locked_goal or "").upper().strip()
        _pending_create_update_sibling = _PENDING_CREATE_UPDATE_SIBLINGS.get(
            _locked_goal_for_confirmation
        )
        if (
            expected_input == "CONFIRMATION"
            and _pending_create_update_sibling
            and raw_intent == _pending_create_update_sibling
        ):
            logger.info(
                "[Interpreter] Correction de brouillon non persisté (%s) pendant CONFIRMATION "
                "— intent-jumeau %s absorbé comme UPDATE du même goal, pas une interruption.",
                _locked_goal_for_confirmation,
                raw_intent,
            )
            raw_event = "UPDATE"
            raw_intent = _locked_goal_for_confirmation

        if expected_input in {"SELECTION", "CONFIRMATION"} and raw_event not in {
            "SELECTION",
            "CONFIRM",
            "REJECT",
            "UPDATE",
        }:
            # Seuil ALIGNÉ sur tunnel_manager (INTERRUPTION_CONFIDENCE_THRESHOLD) :
            # sous ce seuil, on reste proprement dans la confirmation/sélection
            # (RÈGLE 2 du goal_planner re-verrouille le goal) ; au-dessus, la
            # bascule est acceptée par tunnel_manager aussi — plus de zone morte.
            if (
                raw_intent != "UNKNOWN"
                and confidence >= INTERRUPTION_CONFIDENCE_THRESHOLD
            ):
                logger.info(
                    "Interruption détectée pendant %s → intent=%s (confidence=%.2f)",
                    expected_input,
                    raw_intent,
                    confidence,
                )
                raw_event = "INTERRUPTION"
            else:
                logger.warning(
                    "Dérive conversationnelle détectée : attendait %s, utilisateur a dévié (intent=%s, confidence=%.2f).",
                    expected_input,
                    raw_intent,
                    confidence,
                )
                raw_event = "UNKNOWN"

        # Panier en attente de validation : un accord jugé par le LLM (CONFIRM)
        # déclenche la précommande. On émet le MÊME signal que l'ancien
        # fast-path supprimé (NEW_TASK → BUYER_PREORDER_INIT), mais c'est le
        # LLM qui a jugé l'accord en langage libre, pas une liste de mots. Un
        # refus (REJECT) reste un refus (l'annulation est gérée en aval).
        if cart_pending and raw_event == "CONFIRM":
            logger.info(
                "[Interpreter] Panier validé en langage libre → BUYER_PREORDER_INIT"
            )
            raw_event = "NEW_TASK"
            raw_intent = "BUYER_PREORDER_INIT"
            confidence = max(confidence, 0.9)

        if raw_event not in {
            "NEW_TASK",
            "ANSWER",
            "CONFIRM",
            "REJECT",
            "SELECTION",
            "UPDATE",
            "INTERRUPTION",
            "RESUME",
            "OUT_OF_SCOPE",
            "UNKNOWN",
            "ONBOARDING_INPUT",
        }:
            raw_event = "UNKNOWN"

        # ══════════════════════════════════════════════════════════════════
        # FINALISATION DES ENTITÉS — le LLM est la source PRIMAIRE.
        # On part de SON extraction et de SON verdict qualité (validation_status),
        # puis les couches déterministes n'interviennent QUE :
        #   • en FALLBACK (le LLM a sous-extrait un champ) ;
        #   • en GARDE ciblée contre un échec LLM PROUVÉ (ancrage d'unité).
        # ══════════════════════════════════════════════════════════════════
        remapped_entities = _remap_entities(parsed.get("extracted_entities") or {})

        # validation_status = signal de QUALITÉ produit PAR LE LLM lui-même : il
        # nous dit s'il a su lire l'unité. On le CONSOMME (avant, il était
        # write-only). C'est la voie primaire pour savoir « unité fiable ou non ».
        raw_validation = parsed.get("validation_status")
        validation_status = (
            str(raw_validation).upper().strip() if raw_validation else None
        )
        if validation_status not in {
            "VALID",
            "INVALID_MISSING_UNIT",
            "INVALID_AMBIGUOUS_UNIT",
        }:
            validation_status = None

        # FALLBACK numérique : si le LLM a manqué la quantité, la regex la comble
        # (jamais l'inverse — on n'écrase pas une valeur que le LLM a fournie).
        #
        # (2026-09-01, contrat d'action structurée) : ce repli est SUSPENDU
        # dès que ce tour produit un `agent_action` (voir
        # domain/selection_actions.py + règle 4bis du prompt système). Un
        # nombre+unité dans le MÊME message qu'une action structurée ("le
        # premier, c'est à dire 5 L") décrit l'OPTION choisie, jamais une
        # quantité d'achat séparée — c'est exactement l'incident réel
        # (2026-09-01) qui a motivé ce contrat : `selection_index=1` (→
        # palier 5 L, correct) ET `quantity=5.0` (le même "5 L" recapté comme
        # quantité) cohabitaient dans le même tour, et le code en aval
        # faisait ensuite confiance à ce 5.0 comme un nombre de paquets déjà
        # répondu. Couvre les 4 types d'action, pas seulement le palier —
        # remplace l'ancien garde `_tier_pick_this_turn`, spécifique au seul
        # `selected_value`.
        # `selected_value`/`selection_index` : filet legacy — un LLM qui, en
        # dépit du prompt, répond encore dans l'ancien format pendant que le
        # tunnel producteur/palier est actif (`selection_context.expected_action`)
        # doit bénéficier de la MÊME suspension, sinon le "10 L" de "je veux
        # celui de 10 L" repeuple `quantity`/`unit` en marge du contrat.
        _legacy_pick_this_turn = bool(
            remapped_entities.get("selected_value")
            or remapped_entities.get("selection_index")
        ) and bool(selection_context and selection_context.expected_action)
        _structured_action_this_turn = (
            bool(remapped_entities.get("agent_action")) or _legacy_pick_this_turn
        )
        _adjacent = (
            {}
            if _structured_action_this_turn
            else (_fallback_quantity_unit_from_text(text) or {})
        )
        fallback_applied = False
        for key, value in _adjacent.items():
            if key not in remapped_entities and value not in (None, "", [], {}):
                remapped_entities[key] = value
                fallback_applied = True

        # ── UNITÉ (le point sensible : biais d'ancrage TONNE vécu en prod) ──
        # Unité LITTÉRALEMENT écrite dans le message : adjacente au nombre
        # ("200kg", captée par le parseur) OU ailleurs ("200, en sacs", captée
        # par le scan de tokens). None si le message ne porte AUCUNE unité.
        _text_unit = _adjacent.get("unit") or extract_unit_only_from_text(text)
        _has_quantity = remapped_entities.get("quantity") is not None

        if _has_quantity:
            if _text_unit:
                # Une unité écrite noir sur blanc fait TOUJOURS foi contre le LLM :
                # c'est la garde anti-ancrage (le LLM dit parfois TONNE alors que
                # l'utilisateur a tapé "kg"). Ce n'est pas « la regex prime sur le
                # LLM » — c'est « le TEXTE de l'utilisateur prime sur une
                # supposition du LLM », ce qui est la bonne priorité.
                if remapped_entities.get("unit") != _text_unit:
                    logger.warning(
                        "[Interpreter] Unité : texte='%s' retenu contre LLM='%s' (anti-ancrage).",
                        _text_unit,
                        remapped_entities.get("unit"),
                    )
                    remapped_entities["unit"] = _text_unit
                    fallback_applied = True
            elif remapped_entities.get("unit") and validation_status in {
                "INVALID_MISSING_UNIT",
                "INVALID_AMBIGUOUS_UNIT",
            }:
                # Le message ne porte AUCUNE unité ET le LLM signale LUI-MÊME
                # qu'elle est manquante/ambiguë : on suit son verdict et on
                # écarte l'unité (contradictoire) qu'il aurait tout de même
                # posée — défaut registre (KG) / élevage (TETE) appliqué en aval.
                logger.info(
                    "[Interpreter] LLM signale %s + texte sans unité — unité '%s' écartée.",
                    validation_status,
                    remapped_entities.get("unit"),
                )
                remapped_entities.pop("unit", None)
            elif remapped_entities.get("unit"):
                # Message sans unité, mais le LLM en renvoie une en se déclarant
                # VALID : il n'a aucun appui textuel — c'est une supposition
                # (ancrage). Dernier filet de sécurité : on l'écarte plutôt que
                # de risquer un "200 TONNE" fantôme. (Le prompt interdit désormais
                # d'inventer une unité — ce cas doit devenir rarissime.)
                logger.warning(
                    "[Interpreter] Unité LLM '%s' sans appui dans le texte — écartée (filet anti-ancrage).",
                    remapped_entities.get("unit"),
                )
                remapped_entities.pop("unit", None)

        # ── QUANTITÉ COMPOSÉE (incident réel 2026-09-03) : « 2 tonnes et
        # 250 kg » — le LLM a renvoyé quantity=2/unit=TONNE, silencieusement
        # tronqué le "et 250 kg". `parse_compound_quantity` (services/domain/
        # quantity_unit.py) existe précisément pour ce motif — somme en KG
        # quand ≥2 paires quantité+unité toutes convertibles (KG/TONNE) sont
        # présentes dans le texte — mais n'était câblée à AUCUN site
        # d'extraction réel jusqu'ici (code mort, seulement testé en
        # isolation). Même principe que la garde anti-ancrage d'unité
        # ci-dessus : le texte de l'utilisateur prime sur une extraction LLM
        # possiblement partielle — pas seulement pour combler un trou, mais
        # pour CORRIGER une valeur déjà posée quand le texte porte
        # explicitement plusieurs paires. Neutre si le texte ne contient
        # qu'une seule paire ou des unités non convertibles (bascule vers
        # `parse_quantity_unit_from_text`, résultat alors identique au
        # simple, donc aucune correction n'est appliquée) ; suspendu comme
        # le reste pendant une action structurée (menu palier/sélection).
        if not _structured_action_this_turn:
            _compound = parse_compound_quantity(text)
            if (
                _compound.unit == "KG"
                and _compound.quantity is not None
                and (
                    remapped_entities.get("quantity") != _compound.quantity
                    or remapped_entities.get("unit") != _compound.unit
                )
            ):
                _single = parse_quantity_unit_from_text(text)
                if _compound.quantity != _single.quantity:
                    logger.warning(
                        "[Interpreter] Quantité composée détectée dans le texte "
                        "('%s' → %.1f KG) — retenue contre LLM=%r/%r (anti-troncature).",
                        text,
                        _compound.quantity,
                        remapped_entities.get("quantity"),
                        remapped_entities.get("unit"),
                    )
                    remapped_entities["quantity"] = _compound.quantity
                    remapped_entities["unit"] = _compound.unit
                    fallback_applied = True

        if "product" in remapped_entities:
            validated_product = await _validate_and_sanitize_product(
                remapped_entities.get("product"), mc_runtime
            )
            remapped_entities["product"] = validated_product
        if onboarding_active:
            return _emit_onboarding(remapped_entities, "onboarding_override")

        if fallback_applied:
            if not validation_status:
                validation_status = (
                    "VALID" if remapped_entities.get("unit") else "INVALID_MISSING_UNIT"
                )
            if raw_event == "UNKNOWN" and expected_input in {
                "QUANTITY",
                "UNIT",
                "PRICE",
            }:
                raw_event = "ANSWER"
            if raw_intent == "UNKNOWN" and locked_goal:
                raw_intent = str(locked_goal).upper()

        # Repli symétrique, INCONDITIONNEL cette fois (pas seulement en repli
        # modèle dégradé ci-dessus) : incident réel (2026-08-30) — pour un
        # message multi-tarifs ("25 L à 500 fcfa et 40 L à 900 fcfa") en
        # réponse à PRICE, le LLM (modèle NON dégradé) extrayait correctement
        # `pricing_tiers` MAIS classait quand même l'événement en UNKNOWN/
        # OUT_OF_SCOPE — probablement parce qu'aucune valeur `price` scalaire
        # unique ne "répond" littéralement à la question posée. Sans repli,
        # `memory_update` ne fusionne rien (gardé par `interpreter_event in
        # {ANSWER, UPDATE}`), et l'agent boucle indéfiniment sur la même
        # question de prix malgré une extraction pourtant réussie. Un
        # `pricing_tiers` non-vide EST une réponse exploitable au slot
        # PRICE/QUANTITY/CONFIRMATION, quel que soit le jugement du LLM sur
        # l'événement.
        if raw_event in {"UNKNOWN", "OUT_OF_SCOPE"} and expected_input in {
            "QUANTITY",
            "PRICE",
            "CONFIRMATION",
        }:
            _tiers = remapped_entities.get("pricing_tiers")
            if isinstance(_tiers, list) and any(
                isinstance(t, dict) for t in _tiers
            ):
                raw_event = "UPDATE" if expected_input == "CONFIRMATION" else "ANSWER"
                if raw_intent == "UNKNOWN" and locked_goal:
                    raw_intent = str(locked_goal).upper()

        return {
            "interpreted_event": raw_event,
            "detected_intent": raw_intent,
            "interpreter_confidence": confidence,
            "validation_status": validation_status,
            "extracted_entities": remapped_entities,
            "raw_analysis": {
                "path": "llm",
                "role": role_up,
                "model_used": _actual_model,
                "degraded_model": _degraded_model_used,
            },
        }

    async def input_interpreter(
        state: MarketAgentState, mc_runtime: MarketRuntime
    ) -> Dict[str, Any]:
        """Point de passage UNIQUE (mandat §9-13) : tout ce que produit
        `_input_interpreter_impl` — fast-path déterministe, bypass
        interactif, onboarding, repli dégradé, ou appel LLM — est converti
        en `InterpreterResult` avant de devenir un patch d'état. C'est ce
        qui rend un `UNKNOWN` sans `UnknownReason` structurellement
        impossible, plutôt que dépendant de la discipline de chaque `return`
        interne (~15 points de sortie distincts dans l'implémentation)."""
        raw = await _input_interpreter_impl(state, mc_runtime)
        return InterpreterResult.from_legacy_dict(raw).to_state_patch()

    return input_interpreter


# =====================================================================
# ROUTING AFTER VALIDATOR (Aiguillage avec typage d'état AG-UI)
# =====================================================================


def make_route_after_validator(role: str = "PRODUCER"):
    """Crée la fonction de routage conditionnel après le nœud `validator`."""
    role_up = str(role or "PRODUCER").upper().strip()

    def route_after_validator(state: MarketAgentState) -> str:
        """Détermine le prochain nœud du graphe en fonction de l'état de l'IHM."""
        interpreted_event = state.get("interpreted_event")
        missing_fields = state.get("missing_fields") or []
        # (2026-09-02, "no legacy shim") : source unique, dérivée de
        # `pending_interaction`.
        expected_input = to_tunnel_category(get_pending_interaction(state))
        strategy = str(state.get("response_strategy") or "").upper().strip()

        # (2026-09-02, "no legacy shim") : source UNIQUE — plus de lecture
        # directe de `waiting_for_confirmation`. `status=="WAITING_CONFIRMATION"`
        # reste un signal légitime et DISTINCT (statut machine-à-états du
        # tour, pas "qu'attend-on de l'utilisateur") — conservé tel quel.
        if (
            get_pending_interaction(state).kind == InteractionKind.CONFIRM_ACTION
            or state.get("status") == "WAITING_CONFIRMATION"
        ):
            logger.info(
                "[%s ROUTER] En attente de confirmation — Routage vers confirmation_gate",
                role_up,
            )
            logger.debug("[AfterValidator] role=%s decision=to_confirmation", role_up)
            return "to_confirmation"

        # 1. PROTECTION SÉCURITÉ ANTI-DÉRIVE (AG-UI Court-circuit)
        if interpreted_event in {"UNKNOWN", "OUT_OF_SCOPE"}:
            logger.info(
                "[%s ROUTER] Dérive détectée (%s) — Routage forcé vers response_strategy",
                role_up,
                interpreted_event,
            )
            logger.debug(
                "[AfterValidator] role=%s decision=to_strategy reason=drift event=%s",
                role_up,
                interpreted_event,
            )
            return "to_strategy"

        # 2. TUNNEL DE FORMULAIRE INCOMPLET
        current_goal = str(state.get("current_goal") or "").upper()
        if missing_fields and current_goal not in _NAVIGATION_INTENTS:
            logger.info(
                "[%s ROUTER] Formulaire incomplet (%d champs manquants) — Routage vers response_strategy",
                role_up,
                len(missing_fields),
            )
            logger.debug(
                "[AfterValidator] role=%s decision=to_strategy reason=missing_fields n=%d",
                role_up,
                len(missing_fields),
            )
            return "to_strategy"

        if missing_fields and current_goal in _NAVIGATION_INTENTS:
            logger.info(
                "[%s ROUTER] Navigation intent %s prioritaire malgré %d champ(s) manquant(s)",
                role_up,
                current_goal,
                len(missing_fields),
            )

            return "to_resolver"

        # 2B. MENU DE SÉLECTION (sans missing_fields)
        if state.get("status") == "WAITING_INPUT" and (
            expected_input == "SELECTION" or strategy == "SELECTION_MENU"
        ):
            logger.info(
                "[%s ROUTER] Sélection attendue — Routage vers response_strategy",
                role_up,
            )
            logger.debug(
                "[AfterValidator] role=%s decision=to_strategy reason=selection_wait",
                role_up,
            )
            return "to_strategy"

        # 4. FLUX NOMINAL
        logger.info(
            "[%s ROUTER] Input validé et complet — Routage vers context_resolver",
            role_up,
        )
        logger.debug("[AfterValidator] role=%s decision=to_resolver", role_up)
        return "to_resolver"

    return route_after_validator


__all__ = [
    "PRODUCER_INTENTS",
    "BUYER_INTENTS",
    "COMMON_INTENTS",
    "allowed_intents_for_role",
    "_remap_entities",
    "_build_dynamic_interpreter_prompt",
    "_interpret_fast_path",
    "make_input_interpreter",
    "goal_planner",
    "make_route_after_validator",
    "INTENT_TO_GOAL_MAP",
]
