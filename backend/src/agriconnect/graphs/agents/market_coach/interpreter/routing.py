"""Market — Interpreter & Routing.

Centralise l'appel LLM d'interprétation et le routage post-validator.
Le prompt système est construit dynamiquement à partir de `INTENT_CONFIG`
filtré par rôle (PRODUCER / BUYER) afin d'éviter qu'un LLM ne propose
une intention inappropriée pour le profil utilisateur.

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

from agriconnect.graphs.agents.market_coach.interpreter.intent import (
    INTENT_CONFIG,
    INTENT_ROLE,
)
from agriconnect.graphs.agents.market_coach.interpreter.prompts import INTERPRETER_USER_PROMPT
from agriconnect.graphs.agents.market_coach.core.state import MarketAgentState
from agriconnect.graphs.agents.market_coach.core.slots import get_slot_hint, SLOT_FILLING_INPUTS
from agriconnect.graphs.agents.market_coach.utils import (
    MarketRuntime,
    canonical_unit_label,
)

from agriconnect.graphs.agents.market_coach.interpreter.entities import (
    _remap_entities,
    _fallback_quantity_unit_from_text,
)
from agriconnect.graphs.agents.market_coach.services.domain.quantity_unit import (
    scan_number_candidates,
)
from agriconnect.graphs.agents.market_coach.services.domain.product_validation import (
    _validate_and_sanitize_product,
)
from agriconnect.graphs.agents.market_coach.interpreter.goal_planner import (
    INTENT_TO_GOAL_MAP,
    _NAVIGATION_INTENTS,
    _init_intent_to_goal_map,
    _looks_like_buyer_product_request,
    _extract_buyer_product,
    goal_planner,
)
# Source UNIQUE du seuil de confiance de rupture d'intention. L'interpréteur
# (ici) et tunnel_manager (goal_planner) DOIVENT utiliser exactement le même :
# sinon l'interpréteur promeut un message en INTERRUPTION à un seuil que
# tunnel_manager refuse ensuite → zone morte produisant un "je n'ai pas compris"
# confus au lieu d'une bascule propre OU d'une continuation propre.
from agriconnect.graphs.agents.market_coach.core.tunnel_manager import (
    INTERRUPTION_CONFIDENCE_THRESHOLD,
)

logger = logging.getLogger("AgriConnect.Market.InterpreterRouting")

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
      "quantity": <float|null>,
      "unit": "<KG|TONNE|SAC|PANIER|null>",
      "price": <float|null>,
      "estimated_available_at": "<YYYY-MM-DD|null>",
      "expected_harvest_date": "<YYYY-MM-DD|null>",
      "deadline": "<YYYY-MM-DD|null>",
      "zone": "<str|null>",
      "farm_name": "<str|null>",
      "price_unit": "<KG|TONNE|SAC|PANIER|null>",
      "selection_index": <int|null>,
      "selected_value": "<str|null>",
      "movement_type": "<IN|OUT|null>",
      "reason": "<str|null>"
  }
}

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

2. **Extraction des entités (OBLIGATOIRE)** :
   - Tu dois copier TOUT nom de culture/produit détecté (tomates, maïs, riz, oignons...) dans `extracted_entities.product`.
   - Si un mot suit "de", "du", "des", "d'" après une quantité ou une unité, considère-le comme un candidat produit et renseigne `product`.
   - N'utilise PAS d'autres clés (« product_name », « item_name »...) dans la sortie JSON : seule la clé `product` est contractuelle.
   - `product` ne doit être `null` que si aucun produit explicite n'est présent dans le texte.
   - **Nom de domaine/exploitation** : si `expected_input` vaut `FARM_NAME`, ou si `last_agent_question` demande le nom de la ferme/exploitation/domaine, tout texte libre fourni (même un seul mot, ex: "Matata", "Ferme du Soleil") EST ce nom — copie-le TEL QUEL dans `extracted_entities.farm_name` et mets `interpreted_event = ANSWER`. Ne renvoie JAMAIS OUT_OF_SCOPE/UNKNOWN dans ce contexte pour un mot ou une courte phrase qui ne correspond à aucune autre intention du catalogue : c'est un nom propre, pas un message hors-sujet.

3. **detected_intent** identifie l'intention métier PRÉCISE.

4. **ANTI-HALLUCINATION D'IDS** : Tu n'inventes jamais d'ID technique. Si choix de l'IHM :
   - chiffre pur ou ordinal ("le 2ème", "option 1") → `selection_index` (int).
   - nom propre ou texte ("l'offre de Diallo") → `selected_value` (str).

5. **Segmentation stricte des quantités** :
   - "product" doit être un libellé pur (ex: "tomates"), SANS chiffres ni unités.
   - "quantity" est un float (ex: 50.0).
   - "unit" doit être explicitement extraite ("KG", "TONNE", "SAC", "PANIER").
   - Si la quantité est donnée sans unité → validation_status = INVALID_MISSING_UNIT.
   - Si l'unité est ambiguë ou contradictoire → validation_status = INVALID_AMBIGUOUS_UNIT.
   - Sinon → validation_status = VALID.
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

6. **NORMALISATION STRICTE DES DATES (OBLIGATOIRE)** :
   - L'année de référence est **2026**.
   - Toute date, estimation de disponibilité ou de récolte formulée en langage naturel (ex: "29 octobre", "fin octobre", "demain", "dans 3 jours") doit être **impérativement convertie au format ISO standard : YYYY-MM-DD**.
   - `estimated_available_at` : Date de disponibilité estimée pour l'acheteur (ex: "disponible le 29 octobre" ou "prêt le 29 oct" → "2026-10-29").
   - `expected_harvest_date` : Date prévue pour la récolte physique (ex: "récolte prévue en octobre" → "2026-10-15" (milieu de mois par défaut si imprécis)).
   - `deadline` : Date LIMITE d'un appel d'offres (jusqu'à quand les producteurs peuvent répondre, ex: "avant le 30 septembre", "réponses jusqu'au 15 oct" → "2026-09-30"/"2026-10-15").
   - N'envoie JAMAIS de texte libre ou de noms de mois écrits en toutes lettres dans ces champs. Si non spécifié ou impossible à déterminer, mets `null`.

7. Tu réponds UNIQUEMENT le JSON, sans markdown, sans explication.

EXEMPLES OBLIGATOIRES (FORMAT STRICT) :
Input: "50kg de patates pour le 29 octobre"
Output: {
  "interpreted_event": "NEW_TASK",
  "detected_intent": "DECLARE_CROP_CYCLE",
  "interpreter_confidence": 0.95,
  "validation_status": "VALID",
  "extracted_entities": {
      "product": "patates",
      "quantity": 50.0,
      "unit": "KG",
      "price": null,
      "estimated_available_at": "2026-10-29",
      "expected_harvest_date": null,
      "zone": null,
      "selection_index": null,
      "selected_value": null,
      "movement_type": null,
      "reason": null
  }
}

Input: "20 tomates"
Output: {
  "interpreted_event": "NEW_TASK",
  "detected_intent": "DECLARE_CROP_CYCLE",
  "interpreter_confidence": 0.85,
  "validation_status": "INVALID_MISSING_UNIT",
  "extracted_entities": {
      "product": "tomates",
      "quantity": 20.0,
      "unit": null,
      "price": null,
      "estimated_available_at": null,
      "expected_harvest_date": null,
      "zone": null,
      "selection_index": null,
      "selected_value": null,
      "movement_type": null,
      "reason": null
  }
}
""")


