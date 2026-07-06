"""Market — Interpreter & Goal Planner.

Centralise l'appel LLM d'interprétation et l'automate de planification.
Le prompt système est construit dynamiquement à partir de `INTENT_CONFIG`
filtré par rôle (PRODUCER / BUYER) afin d'éviter qu'un LLM ne propose
une intention inappropriée pour le profil utilisateur.

Le `goal_planner` reste une machine à états purement déterministe : aucune
heuristique de texte, aucun appel LLM. Il consomme `interpreted_event` et
`detected_intent` produits en amont.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re as _re
import unicodedata as _unicodedata
from string import Template
from typing import Any, Dict, List, Optional

from agriconnect.graphs.agents.market_coach.interpreter.intent import (
    INTENT_CONFIG,
    INTENT_ROLE,
    INTENT_DISAMBIGUATION,
)
from agriconnect.graphs.agents.market_coach.interpreter.prompts import INTERPRETER_USER_PROMPT
from agriconnect.graphs.agents.market_coach.core.state import MarketAgentState
from agriconnect.graphs.agents.market_coach.utils import (
    MarketRuntime,
    canonical_unit_label,
    normalize_slot_keys,
    slot_has_value,
    _clean_candidate_text,
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
# ENTITY KEY REMAPPER (Normalisation vers les clés MCP standard)
# =====================================================================

_ENTITY_KEY_REMAP: Dict[str, str] = {
    "prix": "price",
    "price": "price",
    "montant": "price",
    "montant_enchere": "price",
    "offered_price": "price",
    "max_price": "price",
    "prix_unitaire": "price",
    "quantite": "quantity",
    "quantity": "quantity",
    "qty": "quantity",
    "volume": "quantity",
    "quantity_kg": "quantity",
    "quantity_for_sale": "quantity",
    "unite": "unit",
    "unit": "unit",
    "produit": "product",
    "name": "product",
    "item_name": "product",
    "commodity": "product",
    "culture": "product",
    "product_name": "product",
    "zone": "zone",
    "region": "zone",
    "localite": "zone",
    "target_zone": "zone",
    "price_mentioned": "price",
    "quantity_mentioned": "quantity",
    "unit_mentioned": "unit",
    "zone_name": "zone",
    "selection_index": "selection_index",
    "selected_value": "selected_value",
    "movement_type": "movement_type",
    "reason": "reason",
}


_UNIT_SYNONYMS: Dict[str, str] = {
    "k": "KG",
    "kg": "KG",
    "kgs": "KG",
    "kilo": "KG",
    "kilos": "KG",
    "kilogramme": "KG",
    "kilogrammes": "KG",
    "ton": "TONNE",
    "tons": "TONNE",
    "tone": "TONNE",
    "tones": "TONNE",
    "tonne": "TONNE",
    "tonnes": "TONNE",
    "t": "TONNE",
    "sac": "SAC",
    "sacs": "SAC",
    "sachet": "SAC",
    "sachets": "SAC",
    "panier": "PANIER",
    "paniers": "PANIER",
    "tete": "TETE",
    "tetes": "TETE",
    "unite": "UNITE",
    "unites": "UNITE",
}


def _normalize_unit_token(raw: str) -> str:
    if not raw:
        return ""
    token = _unicodedata.normalize("NFKD", str(raw).strip().lower())
    token = "".join(ch for ch in token if not _unicodedata.combining(ch))
    return token.replace(".", "")

_QUANTITY_UNIT_PATTERN = _re.compile(
    r"(?P<qty>\d+[\d\s.,]*)\s*(?P<unit>[a-zA-ZÀ-ÖØ-öø-ÿ.]+)",
    _re.IGNORECASE,
)


_GENERIC_PRODUCT_STOPWORDS = {
    "merci",
    "bonjour",
    "bonsoir",
    "salut",
    "d'accord",
    "ok",
    "c'est bon",
    "aucun",
    "nothing",
}

_KNOWN_PRODUCT_KEYWORDS = {
    "maïs",
    "mais",
    "riz",
    "sorgho",
    "soja",
    "arachide",
    "oignon",
    "tomate",
    "piment",
    "gombo",
    "banane",
    "igname",
    "coton",
    "manioc",
    "mil",
    "niébé",
    "engrais",
    "urée",
    "npk",
    "intrant",
    "semence",
    "fertilisant",
    "poivron",
    "carotte",
    "chou",
}

_SUSPICIOUS_PRODUCT_TOKENS = {
    "commande",
    "commandes",
    "precommande",
    "précommande",
    "précommander",
    "precommander",
    "prix",
    "payer",
    "paiement",
    "client",
    "livraison",
    "acheteur",
    "achete",
    "acheté",
    "acheter",
    "vendeur",
    "vendre",
    "bonjour",
    "bonsoir",
    "merci",
    "urgent",
}


async def _catalog_has_product(name: str, mc_runtime: Optional[MarketRuntime]) -> bool:
    if not mc_runtime:
        return False
    try:
        db_service = mc_runtime.ensure_db()
    except Exception:
        return False
    if not db_service or not hasattr(db_service, "get_public_products"):
        return False
    try:
        result = await db_service.get_public_products(search=name, limit=1)
    except Exception as exc:
        logger.debug("Product catalog lookup failed: %s", exc)
        return False
    if not isinstance(result, dict):
        return False
    for key in ("items", "data", "results"):
        items = result.get(key)
        if isinstance(items, list) and items:
            return True
    return False


async def _validate_and_sanitize_product(
    product_value: Optional[str], mc_runtime: Optional[MarketRuntime]
) -> Optional[str]:
    if not product_value:
        return None
    candidate = _clean_candidate_text(product_value)
    if not candidate:
        return None
    lowered = candidate.lower()
    if lowered in _GENERIC_PRODUCT_STOPWORDS:
        return None
    if len(candidate) < 2 or len(candidate) > 40:
        return None
    if any(token in lowered for token in _SUSPICIOUS_PRODUCT_TOKENS):
        return None
    if _re.search(r"http[s]?://|www\\.|@|#", lowered):
        return None
    words = lowered.split()
    if any(tok in lowered for tok in _KNOWN_PRODUCT_KEYWORDS):
        return candidate
    if len(words) <= 3:
        return candidate
    has_catalog_match = await _catalog_has_product(candidate, mc_runtime)
    return candidate if has_catalog_match else None


def _sanitize_product_candidate(value: Any) -> Optional[str]:
    candidate = _clean_candidate_text(str(value)) if isinstance(value, str) else None
    if not candidate:
        return None
    lowered = candidate.lower()
    if lowered in _GENERIC_PRODUCT_STOPWORDS:
        return None
    if any(tok in lowered for tok in _KNOWN_PRODUCT_KEYWORDS):
        return candidate
    words = lowered.split()
    if len(words) > 6:
        return None
    if _re.search(r"http[s]?://|www\.|@|#", lowered):
        return None
    if not _re.search(r"[a-zàâçéèêëîïôûùüÿñæœ]", lowered):
        return None
    return candidate


def _remap_entities(raw_entities: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise les clés d'entités extraites par le LLM vers les clés canoniques."""

    canonicalized = normalize_slot_keys({
        _ENTITY_KEY_REMAP.get(str(k).lower().strip(), k): v
        for k, v in (raw_entities or {}).items()
    })
    normalized: Dict[str, Any] = {}
    for key, value in canonicalized.items():
        if not slot_has_value(value):
            continue
        if key in {"price", "quantity"}:
            try:
                clean_str = str(value).replace(",", ".").replace(" ", "").replace("\xa0", "")
                normalized[key] = float(clean_str)
            except (ValueError, TypeError):
                logger.warning("Entity '%s' non numérique: %r — ignoré", key, value)
                continue
        elif key == "selection_index":
            try:
                normalized[key] = int(value)
            except (ValueError, TypeError):
                logger.warning("selection_index non entier: %r — ignoré", value)
                continue
        elif key == "product":
            sanitized = _sanitize_product_candidate(value)
            if sanitized:
                normalized[key] = sanitized
        else:
            normalized[key] = str(value).strip() if isinstance(value, str) else value
    return normalized


