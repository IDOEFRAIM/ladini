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
import hashlib
import json
import logging
import re as _re
from datetime import date
from string import Template
from typing import Any, Dict, List, Optional, Tuple

from ladini.agents.confirmation_phrases import (
    _CONFIRM_EXACT_PHRASES,
    _REJECT_EXACT_PHRASES,
)
from ladini.core.idempotency import get_cached as _get_cached_value
from ladini.core.idempotency import set_cached as _set_cached_value
from ladini.domain.commercial_offer_flow import (
    FIELD_PACKAGE_SIZE,
    FIELD_PRICE_BASIS,
    parse_basis_reply,
    parse_generic_package_count_and_size,
    parse_package_content,
    parse_package_price_reply,
    parse_packaged_stock_message,
    short_content_unit,
)
from ladini.domain.quantity_unit import (
    all_numbers_accounted_for,
    extract_unit_only_from_text,
    is_pure_numeric_answer,
    packaged_compound_total,
    parse_compound_quantity,
    parse_packaging_message,
    parse_quantity_unit_from_text,
    scan_number_candidates,
)
from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    get_pending_interaction,
    to_tunnel_category,
)
from ladini.graphs.agents.market_coach.core.slots import (
    SLOT_FILLING_INPUTS,
    get_slot_hint,
)
from ladini.graphs.agents.market_coach.core.state import (
    MarketAgentState,
    resolve_current_goal,
)

# Source UNIQUE du seuil de confiance de rupture d'intention. L'interpréteur
# (ici) et tunnel_manager (goal_planner) DOIVENT utiliser exactement le même :
# sinon l'interpréteur promeut un message en INTERRUPTION à un seuil que
# tunnel_manager refuse ensuite → zone morte produisant un "je n'ai pas compris"
# confus au lieu d'une bascule propre OU d'une continuation propre.
from ladini.graphs.agents.market_coach.core.tunnel_manager import (
    INTERRUPTION_CONFIDENCE_THRESHOLD,
)
from ladini.graphs.agents.market_coach.domain.selection_actions import (
    ActionType,
    build_selection_context,
    fast_path_action,
    tier_menu_prompt_block,
)
from ladini.graphs.agents.market_coach.domain.stock_shortage import (
    resolve_quantity_reply,
    shortage_awaiting_reply,
)
from ladini.graphs.agents.market_coach.domain.tier_interaction import (
    pending_pack_count_tier,
)
from ladini.graphs.agents.market_coach.interpreter.entities import (
    _fallback_quantity_unit_from_text,
    _remap_entities,
)
from ladini.graphs.agents.market_coach.interpreter.goal_planner import (
    _NAVIGATION_INTENTS,
    INTENT_TO_GOAL_MAP,
    _extract_buyer_product,
    _init_intent_to_goal_map,
    _looks_like_buyer_product_request,
    goal_planner,
)
from ladini.graphs.agents.market_coach.interpreter.intent import (
    INTENT_CONFIG,
    INTENT_ROLE,
)
from ladini.graphs.agents.market_coach.interpreter.interpreter_result import (
    InterpreterResult,
)
from ladini.graphs.agents.market_coach.interpreter.numeric_protocol import (
    interpret_buyer_numeric_protocol,
)
from ladini.graphs.agents.market_coach.interpreter.product_switch import (
    detect_buyer_product_switch,
    detect_buyer_same_product_continuation,
)
from ladini.graphs.agents.market_coach.interpreter.prompts import (
    INTERPRETER_USER_PROMPT,
)
from ladini.graphs.agents.market_coach.services.domain.commercial_gate import (
    bid_price_reply_expected,
    commercial_question_from_state,
)
from ladini.graphs.agents.market_coach.services.domain.product_validation import (
    _validate_and_sanitize_product,
)
from ladini.graphs.agents.market_coach.utils import (
    MarketRuntime,
    canonical_unit_label,
)

logger = logging.getLogger("Ladini.Market.InterpreterRouting")

# =====================================================================
# CONFIRMATION KEYWORDS — filet déterministe (voir _interpret_fast_path)
# Vocabulaire fermé, EXACT MATCH uniquement sur texte normalisé — reprend
# mot pour mot les exemples déjà documentés dans le prompt LLM ci-dessous
# (§ "CONFIRM"/"REJECT"), jamais une liste inventée séparément.
#
# "oui oui"/"non non" (incident réel, 2026-09-26) : la forme doublée
# (emphase, très courante en français) ne matchait ni "oui" ni "non" en
# EXACT MATCH sur `_bare` — le message retombait sur la classification LLM,
# qui a échoué pendant une panne du gateway LLM, laissant la confirmation
# sans réponse (récap réaffiché au lieu de CONFIRM). Ajout littéral au
# vocabulaire fermé existant, pas une nouvelle heuristique/regex.
#
# (2026-09-26, mandat onboarding "'ok' ne confirme pas le profil") : SOURCE
# UNIQUE désormais `ladini.agents.confirmation_phrases` (déplacé, jamais
# dupliqué, voir l'import en tête de fichier) — `agents/onboarding.py` (module
# volontairement sans dépendance à `graphs.*`) réutilise EXACTEMENT ce même
# vocabulaire pour sa propre confirmation de profil, au lieu d'un second
# moteur ad hoc. "ok ok"/"valider"/"je valide"/"tout est bon"/"go" ajoutés au
# passage (déjà des synonymes évidents des entrées existantes, jamais
# couverts avant).
# =====================================================================


def _fix_bare_confirmation_typo(text: str) -> str:
    """« confimer », « confirmr », « anuler » → forme canonique. Incident
    2026-09-19 : un producteur a tapé « confimer » sur son mobile ; le mot
    n'étant pas dans le vocabulaire fermé, le LLM répondait « je ne comprends
    pas ». Borné : un SEUL mot d'au moins 6 lettres, proche (difflib ≥ 0.85)
    de « confirmer »/« annuler » — jamais une phrase, jamais un autre mot."""
    import difflib

    if " " in text or len(text) < 6 or text in _CONFIRM_EXACT_PHRASES | _REJECT_EXACT_PHRASES:
        return text
    close = difflib.get_close_matches(text, ("confirmer", "annuler"), n=1, cutoff=0.85)
    return close[0] if close else text


#: Commandes de précommande reconnues SANS ambiguïté quand un panier est prêt (B9) — le mot affiché par
#: l'UI (« Répondez *précommander* pour valider ») et ses graphies courantes. Les accords nus
#: (« okay », « oui », « confirmer », « valider »…) viennent de `_CONFIRM_EXACT_PHRASES` (source unique).
_PREORDER_COMMANDS = frozenset(
    {
        "précommander", "precommander", "pré-commander", "pre-commander", "précommande",
        "precommande", "je précommande", "je precommande",
    }
)


_CART_GOALS = frozenset({"BUYER_ADD_TO_CART", "BUYER_REQUEST"})


def _live_ctx(ctx: Any) -> bool:
    return isinstance(ctx, dict) and bool(ctx) and not ctx.get("__reset__")


def _cart_ready_for_preorder(state: Dict[str, Any]) -> bool:
    """PANIER PRÊT À PRÉCOMMANDER (B10) — état MÉTIER, jamais le texte affiché.

    Prêt ⇔ panier non vide en phase CART (`_cart_pending_signal`) ET aucune ligne en cours de
    construction. Une ligne est « en cours » quand un slot est RÉELLEMENT vivant : menu producteur /
    palier, ou slot quantité / nombre de paquets adossé à un contexte vendeur/palier/produit encore
    actif pour un article PAS encore ajouté. Un slot quantité qui survit SANS contexte (ou dont le
    produit est déjà la dernière ligne du panier) est PÉRIMÉ — l'article est déjà ajouté — et ne doit
    jamais absorber la confirmation."""
    if not _cart_pending_signal(state):
        return False
    goal = str(resolve_current_goal(state) or "").upper()
    pending = get_pending_interaction(state)
    if goal and goal not in _CART_GOALS:
        return False  # un AUTRE but est actif (suivi de commande, vente…)
    if pending.kind == InteractionKind.NONE and not goal:
        return True
    if pending.kind == InteractionKind.SELECTION_MENU:
        return False  # menu producteur/palier/disambiguation réellement affiché
    quantity_slot = pending.kind in (
        InteractionKind.ENTER_QUANTITY,
        InteractionKind.ENTER_PACKAGE_COUNT,
    ) or (
        pending.kind == InteractionKind.ENTER_FIELD
        and str(pending.field or "").lower() == "quantity"
    )
    if pending.kind != InteractionKind.NONE and not quantity_slot:
        return False  # confirmation, GPS, produit demandé… : un vrai tunnel est actif
    vctx = state.get("vendor_selection_context")
    tctx = state.get("tier_selection_context")
    payload = state.get("transaction_payload") or {}
    if _live_ctx(tctx) and not (tctx or {}).get("resolved_tier_id"):
        return False  # menu de paliers non résolu
    if _live_ctx(vctx):
        chosen = (vctx or {}).get("chosen_vendor") or {}
        cart = state.get("active_cart") or []
        last_pid = (cart[-1] or {}).get("product_id") if cart else None
        # contexte vendeur vivant pour un article DÉJÀ ajouté (dernière ligne) = reliquat
        return bool(last_pid and chosen.get("product_id") == last_pid)
    if _live_ctx(payload) and payload.get("product"):
        return False  # un article est en cours d'édition
    return True


def _cart_ready_confirmation_alias(state: Dict[str, Any], text: str) -> Optional[str]:
    """Alias de confirmation d'un PANIER PRÊT À VALIDER (B9), sinon `None`.

    Contexte = état canonique `_cart_pending_signal` (phase CART + panier non vide) ; vocabulaire =
    FERMÉ (`_CONFIRM_EXACT_PHRASES` + `_PREORDER_COMMANDS`), jamais une phrase libre (celles-ci
    restent à la charge du LLM, qui reçoit `cart_pending` comme contexte). Tous les alias mènent au
    MÊME chemin canonique (`BUYER_PREORDER_INIT`), jamais six handlers différents."""
    if not _cart_ready_for_preorder(state):
        return None
    alias = _fix_bare_confirmation_typo(str(text or "").strip().lower().strip(" .!?,;: "))
    if alias in _CONFIRM_EXACT_PHRASES or alias in _PREORDER_COMMANDS:
        return alias
    return None


def _cart_pending_signal(state: Dict[str, Any]) -> bool:
    """Panier acheteur en attente de précommande — signal d'ÉTAT (phase CART
    + panier non vide), jamais un mot-clé du texte. Source unique pour les
    trois consommateurs (fast-path déterministe ci-dessous, prompt legacy,
    prompt `new_task_v2`) — auparavant recalculé trois fois à l'identique.

    (2026-09-13, incident réel WhatsApp #2) : gardait auparavant un
    `role_up == "BUYER"` — un filtrage par le RÔLE PAR DÉFAUT du graphe
    compilé (`orchestrator.py`, dérivé du `workspace_type`/profil DB), PAS
    par le contexte réel de CETTE conversation. Sous double-rôle, un
    utilisateur au profil PRODUCER peut parfaitement être en train d'ACHETER
    (le graphe qui traite son message reste `role_up="PRODUCER"` tant que
    son profil n'est pas explicitement BUYER) — `active_cart`/
    `preorder_workflow` ne sont de toute façon écrits QUE par du code panier
    acheteur : leur seule présence est déjà une preuve suffisante et exacte
    du contexte, le rôle du graphe n'ajoute rien et masquait au contraire ce
    signal pour tout profil non explicitement BUYER. Root cause du bug
    "okay"/"je suis d'accord" jamais reconnu : ni ce fast-path ni le prompt
    LLM (`cart_pending`) ne recevaient JAMAIS `True` pour ces conversations."""
    _cart_phase = str((state.get("preorder_workflow") or {}).get("phase") or "").upper()
    return _cart_phase == "CART" and bool(state.get("active_cart"))


def _producer_order_action_pending_signal(state: Dict[str, Any]) -> bool:
    """Vente(s) en attente de confirmation producteur, juste montrées —
    signal d'ÉTAT (même principe que `_cart_pending_signal` ci-dessus,
    2026-09-14). Posé par `flows/buyer/order_tracking.py::list_orders` dans
    `working_memory["producer_order_action_hint"]` quand `_producer_sales_
    block` affiche au moins une vente 🟡 avec l'invite "Tapez *confirmer*...
    ou *annuler*...". Consommé UNIQUEMENT comme CONTEXTE du prompt NEW_TASK
    (`NewTaskPromptContext.producer_order_action_pending`) — jamais un
    court-circuit Python : `_interpret_fast_path` ne peut structurellement
    introduire aucun nouveau but (voir `TestGoalLockOnlyValidatedOnTheLlmPath`,
    tests/architecture/test_fastpath_normal_path_equivalence.py), donc ce
    signal doit rester la propriété exclusive du chemin LLM, exactement
    comme `cart_pending`."""
    return bool((state.get("working_memory") or {}).get("producer_order_action_hint"))

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


# (2026-09-14, Deep Intent Architecture Cleanup) : `_DISABLED_INTENT_
# PREFIXES`/`_DEPRECATED_INTENTS` SUPPRIMÉS — ce chantier a tranché chaque
# entrée qu'ils masquaient : soit réellement DELETE (code mort supprimé
# d'`INTENT_CONFIG` lui-même — CROP_*/AGRO_*/SYSTEM_*/STOCK write ops/etc.,
# voir intent.py et le rapport final), soit RE-EXPOSÉE (FINANCE_* : capacité
# réelle et câblée, masquée uniquement pour une pression coût LLM que
# new_task_v2 a largement résorbée). Il n'existe donc plus, par construction,
# d'entrée dans `INTENT_CONFIG` qui ne soit pas un objectif utilisateur réel
# — `_classifiable_intents()`/`allowed_intents_for_role()` n'ont plus besoin
# de filtrer quoi que ce soit au-delà du catalogue lui-même.


def _classifiable_intents() -> frozenset:
    """Intentions réellement proposables au LLM. Fonction SÉPARÉE de
    `allowed_intents_for_role()` (pas un simple alias) — pour la même
    raison qu'avant ce chantier : qu'un futur correctif de
    `allowed_intents_for_role` qui réintroduirait un VRAI filtrage par
    rôle (le bug corrigé le 2026-09-08) ne puisse pas silencieusement
    recontaminer le catalogue LLM par une signature partagée."""
    return frozenset(INTENT_CONFIG)


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
    return PRODUCER_INTENTS | BUYER_INTENTS | COMMON_INTENTS | frozenset(missing)


# Goal de CRÉATION (pas encore persisté, en attente de CONFIRMATION) → intent
# "jumeau" décrit dans le catalogue comme une mise à jour d'une entité déjà
# EXISTANTE. Une correction en langage libre pendant la confirmation ("non
# c'est 200 tonnes") ressemble structurellement à ce jumeau pour le LLM — voir
# le garde-fou dans `make_input_interpreter` juste avant le forçage
# INTERRUPTION sur CONFIRMATION.
_PENDING_CREATE_UPDATE_SIBLINGS: Dict[str, str] = {
    "SALES_PUBLISH_PRODUCT": "SALES_UPDATE_PRODUCT",
    "PRODUCTION_DECLARE_FUTURE": "PRODUCTION_UPDATE_FUTURE",
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
      "action_offer_id": "<str|null>",
      "action_producer_id": "<str|null>",
      "action_pricing_tier_id": "<str|null>",
      "action_package_count": <float|null>,
      "action_quantity": <float|null>,
      "action_unit": "<str|null>"
  }
}

Les 7 champs `agent_action`/`action_*` ne sont utilisés QUE quand le contexte
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