_UNIFIED_PROMPT_CACHE_KEY = "UNIFIED"


def _build_dynamic_interpreter_prompt(role: str = "PRODUCER") -> str:
    """Construit le prompt système avec le catalogue COMPLET des intentions.

    Refonte double-rôle : le paramètre `role` n'a plus d'effet sur le contenu
    (conservé pour compat de signature) — chaque utilisateur peut vendre ET
    acheter, donc l'interpréteur doit reconnaître les deux familles d'intents
    dans le même message, sans filtrage préalable.
    """
    if _UNIFIED_PROMPT_CACHE_KEY in _PROMPT_CACHE:
        return _PROMPT_CACHE[_UNIFIED_PROMPT_CACHE_KEY]

    allowed = allowed_intents_for_role(role)
    intent_lines: List[str] = []
    for intent_key, config in INTENT_CONFIG.items():
        if intent_key not in allowed:
            continue
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
    state: Dict[str, Any], text: str, *, skip_numeric_shortcut: bool = False,
) -> Optional[Dict[str, Any]]:
    """Court-circuite le LLM uniquement pour les actions structurelles pures d'AG-UI.

    `skip_numeric_shortcut` désactive le raccourci regex PRICE/QUANTITY quand
    un LLM est disponible sur le runtime : celui-ci est la source prioritaire
    pour extraire quantity+unit et price+price_unit ENSEMBLE (il voit tout le
    message en contexte) ; ce raccourci ne doit plus servir que de repli
    quand le LLM est indisponible.
    """
    expected = state.get("expected_input")
    clean = text.strip().lower()
    working = state.get("working_memory") or {}
    locked_goal = state.get("current_goal") or working.get("active_goal") or working.get("locked_intent")

    if not clean:
        return None

    # Correction chiffrée pendant la CONFIRMATION ("j'ai plutôt 795 kg", "non
    # j'ai 795 kg" sur un récap déjà affiché) : un nombre typé sans ambiguïté
    # (unité de poids/comptage OU devise à proximité) est une correction
    # directe du brouillon, pas une nouvelle tâche — à traiter ici en
    # UPDATE, avant même de risquer une reclassification LLM (le LLM a
    # laissé cette correction sans effet en prod : le récap restait figé sur
    # l'ancienne quantité malgré 2 corrections explicites successives).
    _confirmation_correction = expected == "CONFIRMATION" and bool(_re.search(r"\d", clean))

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
        _qty_candidate = next((c for c in candidates if c["unit"] and not c["near_currency"]), None)
        _price_candidate = next((c for c in candidates if c["near_currency"]), None)
        if _qty_candidate is not None and _price_candidate is not None and _qty_candidate is not _price_candidate:
            compound_entities: Dict[str, Any] = {
                "quantity": _qty_candidate["value"],
                "unit": _qty_candidate["unit"],
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
                return {
                    "interpreted_event": _event_type,
                    "detected_intent": str(locked_goal or "UNKNOWN").upper(),
                    "interpreter_confidence": 0.98,
                    "extracted_entities": {"quantity": _qty_candidate["value"], "unit": _qty_candidate["unit"]},
                    "raw_analysis": {"path": "fast_path_confirmation_correction", "slot": "quantity"},
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
                    "raw_analysis": {"path": "fast_path_confirmation_correction", "slot": "price"},
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
            _unambiguous_single = next((c for c in candidates if c["unit"] and not c["near_currency"]), None)
        else:
            _unambiguous_single = next((c for c in candidates if c["near_currency"]), None)

        if skip_numeric_shortcut and _unambiguous_single is None:
            candidates = []

        if candidates:
            if expected == "QUANTITY":
                chosen = next((c for c in candidates if c["unit"] and not c["near_currency"]), None)
                if chosen is None:
                    chosen = next((c for c in candidates if not c["near_currency"]), None)
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
                    "raw_analysis": {"path": "fast_path_slot_numeric_answer", "slot": slot},
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
            str((state.get("working_memory") or {}).get("available_mapping_kind") or "").lower()
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
    """Crée un nœud `input_interpreter` configuré pour un rôle donné (AG-UI)."""
    role_up = str(role or "PRODUCER").upper().strip()
    allowed = allowed_intents_for_role(role_up)

    async def input_interpreter(state: MarketAgentState, mc_runtime: MarketRuntime) -> Dict[str, Any]:
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
        expected_input = state.get("expected_input")
        onboarding_active = bool(state.get("is_onboarding"))
        working = state.get("working_memory") or {}
        locked_goal = state.get("current_goal") or working.get("active_goal") or working.get("locked_intent")

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
                logger.info("[Interpreter InteractiveBypass] CONFIRM (payload=%s)", interactive)
                return {
                    "interpreted_event": "CONFIRM",
                    "detected_intent": str(locked_goal or "UNKNOWN").upper(),
                    "interpreter_confidence": 1.0,
                    "extracted_entities": {},
                    "raw_analysis": {"path": "interactive_bypass_confirm"},
                }
            if up in {"REJECT", "NON", "NO", "ANNULER", "CANCEL"}:
                logger.info("[Interpreter InteractiveBypass] REJECT (payload=%s)", interactive)
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
        _skip_numeric_shortcut = expected_input in ("PRICE", "QUANTITY") and llm is not None
        fast = None if onboarding_active else _interpret_fast_path(
            {**state, "forced_role": role_up}, text, skip_numeric_shortcut=_skip_numeric_shortcut,
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
                logger.info("[Interpreter] LLM absent — repli déterministe %s", degraded["detected_intent"])
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
        _slot_hint = get_slot_hint(_exp_input.lower()) if _exp_input not in ("NONE", "CONFIRMATION", "SELECTION") else ""
        _slot_hint_line = f" → {_slot_hint}" if _slot_hint and _slot_hint != _exp_input.lower() else ""
        _suspended_goal = state.get("suspended_goal")
        _goal_stack = state.get("goal_stack") or []
        _suspended_task = str(_suspended_goal or (_goal_stack[-1] if _goal_stack else "")) or "aucune"
        # Panier prêt à valider : signal d'état (phase CART + panier non vide),
        # PAS un mot-clé du texte. Le LLM s'en sert pour comprendre un accord
        # libre ("je suis d'accord") comme une validation de précommande.
        _cart_phase = str((state.get("preorder_workflow") or {}).get("phase") or "").upper()
        cart_pending = role_up == "BUYER" and _cart_phase == "CART" and bool(state.get("active_cart"))
        user_prompt = INTERPRETER_USER_PROMPT.format(
            current_goal=state.get("current_goal") or "AUCUN",
            expected_input=_exp_input,
            slot_hint_line=_slot_hint_line,
            last_agent_question=state.get("last_agent_question") or "—",
            expected_candidates=", ".join(state.get("expected_candidates") or []) or "—",
            suspended_task=_suspended_task,
            cart_pending="OUI" if cart_pending else "non",
            normalized_text=text,
        )

        # 4. Appel LLM d'analyse sémantique et contextuelle
        # Timeout de sécurité (Phase 4) : un appel Groq suspendu ici gelait le
        # tour ENTIER jusqu'au timeout global orchestrateur (45s). TimeoutError
        # est capturé par le except ci-dessous → fallback UNKNOWN propre.
        try:
            completion = await asyncio.wait_for(
                asyncio.to_thread(
                    lambda: llm.chat.completions.create(
                        model=getattr(mc_runtime, "model_answer", "llama-3.3-70b-versatile"),
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        response_format={"type": "json_object"},
                        temperature=0.0,
                    )
                ),
                timeout=15.0,
            )
            parsed = json.loads(completion.choices[0].message.content or "{}")
        except Exception as exc:
            # Dégradation attendue (timeout, 429/5xx Groq) : WARNING, pas de
            # traceback — bruit de log inutile pour un cas déjà géré par le
            # fallback UNKNOWN ci-dessous. Réserver ERROR+exc_info aux échecs
            # non anticipés (bug de parsing JSON, etc.).
            is_expected = isinstance(exc, (asyncio.TimeoutError, TimeoutError))
            if not is_expected:
                try:
                    from groq import APIStatusError, APITimeoutError
                    is_expected = isinstance(exc, (APIStatusError, APITimeoutError))
                except ImportError:
                    pass
            if is_expected:
                logger.warning("Interpreter LLM indisponible (%s) — forcing UNKNOWN", exc)
            else:
                logger.error("Interpreter LLM CRASH : %s — forcing UNKNOWN", exc, exc_info=True)
            if onboarding_active:
                return _emit_onboarding({}, "onboarding_llm_crash")
            # Repli déterministe AVANT d'abandonner en UNKNOWN : une demande
            # d'achat évidente reste servie même si Groq est en panne/quota
            # épuisé (cause racine des récaps vides observés en prod).
            degraded = _degraded_fallback(role_up, text)
            if degraded is not None:
                logger.info("[Interpreter] LLM en panne — repli déterministe %s (produit=%s)",
                            degraded["detected_intent"], degraded["extracted_entities"].get("product"))
                return degraded
            return {
                "interpreted_event": "UNKNOWN",
                "detected_intent": "UNKNOWN",
                "interpreter_confidence": 0.0,
                "extracted_entities": {},
                "raw_analysis": {"path": "llm_crash", "error": str(exc)},
            }

        # 5. Normalisation sémantique et gardes-fous anti-dérive
        raw_event = str(parsed.get("interpreted_event") or "UNKNOWN").upper().strip()
        if raw_event == "PROVIDE_INFO":
            raw_event = "ANSWER"

        raw_intent = str(parsed.get("detected_intent") or "UNKNOWN").upper().strip()
        if raw_intent != "UNKNOWN" and raw_intent not in allowed:
            logger.warning(
                "LLM a retourné une intention hors-périmètre %s pour le rôle %s — ignoré",
                raw_intent,
                role_up,
            )
            raw_intent = "UNKNOWN"

        try:
            confidence = max(0.0, min(1.0, float(parsed.get("interpreter_confidence") or 0.0)))
        except (TypeError, ValueError):
            confidence = 0.0

        if role_up == "BUYER" and _looks_like_buyer_product_request(text.strip().lower()):
            if raw_event == "UNKNOWN":
                raw_event = "NEW_TASK"
            if raw_intent not in {"BUYER_REQUEST", "BUYER_ADD_TO_CART", "PROCUREMENT_CREATE_REQUEST"}:
                raw_intent = "BUYER_REQUEST"
                confidence = max(confidence, 0.85)

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
        _fresh_product_preview = str(_raw_entities_preview.get("product") or "").strip().lower()
        _cur_product_preview = str((state.get("transaction_payload") or {}).get("product") or "").strip().lower()
        _is_different_product = bool(_fresh_product_preview) and _fresh_product_preview != _cur_product_preview

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
        if (
            expected_input in SLOT_FILLING_INPUTS
            and raw_event == "NEW_TASK"
            and not _is_different_product
            and not _is_different_goal
        ):
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
        _pending_create_update_sibling = _PENDING_CREATE_UPDATE_SIBLINGS.get(_locked_goal_for_confirmation)
        if (
            expected_input == "CONFIRMATION"
            and _pending_create_update_sibling
            and raw_intent == _pending_create_update_sibling
        ):
            logger.info(
                "[Interpreter] Correction de brouillon non persisté (%s) pendant CONFIRMATION "
                "— intent-jumeau %s absorbé comme UPDATE du même goal, pas une interruption.",
                _locked_goal_for_confirmation, raw_intent,
            )
            raw_event = "UPDATE"
            raw_intent = _locked_goal_for_confirmation

        if expected_input in {"SELECTION", "CONFIRMATION"} and raw_event not in {"SELECTION", "CONFIRM", "REJECT", "UPDATE"}:
            # Seuil ALIGNÉ sur tunnel_manager (INTERRUPTION_CONFIDENCE_THRESHOLD) :
            # sous ce seuil, on reste proprement dans la confirmation/sélection
            # (RÈGLE 2 du goal_planner re-verrouille le goal) ; au-dessus, la
            # bascule est acceptée par tunnel_manager aussi — plus de zone morte.
            if raw_intent != "UNKNOWN" and confidence >= INTERRUPTION_CONFIDENCE_THRESHOLD:
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
            logger.info("[Interpreter] Panier validé en langage libre → BUYER_PREORDER_INIT")
            raw_event = "NEW_TASK"
            raw_intent = "BUYER_PREORDER_INIT"
            confidence = max(confidence, 0.9)

        if raw_event not in {"NEW_TASK", "ANSWER", "CONFIRM", "REJECT", "SELECTION", "UPDATE", "INTERRUPTION", "RESUME", "OUT_OF_SCOPE", "UNKNOWN", "ONBOARDING_INPUT"}:
            raw_event = "UNKNOWN"

        remapped_entities = _remap_entities(parsed.get("extracted_entities") or {})
        fallback_entities = _fallback_quantity_unit_from_text(text)
        fallback_applied = False
        if fallback_entities:
            for key, value in fallback_entities.items():
                if key not in remapped_entities and value not in (None, "", [], {}):
                    remapped_entities[key] = value
                    fallback_applied = True
                elif (
                    key == "unit"
                    and value not in (None, "", [], {})
                    and remapped_entities.get("unit") not in (None, "", [], {})
                    and remapped_entities["unit"] != value
                ):
                    # Le LLM a renvoyé une unité qui CONTREDIT celle réellement
                    # écrite dans le message ("200kg" → LLM renvoie parfois
                    # "TONNE", un biais d'ancrage sur une unité mentionnée plus
                    # tôt dans la conversation — observé en prod : un producteur
                    # reste "bloqué" sur TONNE malgré des corrections explicites
                    # en kg). L'extraction déterministe (regex sur le texte
                    # littéral) fait foi : jamais laisser le LLM contredire une
                    # unité sans ambiguïté présente dans le message lui-même.
                    logger.warning(
                        "[Interpreter] Unité LLM '%s' contredit le texte ('%s' détecté par regex) — "
                        "unité déterministe retenue.",
                        remapped_entities["unit"], value,
                    )
                    remapped_entities["unit"] = value
                    fallback_applied = True
        if "product" in remapped_entities:
            validated_product = await _validate_and_sanitize_product(
                remapped_entities.get("product"), mc_runtime
            )
            remapped_entities["product"] = validated_product
        if onboarding_active:
            return _emit_onboarding(remapped_entities, "onboarding_override")

        raw_validation = parsed.get("validation_status")
        validation_status = str(raw_validation).upper().strip() if raw_validation else None
        if validation_status not in {"VALID", "INVALID_MISSING_UNIT", "INVALID_AMBIGUOUS_UNIT"}:
            validation_status = None

        if fallback_applied:
            if not validation_status:
                validation_status = "VALID" if remapped_entities.get("unit") else "INVALID_MISSING_UNIT"
            if raw_event == "UNKNOWN" and expected_input in {"QUANTITY", "UNIT", "PRICE"}:
                raw_event = "ANSWER"
            if raw_intent == "UNKNOWN" and locked_goal:
                raw_intent = str(locked_goal).upper()

        return {
            "interpreted_event": raw_event,
            "detected_intent": raw_intent,
            "interpreter_confidence": confidence,
            "validation_status": validation_status,
            "extracted_entities": remapped_entities,
            "raw_analysis": {"path": "llm", "role": role_up},
        }

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
        expected_input = str(state.get("expected_input") or "NONE").upper().strip()
        strategy = str(state.get("response_strategy") or "").upper().strip()

        if state.get("waiting_for_confirmation") or state.get("status") == "WAITING_CONFIRMATION":
            logger.info("[%s ROUTER] En attente de confirmation — Routage vers confirmation_gate", role_up)
            logger.debug("[AfterValidator] role=%s decision=to_confirmation", role_up)
            return "to_confirmation"

        # 1. PROTECTION SÉCURITÉ ANTI-DÉRIVE (AG-UI Court-circuit)
        if interpreted_event in {"UNKNOWN", "OUT_OF_SCOPE"}:
            logger.info(
                "[%s ROUTER] Dérive détectée (%s) — Routage forcé vers response_strategy",
                role_up,
                interpreted_event
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
                len(missing_fields)
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
        if state.get("status") == "WAITING_INPUT" and (expected_input == "SELECTION" or strategy == "SELECTION_MENU"):
            logger.info(
                "[%s ROUTER] Sélection attendue — Routage vers response_strategy",
                role_up,
            )
            logger.debug("[AfterValidator] role=%s decision=to_strategy reason=selection_wait", role_up)
            return "to_strategy"

        # 4. FLUX NOMINAL
        logger.info("[%s ROUTER] Input validé et complet — Routage vers context_resolver", role_up)
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
