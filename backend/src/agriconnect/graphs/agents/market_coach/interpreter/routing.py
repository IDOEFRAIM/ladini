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
from agriconnect.graphs.agents.market_coach.core.slots import get_slot_hint
from agriconnect.graphs.agents.market_coach.utils import (
    MarketRuntime,
    canonical_unit_label,
)

from agriconnect.graphs.agents.market_coach.interpreter.entities import (
    _UNIT_SYNONYMS,
    _normalize_unit_token,
    _remap_entities,
    _fallback_quantity_unit_from_text,
)
from agriconnect.graphs.agents.market_coach.services.domain.product_validation import (
    _validate_and_sanitize_product,
)
from agriconnect.graphs.agents.market_coach.interpreter.goal_planner import (
    INTENT_TO_GOAL_MAP,
    _NAVIGATION_INTENTS,
    _init_intent_to_goal_map,
    _looks_like_buyer_product_request,
    goal_planner,
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
    """Retourne l'ensemble des intentions autorisées pour un rôle utilisateur.

    `role` ∈ {"PRODUCER", "BUYER"}. Les "BOTH" sont toujours inclus.
    Toute intention présente dans `intent.INTENT_CONFIG` mais absente d'`INTENT_ROLE`
    est traitée comme PRODUCER par défaut (compat asc.) et logguée en warning.
    """
    role_up = str(role).upper().strip()
    missing = set(INTENT_CONFIG) - set(INTENT_ROLE)
    if missing:
        logger.warning(
            "Intents missing INTENT_ROLE entry (defaulting to PRODUCER): %s",
            sorted(missing),
        )
    base = BUYER_INTENTS if role_up == "BUYER" else PRODUCER_INTENTS
    return base | COMMON_INTENTS | frozenset(missing)


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
      "zone": "<str|null>",
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

2. **Extraction des entités (OBLIGATOIRE)** :
   - Tu dois copier TOUT nom de culture/produit détecté (tomates, maïs, riz, oignons...) dans `extracted_entities.product`.
   - Si un mot suit "de", "du", "des", "d'" après une quantité ou une unité, considère-le comme un candidat produit et renseigne `product`.
   - N'utilise PAS d'autres clés (« product_name », « item_name »...) dans la sortie JSON : seule la clé `product` est contractuelle.
   - `product` ne doit être `null` que si aucun produit explicite n'est présent dans le texte.

3. **detected_intent** identifie l'intention métier PRÉCISE.
4. **ANTI-HALLUCINATION D'IDS** : Tu n'inventes jamais d'ID technique. Si choix de l'IHM :
   - chiffre pur ou ordinal ("le 2ème", "option 1") → `selection_index` (int).
   - nom propre ou texte ("l'offre de Diallo") → `selected_value` (str).

4. Segmentation stricte des quantités :
   - "product" doit être un libellé pur (ex: "tomates"), SANS chiffres ni unités.
   - "quantity" est un float (ex: 50.0).
   - "unit" doit être explicitement extraite ("KG", "TONNE", "SAC", "PANIER").
   - Si la quantité est donnée sans unité → validation_status = INVALID_MISSING_UNIT.
   - Si l'unité est ambiguë ou contradictoire → validation_status = INVALID_AMBIGUOUS_UNIT.
   - Sinon → validation_status = VALID.
5. Tu réponds UNIQUEMENT le JSON, sans markdown, sans explication.

EXEMPLES OBLIGATOIRES (FORMAT STRICT) :
Input: "50kg de patates"
Output: {"product": "patates", "quantity": 50.0, "unit": "KG", "validation_status": "VALID"}

Input: "20 tomates"
Output: {"product": "tomates", "quantity": 20.0, "unit": null, "validation_status": "INVALID_MISSING_UNIT"}
""")


def _build_dynamic_interpreter_prompt(role: str = "PRODUCER") -> str:
    """Construit le prompt système avec UNIQUEMENT les intentions du rôle actif."""
    cache_key = (role or "PRODUCER").upper()
    if cache_key in _PROMPT_CACHE:
        return _PROMPT_CACHE[cache_key]

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
    role_label = "ACHETEUR" if cache_key == "BUYER" else "PRODUCTEUR"
    role_label_plural = "ACHETEURS" if cache_key == "BUYER" else "PRODUCTEURS"

    prompt = _SYSTEM_PROMPT_TEMPLATE.substitute(
        role_label=role_label,
        role_label_plural=role_label_plural,
        intent_catalog=intent_catalog,
    )
    _PROMPT_CACHE[cache_key] = prompt
    return prompt


# =====================================================================
# FAST-PATH SÉCURISÉ (Zéro Heuristique de Token Floue)
# =====================================================================

# Patterns déterministes pour RESUME ("reprendre la tâche suspendue").
# Activés uniquement quand `state.suspended_goal` est non vide.
_RESUME_PATTERNS = (
    "continue", "continuer", "continué",
    "reprends", "reprendre", "reprend",
    "on reprend", "on continue",
    "oui continue", "ok continue", "vasy", "vas-y",
    "finis", "finir", "termine", "on termine",
    "reviens", "retourà",
    "comme avant",
)

_PRODUCER_ORDER_TOKENS: tuple[str, ...] = (
    "mes commandes",
    "mes commande",
    "ma commande",
    "mes précommandes",
    "mes precommandes",
    "mes ventes",
    "mes ventes en cours",
    "commande client",
    "commandes client",
    "commande acheteur",
    "commandes acheteurs",
    "commandes des acheteurs",
    "suivi commande",
    "suivi de commande",
    "statut commande",
    "mes ordres",
    "mes deals",
)

_PRODUCER_STATUS_HINTS = (
    ("attente", "PENDING"),
    ("en attente", "PENDING"),
    ("confirm", "CONFIRMED"),
    ("confirme", "CONFIRMED"),
    ("livré", "DELIVERED"),
    ("livree", "DELIVERED"),
    ("livrée", "DELIVERED"),
    ("annul", "CANCELLED"),
    ("préparation", "IN_PROGRESS"),
    ("expédi", "SHIPPED"),
)


def _strip_accents(s: str) -> str:
    import unicodedata
    return "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    )


# Approximate "yes / go ahead" confirmations a buyer types instead of the exact
# word we prompt for. Accent-stripped, lowercase.
_APPROX_CONFIRM = frozenset({
    "oui", "ouais", "ouai", "ok", "okay", "oke", "okey", "daccord", "d accord",
    "cest bon", "c est bon", "cbon", "go", "vasy", "vas y", "allons y", "allez",
    "parfait", "ca marche", "ca roule", "yes", "yep", "yo", "ouep", "bien sur",
    "je confirme", "confirme", "confirmer", "valide", "valider", "valides",
    "finalise", "finaliser", "je valide", "top", "nickel", "carrement",
})


def _looks_like_preorder_trigger(clean: str) -> bool:
    """Typo-tolerant detection of a 'validate my cart / preorder' intent.

    The buyer rarely types the exact word we ask for ("précommander"). They send
    an approximate confirmation ("precomende", "ok", "valide", "c'est bon"). We
    accept any short message that either (a) begins with a précommande-like token
    or (b) is a known approximate confirmation. Kept SHORT-only so a real new
    request like "je veux commander des tomates" is never captured here.
    """
    norm = _strip_accents(clean)
    # Normalise punctuation the buyer scatters around ("c'est", "vas-y", "ok!").
    for ch in ("'", "’", "-", "!", ".", ","):
        norm = norm.replace(ch, " ")
    norm = " ".join(norm.split())
    if not norm or len(norm) > 22:
        return False
    # (a) précommande word with typo tolerance: any token starting with "precom"
    #     (precommande, precomende, precomande, precommender…). "prec" alone is
    #     too loose, so we require the "com" cluster.
    for w in norm.split():
        if w.startswith("precom") or w.startswith("precmd") or w.startswith("precon"):
            return True
    # (b) approximate confirmation
    return norm in _APPROX_CONFIRM


def _interpret_fast_path(state: Dict[str, Any], text: str) -> Optional[Dict[str, Any]]:
    """Court-circuite le LLM uniquement pour les actions structurelles pures d'AG-UI."""
    expected = state.get("expected_input")
    clean = text.strip().lower()
    working = state.get("working_memory") or {}
    locked_goal = state.get("current_goal") or working.get("active_goal") or working.get("locked_intent")
    role_up = str(state.get("forced_role") or state.get("user_role") or "").upper().strip()
    mapping_kind = str(working.get("available_mapping_kind") or "").lower().strip()

    if not clean:
        return None

    # Priorité slot-filling : en attente de quantité/prix, un nombre doit rester une ANSWER
    if expected in ("PRICE", "QUANTITY"):
        number_match = _re.search(r"\b(\d+[\d\s,.]*)\b", clean)
        if number_match:
            raw_number = (number_match.group(1) or "").replace(" ", "").replace(",", ".")
            try:
                numeric_value = float(raw_number)
            except (TypeError, ValueError):
                numeric_value = None

            if numeric_value is not None:
                entities: Dict[str, Any] = {
                    "price" if expected == "PRICE" else "quantity": numeric_value
                }

                unit_match = _re.search(
                    r"\b(k|kg|kgs|kilo|kilogramme|kilogrammes|ton|tons|tone|tones|tonne|tonnes|t|sac|sacs|sachet|sachets|panier|paniers|tete|têtes|tetes|unite|unité|unites|unités)\b",
                    clean,
                )
                if unit_match:
                    unit_raw = _normalize_unit_token(unit_match.group(1) or "")
                    mapped_unit = _UNIT_SYNONYMS.get(unit_raw)
                    if mapped_unit:
                        entities["unit"] = mapped_unit
                    elif expected == "QUANTITY":
                        payload = state.get("transaction_payload") or {}
                        fallback_unit = (
                            payload.get("unit_display")
                            or payload.get("original_unit")
                            or payload.get("unit")
                        )
                        if fallback_unit not in (None, "", [], {}):
                            entities["unit"] = canonical_unit_label(fallback_unit)

                return {
                    "interpreted_event": "ANSWER",
                    "detected_intent": str(locked_goal or "UNKNOWN").upper(),
                    "interpreter_confidence": 0.98,
                    "extracted_entities": entities,
                    "raw_analysis": {"path": "fast_path_slot_numeric_answer"},
                }

    if role_up == "PRODUCER" and any(tok in clean for tok in _PRODUCER_ORDER_TOKENS):
        status_hint = None
        for needle, mapped in _PRODUCER_STATUS_HINTS:
            if needle in clean:
                status_hint = mapped
                break

        limit_hint = None
        limit_match = _re.search(r"(\d+)[^\d]{0,6}(?:derniere|dernières|dernieres|derniers|commandes?|ventes?)", clean)
        if limit_match:
            try:
                limit_hint = max(1, int(limit_match.group(1)))
            except ValueError:
                limit_hint = None

        entities: Dict[str, Any] = {}
        if status_hint:
            entities["status"] = status_hint
        if limit_hint:
            entities["limit"] = limit_hint

        return {
            "interpreted_event": "NEW_TASK",
            "detected_intent": "SALES_LIST_ORDERS",
            "interpreter_confidence": 1.0,
            "extracted_entities": entities,
            "raw_analysis": {
                "path": "fast_path_producer_orders",
                "status_hint": status_hint,
                "limit_hint": limit_hint,
            },
        }

    # Fast-path producteur : découverte des appels d'offres + suivi des offres.
    # On n'intervient PAS pendant un slot-filling actif (prix/quantité/sélection/
    # confirmation) pour ne pas casser le dépôt d'une offre en cours.
    if role_up == "PRODUCER" and expected not in {"PRICE", "QUANTITY", "SELECTION", "CONFIRMATION"}:
        _norm = _strip_accents(clean)
        _my_bid_tokens = (
            "mes offres", "mes propositions", "mes bids", "mes enchere", "mes encheres",
            "suivre mes offres", "statut de mes offres", "etat de mes offres",
            "mes propositions de prix",
        )
        _browse_tokens = (
            "enchere", "encheres", "appel d'offre", "appel doffre", "appels d'offre",
            "appels doffre", "appel d offre", "encherir", "participer",
            "voir les demandes", "demandes du marche", "voir les enchere",
            "voir enchere", "les enchere", "appels d offre",
        )
        if any(tok in _norm for tok in _my_bid_tokens):
            return {
                "interpreted_event": "NEW_TASK",
                "detected_intent": "MARKET_GET_MY_PROPOSALS",
                "interpreter_confidence": 0.96,
                "extracted_entities": {},
                "raw_analysis": {"path": "fast_path_producer_my_bids"},
            }
        if any(tok in _norm for tok in _browse_tokens):
            return {
                "interpreted_event": "NEW_TASK",
                "detected_intent": "MARKET_GET_REQUESTS",
                "interpreter_confidence": 0.95,
                "extracted_entities": {},
                "raw_analysis": {"path": "fast_path_producer_browse_auctions"},
            }

    # Fast-path 0bis : annulation explicite sur menus / confirmations
    cancel_tokens = {"annuler", "annule", "annulation", "stop", "cancel", "quitter", "arrete", "arrête"}
    if expected in {"SELECTION", "CONFIRMATION"} and clean in cancel_tokens:
        return {
            "interpreted_event": "REJECT",
            "detected_intent": str(locked_goal or "UNKNOWN").upper(),
            "interpreter_confidence": 0.95,
            "extracted_entities": {},
            "raw_analysis": {"path": "fast_path_cancel_keyword", "expected": expected},
        }

    # Fast-path 0quater : précommande depuis un panier actif — tolérant aux
    # confirmations approximatives ("precomende", "ok", "valide", "c'est bon").
    # On ne dépend PAS du LLM qui confond précommande et appel d'offres.
    expected_up = str(expected or "").upper().strip()
    if role_up == "BUYER" and not state.get("active_form"):
        preorder_phase = str(
            (state.get("preorder_workflow") or {}).get("phase") or ""
        ).upper().strip()
        has_cart = bool(state.get("active_cart") or (state.get("working_memory") or {}).get("last_active_cart"))

        # (2) Recap affiché → une confirmation approximative valide la précommande.
        if preorder_phase == "PREORDER_DRAFTED" and _looks_like_preorder_trigger(clean):
            return {
                "interpreted_event": "CONFIRM",
                "detected_intent": "BUYER_PREORDER_CONFIRM",
                "interpreter_confidence": 1.0,
                "extracted_entities": {},
                "raw_analysis": {"path": "fast_path_buyer_preorder_confirm", "matched": clean[:22]},
            }

        # (1) Panier actif + intention de valider → on crée le brouillon + récap.
        # NB : le rendu du panier pose expected_input=CONFIRMATION ; dans ce
        # contexte (panier actif, phase CART) une confirmation approximative
        # DOIT lancer la précommande. On n'exclut donc que les saisies où "ok"
        # ne veut pas dire "précommander" (quantité, produit, prix, sélection).
        if (
            has_cart
            and preorder_phase in ("", "CART")
            and expected_up not in ("SELECTION", "PRICE", "QUANTITY", "PRODUCT")
            and _looks_like_preorder_trigger(clean)
        ):
            return {
                "interpreted_event": "NEW_TASK",
                "detected_intent": "BUYER_PREORDER_INIT",
                "interpreter_confidence": 1.0,
                "extracted_entities": {},
                "raw_analysis": {"path": "fast_path_buyer_preorder_from_cart", "matched": clean[:22]},
            }

    # Fast-path 0ter : escalade explicite "appel" depuis un menu de sélection acheteur.
    if (
        role_up == "BUYER"
        and str(locked_goal or "").upper() == "BUYER_REQUEST"
        and expected == "SELECTION"
        and clean in {"appel", "appels", "appel d'offres", "appel doffres", "appel d offre", "lancer appel"}
    ):
        return {
            "interpreted_event": "ANSWER",
            "detected_intent": "BUYER_REQUEST",
            "interpreter_confidence": 1.0,
            "extracted_entities": {},
            "raw_analysis": {
                "path": "fast_path_buyer_request_escalate_call",
                "mapping_kind": mapping_kind,
            },
        }

    procurement_track_tokens = (
        "suivre mes appels",
        "suivre mes appels d'offres",
        "suivi de mes appels",
        "voir mes appels",
        "mes appels d'offres",
        "suivre appel d'offres",
        "suivi appel",
        # Formulation "enchère" (miroir du fast-path producteur ci-dessus) —
        # sans ces tokens, "voir mes enchères" ratait le fast-path et tombait
        # sur la classification LLM complète (round-trip réseau évitable + non
        # déterministe pour une intention pourtant sans ambiguïté).
        "mes enchere", "mes encheres", "mes enchères",
        "voir mes enchere", "voir mes encheres", "voir mes enchères",
        "suivre mes enchere", "suivre mes encheres",
    )
    procurement_track_hit = any(token in clean for token in procurement_track_tokens)
    if not procurement_track_hit and "appel" in clean and ("suivre" in clean or "voir" in clean) and "commande" not in clean:
        procurement_track_hit = True
    if not procurement_track_hit and "enchere" in _strip_accents(clean) and ("suivre" in clean or "voir" in clean or "mes" in clean):
        procurement_track_hit = True
    if role_up == "BUYER" and procurement_track_hit:
        return {
            "interpreted_event": "NEW_TASK",
            "detected_intent": "BUYER_LIST_AUCTIONS",
            "interpreter_confidence": 0.95,
            "extracted_entities": {},
            "raw_analysis": {
                "path": "fast_path_buyer_procurement_tracking",
                "matched": next((token for token in procurement_track_tokens if token in clean), "keyword"),
            },
        }

    # Fast-path 0 : RESUME explicite si une tâche est suspendue dans la pile
    suspended = state.get("suspended_goal")
    goal_stack = state.get("goal_stack") or []
    if (suspended or goal_stack):
        for pat in _RESUME_PATTERNS:
            if pat in clean and len(clean) <= 40:
                return {
                    "interpreted_event": "RESUME",
                    "detected_intent": "UNKNOWN",
                    "interpreter_confidence": 1.0,
                    "extracted_entities": {},
                    "raw_analysis": {"path": "fast_path_resume", "matched": pat},
                }

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

    # Fast-path 2 : Saisie de valeur numérique directe lors du slot-filling actif
    if expected in ("PRICE", "QUANTITY"):
        num_match = _re.match(
            r"^[\s]*(\d+[\s,.]?\d*)\s*(k|kg|kgs?|kilo|kilogramme|ton|tonne|tonnes|t|sac|sacs|sachet|sachets|panier|paniers|fcfa|f|cfa)?[\s]*$",
            clean,
        )
        if num_match:
            try:
                val = float(num_match.group(1).replace(",", ".").replace(" ", ""))
            except ValueError:
                pass
            else:
                entity_key = "price" if expected == "PRICE" else "quantity"
                entities: Dict[str, Any] = {entity_key: val}
                unit_raw = num_match.group(2)
                if unit_raw:
                    normalized_unit = _normalize_unit_token(unit_raw)
                    mapped_unit = _UNIT_SYNONYMS.get(normalized_unit)
                    if mapped_unit:
                        entities["unit"] = mapped_unit
                return {
                    "interpreted_event": "ANSWER",
                    "detected_intent": "UNKNOWN",
                    "interpreter_confidence": 0.95,
                    "extracted_entities": entities,
                    "raw_analysis": {"path": "fast_path_numeric_answer"},
                }

    return None


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

        def _emit_onboarding(extracted: Dict[str, Any], source: str) -> Dict[str, Any]:
            return {
                "interpreted_event": "ONBOARDING_INPUT",
                "detected_intent": "ONBOARDING",
                "interpreter_confidence": 1.0,
                "extracted_entities": extracted,
                "raw_analysis": {"path": source, "role": role_up},
            }

        # 1. Traitement prioritaire par Fast-path structurel rigide
        fast = None if onboarding_active else _interpret_fast_path({**state, "forced_role": role_up}, text)
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
        llm = getattr(mc_runtime, "llm", None)
        if llm is None:
            logger.warning("No LLM on runtime — interpreter returns UNKNOWN")
            if onboarding_active:
                return _emit_onboarding({}, "onboarding_no_llm")
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
        user_prompt = INTERPRETER_USER_PROMPT.format(
            current_goal=state.get("current_goal") or "AUCUN",
            expected_input=_exp_input,
            slot_hint_line=_slot_hint_line,
            last_agent_question=state.get("last_agent_question") or "—",
            expected_candidates=", ".join(state.get("expected_candidates") or []) or "—",
            normalized_text=text,
        )

        # 4. Appel LLM d'analyse sémantique et contextuelle
        try:
            completion = await asyncio.to_thread(
                lambda: llm.chat.completions.create(
                    model=getattr(mc_runtime, "model_answer", "llama-3.3-70b-versatile"),
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    response_format={"type": "json_object"},
                    temperature=0.0,
                )
            )
            parsed = json.loads(completion.choices[0].message.content or "{}")
        except Exception as exc:
            logger.error("Interpreter LLM CRASH : %s — forcing UNKNOWN", exc, exc_info=True)
            if onboarding_active:
                return _emit_onboarding({}, "onboarding_llm_crash")
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

        if expected_input in {"PRODUCT", "PRICE", "QUANTITY", "UNIT", "LOCATION", "DATE"} and raw_event == "NEW_TASK":
            raw_event = "ANSWER"

        if expected_input in {"SELECTION", "CONFIRMATION"} and raw_event not in {"SELECTION", "CONFIRM", "REJECT"}:
            if raw_intent != "UNKNOWN" and confidence >= 0.55:
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