1bis-bis. **VENTE PRODUCTEUR EN ATTENTE DE CONFIRMATION** (2026-09-14,
   incident réel — "confirmer"/"annuler" nu retombait en UNKNOWN) : si le
   contexte agent indique `vente_producteur_en_attente_de_confirmation = OUI`,
   le producteur vient de voir une liste de ventes l'invitant EXPLICITEMENT à
   taper « confirmer » ou « annuler » (aucun numéro requis s'il n'y a qu'une
   seule vente en attente — le résolveur identifie déjà seul la commande
   concernée). Un message réduit à ce seul mot (« confirmer », « je
   confirme », « j'accepte ») → interpreted_event = NEW_TASK, detected_intent
   = PRODUCER_CONFIRM_ORDER. Réduit à « annuler »/« je refuse »/« je ne peux
   pas honorer » → interpreted_event = NEW_TASK, detected_intent =
   PRODUCER_CANCEL_ORDER (JAMAIS BUYER_CANCEL_ORDER : c'est une vente reçue,
   pas un achat). N'extrais AUCUNE entité dans ce cas (`extracted_entities`
   vide) — la résolution de LA commande concernée est déjà assurée en aval.

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

   **EXCEPTION (2026-09-11, incident réel corrigé)** : si le message NOMME
   EXPLICITEMENT un produit/une action qui NE CORRESPOND À AUCUNE option de
   la liste fournie dans le contexte (`expected_candidates` ci-dessus) — ex:
   le menu liste des NUMÉROS DE COMMANDE ("Commande #65280745", "Commande
   #6EB828D8...") et le message dit "je veux commander des poulets" (aucune
   commande listée ne s'appelle "poulets") — alors ce N'EST PAS une réponse
   au menu : classe-le selon sa VRAIE intention (ex: `BUYER_REQUEST`,
   confidence normale selon ta certitude), PAS `SELECTION`. Le mécanisme de
   détection d'interruption en aval (basé sur la confidence) gère ensuite la
   sortie de tunnel proprement — pas besoin de la forcer ici. Cette exception
   ne s'applique QUE si le produit/l'action nommé est clairement absent de la
   liste ; en cas de doute réel (le message pourrait désigner une des
   options), reste sur `SELECTION` comme la règle générale l'exige.

   **EXCEPTION 2 (2026-09-12, incident réel corrigé)** : si le message
   exprime une demande de GESTION DE CATALOGUE ("je veux voir mes
   produits", "mon catalogue", "mes produits en vente", "gérer mon stock",
   "qu'est-ce que je vends") — vocabulaire PRODUIT/CATALOGUE, sans aucun
   chiffre, ordinal, ni mot de sélection ("le premier", "le 2", "celui-là")
   — et que le menu actif porte sur un domaine différent (ex: une liste de
   COMMANDES, "Commande #65280745"...) : ce N'EST PAS une réponse au menu,
   même si aucun produit/action précis n'est nommé. Classe-le selon sa VRAIE
   intention (ex: `SALES_GET_CATALOG`), PAS `SELECTION`. Exemple : le menu
   liste des commandes et le message dit "je veux voir mes produits" → ce
   n'est pas une réponse au menu, classe `SALES_GET_CATALOG`. En cas de
   doute réel (le message pourrait être une réponse au menu), reste sur
   `SELECTION`.

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
   - `SELECT_PRODUCER` → `extracted_entities.action_offer_id` = le
     `offer_id` EXACT listé dans le bloc, copié tel quel — jamais un id
     que tu inventes, jamais un id d'une AUTRE liste. (Un même producteur
     peut avoir PLUSIEURS offres : c'est l'`offer_id`, pas le producteur, qui
     désigne la ligne choisie.)
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