def _fallback_quantity_unit_from_text(text: str) -> Optional[Dict[str, Any]]:
    """Capture déterministe d'un motif numérique suivi d'une unité standard."""
    if not text:
        return None
    match = _QUANTITY_UNIT_PATTERN.search(text)
    if not match:
        return None

    qty_raw = (match.group("qty") or "").replace(" ", "").replace("\xa0", "").replace(",", ".")
    try:
        qty_val = float(qty_raw)
    except (ValueError, TypeError):
        return None

    unit_raw = _normalize_unit_token(match.group("unit") or "")
    mapped_unit = _UNIT_SYNONYMS.get(unit_raw)

    result: Dict[str, Any] = {"quantity": qty_val}
    if mapped_unit:
        result["unit"] = mapped_unit
    return result


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
    # (et ne jamais être interprété comme un nouveau nom de produit).
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

    # Fast-path 0ter : escalade explicite "appel" depuis un menu de sélection acheteur.
    # Objectif : éviter le garde-fou (expected_input=SELECTION => UNKNOWN) qui déclenche RECOVERY.
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
    )
    procurement_track_hit = any(token in clean for token in procurement_track_tokens)
    if not procurement_track_hit and "appel" in clean and ("suivre" in clean or "voir" in clean) and "commande" not in clean:
        procurement_track_hit = True
    if role_up == "BUYER" and procurement_track_hit:
        return {
            "interpreted_event": "NEW_TASK",
            "detected_intent": "MARKET_GET_REQUESTS",
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

    # Fast-path 2 : Saisie de valeur numérique directe lors du slot-filling actif (ex: prix ou quantité)
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
        # Only consider onboarding active if is_onboarding is explicitly True.
        # onboarding_step can be stale ("COMPLETED") from metadata — ignore it.
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
        user_prompt = INTERPRETER_USER_PROMPT.format(
            current_goal=state.get("current_goal") or "AUCUN",
            expected_input=expected_input or "NONE",
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

        # Conservation de session : quand on attend un slot (quantité/prix/produit...),
        # un "NEW_TASK" est souvent une réponse partielle (ex: "100kg").
        if expected_input in {"PRODUCT", "PRICE", "QUANTITY", "UNIT", "LOCATION", "DATE"} and raw_event == "NEW_TASK":
            raw_event = "ANSWER"

        # Protection stricte IHM : Si l'utilisateur dévie alors qu'on attend une action binaire/choix de bouton
        if expected_input in {"SELECTION", "CONFIRMATION"} and raw_event not in {"SELECTION", "CONFIRM", "REJECT"}:
            logger.warning("Dérive conversationnelle détectée : attendait %s, utilisateur a dévié.", expected_input)
            raw_event = "UNKNOWN"

        if raw_event not in {"NEW_TASK", "ANSWER", "CONFIRM", "REJECT", "SELECTION", "UPDATE", "INTERRUPTION", "RESUME", "OUT_OF_SCOPE", "UNKNOWN", "ONBOARDING_INPUT"}:
            raw_event = "UNKNOWN"

        raw_intent = str(parsed.get("detected_intent") or "UNKNOWN").upper().strip()
        if raw_intent != "UNKNOWN" and raw_intent not in allowed:
            logger.warning("LLM a retourné une intention hors-périmètre %s pour le rôle %s — ignoré", raw_intent, role_up)
            raw_intent = "UNKNOWN"

        try:
            confidence = max(0.0, min(1.0, float(parsed.get("interpreter_confidence") or 0.0)))
        except (TypeError, ValueError):
            confidence = 0.0

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
# NODE 4 — GOAL PLANNER (Machine à États Pure orientée Formulaires UI)
# =====================================================================

INTENT_TO_GOAL_MAP: Dict[str, str] = {
    intent_key: intent_key for intent_key in PRODUCER_INTENTS | BUYER_INTENTS | COMMON_INTENTS
}

_NAVIGATION_INTENTS = frozenset({
    "BUYER_VIEW_CART",
    "BUYER_LIST_ORDERS",
    "BUYER_CHECK_ORDER_STATUS",
    "BUYER_CANCEL_ORDER",
    "MARKET_GET_REQUESTS",
})


async def goal_planner(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    """Machine à états pure pour la gestion du cycle de vie des intentions."""
    event = str(state.get("interpreted_event") or "UNKNOWN").upper()
    detected_intent = str(state.get("detected_intent") or "UNKNOWN").upper()
    working = state.get("working_memory") or {}
    current_goal = state.get("current_goal") or working.get("active_goal") or working.get("locked_intent")
    expected_input = state.get("expected_input")
    goal_stack = list(state.get("goal_stack") or [])
    in_tunnel = bool(current_goal and expected_input and expected_input != "NONE")
    text = str(state.get("normalized_text") or state.get("user_query") or "").strip().lower()
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
                    text = content.strip().lower()
                    break
    is_short = len(text) <= 4 or text.isdigit() or text in {"oui", "non", "ok", "yes", "no"}

    updates: Dict[str, Any] = {
        "status": "PLANNING",
        "interruption_detected": False,
    }

    logger.info(
        "[GoalPlanner IN] event=%s intent=%s current_goal=%s expected=%s",
        event,
        detected_intent,
        current_goal,
        expected_input,
    )

    def _lock(goal: Optional[str], extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        wm = dict(working)
        if goal and goal != "DISAMBIGUATION_PENDING":
            wm["active_goal"] = goal
            wm["locked_intent"] = goal
            wm.setdefault("step_index", 0)
        if extra:
            wm.update(extra)
        return wm

    def _clear_goal_lock() -> Dict[str, Any]:
        """Conserve les caches, supprime uniquement les verrous de tunnel/UI."""
        wm = dict(working)
        for k in (
            "active_goal",
            "locked_intent",
            "step_index",
            "available_mapping_kind",
            "auction_menu",
            "bids_menu",
            "stocks_menu",
            "generic_menu",
            "disambiguation_pending",
            "disambiguation_trigger_id",
        ):
            wm.pop(k, None)
        return wm

    def _purge_transaction_state() -> Dict[str, Any]:
        """Purge totale des champs transactionnels/AG-UI lors d'un switch d'intention."""
        return {
            "transaction_payload": {"__reset__": True},
            "draft_payload": {"__reset__": True},
            "stable_entities": {"__reset__": True},
            "missing_fields": [],
            "completed_fields": [],
            "last_missing_field": None,
            "expected_input": "NONE",
            "expected_candidates": [],
            "available_mapping": {},
            "waiting_for_confirmation": False,
            "confirmation_summary": None,
            "selected_tool": None,
            "selected_tool_args": {"__reset__": True},
            "execution_result": {"__reset__": True},
            "retry_count": 0,
            "vendor_selection_context": {"__reset__": True},
            "negotiation_context": {"__reset__": True},
            "preorder_workflow": {"__reset__": True},
        }

    def _with_goal_metadata(payload: Dict[str, Any], goal_hint: Optional[str] = None) -> Dict[str, Any]:
        target_goal = goal_hint
        if target_goal is None:
            target_goal = payload.get("current_goal")
            if not target_goal:
                wm_payload = payload.get("working_memory") or {}
                target_goal = (
                    wm_payload.get("active_goal")
                    or wm_payload.get("locked_intent")
                    or state.get("current_goal")
                )
        goal_key = str(target_goal or "").upper().strip()
        cfg = INTENT_CONFIG.get(goal_key, {})
        lifecycle = str(cfg.get("lifecycle_mode") or "").upper().strip()
        if lifecycle not in {"CREATE", "UPDATE", "READ"}:
            lifecycle = "READ"
        payload["goal_metadata"] = {
            "lifecycle_mode": lifecycle,
            "update_mode": lifecycle == "UPDATE",
        }
        return payload

    # RÈGLE 0bis — RÉSOLUTION DE DÉSAMBIGUÏSATION
    # Si l'utilisateur a sélectionné dans le menu posé par semantic_disambiguation,
    # on lit la sélection directement depuis state.extracted_entities +
    # state.available_mapping (goal_planner s'exécute AVANT memory_update).
    if current_goal == "DISAMBIGUATION_PENDING":
        override_goal = INTENT_TO_GOAL_MAP.get(detected_intent)
        if event in {"INTERRUPTION", "NEW_TASK"} and override_goal and override_goal != "DISAMBIGUATION_PENDING":
            logger.info(
                "[Disambiguation Override] event=%s intent=%s -> current_goal=%s",
                event,
                detected_intent,
                override_goal,
            )
            return _with_goal_metadata({
                "status": "PLANNING",
                "current_goal": override_goal,
                "goal_status": "ACTIVE",
                "interruption_detected": event == "INTERRUPTION",
                "working_memory": {
                    **_lock(override_goal),
                    "disambiguation_pending": False,
                    "available_mapping_kind": None,
                },
                **_purge_transaction_state(),
            }, override_goal)

        extracted = state.get("extracted_entities") or {}
        mapping = dict(state.get("available_mapping") or {})

        trigger_id = str((state.get("working_memory") or {}).get("disambiguation_trigger_id") or "").strip()
        if trigger_id:
            entry = INTENT_DISAMBIGUATION.get(trigger_id)
            if entry:
                rebuilt: Dict[str, str] = {}
                for i, opt in enumerate(entry.get("options") or [], start=1):
                    intent_key = None
                    if isinstance(opt, (tuple, list)) and len(opt) >= 2:
                        intent_key = opt[0]
                    elif isinstance(opt, dict):
                        intent_key = opt.get("intent")
                    if intent_key:
                        rebuilt[str(i)] = str(intent_key)
                if rebuilt:
                    if mapping and mapping != rebuilt:
                        logger.warning(
                            "[Disambiguation] stale mapping overridden by trigger=%s",
                            trigger_id,
                        )
                    mapping = rebuilt
                    logger.info(
                        "[Disambiguation] rebuilt mapping from trigger=%s with %d options",
                        trigger_id,
                        len(mapping),
                    )
                        
        sel_idx = extracted.get("selection_index")
        sel_val = extracted.get("selected_value")

        resolved_intent: Optional[str] = None
        if sel_idx is not None:
            resolved_intent = mapping.get(str(sel_idx))
        if resolved_intent is None and sel_val is not None:
            resolved_intent = mapping.get(str(sel_val))

        if (
            resolved_intent is None
            and detected_intent in INTENT_TO_GOAL_MAP
            and detected_intent not in {"", "UNKNOWN", "DISAMBIGUATION_PENDING"}
        ):
            resolved_intent = detected_intent

        if resolved_intent and resolved_intent in INTENT_TO_GOAL_MAP:
            logger.info(
                "[Disambiguation Resolved] selection=%s promoted to current_goal=%s",
                sel_idx or sel_val, resolved_intent,
            )
            return _with_goal_metadata({
                "status": "PLANNING",
                "current_goal": resolved_intent,
                "goal_status": "ACTIVE",
                "interruption_detected": False,
                "expected_input": "NONE",
                "expected_candidates": [],
                "available_mapping": {},
                "missing_fields": [],
                "completed_fields": [],
                "working_memory": {
                    **_lock(resolved_intent),
                    "disambiguation_pending": False,
                    "available_mapping_kind": None,
                },
            }, resolved_intent)
        # Sélection invalide ou pas encore reçue : on garde le menu actif.
        updates["current_goal"] = "DISAMBIGUATION_PENDING"
        updates["goal_status"] = "WAITING_INPUT"
        updates["expected_input"] = "SELECTION"
        updates["waiting_for_confirmation"] = False
        updates["confirmation_summary"] = None
        updates["response_strategy"] = "SELECTION_MENU"
        updates["working_memory"] = {
            **dict(working),
            "disambiguation_pending": True,
            "available_mapping_kind": "intent_disambiguation",
        }
        return _with_goal_metadata(updates)

    # RÈGLE 1 — CANCEL/REJECT (Annulation explicite)
    # Si l'utilisateur rejette en dehors du contexte de confirmation, on purge.
    if event == "REJECT":
        waiting_confirm = bool(state.get("waiting_for_confirmation") or str(expected_input or "").upper() == "CONFIRMATION")
        if not waiting_confirm:
            return _with_goal_metadata({
                "status": "WAITING_INPUT",
                "current_goal": None,
                "goal_status": "IDLE",
                "interruption_detected": False,
                "response_strategy": "CLARIFICATION",
                "working_memory": _clear_goal_lock(),
                **_purge_transaction_state(),
            })

        # Rejet pendant confirmation : laisser confirmation_gate gérer la logique.
        updates["current_goal"] = current_goal
        updates["goal_status"] = "ACTIVE"
        updates["working_memory"] = _lock(current_goal)
        return _with_goal_metadata(updates)

    # RÈGLE 1bis — TUNNEL LOCKING (Maintien des formulaires d'IHM)
    if event in {"CONFIRM", "SELECTION", "ANSWER", "UPDATE"}:
        updates["current_goal"] = current_goal
        updates["goal_status"] = "ACTIVE"
        updates["working_memory"] = _lock(current_goal)
        return _with_goal_metadata(updates)

    # RÈGLE 1ter — PERSISTENCE PAR DÉFAUT
    # Si aucune nouvelle intention fiable n'est détectée, on reste verrouillé sur le tunnel courant.
    if current_goal and detected_intent == "UNKNOWN" and event in {"UNKNOWN", "NEW_TASK"}:
        updates["current_goal"] = current_goal
        updates["detected_intent"] = str(current_goal).upper()
        updates["goal_status"] = "ACTIVE" if expected_input in (None, "", "NONE") else "WAITING_INPUT"
        updates["working_memory"] = _lock(current_goal)
        return _with_goal_metadata(updates)

    # RÈGLE 1quater — VERROUILLAGE PENDANT SLOT-FILLING
    # Tant qu'on attend explicitement une information utilisateur, on n'autorise pas un switch
    # d'intention via NEW_TASK (sauf via event=INTERRUPTION).
    if current_goal and event == "NEW_TASK" and expected_input not in (None, "", "NONE"):
        nav_goal = INTENT_TO_GOAL_MAP.get(detected_intent)
        if nav_goal in _NAVIGATION_INTENTS:
            updates["current_goal"] = nav_goal
            updates["goal_status"] = "ACTIVE"
            updates["working_memory"] = _lock(nav_goal)
            updates.update(_purge_transaction_state())
            logger.info("[GoalPlanner] Navigation override vers %s", nav_goal)
            return _with_goal_metadata(updates)

        updates["current_goal"] = current_goal
        updates["detected_intent"] = str(current_goal).upper()
        updates["goal_status"] = "WAITING_INPUT"
        updates["working_memory"] = _lock(current_goal)
        return _with_goal_metadata(updates)

    if is_short and current_goal:
        updates["current_goal"] = current_goal
        updates["detected_intent"] = str(current_goal).upper()
        updates["goal_status"] = "ACTIVE" if expected_input in {None, "NONE"} else "WAITING_INPUT"
        updates["working_memory"] = _lock(current_goal)
        return _with_goal_metadata(updates)

    # RÈGLE 2 — POLLUTION PENDANT SLOT-FILLING
    if event == "UNKNOWN" and in_tunnel:
        updates["current_goal"] = current_goal
        updates["goal_status"] = "WAITING_INPUT"
        updates["detected_intent"] = str(current_goal).upper()
        updates["working_memory"] = _lock(current_goal)
        return _with_goal_metadata(updates)

    # RÈGLE 3 — OUT_OF_SCOPE
    if event == "OUT_OF_SCOPE":
        updates["current_goal"] = current_goal
        updates["goal_status"] = "ACTIVE" if current_goal else "IDLE"
        updates["response_strategy"] = "CLARIFICATION"
        updates["status"] = "WAITING_INPUT"
        updates["working_memory"] = _lock(current_goal)
        return _with_goal_metadata(updates)

    # RÈGLE 4 — INTERRUPTION (Suspension d'un workflow IHM en cours)
    if event == "INTERRUPTION":
        new_goal = INTENT_TO_GOAL_MAP.get(detected_intent)
        if new_goal and new_goal != current_goal:
            if current_goal:
                goal_stack.append(current_goal)
            return _with_goal_metadata({
                "status": "PLANNING",
                "current_goal": new_goal,
                "goal_stack": goal_stack,
                "goal_status": "ACTIVE",
                "interruption_detected": True,
                "suspended_goal": current_goal,
                "suspended_payload": state.get("transaction_payload") or {},
                **_purge_transaction_state(),
                "working_memory": _lock(new_goal),
            }, new_goal)
        updates["current_goal"] = current_goal
        updates["response_strategy"] = "CLARIFICATION"
        updates["status"] = "WAITING_INPUT"
        updates["goal_status"] = "ACTIVE" if current_goal else "IDLE"
        updates["working_memory"] = _lock(current_goal)
        return _with_goal_metadata(updates)

    # RÈGLE 4bis — RESUME (Reprise d'une tâche suspendue dans goal_stack)
    if event == "RESUME":
        if goal_stack:
            resumed = goal_stack.pop()
            return _with_goal_metadata({
                "status": "PLANNING",
                "current_goal": resumed,
                "goal_stack": goal_stack,
                "goal_status": "ACTIVE",
                "interruption_detected": False,
                "suspended_goal": None,
                "transaction_payload": state.get("suspended_payload") or {},
                "suspended_payload": {},
                "stable_entities": {},
                "missing_fields": [],
                "completed_fields": [],
                "last_missing_field": None,
                "expected_input": "NONE",
                "expected_candidates": [],
                "available_mapping": {},
                "waiting_for_confirmation": False,
                "confirmation_summary": None,
                "working_memory": _lock(resumed),
            }, resumed)
        # Aucun goal suspendu — traiter comme clarification
        updates["current_goal"] = current_goal
        updates["response_strategy"] = "CLARIFICATION"
        updates["status"] = "WAITING_INPUT"
        updates["goal_status"] = "ACTIVE" if current_goal else "IDLE"
        updates["working_memory"] = _lock(current_goal)
        return _with_goal_metadata(updates)

    # RÈGLE 5 — NEW_TASK (Instanciation et purge des champs AG-UI)
    if event == "NEW_TASK":
        new_goal = INTENT_TO_GOAL_MAP.get(detected_intent)
        if new_goal:
            updates["current_goal"] = new_goal
            updates["goal_status"] = "ACTIVE"
            updates["working_memory"] = _lock(new_goal)
            if current_goal and new_goal != current_goal:
                updates.update(_purge_transaction_state())
            logger.info("[GoalPlanner OUT] new_goal=%s", new_goal)
            logger.debug("[GoalPlanner OUT] new_goal=%s", new_goal)
            return _with_goal_metadata(updates)

    # Fallback par défaut vers clarification
    updates["current_goal"] = current_goal
    updates["response_strategy"] = "CLARIFICATION"
    updates["status"] = "WAITING_INPUT"
    updates["goal_status"] = "ACTIVE" if current_goal else "IDLE"
    if current_goal:
        updates["detected_intent"] = str(current_goal).upper()
    updates["working_memory"] = _lock(current_goal)
    return _with_goal_metadata(updates)


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

        # 2B. MENU DE SÉLECTION (sans missing_fields) — ex: choix ferme / enchère / stock
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