5ter. **QUANTITÉ TOTALE RÉPARTIE EN PLUSIEURS CONDITIONNEMENTS, UN SEUL PRIX
   (jamais de troncature)** : si l'utilisateur décrit sa quantité totale en
   PLUSIEURS groupes de conditionnements DIFFÉRENTS pour le MÊME produit, SANS
   prix distinct par groupe (contrairement à la règle 5bis ci-dessus — un
   SEUL prix pour l'ensemble, donné dans ce message ou à venir plus tard),
   calcule la quantité TOTALE en additionnant chaque groupe (nombre de
   conditionnements × contenu de chacun), jamais seulement le premier nombre
   rencontré. Exemple : "60 bidons de 5 litres et 20 bidons de 20 litres" =
   (60×5) + (20×20) = 300 + 400 → quantity=700.0, unit="LITRE" (jamais
   quantity=60, qui ne serait que le nombre de bidons du premier groupe pris
   à tort pour la quantité). Cette règle s'applique quelle que soit la
   formulation exacte (virgules, "plus", "et aussi", conditionnements
   différents entre les groupes comme "bidons" puis "fûts"...) — c'est
   TOUJOURS le calcul (paquets × contenu, sommé) qui compte, jamais la forme
   de la phrase.

5quater. **QUANTITÉ TOTALE (5ter) + PRIX DE RÉFÉRENCE PAR UNITÉ + TARIFS PAR
   CONDITIONNEMENT (5bis) — LE MESSAGE PEUT COMBINER LES TROIS À LA FOIS** :
   ex: "60 bidons de 5 litres et 30 bidons de 20 litres. Prix : 3000 FCFA le
   litre, et 1 bidon de 5 L coûte 10000 FCFA, 1 bidon de 20 L coûte 50000
   FCFA." Traite chaque partie SÉPARÉMENT, sans jamais laisser l'une écraser
   ou vider les autres :
   - `quantity`/`unit` RACINE = la quantité totale en stock, calculée par la
     règle 5ter (60×5 + 30×20 = 900.0, unit="LITRE") — INDÉPENDAMMENT des
     prix qui suivent.
   - Un prix donné "par unité de base" SANS conditionnement précis ("3000
     FCFA le litre", "3000 FCFA/L") est le prix DE RÉFÉRENCE global : va
     dans `price`/`price_unit` RACINE (price=3000.0, price_unit="LITRE"),
     JAMAIS comme un `pricing_tiers` distinct — ce n'est pas un tarif propre
     à un conditionnement précis, juste le taux appliqué par défaut.
   - Chaque prix donné "pour UN conditionnement précis" ("1 bidon de 5 L
     coûte 10000 FCFA") EST un tarif au sens de la règle 5bis : un objet
     dans `pricing_tiers` (quantity=5.0, unit="L", price=10000.0,
     packaging="bidon"), et de même pour le second ("1 bidon de 20 L coûte
     50000 FCFA" → quantity=20.0, unit="L", price=50000.0,
     packaging="bidon"). Résultat attendu pour l'exemple ci-dessus :
     quantity=900.0, unit="LITRE", price=3000.0, price_unit="LITRE",
     pricing_tiers=[{"quantity":5.0,"unit":"L","price":10000.0,
     "packaging":"bidon"},{"quantity":20.0,"unit":"L","price":50000.0,
     "packaging":"bidon"}] — jamais un tarif fantôme associant le prix de
     référence (3000) à l'un des conditionnements (ex: "20 L = 3000 FCFA"),
     et jamais la quantité totale réduite au nombre de bidons d'un seul
     groupe.

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
"50kg de patates pour le 29 octobre" → {"interpreted_event":"NEW_TASK","detected_intent":"PRODUCTION_DECLARE_FUTURE","interpreter_confidence":0.95,"validation_status":"VALID","extracted_entities":{"product":"patates","quantity":50.0,"unit":"KG","estimated_available_at":"2026-10-29"}}
"20 tomates" → {"interpreted_event":"NEW_TASK","detected_intent":"PRODUCTION_DECLARE_FUTURE","interpreter_confidence":0.85,"validation_status":"INVALID_MISSING_UNIT","extracted_entities":{"product":"tomates","quantity":20.0,"unit":null}}
"je cherche des œufs et de la laitue dans ma région" → {"interpreted_event":"NEW_TASK","detected_intent":"BUYER_REQUEST","interpreter_confidence":0.9,"validation_status":"VALID","extracted_entities":{"product":"œufs","additional_products":["laitue"]}}
"60 bidons de 5 litres et 20 bidons de 20 litres" (règle 5ter, quantité totale par groupes) → {"interpreted_event":"ANSWER","detected_intent":"SALES_PUBLISH_PRODUCT","interpreter_confidence":0.95,"validation_status":"VALID","extracted_entities":{"quantity":700.0,"unit":"LITRE"}}
"60 bidons de 5 litres et 30 bidons de 20 litres. Prix : 3000 FCFA le litre, et 1 bidon de 5 L coûte 10000 FCFA, 1 bidon de 20 litres coûte 50000 FCFA." (règle 5quater, quantité + prix de référence + tarifs par conditionnement combinés) → {"interpreted_event":"ANSWER","detected_intent":"SALES_PUBLISH_PRODUCT","interpreter_confidence":0.95,"validation_status":"VALID","extracted_entities":{"quantity":900.0,"unit":"LITRE","price":3000.0,"price_unit":"LITRE","pricing_tiers":[{"quantity":5.0,"unit":"L","price":10000.0,"packaging":"bidon"},{"quantity":20.0,"unit":"L","price":50000.0,"packaging":"bidon"}]}}
""")


# =====================================================================
# CACHE D'INTERPRÉTATION LLM — retry-safety (2026-09-12)
#
# `process_agent_task` (api/tasks.py) a `autoretry_for=(Exception,),
# max_retries=3` : une erreur survenant N'IMPORTE OÙ dans le tour — DB,
# MCP, WhatsApp, un bug d'executor — APRÈS un appel LLM d'interprétation
# déjà réussi relance TOUT le tour depuis zéro, y compris ce même appel
# payant, pour un résultat qu'on connaît déjà. `message_sid` (identifiant
# STABLE de l'événement WhatsApp, injecté dans l'état initial par
# `orchestrator.py::_run_market`) sert de clé : le retry Celery relit le
# résultat mis en cache au lieu de rappeler le LLM.
#
# La clé combine `message_sid` + `INTERPRETER_PROMPT_VERSION` + le modèle
# DEMANDÉ + un hash de `system_prompt + user_prompt` concaténés. `message_sid`
# seul ne suffit PAS : sans la version/le modèle/le hash du prompt complet
# (system ET user, pas seulement user — le system prompt PEUT changer entre
# deux déploiements, ex: un correctif de règle ou un changement de modèle
# FAST/REASONING, sans qu'aucune valeur d'état ne change), un retry Celery
# survenant APRÈS un déploiement aurait silencieusement réutilisé une
# completion produite par l'ANCIEN prompt/modèle — incohérence invisible,
# jamais un crash. `INTERPRETER_PROMPT_VERSION` DOIT être incrémentée à
# chaque changement comportemental du prompt système (règles, catalogue,
# format du contrat JSON) : la clé change alors mécaniquement, un ancien
# cache ne peut plus jamais être relu après un tel changement, même si son
# TTL n'a pas encore expiré.
#
# Portée volontairement étroite : ne cache QUE l'appel LLM le plus coûteux
# et le plus fréquent (l'interprète, 1 par tour) — pas les autres appels
# (clarification/ask/deviation), plus rares et déjà chacun protégés par
# leurs propres gardes (voir leurs modules respectifs). TTL 1h, largement
# supérieur à la fenêtre de retry Celery (backoff exponentiel, 3 tentatives).
_LLM_INTERPRETATION_CACHE_TTL_SECONDS = 3600

# Version explicite du prompt d'interprétation — à incrémenter à CHAQUE
# changement comportemental de `_SYSTEM_PROMPT_TEMPLATE`/`_build_dynamic_
# interpreter_prompt` (nouvelle règle, catalogue modifié, format du contrat
# JSON changé). Sert de composante de la clé de cache LLM (voir ci-dessus)
# ET de dimension Langfuse (`prompt_version`, voir plus bas) pour pouvoir
# comparer le comportement/coût AVANT/APRÈS un changement de prompt.
INTERPRETER_PROMPT_VERSION = "interpreter_v4"


def _llm_cache_key(
    message_sid: Optional[str],
    system_prompt: str,
    user_prompt: str,
    requested_model: Optional[str],
) -> Optional[str]:
    """`None` si `message_sid` est absent (ex: appel direct hors webhook,
    test) — pas de cache possible sans identifiant stable, l'appelant
    retombe alors simplement sur le chemin normal (toujours appeler le LLM).

    Le hash porte sur `system_prompt` ET `user_prompt` concaténés (séparés
    par un octet NUL, jamais présent dans du texte normal, pour éviter
    qu'une coupure ambiguë entre les deux fasse collision) — pas seulement
    `user_prompt` : le system prompt encode le catalogue d'intentions et
    les règles, silencieusement modifiables entre deux tentatives d'un
    même message (déploiement, hotfix)."""
    if not message_sid:
        return None
    digest = hashlib.sha256(
        f"{system_prompt}\x00{user_prompt}".encode("utf-8")
    ).hexdigest()[:16]
    model_part = requested_model or "unknown_model"
    return (
        f"interp_llm_cache:{INTERPRETER_PROMPT_VERSION}:{model_part}:"
        f"{message_sid}:{digest}"
    )


def _load_cached_completion(
    cache_key: Optional[str],
) -> Optional[Tuple[Dict[str, Any], Optional[str]]]:
    raw = _get_cached_value(cache_key)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
        return payload.get("parsed") or {}, payload.get("model")
    except Exception:
        # Cache corrompu/format inattendu : fail-open, comme une absence de
        # cache — jamais bloquer le tour pour une valeur qu'on ne sait pas lire.
        logger.warning("[Interpreter] Cache LLM illisible — appel normal")
        return None


def _store_cached_completion(
    cache_key: Optional[str], parsed: Dict[str, Any], model: Optional[str]
) -> None:
    if not cache_key:
        return
    try:
        _set_cached_value(
            cache_key,
            json.dumps({"parsed": parsed, "model": model}),
            ttl_seconds=_LLM_INTERPRETATION_CACHE_TTL_SECONDS,
        )
    except Exception:
        # Best-effort : une écriture de cache qui échoue ne doit jamais faire
        # échouer le tour qui vient de produire ce résultat.
        logger.warning("[Interpreter] Écriture cache LLM échouée — ignorée")


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

    # (2026-09-12, optimisation coût/taille prompt) : DEUX réductions
    # additives, chacune sans risque comportemental vérifié —
    #
    # 1. `_classifiable_intents()` retire les entrées mortes/désactivées du
    #    texte envoyé au LLM (voir sa docstring) — ~39% du catalogue, sans
    #    toucher `INTENT_CONFIG` ni réintroduire de filtrage par rôle.
    # 2. La clause `[requis: ...]` est retirée du format de ligne — vérifié
    #    qu'AUCUN consommateur ne la reparse depuis ce texte de prompt :
    #    `required` est lu directement sur `INTENT_CONFIG` par
    #    `nodes/validation.py`, `nodes/executor.py`, `nodes/cleaner.py`,
    #    `services/mcp/schema_resolver.py` — jamais depuis la sortie du LLM.
    #    Le rôle du LLM ici est de CHOISIR l'intention, pas de connaître ses
    #    champs requis (le remplissage de slot est un tour conversationnel
    #    séparé, avec sa propre source de vérité). Économie mesurée :
    #    ~51% du catalogue combiné aux deux coupes (~400-600 tokens/appel).
    classifiable = _classifiable_intents()
    intent_lines: List[str] = [
        f"  - {intent_key} : {config.get('label', intent_key)}"
        for intent_key, config in INTENT_CONFIG.items()
        if intent_key in classifiable
    ]

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


def _fast_path_update_field_correction(
    locked_goal: Optional[str], text: str
) -> Optional[Dict[str, Any]]:
    """Court-circuite déterministiquement le tunnel "quel champ modifier ?"
    (`field_name="update_field"`, catégorie résolue `"UPDATE_FIELD"` — voir
    le commentaire d'appel dans `_interpret_fast_path` pour l'incident
    complet). Réutilise le parseur déterministe DÉJÀ correct de la couche
    flow concernée — jamais une copie qui dériverait — pour les 3 mini-flows
    connus qui partagent ce même motif. `None` = abstention explicite
    (goal non reconnu, aucun champ extrait, ou message ambigu avec un
    palier tarifaire précis) : l'appelant retombe alors sur le comportement
    existant (classification LLM), inchangé.
    """
    from ladini.domain.quantity_unit import (
        extract_deterministic_pricing_tiers,
        extract_single_pricing_tier_correction,
    )

    # Abstention délibérée : une correction de PALIER précis ("prix bidon de
    # 20 L à 70000 fcfa") ne peut être résolue sans risque de faux positif
    # qu'avec les tarifs RÉELS du produit (chargés en DB par flow.py,
    # `current_tiers`) — ce fast-path, sans accès DB, ne les a pas. Sans
    # cette garde, un message qui a la FORME d'une correction de palier
    # serait mal-interprété comme un prix scalaire plat (exactement le bug
    # déjà corrigé le 2026-09-14 côté flow.py, voir
    # `flow.py::_extract_pricing_tier_update`) — jamais reproduit ici.
    if extract_single_pricing_tier_correction(text) or extract_deterministic_pricing_tiers(text):
        return None

    goal_up = str(locked_goal or "").upper()
    correction: Dict[str, Any]
    if goal_up == "SALES_UPDATE_PRODUCT":
        from ladini.graphs.agents.market_coach.flows.producer.flow import (
            _parse_update_correction,
        )

        correction = _parse_update_correction(text, allow_type_date=False)
    elif goal_up == "PRODUCTION_UPDATE_FUTURE":
        from ladini.graphs.agents.market_coach.flows.producer.flow import (
            _parse_update_correction,
        )

        correction = _parse_update_correction(text, allow_type_date=True)
    elif goal_up == "PROCUREMENT_UPDATE_REQUEST":
        from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import (
            _parse_auction_update_correction,
        )

        correction = _parse_auction_update_correction(text)
    else:
        # Goal non reconnu par ce fast-path (mini-flow futur qui réutilise
        # le même sentinel "update_field" sans être câblé ici) — abstention
        # plutôt qu'une supposition : comportement inchangé pour lui.
        return None

    if not correction:
        return None

    return {
        "interpreted_event": "ANSWER",
        "detected_intent": goal_up,
        "interpreter_confidence": 0.95,
        "extracted_entities": dict(correction),
        "raw_analysis": {"path": "fast_path_update_field_correction"},
    }


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
    #
    # (2026-09-13, incident réel WhatsApp, PUIS décision produit explicite) :
    # une tentative précédente étendait ce filet au signal d'état
    # `cart_pending` (panier non vide, `expected in {"NONE", "SELECTION"}`)
    # pour couvrir l'affichage du panier (`BUYER_VIEW_CART`, qui ne pose
    # aucun `PendingInteraction` de type CONFIRMATION). Rejetée : décider à
    # coups de vocabulaire fermé lequel des innombrables synonymes/graphies
    # d'accord libre ("okay", "je valide", "je suis d'accord", "ça marche"...)
    # mérite un court-circuit est un jeu qu'on ne peut pas gagner ("à vouloir
    # tout prédire, on ne pourra pas s'en sortir" — retour explicite). Le
    # signal `cart_pending` (`_cart_pending_signal`) reste calculé et transmis
    # aux DEUX prompts LLM (legacy + `new_task_v2`) comme CONTEXTE — c'est le
    # LLM, jamais Python, qui décide si le texte constitue un accord. Ce
    # fast-path reste donc strictement scopé à `CONFIRM_ACTION`
    # (`expected == "CONFIRMATION"`), son périmètre d'origine.
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

    # ⚠️ CORRECTIF (2026-09-19, incident réel) — mini-flows "quel champ
    # modifier ?" (SALES_UPDATE_PRODUCT / PRODUCTION_UPDATE_FUTURE /
    # PROCUREMENT_UPDATE_REQUEST, `ENTER_FIELD` avec `field_name=
    # "update_field"`, voir `core/pending_interaction.py::to_tunnel_category`
    # lignes 417-426). Ce champ est un SENTINEL INTERNE délibérément absent
    # de `core/slots.py::SLOT_FILLING_INPUTS` (le champ à corriger n'est
    # connu qu'APRÈS lecture du message : prix, quantité, nom, unité, date,
    # palier...) — `to_tunnel_category` résout donc "UPDATE_FIELD", qui ne
    # peut PAS matcher `SLOT_FILLING_INPUTS` dans `state_router.py::
    # choose_interpretation_route`, donc CE tunnel ne route JAMAIS vers le
    # micro-prompt spécialisé ACTIVE_SLOT — il retombe sur la route NEW_TASK
    # (l'interpréteur unifié historique, un seul gros prompt ~60 intentions).
    #
    # Incident réel observé : "prix 485000 fcfa et la quantite est
    # maintenant de 95" (double correction, dans le tunnel) classé UNKNOWN
    # par ce classifieur générique → `cognitive_guard` déclenche RECOVERY
    # (le message est accusé réception puis IGNORÉ — le flow.py de mise à
    # jour, dont le parseur déterministe `_parse_update_correction` est
    # pourtant déjà correct pour CE cas exact, n'est jamais invoqué) ; au
    # 2e essai consécutif classé UNKNOWN, `reset_abandoned_conversation_
    # context` efface le tunnel entier — un simple "prix 485000 fcfa" (le
    # format d'exemple donné PAR LE BOT lui-même) tombe alors hors contexte
    # et reçoit le message générique "je n'ai pas compris".
    #
    # Correctif — même principe que tout le reste de ce fast-path (le
    # texte, jamais le LLM, tranche dès qu'une extraction déterministe est
    # possible, voir la garde anti-ancrage documentée dans ce module) :
    # tente le parseur déterministe DÉJÀ correct de la couche flow AVANT de
    # laisser la main au classifieur générique. `_fast_path_update_field_
    # correction` s'abstient explicitement (retourne `None`, comportement
    # inchangé) sur tout goal non reconnu ou tout message qui ressemble à
    # une correction de PALIER tarifaire précis (ex: "prix bidon de 20 L à
    # 70000 fcfa") — cette dernière a besoin des tarifs RÉELS du produit
    # (chargés en DB par flow.py) pour être résolue sans ambiguïté
    # (incident distinct déjà corrigé le 2026-09-14, voir
    # `flow.py::_extract_pricing_tier_update`) ; ce fast-path, sans accès
    # DB, ne doit jamais deviner à sa place.
    if expected == "UPDATE_FIELD":
        fast_update = _fast_path_update_field_correction(locked_goal, text)
        if fast_update is not None:
            return fast_update

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

        # ── QUANTITÉ AMBIGÜE : 2+ NOMBRES PORTANT CHACUN UNE UNITÉ ──
        # ("60 bidons de 5 litres et 20 bidons de 20 litres") : incident réel
        # (2026-09-14) — `scan_number_candidates` ci-dessus associe un nombre
        # à N'IMPORTE QUELLE unité trouvée dans sa fenêtre de `_SCAN_WINDOW`
        # caractères, sans vérifier l'adjacence réelle. Le "60" (un NOMBRE DE
        # PAQUETS, sans dimension propre) captait l'unité "litres" du bidon
        # voisin → `quantity=60, unit=LITRE` au lieu du volume total réel
        # (700 L), le second groupe "20 bidons de 20 litres" disparaissant
        # purement et simplement — le pick "premier candidat typé" plus bas
        # dans cette fonction choisissait ainsi un résultat FAUX avec une
        # fausse confiance.
        #
        # Décision produit explicite (2026-09-14) : la correction n'est PAS
        # d'empiler une regex par tournure de phrase possible — le langage
        # humain est trop divers pour ça et un producteur ne doit jamais être
        # contraint à un format précis. Dès que 2+ nombres portent chacun une
        # unité, la situation est structurellement ambiguë pour un simple
        # "premier candidat" (même logique déjà en place pour les tarifs
        # multiples juste en dessous, `_currency_candidates >= 2`) : on tente
        # le parseur déterministe UNIQUEMENT sur le motif explicite et sans
        # ambiguïté "N <conditionnement> de M <unité>" (filet de fiabilité
        # rapide, jamais de résultat partiel deviné) ; s'il ne reconnaît pas
        # la structure, on laisse la main au LLM (`return None`) plutôt que
        # de risquer le pick arbitraire — c'est lui, pas une regex, qui est
        # armé pour la diversité réelle des formulations (voir règle 5ter du
        # prompt système).
        _qty_unit_candidates = [
            c for c in candidates if c["unit"] and not c["near_currency"]
        ]

        # ── ANALYSE "packaging / multi-tarification" : UNE SEULE PASSE ──
        # (2026-09-21, refonte STRUCTURELLE) `scan_number_candidates`
        # ci-dessus reste ce qui AIGUILLE (fenêtre glissante, aucune notion
        # de clause) ; l'EXTRACTION réelle de ce thème, elle, vient
        # désormais d'un seul et même moteur, `parse_packaging_message` —
        # au lieu de trois analyses indépendantes du même texte qui
        # pouvaient diverger sans que rien ne le détecte (cause commune des
        # incidents 2026-08-29 / 08-30 / 09-14 / 09-19 / 09-21 : voir le
        # commentaire de section dans `domain/quantity_unit.py`). Quantité
        # globale ET paliers tarifaires d'un même message sortent donc
        # maintenant de la MÊME segmentation en clauses, et le moteur dit
        # lui-même s'il reste un nombre qu'il n'explique pas.
        _packaging = parse_packaging_message(text)

        # ── MULTI-TARIFICATION RÉSOLUE DÉTERMINISTEMENT ──
        # "le bidon de 5 L coûte 500 fcfa et celui de 10 L coûte 900 fcfa",
        # avec ou sans quantité globale ("j'ai 600 L de lait...") devant.
        # Condition d'entrée : la CONFIANCE DU MOTEUR, rien d'autre — 2+
        # paliers reconnus, aucune clause ambiguë, aucun nombre inexpliqué.
        #
        # (2026-09-21) Cette résolution était auparavant enfouie sous
        # `skip_numeric_shortcut` (un drapeau qui signifie "un LLM est
        # disponible sur ce runtime") : sans LLM, le message retombait sur
        # le pick "premier nombre près d'une devise" et publiait un prix
        # FAUX (mesuré : 5 FCFA/L au lieu des deux paliers, quantité
        # perdue). Une extraction 100% déterministe et complète n'a aucune
        # raison de dépendre de la présence d'un LLM — c'est au contraire
        # le cas où elle est le plus nécessaire. Elle passe donc AVANT les
        # raccourcis numériques ; quand le moteur n'est pas certain, rien
        # ne change (on retombe sur les branches existantes ci-dessous).
        if (
            _packaging.pricing_tiers
            and not _packaging.ambiguous
            and not _packaging.unexplained_numbers
        ):
            _tier_entities: Dict[str, Any] = {
                "pricing_tiers": [dict(t) for t in _packaging.pricing_tiers]
            }
            # La quantité globale vient de la MÊME analyse en clauses que
            # les paliers, pas d'un second parseur qu'il faudrait recouper :
            # une clause est un palier OU une quantité globale, jamais les
            # deux, et jamais ni l'une ni l'autre en silence. C'est ce qui
            # ferme l'incident récurrent "600 L de lait... le bidon de 5 L
            # coûte 500 fcfa..." (quantité globale perdue, l'agent la
            # redemandait juste après avoir accusé réception des tarifs).
            if _packaging.quantity is not None and _packaging.unit:
                _tier_entities["quantity"] = _packaging.quantity
                _tier_entities["unit"] = _packaging.unit
            # Double filet, volontairement conservé : le moteur signale
            # déjà tout nombre qu'il n'explique pas, mais
            # `scan_number_candidates` reste une segmentation DIFFÉRENTE du
            # même texte (fenêtre glissante vs clauses) — les faire se
            # recouper ici détecte une divergence entre les deux AVANT
            # qu'elle n'atteigne un brouillon. En cas de désaccord on ne
            # renvoie rien : les branches suivantes, puis le LLM, décident.
            if all_numbers_accounted_for(
                [c["value"] for c in candidates], _tier_entities
            ):
                return {
                    "interpreted_event": (
                        "UPDATE" if expected == "CONFIRMATION" else "ANSWER"
                    ),
                    "detected_intent": str(locked_goal or "UNKNOWN").upper(),
                    "interpreter_confidence": 0.98,
                    "extracted_entities": _tier_entities,
                    "raw_analysis": {
                        "path": "fast_path_deterministic_pricing_tiers"
                    },
                }
            logger.info(
                "[Interpreter fast-path] pricing_tiers déterministe en "
                "désaccord avec le balayage numérique — repli sur le LLM "
                "plutôt qu'un résultat partiel"
            )

        # ── STOCK GÉNÉRIQUE "N <label libre> de M <unité>" ──
        # (2026-09-30, Étape 3 du mandat "PARSER GÉNÉRIQUE package_count ×
        # package_size") : "j'ai 50 pot de 4 litre" décrit le STOCK
        # disponible, jamais un tarif — d'où le scope `expected == "QUANTITY"`
        # strict ici (§9 du mandat : ne jamais mélanger stock et pricing tier ;
        # une déclaration de PRIX passe par la branche tarifaire ci-dessus,
        # inchangée). `parse_generic_package_count_and_size` (domain/
        # commercial_offer_flow.py) fait tout son propre travail de sûreté
        # (aucune devise dans le texte, exactement 1 groupe, tous les nombres
        # expliqués, compte entier) — aucune whitelist de conditionnement,
        # contrairement à `packaged_compound_total` juste en dessous (limité à
        # `_TIER_PACKAGING_WORDS`, 9 mots fermés, conservé pour ses propres
        # tests de non-régression). Produit `package_count`/`package_label`/
        # `package_size`/`package_unit` — JAMAIS `quantity`/`unit` directement
        # : c'est `derive_available_quantity_from_package` (domain/
        # commercial_offer.py), appelé plus loin par `commercial_offer_flow`,
        # qui fait l'unique calcul `count × size` (mandat §7 — jamais recalculé
        # ici).
        if expected == "QUANTITY":
            _generic_package = parse_generic_package_count_and_size(text)
            if _generic_package is not None:
                logger.info(
                    "PACKAGED_QUANTITY_PARSED count=%s label=%s size=%s "
                    "unit=%s",
                    _generic_package.count,
                    _generic_package.package_type,
                    _generic_package.content_amount,
                    _generic_package.content_unit,
                )
                return {
                    "interpreted_event": "ANSWER",
                    "detected_intent": str(locked_goal or "UNKNOWN").upper(),
                    "interpreter_confidence": 0.98,
                    "extracted_entities": {
                        "package_count": _generic_package.count,
                        "package_label": _generic_package.package_type,
                        "package_size": _generic_package.content_amount,
                        "package_unit": _generic_package.content_unit,
                    },
                    "raw_analysis": {
                        "path": "fast_path_generic_package_count_size"
                    },
                }

        # Jamais quand un prix traîne AILLEURS dans le même message ("60
        # bidons de 5L et 30 bidons de 20L. prix : 3000fcfa/L...") : ce
        # parseur ne sait sommer QUE des groupes quantité×conditionnement, il
        # confondrait alors un "1 bidon de 5 L coûte 10000 fcfa" (une clause
        # de PRIX, qui matche aussi le motif conditionnement) avec un groupe
        # de stock supplémentaire — un message aussi composé est structurel-
        # lement du ressort du LLM (règle 5bis/5ter), jamais d'une somme
        # aveugle de tous les nombres conditionnés du texte.
        if (
            expected == "QUANTITY"
            and pack_count_tier is None
            and len(_qty_unit_candidates) >= 2
            and not any(c["near_currency"] for c in candidates)
        ):
            _packaged = packaged_compound_total(_packaging)
            if _packaged.quantity is not None and _packaged.unit is not None:
                return {
                    "interpreted_event": "ANSWER",
                    "detected_intent": str(locked_goal or "UNKNOWN").upper(),
                    "interpreter_confidence": 0.98,
                    "extracted_entities": {
                        "quantity": _packaged.quantity,
                        "unit": _packaged.unit,
                    },
                    "raw_analysis": {
                        "path": "fast_path_packaged_compound_quantity"
                    },
                }
            return None

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
        #
        # (2026-09-21) Cette branche est redevenue une ABSTENTION PURE : la
        # tentative déterministe qu'elle contenait a été remontée plus haut
        # (voir "MULTI-TARIFICATION RÉSOLUE DÉTERMINISTEMENT"), où elle ne
        # dépend plus de `skip_numeric_shortcut`. Si on arrive ici, c'est
        # que le moteur n'a PAS su résoudre le message sans ambiguïté —
        # donc exactement le cas où il faut laisser la main au LLM.
        _currency_candidates = [c for c in candidates if c["near_currency"]]
        if (
            (skip_numeric_shortcut or (_confirmation_correction and llm_available))
            and len(_currency_candidates) >= 2
        ):
            return None

        # Réponse composée non-ambiguë ("775 kg d'oignon et le kg coûte 175
        # fcfa") : un nombre porte une unité de poids/comptage SANS devise à
        # proximité (quantité), un AUTRE porte une devise à proximité (prix) —
        # c'est de l'extraction structurée par correspondance exacte de
        # tokens, pas une supposition floue. On remplit les DEUX slots
        # d'un coup, TOUJOURS (même LLM disponible) : router un cas aussi
        # net vers le LLM ne fait que l'exposer à une mauvaise classification
        # d'intention (vécu en prod — un message quantité+prix composé s'est
        # fait détourner vers PRODUCTION_DECLARE_FUTURE alors que le tunnel actif
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
            and is_pure_numeric_answer(text)
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

        # (Phase 2.5, H5) : cette branche ne DÉCIDE plus rien dès qu'un vrai
        # classifieur existe pour trancher — elle ne fait que préserver, le
        # cas échéant, l'ancien comportement déterministe pour les runtimes
        # SANS classifieur réel (tests bas niveau, environnements sans LLM
        # configuré). Un simple nombre tapé pendant une CONFIRMATION n'a
        # ENCORE aucune preuve d'appartenir au brouillon actif : il peut tout
        # aussi bien porter une tâche nouvelle et indépendante et isolable
        # ("je veux 30 poulets chaque semaine" pendant la confirmation d'un
        # tout autre produit — incident H5). Fabriquer ici `interpreted_event`
        # AVANT tout passage par l'interpréteur/`cognitive_guard` revient à
        # trancher "correction vs nouvelle tâche" sans le contexte complet
        # (goal actif, confiance, intent détecté) — exactement l'invariant
        # manquant identifié en Phase 2.5. Avec un classifieur réel
        # disponible, on s'abstient (`return None`) : le message suit le
        # chemin NEW_TASK normal, et c'est `decide_active_draft_reply`
        # (`core/turn_policy.py`), avec la classification RÉELLE en main, qui
        # tranche correction vs tâche indépendante — jamais ce raccourci.
        # L'EXACTITUDE de la valeur (somme d'une quantité composée que le LLM
        # pourrait tronquer) reste, elle, garantie séparément par la surcouche
        # de correction posée sur le point de passage unique
        # (`input_interpreter` — voir `_apply_confirmation_value_overlay`),
        # qui ne décide RIEN mais corrige la valeur d'un tour déjà classifié.
        if _confirmation_correction and not llm_available:
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
        elif _confirmation_correction:
            # `llm_available` : un classifieur réel existe, la correction
            # explicite de valeur ci-dessus est désactivée (frontière H5).
            # `return None` explicite ici, PAS une simple absence de retour :
            # sans lui, ce nombre retomberait dans le bloc générique "cas
            # simple non-ambigu restant" un peu plus bas (`skip_numeric_shortcut`
            # y vaut toujours False pour CONFIRMATION — il ne filtre que
            # PRICE/QUANTITY, voir son calcul dans `input_interpreter`), qui
            # le fast-patherait quand même en ANSWER/UPDATE et annulerait
            # silencieusement l'abstention voulue ci-dessus.
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
            if numeric_value is not None and not is_pure_numeric_answer(text):
                # (2026-09-30, incident réel : "60 L de miel" avec product
                # courant="lait" et expected=QUANTITY) — un nombre typé sans
                # ambiguïté ("60", unité LITRE) ne suffit PAS à autoriser ce
                # fast-path : le reste du message ("de miel") peut porter une
                # entité métier explicite (nom de produit, marqueur de
                # correction "finalement"/"non"...) que ce fast-path ne sait
                # pas lire — il ne regarde que les nombres. L'invariant :
                # un fast-path ne peut produire un résultat QUE s'il comprend
                # complètement les éléments métier significatifs du message
                # pour son périmètre ; sinon il s'abstient et laisse la main
                # à l'interprétation complète, seule capable d'extraire
                # `product` et d'atteindre la logique de conflit déjà
                # correcte de `nodes/memory.py::_apply_slot`.
                #
                # (2026-09-30, durcissement) — CETTE abstention ne dépend PLUS
                # de `llm_available` : la sûreté métier (ne jamais attacher
                # silencieusement une quantité/un prix au mauvais produit) ne
                # peut pas dépendre de la disponibilité d'un provider LLM, d'un
                # timeout ou d'un mode dégradé. `llm_available` ne change QUE
                # ce qui se passe APRÈS cette abstention :
                #   - LLM disponible  -> l'interprétation complète (micro-
                #     prompt ACTIVE_SLOT) tranche avec le contexte réel ;
                #   - LLM indisponible -> `_interpret_fast_path` renvoie quand
                #     même `None` ici, et l'appelant (`_input_interpreter_impl`)
                #     retombe sur son repli déjà existant sans LLM
                #     (`interpreted_event="UNKNOWN"`, voir le warning "No LLM
                #     on runtime" — AUCUN champ métier n'est écrit, donc AUCUNE
                #     mutation incorrecte n'est possible en aval) — jamais un
                #     second mécanisme de clarification créé ici.
                logger.info(
                    "[Interpreter fast-path] ABSTAIN expected=%s "
                    "reason=NON_NUMERIC_BUSINESS_CONTENT fallback=%s",
                    expected,
                    "FULL_INTERPRETATION" if llm_available else "SAFE_UNKNOWN",
                )
            elif numeric_value is not None:
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
                        # (B8) une unité SUPPOSÉE par défaut (`unit_was_assumed`, ex. « KG » pour du
                        # lait) n'est pas une unité de l'acheteur : un nombre nu ne l'hérite pas
                        # (sinon « 5 » devient « 5 kg » puis est refusé/converti à tort).
                        if payload.get("unit_was_assumed"):
                            fallback_unit = None
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
        # (B6, 2026-10-02) : un pending qui attend une VALEUR de slot (quantité/prix/unité…) n'est
        # pas un menu — `expected_candidates`/`available_mapping_kind` survivent au menu producteur
        # DÉJÀ résolu : sans ce garde, la quantité « 2 » après le choix du producteur était relue
        # comme « producteur n°2 » (tout chiffre ≤ nombre de producteurs).
        if expected not in SLOT_FILLING_INPUTS and (
            expected == "SELECTION" or len(candidates) > 0 or has_active_mapping
        ):
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
    if raw.get("offer_id"):
        entities["action_offer_id"] = raw["offer_id"]
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


async def _bare_confirmation_for_pending_producer_order(
    mc_runtime: Any, phone: str, bare_text: str, role: str = "PRODUCER"
) -> Optional[Dict[str, Any]]:
    """Incident réel RÉPÉTÉ (2026-09-14, 2026-09-15) : un producteur reçoit
    une notification PROACTIVE hors-conversation ("Nouvelle commande —
    confirmation requise... Tapez *confirmer*... ou *annuler*...",
    `workers/outbox/templates.py::_render_preorder_confirmed_producer`) —
    AUCUN tour de conversation n'a eu lieu pour verrouiller quoi que ce soit
    dans l'état persisté (contrairement à `flows/buyer/order_tracking.py::
    list_orders`, qui verrouille `PendingInteraction(CONFIRM_ACTION)` en
    tour normal — voir son docstring). La réponse arrive donc comme un mot
    NU, sans tunnel actif, sans aucun autre ancrage — exactement le cas où
    une classification LLM libre s'est avérée, empiriquement, structurel-
    lement peu fiable (2 correctifs précédents : label enrichi, puis signal
    d'état donné en CONTEXTE au prompt — tous deux insuffisants).

    Vérification déterministe et BORNÉE, jamais un résultat deviné : ne
    répond QUE si `bare_text` est exactement un mot d'accord/refus fermé
    (même vocabulaire que `_CONFIRM_EXACT_PHRASES`/`_REJECT_EXACT_PHRASES`
    ci-dessus) ET qu'il existe EXACTEMENT une vente PENDING_PRODUCER_
    CONFIRMATION pour ce producteur — `_resolve_pending_order_action`
    (flows/producer/flow.py) répondrait de toute façon proprement ("rien à
    confirmer", ou un menu numéroté) si ce n'était pas le cas, donc aucun
    risque d'action erronée à se tromper ici : au pire, ce filet ne
    s'applique pas et le message retombe sur la classification normale."""
    bare_text = _fix_bare_confirmation_typo(bare_text)
    if bare_text in _CONFIRM_EXACT_PHRASES:
        intent = "PRODUCER_CONFIRM_ORDER"
    elif bare_text in _REJECT_EXACT_PHRASES:
        intent = "PRODUCER_CANCEL_ORDER"
    else:
        return None
    if not phone:
        return None
    try:
        from ladini.graphs.agents.market_coach.services.mcp.gateway import (
            OrderTrackingGateway,
        )

        # UN seul appel sans filtre de statut (même source que la liste
        # "Vos ventes" affichée à l'utilisateur) : on distingue ainsi
        # "aucune vente" de "ventes déjà confirmées, rien en attente".
        result = await OrderTrackingGateway(mc_runtime).get_producer_orders(phone=phone)
    except Exception as exc:
        logger.warning(
            "[Interpreter] bare_confirmation_pending_producer_order: "
            "get_producer_orders a échoué (%s) — repli sur la classification normale",
            exc,
        )
        return None
    sales = (result or {}).get("data") or []
    pending = [
        o
        for o in sales
        if str(o.get("status") or "").upper() == "PENDING_PRODUCER_CONFIRMATION"
    ]
    if len(pending) > 1:
        return None
    if not pending:
        # Incident réel (2026-09-19) : la liste "Vos ventes" montrait une
        # vente déjà 🟢 (gagnée par enchère → CONFIRMED d'emblée) et l'invite
        # "Tapez *confirmer*" ; "confirmer" partait au LLM → "je ne comprends
        # pas". On route vers le résolveur, qui répond "aucune commande en
        # attente". Limité à "confirmer" (jamais "annuler", ambigu avec
        # l'annulation d'un achat) et aux utilisateurs ayant des ventes.
        if intent != "PRODUCER_CONFIRM_ORDER" or not sales:
            return None
    elif role != "PRODUCER" and intent != "PRODUCER_CONFIRM_ORDER":
        return None
    return {
        "interpreted_event": "NEW_TASK",
        "detected_intent": intent,
        "interpreter_confidence": 0.95,
        "extracted_entities": {},
        "raw_analysis": {"path": "bare_confirmation_pending_producer_order"},
    }


# Vocabulaire SPÉCIFIQUE au digest récurrent — jamais fusionné dans
# `_CONFIRM_EXACT_PHRASES`/`_REJECT_EXACT_PHRASES` (partagées avec le filet
# `CONFIRM_ACTION` générique et le prompt LLM, qui documente MOT POUR MOT ce
# même vocabulaire — voir leur commentaire de tête) : ces phrases-ci
# répondent au wording EXACT du digest (UX pilote "CONFIRMER TOUT / PAS
# DEMAIN"), hors de ce périmètre partagé.
_DIGEST_REJECT_EXTRA_PHRASES = frozenset({"pas cette fois", "pas demain", "rien demain"})
_DIGEST_MODIFY_PHRASES = frozenset({"modifier", "changer"})

# (2026-09-26, mandat digest §9) : "pas demain pour l'oignon" — variante NOMMÉE de "pas demain"
# (`_DIGEST_REJECT_EXTRA_PHRASES` ci-dessus), qui elle rejette TOUS les besoins actionnables du
# digest. Le libellé du produit n'est jamais deviné hors de ce texte : `_DIGEST_SKIP_PRODUCT_RE`
# ne fait qu'isoler le SUFFIXE après "pour" — la résolution réelle contre les besoins RÉELS de
# l'acheteur (un seul candidat, sinon abstention) reste à `_digest_skip_named_product`
# ci-dessous, même discipline BORNÉE que le reste de ce fast-path.
_DIGEST_SKIP_PRODUCT_RE = _re.compile(
    r"^pas\s+(?:demain|cette\s+fois)\s+pour\s+(?:l['’]|le\s+|la\s+|les\s+)?(.+)$"
)


async def _bare_confirmation_for_recurring_supply_digest(
    mc_runtime: Any, phone: str, bare_text: str
) -> Optional[Dict[str, Any]]:
    """Réponse au DIGEST quotidien d'approvisionnement récurrent
    (`RecurringSupplyDigestService`) — message PROACTIF (outbox WhatsApp),
    JAMAIS un tour de conversation : aucun `PendingInteraction` n'ancre la
    réponse, exactement le même problème que
    `_bare_confirmation_for_pending_producer_order` (voir son docstring),
    ici côté acheteur pour l'approvisionnement récurrent plutôt que le
    producteur pour ses ventes.

    Vérification déterministe et BORNÉE, même discipline : ne répond QUE si
    `bare_text` est un mot d'accord/refus/modification FERMÉ ET que
    l'acheteur a RÉELLEMENT au moins un besoin récurrent enregistré
    (`list_my_recurring_needs`) — sinon ce filet ne s'applique pas, le
    message retombe sur la classification normale (LLM, qui reconnaît
    aussi `RESPOND_RECURRING_SUPPLY_PROPOSAL` pour les paraphrases libres).
    « CONFIRMER TOUT » s'applique à TOUTES les propositions actionnables du
    moment (mandat digest : un digest agrège tous les besoins d'un
    acheteur en UN seul message, jamais un par besoin) — la résolution fine
    ("y a-t-il vraiment quelque chose à confirmer ?", CAS 5 : proposition
    déjà traitée/expirée) reste la responsabilité du flow
    (`flows/buyer/recurring_need.py::_respond_to_digest_flow`), jamais
    dupliquée ici.

    Budget de tokens du prompt `new_task_v2` déjà à saturation (spec §50,
    `tests/interpreter/test_new_task_micro.py::TestPromptSizeGuard`) : PAS de
    nouvel intent dédié ici — `action="CONFIRM_MATCH"`/`"REJECT_MATCH"` est
    émis sous l'intent `UPDATE_RECURRING_NEED`, DÉJÀ au catalogue (coût
    marginal nul), exactement l'esprit "réutilise l'existant". `_update_flow`
    (`flows/buyer/recurring_need.py`) reconnaît ces deux valeurs et bifurque
    vers `_respond_to_digest_flow` AVANT toute résolution par nom de produit
    (mandat : "CONFIRMER TOUT" s'applique à TOUS les besoins actionnables,
    jamais un seul résolu par ambiguïté de nom)."""
    bare_text = _fix_bare_confirmation_typo(bare_text)
    product_hint: Optional[str] = None
    _skip_named = _DIGEST_SKIP_PRODUCT_RE.match(bare_text)
    if _skip_named:
        # (mandat digest §9) : "pas demain pour l'oignon" — ne rejette QUE ce besoin nommé,
        # jamais tous les besoins actionnables (contrairement à "pas demain" bare ci-dessous).
        # Même sentinel `action` que CONFIRM_MATCH/REJECT_MATCH (pas de nouvel intent LLM) ;
        # `product_hint` porte le texte NU tel que dit, résolu contre les besoins RÉELS de
        # l'acheteur par `flows/buyer/recurring_need.py::_digest_skip_named_product` — jamais
        # deviné ici.
        action = "DIGEST_SKIP_PRODUCT"
        detected_intent = "UPDATE_RECURRING_NEED"
        product_hint = _skip_named.group(1).strip()
    elif bare_text in _DIGEST_MODIFY_PHRASES:
        # (2026-09-26, mandat digest §5) : AVANT, ceci émettait `GET_MY_NEEDS` (pas d'`action`,
        # donc pas de menu numéroté ni d'ancrage — un simple listing générique en lecture seule,
        # jamais de vraie modification). "modifier" ouvre désormais le mini-flow dédié
        # (`flows/buyer/recurring_need.py::_digest_modify_flow`) sous le MÊME intent
        # `UPDATE_RECURRING_NEED` (aucun nouveau littéral d'intent LLM, même contrainte de budget
        # de tokens documentée ci-dessus) — `action="DIGEST_MODIFY_MENU"` est un sentinel INTERNE
        # reconnu uniquement par `_update_flow`, jamais transmis à `update_recurring_need` (MCP).
        action = "DIGEST_MODIFY_MENU"
        detected_intent = "UPDATE_RECURRING_NEED"
    elif bare_text in _CONFIRM_EXACT_PHRASES:
        action = "CONFIRM_MATCH"
        detected_intent = "UPDATE_RECURRING_NEED"
    elif bare_text in _REJECT_EXACT_PHRASES or bare_text in _DIGEST_REJECT_EXTRA_PHRASES:
        action = "REJECT_MATCH"
        detected_intent = "UPDATE_RECURRING_NEED"
    else:
        return None
    if not phone:
        return None
    try:
        from ladini.graphs.agents.market_coach.services.mcp.gateway import (
            RecurringSupplyGateway,
        )

        result = await RecurringSupplyGateway(mc_runtime).list_my_recurring_needs(phone=phone)
    except Exception as exc:
        logger.warning(
            "[Interpreter] bare_confirmation_recurring_supply_digest: "
            "list_my_recurring_needs a échoué (%s) — repli sur la classification normale",
            exc,
        )
        return None
    if not (result or {}).get("items"):
        return None
    entities: Dict[str, Any] = {"action": action} if action else {}
    if product_hint:
        entities["product"] = product_hint
    return {
        "interpreted_event": "NEW_TASK",
        "detected_intent": detected_intent,
        "interpreter_confidence": 0.95,
        "extracted_entities": entities,
        "raw_analysis": {"path": "bare_confirmation_recurring_supply_digest"},
    }


# Vocabulaire SPÉCIFIQUE à la transition de livraison (VS5 pilote) — même
# discipline que le digest juste au-dessus : jamais fusionné dans les
# ensembles partagés `_CONFIRM_EXACT_PHRASES`/`_REJECT_EXACT_PHRASES`.
_DELIVERY_IN_TRANSIT_PHRASES = frozenset({"en route", "parti", "c'est parti", "expedie", "expédié"})
_DELIVERY_DELIVERED_PHRASES = frozenset({"livre", "livré", "livree", "livrée", "c'est livre", "c'est livré"})


# B11.1 — déclaration de clôture producteur ("commande #REF livrée"). Vocabulaire FERMÉ et borné :
# jamais « toute phrase avec "livrée" » (« quand sera livrée ma commande ? », « ma commande a-t-elle
# été livrée ? » appartiennent à d'autres intents).
_DELIVERY_DECLARATION_RE = _re.compile(
    r"\b(livr[ée]e?s?|j['’ ]ai livr[ée]|remis[e]?|terminee?|terminée?|cloturee?|clôturée?)\b", _re.I
)
_DELIVERY_NEGATION_OR_QUESTION_RE = _re.compile(
    r"\?|\b(pas|non|jamais|quand|comment|pourquoi|est-ce|a-t-elle|a-t-il|combien|ou est|où est)\b", _re.I
)
# Sans référence : phrases entières (déclaratives) uniquement.
_DELIVERY_NO_REF_PHRASES = frozenset(
    {
        "commande livrée", "commande livree", "j'ai livré", "j'ai livre", "j’ai livré", "j’ai livre",
        "j'ai livré la commande", "j'ai livre la commande",  # « livré » nu : filet récurrent 1.7
        "le client a reçu la commande", "le client a recu la commande", "le client a reçu", "le client a recu",
        "commande terminée", "commande terminee",
    }
)


async def _producer_delivery_completion_signal(
    mc_runtime: Any, phone: str, role_up: str, text: str
) -> Optional[Dict[str, Any]]:
    """B11.1 — « commande #11DE2D1B livrée » (et variantes) -> `PRODUCER_CONFIRM_DELIVERY_PAYMENT`,
    SANS LLM. Bug prod : le classifieur renvoyait UNKNOWN (`NewTaskEntities` interdit tout identifiant
    technique, le prompt est saturé) -> « Je n'ai pas bien compris ».

    Signal fort exigé : (a) déclaration de livraison au vocabulaire fermé, ni question ni négation,
    (b) soit une RÉFÉRENCE de commande dans le message (extraction B11 réutilisée, jamais un second
    parser), soit une phrase entière sans référence en rôle PRODUCER. Une référence en rôle BUYER
    (compte double-rôle) n'est routée que si elle désigne réellement une vente de ce producteur.
    L'appartenance et l'éligibilité sont revérifiées ensuite par le résolveur
    (`flows/producer/flow.py::_resolve_order_for_delivery_payment`) AVANT toute mutation."""
    from ladini.graphs.agents.market_coach.flows.producer.flow import (
        _extract_reference_from_text,
        _order_matches_reference,
    )

    raw = (text or "").strip()
    if not raw or role_up not in ("PRODUCER", "BUYER") or not phone:
        return None
    low = raw.lower()
    if _DELIVERY_NEGATION_OR_QUESTION_RE.search(low):
        return None
    ref = _extract_reference_from_text(raw)
    source = "explicit_reference"
    eligible = -1
    if ref:
        if not _DELIVERY_DECLARATION_RE.search(low):
            return None
        if role_up == "BUYER":
            try:
                from ladini.graphs.agents.market_coach.services.mcp.gateway import (
                    OrderTrackingGateway,
                )

                res = await OrderTrackingGateway(mc_runtime).get_producer_orders(phone=phone)
            except Exception:
                return None
            if not any(_order_matches_reference(o, ref) for o in ((res or {}).get("data") or [])):
                return None
    else:
        if role_up != "PRODUCER" or low.strip(" .!,;:") not in _DELIVERY_NO_REF_PHRASES:
            return None
        source = "active_orders_context"
    logger.info(
        "PRODUCER_DELIVERY_INTENT_ROUTED | source=%s | order_ref_present=%s | eligible_order_count=%s | "
        "goal_after=PRODUCER_CONFIRM_DELIVERY_PAYMENT",
        source, bool(ref), eligible,
    )
    return {
        "interpreted_event": "NEW_TASK",
        "detected_intent": "PRODUCER_CONFIRM_DELIVERY_PAYMENT",
        "interpreter_confidence": 0.95,
        "extracted_entities": {},
        "raw_analysis": {"path": "producer_delivery_declaration"},
    }


async def _bare_confirmation_for_delivery_transition(
    mc_runtime: Any, phone: str, bare_text: str
) -> Optional[Dict[str, Any]]:
    """PRODUCTEUR — "en route"/"livré" sur une commande `recurring_supply`
    (VS5 pilote). Même discipline BORNÉE que les fast-paths voisins :
    vocabulaire fermé ET exactement UNE commande dans l'état de départ
    attendu (`get_producer_orders`, filtré ici même — même source que
    `_bare_confirmation_for_pending_producer_order`) — sinon repli sur la
    classification normale (jamais un choix deviné parmi plusieurs
    commandes). Émet `PRODUCER_CONFIRM_ORDER` (déjà au catalogue, coût
    marginal nul — budget du prompt `new_task_v2` déjà saturé, spec §50)
    avec `action="MARK_IN_TRANSIT"|"MARK_DELIVERED"` ; `producer_context_
    resolver` (flows/producer/flow.py) bifurque vers `_resolve_delivery_
    transition` AVANT le tunnel générique de confirmation de vente. Le
    paramètre `bare_text` est déjà normalisé par l'appelant (même
    expression que `_bare_confirm_text`, bloc 1.5) — pas de re-strip ici."""
    if bare_text in _DELIVERY_IN_TRANSIT_PHRASES:
        action, expected_from = "MARK_IN_TRANSIT", "PENDING"
    elif bare_text in _DELIVERY_DELIVERED_PHRASES:
        action, expected_from = "MARK_DELIVERED", "IN_TRANSIT"
    else:
        return None
    if not phone:
        return None
    try:
        from ladini.graphs.agents.market_coach.services.mcp.gateway import (
            OrderTrackingGateway,
        )

        result = await OrderTrackingGateway(mc_runtime).get_producer_orders(phone=phone)
    except Exception as exc:
        logger.warning(
            "[Interpreter] bare_confirmation_delivery_transition: "
            "get_producer_orders a échoué (%s) — repli sur la classification normale",
            exc,
        )
        return None
    orders = (result or {}).get("data") or []
    candidates = [
        o
        for o in orders
        if str(o.get("order_type") or "").upper() == "RECURRING_SUPPLY"
        and str(o.get("delivery_status") or "PENDING").upper() == expected_from
    ]
    if len(candidates) != 1:
        return None
    return {
        "interpreted_event": "NEW_TASK",
        "detected_intent": "PRODUCER_CONFIRM_ORDER",
        "interpreter_confidence": 0.95,
        "extracted_entities": {"action": action},
        "raw_analysis": {"path": "bare_confirmation_delivery_transition"},
    }


# Vocabulaire SPÉCIFIQUE à la réception (VS5 pilote) — idem, jamais fusionné
# dans les ensembles partagés.
_RECEPTION_OK_PHRASES = frozenset({"tout est bon", "tout va bien", "c'est bon", "cest bon", "rien a signaler"})
_RECEPTION_ISSUE_PHRASES = frozenset({"il y a un probleme", "il y a un problème", "probleme", "problème"})


async def _bare_confirmation_for_order_reception(
    mc_runtime: Any, phone: str, bare_text: str
) -> Optional[Dict[str, Any]]:
    """ACHETEUR — "tout est bon"/"il y a un problème" en réponse au message
    proactif de réception (VS5 pilote, `RECURRING_SUPPLY_ORDER_DELIVERED_
    BUYER`, aucun `PendingInteraction`). Même discipline BORNÉE : vocabulaire
    fermé ET exactement une commande `DELIVERED` pour cet acheteur
    (`RecurringSupplyGateway.list_my_deliverable_orders`, dédiée à ce besoin
    — `get_buyer_orders_dashboard` ne renvoie qu'un menu texte prérendu,
    sans champs structurés `order_type`/`delivery_status`) — sinon repli sur
    la classification normale. Émet `BUYER_CHECK_ORDER_STATUS` (déjà au
    catalogue, coût marginal nul) avec `action="RECEIVED_OK"|
    "RECEIVED_ISSUE"` ; `order_tracking_resolver` bifurque vers
    `_record_reception_flow` (flows/buyer/order_tracking.py). Le détail de
    l'incident (type précis, quantité) est demandé ENSUITE, via un menu
    numéroté classique (`PendingInteraction` réel, cette fois) — ce
    fast-path ne fait que déclencher le premier pas, jamais deviner un
    type d'incident. Le paramètre `bare_text` est déjà normalisé par
    l'appelant (même expression que `_bare_confirm_text`, bloc 1.5) — pas
    de re-strip ici."""
    if bare_text in _RECEPTION_OK_PHRASES:
        action = "RECEIVED_OK"
    elif bare_text in _RECEPTION_ISSUE_PHRASES:
        action = "RECEIVED_ISSUE"
    else:
        return None
    if not phone:
        return None
    try:
        from ladini.graphs.agents.market_coach.services.mcp.gateway import (
            RecurringSupplyGateway,
        )

        result = await RecurringSupplyGateway(mc_runtime).list_my_deliverable_orders(
            phone=phone, delivery_status="DELIVERED"
        )
    except Exception as exc:
        logger.warning(
            "[Interpreter] bare_confirmation_order_reception: "
            "list_my_deliverable_orders a échoué (%s) — repli sur la classification normale",
            exc,
        )
        return None
    if len((result or {}).get("items") or []) != 1:
        return None
    return {
        "interpreted_event": "NEW_TASK",
        "detected_intent": "BUYER_CHECK_ORDER_STATUS",
        "interpreter_confidence": 0.95,
        "extracted_entities": {"action": action},
        "raw_analysis": {"path": "bare_confirmation_order_reception"},
    }


# Entités posées par le correctif « stock conditionné » (hotfix 2026-10-03) : tout ce que le LLM a pu mettre
# pour la QUANTITÉ / le PRIX d'un message de publication conditionné est REMPLACÉ, jamais fusionné.
_PACKAGED_STOCK_OVERRIDE_KEYS = (
    "quantity", "unit", "price", "price_unit", "pricing_tiers", "package_count", "package_label",
    "package_size", "package_unit", "package_groups", "package_type", "package_content_amount",
    "package_content_unit", "price_basis",
)


def _apply_packaged_stock_entities(raw: Dict[str, Any], state: Dict[str, Any], text: str) -> Dict[str, Any]:
    """Publication d'un produit CONDITIONNÉ (« 100 sachets de lait de 500 ml », « 50 bidons de 500 ml et 100
    bidons de 330 ml ») : la structure est DÉTERMINISTE, elle ne doit pas dépendre de l'arithmétique du LLM
    (250 litres au lieu de 58) ni de l'unité lue dans la phrase (« 500ml » pris pour l'unité de la quantité).

    Remplace `quantity`/`unit`/`price*`/`pricing_tiers`/`package_*` par la lecture déterministe ; ne touche à
    rien d'autre (produit, description…). Périmètre : SALES_PUBLISH_PRODUCT uniquement."""
    if not isinstance(raw, dict) or state.get("is_onboarding"):
        return raw
    intent = str(raw.get("detected_intent") or "").upper()
    event = str(raw.get("interpreted_event") or "").upper()
    ambiguous_publish = "SALES_PUBLISH_PRODUCT" in (raw.get("candidate_goals") or [])
    if not ambiguous_publish and (
        intent != "SALES_PUBLISH_PRODUCT" or event not in ("NEW_TASK", "ANSWER", "UPDATE")
    ):
        return raw
    stock = parse_packaged_stock_message(text)
    if stock is None:
        # Nouvelle quantité MASS/VOLUME explicite (« je veux vendre 50 litres de lait ») alors qu'un stock
        # conditionné était déjà décrit : l'ancienne structure (compte × contenance) ne survit JAMAIS à une
        # nouvelle description du stock (hotfix 2026-10-03, « annuler → nouvelle publication »). Jamais pendant
        # une réponse de prix (« 500f pour 500 ml » : 500 ml y est une TAILLE, pas un stock).
        payload_now = state.get("transaction_payload") or {}
        entities_now = dict(raw.get("extracted_entities") or {})
        if (
            event in ("NEW_TASK", "UPDATE")
            and (payload_now.get("package_count") or payload_now.get("package_groups"))
            and entities_now.get("quantity") is not None
            and entities_now.get("price") in (None, "")
            and not _re.search(r"(fcfa|cfa|francs?)", str(text).lower())
        ):
            reset = {k: None for k in ("package_count", "package_label", "package_size", "package_unit", "package_groups")}
            patched_reset = dict(raw)
            patched_reset["extracted_entities"] = {**entities_now, **reset}
            return patched_reset
        return raw
    entities: Dict[str, Any] = {
        k: v for k, v in dict(raw.get("extracted_entities") or {}).items() if k not in _PACKAGED_STOCK_OVERRIDE_KEYS
    }
    first = stock.groups[0]
    entities["quantity"] = stock.total_base_quantity
    entities["unit"] = stock.base_unit
    if not entities.get("product") and first.product_hint:
        entities["product"] = first.product_hint
    if len(stock.groups) == 1:
        entities.update(
            {
                "package_count": first.count,
                "package_label": first.label,
                "package_size": first.size,
                "package_unit": first.unit,
            }
        )
        if first.price is not None:
            entities["price"] = first.price
            entities["price_unit"] = first.label.lower()
    else:
        entities["package_groups"] = [
            {"count": g.count, "label": g.label, "size": g.size, "unit": g.unit,
             "size_literal": g.size_literal, "unit_literal": g.unit_literal, "price": g.price}
            for g in stock.groups
        ]
        if stock.all_priced:
            entities["pricing_tiers"] = [
                {"quantity": g.size_literal, "unit": short_content_unit(g.unit_literal), "price": g.price,
                 "packaging": g.label.lower(), "count": g.count}
                for g in stock.groups
            ]
    logger.info(
        "PRODUCER_PACKAGING_PARSED groups=%d package_count=%s package_size=%s package_unit=%s packaging_type=%s "
        "stock_quantity_base=%s base_unit=%s priced=%s source=deterministic_text",
        len(stock.groups), [g.count for g in stock.groups], [g.size for g in stock.groups],
        [g.unit_literal for g in stock.groups], [g.label for g in stock.groups], stock.total_base_quantity,
        stock.base_unit, stock.any_priced,
    )
    logger.info(
        "PRODUCER_PACKAGING_STOCK_NORMALIZED stock_quantity_base=%s base_unit=%s groups=%d tiers=%d "
        "pricing_basis=%s",
        stock.total_base_quantity, stock.base_unit, len(stock.groups), len(entities.get("pricing_tiers") or []),
        "PER_PACKAGE" if stock.any_priced else "UNPRICED",
    )
    patched = dict(raw)
    patched["extracted_entities"] = entities
    ra = dict(patched.get("raw_analysis") or {})
    ra["packaged_stock_override"] = True
    patched["raw_analysis"] = ra
    return patched


async def _fetch_interactive_outbound(mc_runtime: Any, state: Dict[str, Any], text: str):
    """Dernier message sortant interactif (lecture DB), seulement quand il peut changer l'attribution de CE message :
    contexte d'état présent, ou réponse courte/chiffre/alias de menu. Toute panne => `None` (jamais bloquant)."""
    from ladini.graphs.agents.market_coach.interpreter import context_arbitration as ca

    norm = ca.fold(text)
    could_matter = (
        get_pending_interaction(state).kind != InteractionKind.NONE
        or ca._has_ghost_menu(state)
        or norm.isdigit()
        or any(norm in aliases for aliases in ca.MENU_ACTION_ALIASES.values())
    )
    phone = str(state.get("user_phone") or "")
    if not could_matter or not phone:
        return None
    try:
        from ladini.graphs.agents.market_coach.services.mcp.gateway import (
            ModerationGateway,
        )

        return ca.InteractiveOutbound.from_tool_result(
            await ModerationGateway(mc_runtime).get_last_interactive_outbound(phone)
        )
    except Exception as exc:  # pragma: no cover - la lecture sortante ne casse jamais un tour
        logger.warning("get_last_interactive_outbound a échoué (%s) — arbitrage sans contexte sortant", type(exc).__name__)
        return None


async def _buyer_capability(mc_runtime: Any, state: Dict[str, Any]) -> bool:
    """B21.1 — l'acteur possède-t-il la capacité acheteur (`permissions.can_buy` : profil acheteur rattaché, ou
    administrateur — même modèle que `services/database/base.py::_serialize_user_entities`) ? Fail-closed : toute
    erreur, profil absent ou réponse inattendue => `False` (le contrat existant s'applique)."""
    phone = str(state.get("user_phone") or "")
    if not phone:
        return False
    try:
        from ladini.graphs.agents.market_coach.services.mcp.gateway import (
            ProfileGateway,
        )

        res = await ProfileGateway(mc_runtime).get_user_by_phone(phone)
        data = res.get("data") if isinstance(res, dict) and str(res.get("status", "")).upper() == "SUCCESS" else None
        perms = data.get("permissions") if isinstance(data, dict) else None
        return bool(isinstance(perms, dict) and perms.get("can_buy") is True)
    except Exception as exc:  # pragma: no cover - la lecture de capacité ne casse jamais un tour
        logger.warning("capacité acheteur illisible (%s) — navigation recurring non élargie", type(exc).__name__)
        return False


def _screen_context_hint(state: Dict[str, Any]) -> Optional[str]:
    """B24 — description (générée par l'application, jamais le texte utilisateur) de l'écran récurrent vivant, donnée au
    micro-prompt NEW_TASK pour qu'il juge la RELATION du message avec l'écran (corriger la cible affichée, ou nouvelle tâche)."""
    from ladini.graphs.agents.market_coach.interpreter import context_arbitration as ca

    view = ca.live_menu_view(state)
    if view is None:
        return None
    parts = [view["title"]] if view["title"] else []
    target = ca.live_menu_target(state)
    if target is not None:
        parts.append(
            f"cible affichée : besoin récurrent « {target.get('product') or '?'} » "
            f"({target.get('quantity')} {target.get('unit') or ''}, {target.get('frequency') or 'fréquence inconnue'})"
        )
    parts.append("options affichées : " + " ; ".join(view["labels"]))
    return " — ".join(parts)


def _annotate_interpretation(state: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """B24 — UNIQUE point d'annotation/journalisation de l'arbitrage sémantique : ajoute `relation_to_context` et (pour une
    intention qui vise l'écran affiché) la cible contextuelle `context_target`, puis émet `INTENT_ARBITRATION` (sans PII)."""
    from ladini.graphs.agents.market_coach.interpreter import context_arbitration as ca

    relation = ca.derive_relation(state, patch)
    intent = str(patch.get("detected_intent") or "UNKNOWN").upper()
    target = ca.live_menu_target(state) if intent in ca.CONTEXT_TARGETED_INTENTS else None
    out = {**patch, "relation_to_context": relation.value}
    if target is not None:
        out["context_target"] = target
    analysis = patch.get("raw_analysis") or {}
    path = str(analysis.get("path") or "")
    if ca.live_menu_view(state) is None and not path.startswith("context_arbitration"):
        return out
    reason = analysis.get("decision_reason") or analysis.get("guard")
    if path == "context_arbitration_recurring_menu":
        reason = "closed_menu_answer"
    elif path.startswith("context_arbitration_navigation"):
        reason = "explicit_navigation"
    elif not reason or reason == "expectation_superseded":
        if relation == ca.RelationToContext.CORRECTION:
            reason = "contextual_correction"
        elif relation == ca.RelationToContext.ANSWER and target is not None:
            reason = "contextual_command"
        elif relation == ca.RelationToContext.NEW_TASK:
            reason = "explicit_new_task"
        else:
            reason = reason or str(patch.get("interpreted_event") or "unknown").lower()
    ca.log_intent_arbitration(
        state,
        semantic_intent=intent,
        relation=relation,
        route=path or "interpreter",
        reason=str(reason),
        target_type=str(target["type"]) if target is not None else None,
        target_resolution="context" if target is not None else None,
    )
    return out


async def _arbitrate_context(
    state: Dict[str, Any], mc_runtime: Any, text: str, role_up: str, impl: Any
) -> Optional[Dict[str, Any]]:
    """Applique `resolve_conversation_context` ; retourne le patch d'état, ou `None` pour poursuivre le pipeline existant."""
    from ladini.graphs.agents.market_coach.interpreter import context_arbitration as ca

    if state.get("is_onboarding") or not str(text or "").strip() or role_up not in {"BUYER", "PRODUCER"}:
        return None
    outbound = await _fetch_interactive_outbound(mc_runtime, state, text)
    norm = ca.fold(text)
    # Lecture de capacité UNIQUEMENT pour une navigation « mes besoins » hors graphe BUYER (vocabulaire fermé : coût nul
    # pour tout autre message).
    buyer_capable = role_up != "BUYER" and ca.is_recurring_navigation(norm) and await _buyer_capability(mc_runtime, state)
    decision = ca.resolve_conversation_context(state, text, role=role_up, outbound=outbound, buyer_capable=buyer_capable)
    if ca.is_recurring_navigation(norm):  # aucune PII : ni texte, ni téléphone, ni identifiant
        logger.info(
            "RECURRING_NAVIGATION_RESOLVED actor_role=%s buyer_capability=%s route=%s reason=%s",
            role_up, buyer_capable or role_up == "BUYER", (decision.raw or {}).get("detected_intent") or "NONE", decision.reason,
        )
    if decision.kind in (ca.ArbitrationKind.ACTIVE_SLOT, ca.ArbitrationKind.GENERIC_CLASSIFICATION):
        return None
    ca.log_decision(decision, outbound=outbound)
    purge = ca.stale_context_purge_patch(state) if decision.purge else {}
    if decision.raw is not None:
        tagged = {**decision.raw, "raw_analysis": {**(decision.raw.get("raw_analysis") or {}), "decision_reason": decision.reason}}
        patch = InterpreterResult.from_legacy_dict(tagged).to_state_patch()
        return {**patch, **purge}
    # Reclassification libre SANS l'ancien contexte (même idiome que BUYER_PRODUCT_SWITCH ci-dessous).
    neutral = ca.neutral_state_view(state)
    raw = await impl(neutral, mc_runtime)
    raw = _apply_packaged_stock_entities(raw, neutral, text)
    raw = {**raw, "raw_analysis": {**(raw.get("raw_analysis") or {}), "decision_reason": decision.reason}}
    return {**InterpreterResult.from_legacy_dict(raw).to_state_patch(), **purge}



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

        # 0.4 DÉCISION DE RUPTURE DE STOCK (incident 2026-09-28) : le système vient
        # de demander lui-même « répondez N pour prendre directement le stock
        # disponible ». Cette réponse-là est déterministe — aucun LLM : un nombre nu
        # devient l'action `TAKE_AVAILABLE` (quantité d'achat = N), jamais une
        # nouvelle quantité générique, une sélection de menu ou une nouvelle tâche.
        # Générique (voir `domain/stock_shortage.py::resolve_quantity_reply`).
        _shortage = None if onboarding_active else shortage_awaiting_reply(state)
        if _shortage is not None:
            _reply = resolve_quantity_reply(_shortage, text)
            if _reply is not None:
                logger.info(
                    "[Interpreter StockShortage] fast-path %s quantity=%s",
                    _reply["branch"].value,
                    _reply["purchase_quantity"],
                )
                return {
                    "interpreted_event": "ANSWER",
                    "detected_intent": str(locked_goal or "BUYER_REQUEST").upper(),
                    "interpreter_confidence": 0.98,
                    "extracted_entities": {
                        "shortage_decision": _reply["branch"].value,
                        "shortage_quantity": _reply["purchase_quantity"],
                    },
                    "raw_analysis": {"path": "stock_shortage_fast_path"},
                }

        # 0.45 RÉPONSE À UNE QUESTION COMMERCIALE (Phase B1, 2026-09-28) : « 0,5 litre » répondant
        # à « quelle quantité contient un sachet ? » est le CONTENU du conditionnement, jamais une
        # nouvelle quantité à vendre ; « par tonne » répondant à « 500 000 FCFA par tonne ou pour
        # l'ensemble ? » fixe la base du prix. Déterministe (aucun LLM, aucune extraction
        # d'entité) : le `validator` relit le TEXTE dans le contexte de la question posée
        # (domain/commercial_offer_flow.py). Un message qui ne répond pas à la question
        # (« annuler », une autre demande) retombe sur le classifieur normal.
        _commercial_q = None if onboarding_active else commercial_question_from_state(state)
        if _commercial_q is not None and (
            (
                _commercial_q.requested_field == FIELD_PACKAGE_SIZE
                and parse_package_content(text, question=_commercial_q) is not None
            )
            or (
                _commercial_q.requested_field == FIELD_PRICE_BASIS
                and parse_basis_reply(text, commercial_unit=_commercial_q.expected_basis_unit)
                is not None
            )
        ):
            logger.info(
                "[Interpreter CommercialQuestion] fast-path reply to %s",
                _commercial_q.requested_field,
            )
            return {
                "interpreted_event": "ANSWER",
                "detected_intent": str(locked_goal or "UNKNOWN").upper(),
                "interpreter_confidence": 0.98,
                "extracted_entities": {},
                "raw_analysis": {"path": "commercial_question_fast_path"},
            }

        # 0.455 RÉPONSE AU PRIX D'UN CONDITIONNEMENT (hotfix 2026-10-03) : « 500f pour 500 ml » après « Quel est le
        # prix d'un sachet de 500 ml ? » est le prix PAR SACHET — jamais « 500 FCFA par millilitre ». Lu
        # DÉTERMINISTEMENT dans le contexte du stock conditionné déclaré (aucun LLM).
        if (
            _commercial_q is not None
            and _commercial_q.requested_field == "price"
            and _commercial_q.expected_basis == "PER_PACKAGE"
        ):
            _pkg_reply = parse_package_price_reply(text, state.get("transaction_payload") or {})
            if _pkg_reply is not None:
                logger.info(
                    "PRODUCER_PRICING_BASIS_RESOLVED basis=PER_PACKAGE source=question_context tiers=%d",
                    len(_pkg_reply.get("pricing_tiers") or []),
                )
                return {
                    "interpreted_event": "ANSWER",
                    "detected_intent": str(locked_goal or "UNKNOWN").upper(),
                    "interpreter_confidence": 0.98,
                    "extracted_entities": _pkg_reply,
                    "raw_analysis": {"path": "fast_path_package_price_reply"},
                }

        # 0.46 RÉPONSE À UNE QUESTION DE PRIX DE BID (Phase B2b, 2026-09-28) : « 450000 » après « quel prix
        # par tonne ? », « par tonne » après « par tonne ou pour l'ensemble ? ». Lue déterministiquement dans
        # le contexte de la question (domain/bid_pricing_flow.py) — un prix ne dépend pas d'un classifieur LLM.
        if not onboarding_active and bid_price_reply_expected(state, text):
            logger.info("[Interpreter BidPrice] fast-path reply to a bid price question")
            return {
                "interpreted_event": "ANSWER",
                "detected_intent": str(locked_goal or "UNKNOWN").upper(),
                "interpreter_confidence": 0.98,
                "extracted_entities": {},
                "raw_analysis": {"path": "bid_price_fast_path"},
            }

        # 0.47 PROTOCOLE NUMÉRIQUE BUYER (B7, 2026-10-02) : pendant `ENTER_QUANTITY`, un nombre nu
        # est TOUJOURS une quantité, et changer de producteur exige une commande explicite
        # (« producteur 3 », « changer producteur »). Déterministe : ne dépend d'aucun LLM ni d'un
        # ancien menu encore présent dans l'état. Voir `interpreter/numeric_protocol.py`.
        if not onboarding_active:
            _numeric = interpret_buyer_numeric_protocol(
                state, text, llm_available=getattr(mc_runtime, "llm", None) is not None
            )
            if _numeric is not None:
                return _numeric

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

        # 1.5 CONFIRMATION D'UNE VENTE REÇUE, SANS TUNNEL ACTIF (2026-09-15)
        # — voir le docstring de `_bare_confirmation_for_pending_producer_
        # order` pour le contexte complet. Court-circuite la classification
        # LLM (jamais garantie sur un mot nu) par une vérification
        # déterministe et bornée : ne s'applique QUE si aucun tunnel n'est
        # déjà actif (sinon `_interpret_fast_path` ci-dessus a déjà traité
        # le cas, en échoant le goal verrouillé) et que le rôle est
        # PRODUCER.
        _bare_confirm_text = text.strip().lower().strip(" .!?,;: ")

        # 1.4 PANIER PRÊT À VALIDER (B9, 2026-10-02) : le contexte du PANIER gagne sur tout filet
        # « mot nu » ci-dessous (vente producteur, digest récurrent, réception) — sinon un « okay »
        # prononcé après « Répondez *précommander* pour valider » confirmait un AUTRE flux (digest :
        # `accept_match_proposal` + « ✅ C'est confirmé, vos commandes… » alors que le panier n'était
        # pas touché). Déterministe (aucun LLM) et UN seul chemin : `BUYER_PREORDER_INIT`, exactement
        # celui de « précommander » (récap -> confirmation -> exécution certifiée).
        _cart_ready = _cart_ready_for_preorder(state)
        _cart_alias = None if onboarding_active else _cart_ready_confirmation_alias(state, text)
        if _cart_alias is not None:
            _pending_before = get_pending_interaction(state)
            _sel_before = build_selection_context(state)
            _stale_slot = bool(locked_goal) or _pending_before.kind != InteractionKind.NONE
            logger.info(
                "BUYER_PREORDER_CONFIRMATION_REQUESTED cart_item_count=%d source=%s "
                "current_goal=%s preorder_phase=%s",
                len(state.get("active_cart") or []),
                _cart_alias,
                resolve_current_goal(state),
                (state.get("preorder_workflow") or {}).get("phase"),
            )
            logger.info(
                "BUYER_CART_READY_CONFIRMATION_ROUTED source=%s cart_item_count=%d cart_ready=True "
                "current_goal_before=%s pending_before=%s expected_action_before=%s "
                "stale_slot_ignored=%s goal_after=BUYER_PREORDER_INIT",
                _cart_alias,
                len(state.get("active_cart") or []),
                resolve_current_goal(state),
                _pending_before.kind.value,
                _sel_before.expected_action.value if _sel_before and _sel_before.expected_action else None,
                _stale_slot,
            )
            _ready_result: Dict[str, Any] = {
                "interpreted_event": "NEW_TASK",
                "detected_intent": "BUYER_PREORDER_INIT",
                "interpreter_confidence": 0.99,
                "extracted_entities": {},
                "raw_analysis": {"path": "cart_ready_confirmation", "source": _cart_alias},
            }
            if _stale_slot:
                # Reliquat de l'article DÉJÀ ajouté (but BUYER_ADD_TO_CART / pending quantité /
                # `missing_fields`) : purgé ICI pour que ce tour suive exactement le chemin d'un panier
                # propre — sinon `cognitive_guard` le lirait comme une « interruption » du but périmé.
                _ready_result.update(
                    {
                        "current_goal": None,
                        "pending_interaction": None,
                        "missing_fields": [],
                        "last_missing_field": None,
                        "status": "PLANNING",
                    }
                )
            return _ready_result

        if (
            not locked_goal
            and not _cart_ready
            and role_up in ("PRODUCER", "BUYER")
            and not onboarding_active
        ):
            _pending_check = await _bare_confirmation_for_pending_producer_order(
                mc_runtime,
                str(state.get("user_phone") or ""),
                _bare_confirm_text,
                role_up,
            )
            if _pending_check is not None:
                logger.info(
                    "[Interpreter] Confirmation nue résolue via vente unique "
                    "en attente -> %s",
                    _pending_check["detected_intent"],
                )
                return _pending_check

        # 1.6 RÉPONSE AU DIGEST D'APPROVISIONNEMENT RÉCURRENT, SANS TUNNEL
        # ACTIF (mandat digest, VS4 pilote ; élargi 2026-09-26, mandat digest
        # §1/§4 — incident réel racine n°1) — même garde structurelle qu'en
        # 1.5 (aucun tunnel verrouillé) : ANCIENNEMENT restreinte à
        # `role_up == "BUYER"`, ce qui supposait que le rôle COMPILÉ du
        # graphe (`role_up`, figé à la construction — voir
        # `core/graph_builder.py::build_graph`/`orchestrator.py::_run_market`,
        # résolu depuis `Workspace.workspace_type`, lui-même STICKY d'un tour
        # à l'autre) refléterait fidèlement "cette personne est en train de
        # répondre à SON digest acheteur" — faux pour un compte double-rôle
        # (producteur ET acheteur, ex: un producteur qui a aussi des besoins
        # récurrents) dont le workspace reste par défaut "producer" tant
        # qu'aucun tour BUYER n'a eu lieu récemment : `role_up` valait alors
        # "PRODUCER" pour la réponse au digest, ce bloc entier était sauté,
        # et "modifier" retombait sur la classification générique ->
        # `clarification_node` -> repli LLM générique (incident reproduit
        # tel quel, voir tests/integration/test_recurring_supply_digest_routing.py
        # ::TestRootCauseRoleMismatch). Élargi à `role_up in ("PRODUCER",
        # "BUYER")` (même périmètre que 1.5, même garantie d'innocuité : la
        # fonction ci-dessous s'abstient déjà explicitement — `return None`
        # — si `list_my_recurring_needs` ne renvoie aucun besoin réel pour ce
        # numéro, donc un producteur SANS besoin récurrent n'est jamais
        # affecté). Voir le docstring de
        # `_bare_confirmation_for_recurring_supply_digest` pour le contexte
        # complet.
        if (
            not locked_goal
            and not _cart_ready
            and role_up in ("PRODUCER", "BUYER")
            and not onboarding_active
        ):
            # `_bare_confirm_text` est calculé plus haut, inconditionnellement.
            _digest_check = await _bare_confirmation_for_recurring_supply_digest(
                mc_runtime, str(state.get("user_phone") or ""), _bare_confirm_text
            )
            if _digest_check is not None:
                # Log structuré (mandat digest §14) — jamais de secret/PII brute (le
                # numéro n'est pas loggé ici, seul le rôle compilé et l'action résolue).
                logger.info(
                    "recurring_supply_digest.reply_routed | pending_type=%s | "
                    "pending_state=DIGEST_AWAIT_ACTION | role=%s | user_reply=%s | "
                    "resolved_action=%s",
                    InteractionKind.RECURRING_SUPPLY_DIGEST_ACTION.value,
                    role_up,
                    _bare_confirm_text,
                    _digest_check.get("extracted_entities", {}).get("action")
                    or _digest_check.get("detected_intent"),
                )
                return _digest_check

        # 1.65 DÉCLARATION DE CLÔTURE PRODUCTEUR ("commande #REF livrée"), SANS TUNNEL ACTIF (B11.1) —
        # avant le filet « livré » récurrent (1.7, mot nu, `RECURRING_SUPPLY` seulement) et avant tout
        # repli générique. Voir `_producer_delivery_completion_signal`.
        if not locked_goal and not _cart_ready and not onboarding_active:
            _completion = await _producer_delivery_completion_signal(
                mc_runtime, str(state.get("user_phone") or ""), role_up, text
            )
            if _completion is not None:
                return _completion

        # 1.7 TRANSITION DE LIVRAISON ("en route"/"livré"), SANS TUNNEL ACTIF
        # (VS5 pilote) — rôle PRODUCER uniquement. Voir le docstring de
        # `_bare_confirmation_for_delivery_transition`.
        if not locked_goal and role_up == "PRODUCER" and not onboarding_active:
            _delivery_check = await _bare_confirmation_for_delivery_transition(
                mc_runtime, str(state.get("user_phone") or ""), _bare_confirm_text
            )
            if _delivery_check is not None:
                logger.info(
                    "[Interpreter] Transition de livraison résolue -> action=%s",
                    _delivery_check["extracted_entities"].get("action"),
                )
                return _delivery_check

        # 1.8 RÉCEPTION ("tout est bon"/"il y a un problème"), SANS TUNNEL
        # ACTIF (VS5 pilote) — rôle BUYER uniquement. Voir le docstring de
        # `_bare_confirmation_for_order_reception`.
        if not locked_goal and not _cart_ready and role_up == "BUYER" and not onboarding_active:
            _reception_check = await _bare_confirmation_for_order_reception(
                mc_runtime, str(state.get("user_phone") or ""), _bare_confirm_text
            )
            if _reception_check is not None:
                logger.info(
                    "[Interpreter] Réception résolue -> action=%s",
                    _reception_check["extracted_entities"].get("action"),
                )
                return _reception_check

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

        # 2.5 MICRO-PROMPTS SELECTION (Incrément B) / ACTIVE_SLOT (Phase C,
        # 2026-09-12) : ces deux routes sont réellement migrées vers des
        # micro-prompts dédiés — NEW_TASK/STRUCTURED_ACTION continuent SANS
        # AUCUN changement sur le chemin ci-dessous (l'ancien interpréteur
        # unifié). `_legacy_fallback` : partagé entre les deux branches
        # (mutuellement exclusives — une seule route est choisie par tour),
        # tracé explicitement en télémétrie (spec §26) — jamais un repli
        # silencieux.
        _legacy_fallback = False
        # (2026-09-13, Incrément F) : mis à True dans les 3 branches
        # DEVIATION ci-dessous — signale que le message ORIGINAL doit être
        # reclassifié par NEW_TASK (spec §27), jamais par la route qui a
        # détecté la déviation elle-même. Séparé de `_route ==
        # InterpretationRoute.NEW_TASK` (le cas "aucun tunnel actif dès le
        # départ") car les deux empruntent le même classifieur ensuite.
        _deviation_reclass = False
        _reclass_reason: Optional[str] = None  # B24 : pourquoi l'attente a été écartée (journal INTENT_ARBITRATION)
        from ladini.graphs.agents.market_coach.interpreter.state_router import (
            InterpretationRoute,
            choose_interpretation_route,
        )

        _route = None if onboarding_active else choose_interpretation_route(state)

        if _route == InterpretationRoute.SELECTION:
            from ladini.graphs.agents.market_coach.interpreter.selection_micro import (
                SelectionOutcome,
                run_selection_microprompt,
            )

            try:
                _sel_outcome, _sel_result = await run_selection_microprompt(
                    state, mc_runtime, text, locked_goal
                )
            except Exception:
                logger.exception(
                    "[Interpreter SELECTION] micro-prompt en échec "
                    "infrastructurel — repli sur l'interpréteur unifié "
                    "(legacy_fallback=true)"
                )
                _sel_outcome, _sel_result = SelectionOutcome.LEGACY_FALLBACK, None

            if _sel_outcome == SelectionOutcome.RESULT:
                from ladini.graphs.agents.market_coach.interpreter import (
                    context_arbitration as _ca,
                )

                _sel_result = _ca.guard_free_text_selection(state, _sel_result or {}, text)
                if (_sel_result.get("raw_analysis") or {}).get("guard") != _ca.GUARD_FREE_TEXT_ACTION:
                    return _sel_result
                # B24 : une action d'écran (rechercher, retour...) « choisie » par le micro-prompt sur du langage LIBRE n'est pas
                # une réponse prouvée à l'attente — l'index n'est pas exécuté, l'interprétation sémantique (NEW_TASK) tranche.
                logger.info("[Interpreter SELECTION] sélection en langage libre sur une action d'écran — reclassification NEW_TASK")
                _reclass_reason = _ca.GUARD_FREE_TEXT_ACTION
                expected_input = "NONE"
                _deviation_reclass = True
            elif _sel_outcome == SelectionOutcome.INTERRUPTION:
                # Le micro-prompt a confirmé que ce N'EST PAS une réponse au
                # menu — reprend le message ORIGINAL et le fait passer par
                # le classifier NEW_TASK existant (spec §12/§13/§18) : on ne
                # fait que neutraliser la condition de déclenchement de la
                # RÈGLE 1ter ci-dessous (`expected_input == "SELECTION"`),
                # jamais reclassifier nous-mêmes une intention métier ici.
                logger.info(
                    "[Interpreter SELECTION] interruption détectée — "
                    "reclassification via le classifier NEW_TASK (2e appel)"
                )
                expected_input = "NONE"
                _deviation_reclass = True
            else:  # LEGACY_FALLBACK
                _legacy_fallback = True

        elif _route == InterpretationRoute.ACTIVE_SLOT:
            from ladini.graphs.agents.market_coach.interpreter.active_slot_contract import (
                build_active_slot_context,
            )
            from ladini.graphs.agents.market_coach.interpreter.active_slot_micro import (
                ActiveSlotOutcome,
                run_active_slot_microprompt,
            )

            _slot_context = build_active_slot_context(state)
            try:
                _slot_outcome, _slot_result = await run_active_slot_microprompt(
                    state, mc_runtime, text, _slot_context
                )
            except Exception:
                logger.exception(
                    "[Interpreter ACTIVE_SLOT] micro-prompt en échec "
                    "infrastructurel — repli sur l'interpréteur unifié "
                    "(legacy_fallback=true)"
                )
                _slot_outcome, _slot_result = ActiveSlotOutcome.LEGACY_FALLBACK, None

            if _slot_outcome == ActiveSlotOutcome.RESULT:
                return _slot_result
            if _slot_outcome == ActiveSlotOutcome.DEVIATION:
                # Déviation confirmée (ou confiance insuffisante, traitée de
                # façon conservatrice comme une déviation — spec §20) : ne
                # JAMAIS classifier nous-mêmes le nouvel intent ici (spec
                # §11/§12) — neutralise juste la condition déclenchante de
                # la RÈGLE 1bis/1ter ci-dessous (`expected_input`) pour que
                # le classifier NEW_TASK existant reclassifie librement le
                # message ORIGINAL. `current_goal` n'est PAS touché ici —
                # seul `goal_planner`/`cognitive_guard` en aval décident
                # d'un changement de goal.
                logger.info(
                    "[Interpreter ACTIVE_SLOT] déviation détectée — "
                    "reclassification via le classifier NEW_TASK (2e appel)"
                )
                expected_input = "NONE"
                _deviation_reclass = True
            else:  # LEGACY_FALLBACK
                _legacy_fallback = True

        elif _route == InterpretationRoute.STRUCTURED_ACTION:
            from ladini.graphs.agents.market_coach.interpreter.structured_action_micro import (
                StructuredActionOutcome,
                run_structured_action_microprompt,
            )

            try:
                _sa_outcome, _sa_result = await run_structured_action_microprompt(
                    state, mc_runtime, text, selection_context, locked_goal
                )
            except Exception:
                logger.exception(
                    "[Interpreter STRUCTURED_ACTION] micro-prompt en échec "
                    "infrastructurel — repli sur l'interpréteur unifié "
                    "(legacy_fallback=true)"
                )
                _sa_outcome, _sa_result = StructuredActionOutcome.LEGACY_FALLBACK, None

            if _sa_outcome == StructuredActionOutcome.RESULT:
                return _sa_result
            if _sa_outcome == StructuredActionOutcome.DEVIATION:
                # Déviation confirmée — reprend le message ORIGINAL et le
                # fait passer par le classifier NEW_TASK existant (spec
                # §8/§29), jamais une classification faite ici. On neutralise
                # à la fois `expected_input` (RÈGLE 1bis/1ter) ET
                # `selection_context` : sans ce 2e reset, le bloc "ACTION
                # ATTENDUE : SELECT_PRICING_TIER" resterait injecté dans le
                # prompt unifié de CE 2e appel (`selection_context` reste
                # sinon truthy — c'est justement pourquoi cette branche a été
                # choisie), risquant de tirer la reclassification NEW_TASK
                # en arrière vers une sélection au lieu de classifier
                # librement la nouvelle demande.
                logger.info(
                    "[Interpreter STRUCTURED_ACTION] déviation détectée — "
                    "reclassification via le classifier NEW_TASK (2e appel)"
                )
                expected_input = "NONE"
                selection_context = None
                _deviation_reclass = True
            else:  # LEGACY_FALLBACK
                _legacy_fallback = True

        # 2.6 MICRO-PROMPT NEW_TASK (Incrément F, 2026-09-13) : remplace
        # l'interpréteur unifié legacy pour la route `NEW_TASK` elle-même
        # ET pour la reclassification qui suit une DEVIATION confirmée
        # ci-dessus (spec §27 — `_deviation_reclass`). `_legacy_fallback`
        # signifie qu'une des 3 routes prioritaires a déjà échoué pour une
        # raison d'infrastructure : dans ce cas on va DIRECTEMENT sur le
        # legacy (pas de 2e tentative micro-prompt sur un Gateway déjà en
        # échec ce tour). Flag de secours `MARKET_COACH_NEW_TASK_V2_ENABLED`
        # (spec §56) : `False` retombe immédiatement sur legacy, rollback
        # sans redéploiement de code.
        from ladini.core.settings import settings as _mc_settings

        _use_new_task_v2 = (
            not onboarding_active
            and not _legacy_fallback
            and llm is not None
            and (_route == InterpretationRoute.NEW_TASK or _deviation_reclass)
            and _mc_settings.MARKET_COACH_NEW_TASK_V2_ENABLED
        )
        if _use_new_task_v2:
            from ladini.graphs.agents.market_coach.interpreter.new_task_contract import (
                NewTaskPromptContext,
            )
            from ladini.graphs.agents.market_coach.interpreter.new_task_micro import (
                NewTaskOutcome,
                run_new_task_microprompt,
            )

            _cart_pending_nt = _cart_pending_signal(state)
            _nt_context = NewTaskPromptContext(
                reference_date=date.today().isoformat(),
                cart_pending=_cart_pending_nt,
                previous_goal=locked_goal if _deviation_reclass else None,
                producer_order_action_pending=_producer_order_action_pending_signal(
                    state
                ),
                screen_context=_screen_context_hint(state) if _deviation_reclass else None,
            )
            _nt_catalog = {
                intent_key: INTENT_CONFIG[intent_key].get("label", intent_key)
                for intent_key in INTENT_CONFIG
                if intent_key in _classifiable_intents()
            }
            # (2026-09-13) : trace explicite du signal cart_pending et de la
            # disposition retournée — sans ce log, un incident comme "okay"/
            # "je valide" jamais reconnu comme CONFIRM après affichage du
            # panier ne peut être diagnostiqué qu'en devinant depuis les
            # symptômes (voir l'historique de ce fichier plus haut) : il faut
            # pouvoir lire, pour CE tour précis, si cart_pending valait bien
            # True ET ce que le LLM a réellement répondu.
            logger.info(
                "[Interpreter NEW_TASK] cart_pending=%s active_cart_items=%d "
                "preorder_phase=%s",
                _cart_pending_nt,
                len(state.get("active_cart") or []),
                (state.get("preorder_workflow") or {}).get("phase"),
            )
            try:
                _nt_outcome, _nt_result = await run_new_task_microprompt(
                    state, mc_runtime, text, _nt_context, locked_goal, _nt_catalog
                )
            except Exception:
                logger.exception(
                    "[Interpreter NEW_TASK] micro-prompt en échec "
                    "infrastructurel — repli sur l'interpréteur unifié "
                    "(legacy_fallback=true)"
                )
                _nt_outcome, _nt_result = NewTaskOutcome.LEGACY_FALLBACK, None

            logger.info(
                "[Interpreter NEW_TASK] outcome=%s event=%s intent=%s",
                _nt_outcome,
                (_nt_result or {}).get("interpreted_event"),
                (_nt_result or {}).get("detected_intent"),
            )
            if _nt_outcome == NewTaskOutcome.RESULT:
                # (2026-09-13, incident WhatsApp #5) : garde de COHÉRENCE
                # inter-appels — PAS un mot-clé déduit du texte, une
                # contradiction interne entre DEUX jugements LLM successifs
                # sur CE même message. `_deviation_reclass` signifie que la
                # route SELECTION a DÉJÀ tranché : ce message est une
                # interruption, sans rapport avec le menu/but affiché (voir
                # `selection_prompts.py` : un verbe seul type "confirmer"/
                # "annuler" est TOUJOURS une interruption). Si ce 2e appel
                # NEW_TASK revient pourtant avec CONFIRM/REJECT visant
                # `locked_goal` (le but même qui vient d'être jugé étranger
                # au message), les deux jugements du LLM se contredisent —
                # observé en prod : "confirmer" sur une commande producteur
                # reçue pendant qu'un `BUYER_LIST_ORDERS` restait actif,
                # reclassé event=CONFIRM intent=BUYER_LIST_ORDERS, renvoyant
                # l'utilisateur au même menu en boucle. Un résultat
                # contradictoire n'est pas un signal fiable — on retente via
                # l'interpréteur unifié (legacy) plutôt que d'agir dessus,
                # jamais une intention devinée par substitution.
                _nt_event = str(_nt_result.get("interpreted_event") or "").upper()
                _nt_intent = _nt_result.get("detected_intent")
                if _deviation_reclass:
                    # B23/B24 : l'attente (menu/slot) a été jugée non pertinente ; l'intention sémantique décide la tâche.
                    # La raison alimente le journal INTENT_ARBITRATION (émis une seule fois, à la sortie de l'interpréteur).
                    _nt_result["raw_analysis"] = {
                        **(_nt_result.get("raw_analysis") or {}),
                        "decision_reason": _reclass_reason or "expectation_superseded",
                    }
                if (
                    _deviation_reclass
                    and _nt_event in ("CONFIRM", "REJECT")
                    and _nt_intent == locked_goal
                ):
                    logger.warning(
                        "[Interpreter NEW_TASK] résultat contradictoire "
                        "après interruption confirmée (event=%s intent=%s "
                        "== locked_goal) — repli sur l'interpréteur unifié",
                        _nt_event,
                        _nt_intent,
                    )
                    _legacy_fallback = True
                elif _deviation_reclass and (
                    _nt_event in ("UNKNOWN", "OUT_OF_SCOPE")
                    or (_nt_event == "AMBIGUOUS" and _screen_context_hint(state) is not None)
                ):
                    # B24 : « ambigu » face à un écran récurrent vivant = non résolu ; la clarification est celle de
                    # l'écran (ciblée), pas un menu d'intentions génériques.
                    if _nt_event == "AMBIGUOUS":
                        _nt_result = {**_nt_result, "interpreted_event": "UNKNOWN", "detected_intent": "UNKNOWN",
                                      "unknown_reason": "AMBIGUOUS"}
                        _nt_result.pop("candidate_goals", None)
                    # (2026-09-14, incident WhatsApp #9) : la route SELECTION
                    # a déjà tranché que ce message est SANS RAPPORT avec le
                    # menu affiché — si la reclassification NEW_TASK ne
                    # parvient PAS à identifier une intention métier
                    # (`UNKNOWN`/`OUT_OF_SCOPE`), le rendu du menu
                    # (`nodes/rendering/menus.py::render_selection_menu`) ne
                    # doit jamais générer de note d'accompagnement adaptative
                    # ("je comprends que vous voulez X...") : cette note
                    # laisserait croire que le menu ci-dessous (potentiellement
                    # un tout autre tunnel, périmé) est lié au message —
                    # observé en prod : "confirmer" sur une vente producteur
                    # non résolu, note générée "vous voulez confirmer..."
                    # collée à un menu ACHETEUR sans rapport (commande déjà
                    # confirmée). Signal explicite, jamais deviné par le
                    # rendu lui-même.
                    _nt_result["interruption_unresolved"] = True
                    return _nt_result
                else:
                    return _nt_result
            else:
                _legacy_fallback = True

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
        cart_pending = _cart_pending_signal(state)
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
            producer_order_action_pending=(
                "OUI" if _producer_order_action_pending_signal(state) else "non"
            ),
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
        from ladini.graphs.agents.market_coach.llm_gateway import (
            LLMProfile,
            resolve_gateway,
        )

        _gateway = resolve_gateway(mc_runtime)
        # (2026-09-12, chantier State Router — Phase B.1) : l'interpréteur
        # unifié (NEW_TASK/ACTIVE_SLOT/STRUCTURED_ACTION legacy) utilise
        # désormais le profil INTERPRETER dédié (Groq llama-3.1-8b-instant
        # par défaut, voir settings.py::LLM_INTERPRETER_PRIMARY) au lieu de
        # `resolve_profile(mc_runtime)` (qui résolvait toujours REASONING
        # pour ce node). Choix DÉLIBÉRÉ (spec §7) : SEUL le provider/modèle
        # change ici — le prompt système (`_build_dynamic_interpreter_prompt`),
        # le schéma JSON et les règles métier restent STRICTEMENT identiques.
        # `resolve_profile` reste utilisé ailleurs dans le codebase pour les
        # autres nodes métier — ce changement est local à l'interpréteur.
        _profile = LLMProfile.INTERPRETER
        _requested_model = _gateway.primary_model_name(_profile)
        _message_sid = state.get("message_sid")
        _cache_key = _llm_cache_key(
            _message_sid, system_prompt, user_prompt, _requested_model
        )
        # (2026-09-12, instrumentation Langfuse — voir `record_generation`
        # `extra_metadata`) : dimensions minimales pour calculer, PAR
        # MESSAGE, les appels LLM, tokens, taux de cache-hit et distribution
        # de modèle demandés (§4/§5 du sprint). `llm_call_index` : compteur
        # PARTAGÉ Redis par `message_sid` — n'incrémente QUE sur un VRAI
        # appel réseau (jamais sur un cache-hit, voir plus bas) ; volontairement
        # PAS encore de garde-fou dur (`MAX_LLM_CALLS_PER_MESSAGE`) ce
        # sprint — seulement mesurer/tracer, la baseline réelle n'est pas
        # encore observée.
        # (2026-09-12, Incrément A) : `interpretation_route` — instrumentation
        # Langfuse (spec §44). `_route` déjà résolu ci-dessus (étape 2.5) ;
        # `None` uniquement en onboarding (route jamais calculée dans ce cas).
        _interpretation_route = _route.value if _route is not None else None

        _base_metadata: Dict[str, Any] = {
            "message_sid": _message_sid,
            "prompt_version": INTERPRETER_PROMPT_VERSION,
            # (2026-09-13, Incrément G, spec §35) : "legacy" tout court —
            # permet un filtre Prometheus/Langfuse direct
            # (`prompt_family=legacy`), `interpretation_route` juste
            # au-dessous garde le détail de QUELLE route est retombée ici.
            "prompt_family": "legacy",
            "current_goal": state.get("current_goal"),
            "expected_input": _exp_input,
            "interpretation_route": _interpretation_route,
            "legacy_fallback": _legacy_fallback,
        }
        try:
            from ladini.core.telemetry import get_trace_id

            cached = _load_cached_completion(_cache_key)
            if cached is not None:
                parsed, _actual_model = cached
                logger.info(
                    "[Interpreter] Résultat LLM réutilisé du cache (retry sans "
                    "repayer l'appel) | message_sid=%s",
                    _message_sid,
                )
                # Marqueur Langfuse SANS appel réel (latence 0, pas d'usage
                # tokens facturé) — sans ce marqueur, un cache-hit resterait
                # invisible en télémétrie et "% cache hit" serait incalculable.
                try:
                    from ladini.core.telemetry import record_generation

                    record_generation(
                        model=_actual_model or _requested_model or "unknown",
                        messages=[{"role": "user", "content": user_prompt}],
                        output=json.dumps(parsed, ensure_ascii=False),
                        latency_s=0.0,
                        usage=None,
                        name="llm_gateway_completion",
                        agent_node="input_interpreter",
                        extra_metadata={**_base_metadata, "cache_hit": True},
                    )
                except Exception:
                    pass  # observabilité seule — jamais bloquant.
            else:
                from ladini.core.idempotency import increment as _increment_counter

                _llm_call_index = (
                    _increment_counter(
                        f"llm_call_count:{_message_sid}", ttl_seconds=300
                    )
                    if _message_sid
                    else None
                )
                completion = await _gateway.complete(
                    profile=_profile,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    response_format={"type": "json_object"},
                    temperature=0.0,
                    # (2026-09-14, incident WhatsApp — root cause de la
                    # journée) : cet appel ne posait AUCUN plafond explicite
                    # — voir `new_task_micro.py::_MAX_TOKENS` pour le
                    # diagnostic complet (modèles Groq RÉELLEMENT
                    # disponibles pour ce profil = modèles DE RAISONNEMENT,
                    # tokens de "réflexion" décomptés de `max_tokens` avant
                    # le JSON final). Sans plafond explicite, le défaut du
                    # SDK/provider s'appliquait — insuffisant en pratique
                    # (même échec HTTP 400 "Failed to validate JSON" observé
                    # ici qu'avec `max_tokens=300` côté micro-prompt). Même
                    # valeur que `new_task_micro.py` : ce prompt legacy
                    # produit un JSON de sortie comparable (catalogue complet
                    # + jusqu'à ~23 sous-champs d'entités).
                    max_tokens=1200,
                    request_id=get_trace_id(),
                    agent_node="input_interpreter",
                    extra_metadata={
                        **_base_metadata,
                        "cache_hit": False,
                        "llm_call_index": _llm_call_index,
                    },
                )
                parsed = json.loads(completion.choices[0].message.content or "{}")
                _actual_model = getattr(completion, "model", None)
                _store_cached_completion(_cache_key, parsed, _actual_model)
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
            from ladini.graphs.agents.market_coach.llm_gateway import (
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
        # (BUYER_CHECK_ORDER_STATUS, SALES_LIST_ORDERS).
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
        # sur SALES_PUBLISH_PRODUCT/PRODUCTION_DECLARE_FUTURE/FARM_CREATE) : "non c'est
        # 200 tonnes" ressemble fortement, pour le LLM, à l'intent catalogue
        # SALES_UPDATE_PRODUCT/PRODUCTION_UPDATE_FUTURE/FARM_UPDATE — mais ces
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
                remapped_entities["unit"] = None
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
                remapped_entities["unit"] = None

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

        _legacy_result: Dict[str, Any] = {
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
        # (2026-09-14, incident WhatsApp #9) : même garde que côté
        # micro-prompt NEW_TASK ci-dessus — voir son commentaire pour le
        # contexte complet. La route SELECTION a déjà tranché que ce message
        # est sans rapport avec le menu affiché ; si ce repli legacy ne
        # parvient PAS non plus à identifier une intention métier, le rendu
        # du menu ne doit jamais générer de note d'accompagnement laissant
        # croire que le menu (potentiellement un tout autre tunnel périmé)
        # est lié au message.
        if _deviation_reclass and raw_event in ("UNKNOWN", "OUT_OF_SCOPE"):
            _legacy_result["interruption_unresolved"] = True
        if _deviation_reclass:
            _legacy_result["raw_analysis"]["decision_reason"] = _reclass_reason or "expectation_superseded"
        return _legacy_result

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
        text = str(state.get("normalized_text") or state.get("user_query") or "")
        switch = (
            None
            if state.get("is_onboarding")
            else detect_buyer_product_switch(state, text)
        )
        if switch is None:
            # B6 — MÊME produit répété pendant un tunnel Buyer actif : le contexte déjà résolu
            # (vendeur, prix, candidats, pending) est PRÉSERVÉ ; jamais un nouveau BUYER_REQUEST ni
            # une recherche catalogue. Priorité : switch (ci-dessus) > même produit > slot > normal.
            same = (
                None
                if state.get("is_onboarding")
                else detect_buyer_same_product_continuation(state, text)
            )
            if same is not None:
                logger.info(
                    "BUYER_SAME_PRODUCT_CONTINUATION_DETECTED current_goal=%s pending_kind=%s "
                    "product_present=True vendor_present=%s pricing_context_present=%s "
                    "quantity_in_message=%s",
                    resolve_current_goal(state),
                    get_pending_interaction(state).kind.value,
                    bool((state.get("vendor_selection_context") or {}).get("chosen_vendor")),
                    bool(state.get("tier_selection_context")),
                    same.quantity is not None,
                )
                same_entities: Dict[str, Any] = {}
                if same.quantity is not None:
                    same_entities["quantity"] = same.quantity
                    if same.unit:
                        same_entities["unit"] = same.unit
                same_raw: Dict[str, Any] = {
                    "interpreted_event": "ANSWER",
                    "detected_intent": str(resolve_current_goal(state) or "UNKNOWN").upper(),
                    "interpreter_confidence": 0.95,
                    "extracted_entities": same_entities,
                    "raw_analysis": {"path": "deterministic_same_product_continuation"},
                }
                same_patch: Dict[str, Any] = InterpreterResult.from_legacy_dict(
                    same_raw
                ).to_state_patch()
                return same_patch
            # B20 — ARBITRAGE DU CONTEXTE : à quel contexte interactif appartient ce message ? (menu sortant le plus
            # récent > navigation/nouvelle demande explicite > slot actif > classification libre). Voir
            # `interpreter/context_arbitration.py`. Aucun effet quand aucun contexte interactif n'existe.
            arbitrated: Optional[Dict[str, Any]] = await _arbitrate_context(state, mc_runtime, text, role_up, _input_interpreter_impl)
            if arbitrated is not None:
                return _annotate_interpretation(state, arbitrated)
            raw = await _input_interpreter_impl(state, mc_runtime)
            raw = _apply_packaged_stock_entities(raw, state, text)
            return _annotate_interpretation(state, InterpreterResult.from_legacy_dict(raw).to_state_patch())

        # B4 (2026-10-02) — CHANGEMENT DE PRODUIT EXPLICITE pendant un tunnel quantité Buyer :
        # le message n'est PAS une réponse au slot, donc le micro-parser du tunnel
        # (STRUCTURED_ACTION/ACTIVE_SLOT, schéma d'actions fermé) n'a pas son mot à dire — sa
        # sortie invalide/UNKNOWN/hallucinée produisait `recover_active_tunnel` + retry++ (voir
        # `interpreter/product_switch.py`). On interprète le message sur une COPIE de l'état sans
        # contexte de tunnel : il suit alors la route NEW_TASK normale (même classifieur qu'hors
        # tunnel) ; `cognitive_guard`/`goal_planner` (état RÉEL) traitent ensuite l'interruption.
        pending = get_pending_interaction(state)
        logger.info(
            "BUYER_PRODUCT_SWITCH_DETECTED current_goal=%s pending_kind=%s "
            "previous_product_present=%s new_product_present=%s same_product=false "
            "source=deterministic_guard",
            resolve_current_goal(state),
            pending.kind.value,
            True,
            True,
        )
        neutral = {
            **state,
            "pending_interaction": None,
            "vendor_selection_context": {"__reset__": True},
            "tier_selection_context": {"__reset__": True},
        }
        switched: Optional[Dict[str, Any]]
        try:
            switched = await _input_interpreter_impl(neutral, mc_runtime)
        except Exception:
            logger.exception(
                "BUYER_PRODUCT_SWITCH classifieur NEW_TASK en échec — filet déterministe"
            )
            switched = None
        if switched is None or str(switched.get("interpreted_event") or "").upper() in {
            "UNKNOWN",
            "ANSWER",
            "UPDATE",
            "SELECTION",
        }:
            entities: Dict[str, Any] = {"product": switch.new_product}
            qty = parse_quantity_unit_from_text(text)
            if qty.quantity is not None:
                entities["quantity"] = qty.quantity
                if qty.unit:
                    entities["unit"] = qty.unit
            switched = {
                "interpreted_event": "NEW_TASK",
                "detected_intent": "BUYER_REQUEST",
                "interpreter_confidence": 0.9,
                "extracted_entities": entities,
                "raw_analysis": {"path": "deterministic_buyer_product_switch"},
            }
        logger.info(
            "BUYER_PRODUCT_SWITCH_APPLIED tunnel_context_ignored_for_interpretation=true "
            "path=%s event=%s",
            (switched.get("raw_analysis") or {}).get("path"),
            switched.get("interpreted_event"),
        )
        switched_patch: Dict[str, Any] = InterpreterResult.from_legacy_dict(
            switched
        ).to_state_patch()
        return switched_patch

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
        # Import local (anti-cycle : `core.base` déclenche `load_all_actions()`
        # à l'import, qui charge les modules `actions.*` — certains importent
        # `interpreter.routing`). Même discipline que
        # `core/pending_interaction.py::get_pending_interaction`.
        from ladini.graphs.agents.market_coach.core.base import (
            _READ_GOALS,
            _WRITE_GOALS,
        )

        _confirm_goal = str(state.get("current_goal") or "").upper()
        # (2026-09-09, incident réel — généralisé 2026-09-10) : router vers
        # `confirmation_gate` mène à `mcp_tool_executor`, qui EXIGE une action
        # enregistrée pour ce goal (`registry.get_action`). Un goal
        # `handled_by_flow` SANS action (ex: `BUYER_REQUEST`, qui pose lui-même
        # un `CONFIRM_ACTION` pour « lancer un appel d'offres ? ») y déclenchait
        # `RuntimeError("Action non découverte. Migration incomplète.")`. Ces
        # goals gèrent leur propre CONFIRM/REJECT dans leur flow — on les laisse
        # suivre le flux nominal vers `context_resolver`. Discriminant dérivé du
        # registre (`_WRITE_GOALS`/`_READ_GOALS`, core/base.py — mêmes ensembles
        # que `confirmation_gate` consomme), jamais une liste de noms en dur :
        # tout futur goal sans action est couvert sans y penser.
        _goal_has_registered_action = (
            _confirm_goal in _WRITE_GOALS or _confirm_goal in _READ_GOALS
        )
        if _goal_has_registered_action and (
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
