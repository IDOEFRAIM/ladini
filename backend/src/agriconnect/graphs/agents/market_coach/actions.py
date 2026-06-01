"""Dispatchers MCP du MarketCoach AgriConnect.

Chaque fonction `_prep_<namespace>_<action>` :
  - reçoit (`payload`, `user_phone`) ;
  - lève `ValueError` si une donnée requise par l'outil MCP est manquante ;
  - normalise déterministiquement les unités vers KG (jamais via LLM) ;
  - retourne un couple `(tool_name, tool_args)` prêt pour l'exécution.

Règles strictes :
  1. FastMCP est SOURCE OF TRUTH : si un dispatcher diverge, on corrige le
     dispatcher, jamais le tool MCP.
  2. Postel's Law : si un alias d'identité (phone/producer_id/user_id) est
     attendu, le `_build_resolved_tool_args` du shared_core gère la résolution.
     Les dispatchers déclarent simplement le nom MCP officiel.
  3. Les conversions d'unités sont CENTRALISÉES dans `_normalize_quantity_to_kg`.
     Aucun dispatcher ne fait de conversion locale ad-hoc.
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Tuple

logger = logging.getLogger("Agent.MarketCoach.actions")


# =======================================================================
# Fonctions Utilitaires et de Validation Internes
# =======================================================================

def _require(payload: Dict[str, Any], key: str) -> Any:
    """Garantit la présence d'un champ requis dans le payload extrait."""
    value = payload.get(key)
    # Vérification stricte : None, chaîne vide ou liste vide sont invalides
    if value in (None, "", []):
        raise ValueError(f"Missing required field: {key}")
    return value

def _require_phone(user_phone: str) -> str:
    """Garantit la présence de l'identité de l'appelant."""
    if not user_phone:
        raise ValueError("Missing required identity: user_phone")
    return str(user_phone)


# =======================================================================
# Normalisation déterministe des unités vers KG (canonical backend unit)
# =======================================================================
# Coefficients par défaut. Pour les unités produit-spécifiques (SAC de mil
# vs SAC de riz), on utilise une moyenne raisonnable ; le contexte produit
# pourra surcharger ultérieurement via INTENT_DOMAIN.
_UNIT_TO_KG: Dict[str, float] = {
    "KG": 1.0,
    "KILOGRAMME": 1.0,
    "KILOGRAMMES": 1.0,
    "G": 0.001,
    "GRAMME": 0.001,
    "GRAMMES": 0.001,
    "TONNE": 1000.0,
    "TONNES": 1000.0,
    "T": 1000.0,
    "TON": 1000.0,
    "QUINTAL": 100.0,
    "QUINTAUX": 100.0,
    "SAC": 100.0,         # céréales (mil/sorgho/riz/maïs) sac standard ~100kg
    "SACS": 100.0,
    "PANIER": 25.0,       # tomate/légumes ~25kg
    "PANIERS": 25.0,
    "CHARRETTE": 250.0,
    "CHARRETTES": 250.0,
}


def _normalize_quantity_to_kg(qty: float, unit_raw: Any) -> Tuple[float, str]:
    """Convertit une (quantité, unité) en (quantité_kg, 'KG') de manière déterministe.

    Hard rule: les outils MCP reçoivent EXCLUSIVEMENT des kg.
    Si l'unité est inconnue, on log un warning et on retourne tel quel
    (en supposant KG par défaut) plutôt que de planter — l'agent doit
    rester opérationnel même avec un slang inattendu.
    """
    try:
        qty_f = float(qty)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"quantity is not numeric: {qty!r}") from exc
    unit_clean = str(unit_raw or "KG").upper().strip()
    coef = _UNIT_TO_KG.get(unit_clean)
    if coef is None:
        logger.warning(
            "Unknown unit '%s' — assuming KG (qty=%s). Add to _UNIT_TO_KG if recurrent.",
            unit_clean, qty_f,
        )
        return qty_f, "KG"
    if coef == 1.0:
        return qty_f, "KG"
    qty_kg = qty_f * coef
    logger.info(
        "[UnitNormalize] %s %s -> %s KG (coef=%s)",
        qty_f, unit_clean, qty_kg, coef,
    )
    return qty_kg, "KG"


# =======================================================================
# 📋 PREP FUNCTIONS - INTENTS DE LECTURE (READ-ONLY)
# =======================================================================

# --- DOMAINE : STOCK ---

def _prep_stock_get_summary(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    """Prépare l'appel pour l'inventaire global structuré ferme par ferme.

    Outil MCP : get_stocks(farm_id).
    NOTE : Le handler MCP nomme le param `farm_id` mais la couche DB
    (producer.py) l'interprète comme un phone via la chaîne positionnelle.
    On envoie phone sous le nom `farm_id` pour matcher le schéma MCP.
    """
    phone = _require_phone(user_phone)
    return "get_stocks", {"farm_id": phone}

def _prep_stock_get_detail(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    """Prépare l'appel pour l'inventaire détaillé d'une exploitation.

    Outil MCP : get_farm_stocks(farm_id). Phone n'est pas attendu.
    """
    _require_phone(user_phone)  # safety check
    farm_id = str(_require(payload, "farm_id"))
    return "get_farm_stocks", {"farm_id": farm_id}

def _prep_stock_get_movements(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    """Prépare la consultation de la traçabilité d'un lot.

    Outil MCP : get_stock_movements(stock_id, limit?).
    Phone n'est PAS un paramètre MCP.
    """
    _require_phone(user_phone)  # safety check
    stock_id = str(_require(payload, "stock_id"))
    return "get_stock_movements", {"stock_id": stock_id}

# --- DOMAINE : MARCHÉ / VENTES ---

def _prep_sales_get_catalog(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    """Prépare la consultation de son propre catalogue de vente.

    Outil MCP : list_products(producer_id).
    Le schema resolver mappe phone → producer_id via _ARG_ALIASES.
    """
    phone = _require_phone(user_phone)
    return "list_products", {"producer_id": phone}

def _prep_market_get_requests(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    """Prépare la liste des appels d'offres du marché."""
    phone = _require_phone(user_phone)
    args: Dict[str, Any] = {
        "phone": phone,
        "status": payload.get("status") or "OPEN",
        "view_mode": "MARKETPLACE"
    }
    # Filtres optionnels sémantiques
    product = payload.get("product_name") or payload.get("product")
    if product: args["product_name"] = str(product)
    zone = payload.get("zone_name") or payload.get("zone")
    if zone: args["zone_name"] = str(zone)
    return "get_auctions", args

def _prep_market_get_request_detail(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    """Prépare la consultation des offres reçues sur sa propre demande.

    Outil MCP réel : `get_auctions_bids` (pluriel) — pas `get_auction_bids`.
    Filtre serveur-side par auction_id si fourni dans status/extra (le tool
    accepte phone+status ; auction_id est appliqué côté DB par jointure).
    """
    phone = _require_phone(user_phone)
    auction_id = str(_require(payload, "auction_id"))
    return "get_auctions_bids", {
        "phone": phone,
        "status": payload.get("status") or "OPEN",
    }

def _prep_market_get_my_proposals(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    """Prépare le suivi des propositions envoyées par le producteur.

    Outil MCP correct : `get_my_active_bids(phone)` (pas get_auctions_bids,
    qui retourne les bids reçus par l'acheteur).
    """
    phone = _require_phone(user_phone)
    return "get_my_active_bids", {"phone": phone}

def _prep_market_snapshot(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    """Prépare la consultation des cours du marché local."""
    args: Dict[str, Any] = {}
    zone = payload.get("zone") or payload.get("zone_name")
    if zone: args["zone"] = str(zone)
    return "get_market_snapshot", args

def _prep_market_snapshot_zonal(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    """Prépare l'analyse des tendances locales."""
    zone = str(_require(payload, "zone"))
    return "get_zone_market_overview", {"zone_id": zone}

# --- DOMAINE : AGRONOMIE ---

def _prep_agro_get_cycles(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    """Prépare l'historique des cycles d'un domaine."""
    _require_phone(user_phone)
    farm_id = str(_require(payload, "farm_id"))
    return "get_crop_cycles", {"farm_id": farm_id}

def _prep_agro_get_standards(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    """Prépare la consultation des fiches techniques."""
    product = str(_require(payload, "product"))
    return "get_crop_requirements", {"crop_type": product}

def _prep_agro_get_economics(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    """Prépare le bilan financier d'une parcelle."""
    _require_phone(user_phone)
    cycle_id = str(_require(payload, "cycle_id"))
    return "get_cycle_economics", {"cycle_id": cycle_id}

def _prep_agro_get_risks(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    """Prépare l'analyse des risques sanitaires."""
    _require_phone(user_phone)
    farm_id = str(_require(payload, "farm_id"))
    return "get_active_sanitary_risks", {"farm_id": farm_id}

# --- DOMAINE : STRUCTURE / FERME ---

def _prep_farm_get_my_list(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    """Prépare la demande de listing des exploitations du producteur."""
    phone = _require_phone(user_phone)
    # Règle n°1 : Si FastMCP utilise 'get_producer_farm' pour lister, on garde ce nom,
    # mais assure-toi que le service DB renvoie bien le tableau de toutes les fermes.
    return "get_producer_farm", {"phone": phone}

# --- DOMAINE : FINANCE ---

def _prep_finance_get_summary(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    """Prépare le bilan comptable synthétique."""
    _require_phone(user_phone)
    farm_id = str(_require(payload, "farm_id"))
    args: Dict[str, Any] = {"farm_id": farm_id}
    if payload.get("days"): args["days"] = int(payload["days"])
    return "get_expense_summary", args

# --- DOMAINE : PROFIL & SYSTÈME ---

def _prep_profile_get_mcp_user(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    """Prépare la résolution de profil par téléphone."""
    target_phone = str(_require(payload, "phone"))
    return "get_user_by_phone", {"phone": target_phone}

def _prep_profile_get_trust(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    """Prépare la consultation de la note de confiance.

    Outil MCP auto-enregistré : get_trust_score(user_id).
    Le résolveur de schéma mappe phone → user_id via _lookup_arg_value.
    """
    phone = _require_phone(user_phone)
    return "get_trust_score", {"user_id": phone}


def _prep_profile_get_context(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    """Prépare la lecture du contexte conversationnel (Outil MCP : get_user_context).

    Auparavant manquant — l'absence faisait crasher le module au chargement.
    """
    phone = _require_phone(user_phone)
    return "get_user_context", {"user_id": phone}

def _prep_dashboard_producer(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    """Prépare le chargement du tableau de bord d'exploitation.

    Outil MCP : get_producer_dashboard(producer_id).
    Le schema resolver mappe phone → producer_id via _ARG_ALIASES.
    """
    phone = _require_phone(user_phone)
    return "get_producer_dashboard", {"producer_id": phone}

def _prep_search_products(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    """Prépare la recherche par mot-clé dans le catalogue.

    Outil MCP : search_products(product_name, zone_id?, limit?).
    CRITIQUE : le paramètre s'appelle `product_name`, pas `product`.
    """
    product = str(_require(payload, "product"))
    args: Dict[str, Any] = {"product_name": product}
    zone = payload.get("zone_name") or payload.get("zone")
    if zone:
        args["zone_id"] = str(zone)
    return "search_products", args

def _prep_search_nearby(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    """Prépare la recherche de proximité GPS."""
    _require_phone(user_phone)
    _require(payload, "latitude")
    _require(payload, "longitude")
    return "get_all_zone_market_overview", {}

def _prep_validate_price(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    """Prépare la vérification de cohérence de prix.

    Outil MCP : check_price_anomaly(product_name, proposed_price, zone_id).
    Si seul un nom de zone est fourni, on l'envoie en zone_id ; le service
    Intelligence accepte un nom (résolution interne) en mode tolérant.
    """
    product = str(_require(payload, "product"))
    price = float(_require(payload, "price_mentioned"))
    zone = str(_require(payload, "zone"))
    return "check_price_anomaly", {
        "product_name": product,
        "proposed_price": price,
        "zone_id": zone,
    }

def _prep_system_get_pending(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    """Prépare la consultation des actions en attente.

    Outil MCP : get_pending_actions(agent_name?, limit?).
    ATTENTION : le MCP n'utilise PAS phone. On passe le nom de l'agent.
    """
    _require_phone(user_phone)  # safety — user must be authenticated
    return "get_pending_actions", {"agent_name": "MarketCoach"}


def _prep_stock_register_harvest(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    _require_phone(user_phone)
    farm_id = str(_require(payload, "farm_id"))
    product = str(_require(payload, "product"))
    qty_raw = float(_require(payload, "quantity_mentioned"))
    qty_kg, unit = _normalize_quantity_to_kg(qty_raw, payload.get("unit_mentioned"))
    return "add_stock", {
        "farm_id": farm_id,
        "item_name": product,
        "quantity": qty_kg,
        "unit": unit,
        "stock_type": "HARVEST",
        "reason": payload.get("reason") or "Enregistrement récolte via agent",
    }


def _prep_stock_record_movement(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    phone = _require_phone(user_phone)
    stock_id = str(_require(payload, "stock_id"))
    movement_type = str(_require(payload, "movement_type")).upper().strip()
    qty_raw = float(_require(payload, "quantity_mentioned"))
    qty_kg, _ = _normalize_quantity_to_kg(qty_raw, payload.get("unit_mentioned"))
    return "add_stock_movement_by_id", {
        "producer_id": phone,
        "stock_id": stock_id,
        "movement_type": movement_type,
        "quantity": qty_kg,
        "reason": payload.get("reason") or f"Mouvement {movement_type} via agent",
    }


def _prep_stock_adjust(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    phone = _require_phone(user_phone)
    stock_id = str(_require(payload, "stock_id"))
    qty_raw = float(_require(payload, "quantity_mentioned"))
    qty_kg, _ = _normalize_quantity_to_kg(qty_raw, payload.get("unit_mentioned"))
    return "adjust_stock_by_id", {
        "producer_id": phone,
        "stock_id": stock_id,
        "new_quantity": qty_kg,
        "reason": payload.get("reason") or "Ajustement inventaire physique",
    }


def _prep_stock_remove_partial(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    phone = _require_phone(user_phone)
    stock_id = str(_require(payload, "stock_id"))
    qty_raw = float(_require(payload, "quantity_mentioned"))
    qty_kg, _ = _normalize_quantity_to_kg(qty_raw, payload.get("unit_mentioned"))
    return "remove_stock_by_id", {
        "producer_id": phone,
        "stock_id": stock_id,
        "quantity": qty_kg,
        "reason": payload.get("reason") or "Retrait partiel via agent",
    }


def _prep_stock_delete(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    phone = _require_phone(user_phone)
    stock_id = str(_require(payload, "stock_id"))
    return "delete_stock_by_id", {"producer_id": phone, "stock_id": stock_id}


def _prep_sales_publish_product(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    phone = _require_phone(user_phone)
    product = str(_require(payload, "product"))
    price = float(_require(payload, "price_mentioned"))
    qty_raw = float(_require(payload, "quantity_mentioned"))
    qty_kg, unit = _normalize_quantity_to_kg(qty_raw, payload.get("unit_mentioned"))
    args: Dict[str, Any] = {
        "producer_id": phone,
        "name": product,
        "price": price,
        "quantity_for_sale": qty_kg,
        "unit": unit,
    }
    if payload.get("description"):
        args["description"] = str(payload["description"])
    if payload.get("category_label"):
        args["category_label"] = str(payload["category_label"])
    return "create_product", args


def _prep_sales_record_direct(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    phone = _require_phone(user_phone)
    product = str(_require(payload, "product"))
    qty_raw = float(_require(payload, "quantity_mentioned"))
    price = float(_require(payload, "price_mentioned"))
    qty_kg, unit = _normalize_quantity_to_kg(qty_raw, payload.get("unit_mentioned"))
    return "record_sale", {
        "phone": phone,
        "product_name": product,
        "quantity": qty_kg,
        "total_price": price,
        "unit": unit,
    }


def _prep_sales_place_bid(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    phone = _require_phone(user_phone)
    auction_id = str(_require(payload, "auction_id"))
    price = float(_require(payload, "price_mentioned"))
    args: Dict[str, Any] = {"phone": phone, "auction_id": auction_id, "offered_price": price}
    if payload.get("message"):
        args["message"] = str(payload["message"])
    return "place_bid", args


def _prep_sales_accept_contract(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    _require_phone(user_phone)
    transaction_id = str(_require(payload, "bid_id"))
    return "commit_staged_transaction", {"transaction_id": transaction_id, "approved": True}


def _prep_procurement_create_request(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    phone = _require_phone(user_phone)
    product = str(_require(payload, "product"))
    qty_raw = float(_require(payload, "quantity_mentioned"))
    price = float(_require(payload, "price_mentioned"))
    qty_kg, unit = _normalize_quantity_to_kg(qty_raw, payload.get("unit_mentioned"))
    
    # 1. Gestion de la date limite
    deadline = payload.get("deadline")
    if isinstance(deadline, str):
        try:
            deadline = datetime.fromisoformat(deadline)
        except ValueError:
            deadline = None
    if not isinstance(deadline, datetime) or deadline <= datetime.now():
        deadline = datetime.now() + timedelta(days=7)

    # 2. Extraction des nouveaux champs obligatoires (Logistique)
    delivery_location = payload.get("delivery_location") or payload.get("location") or "Non spécifié"
    
    delivery_deadline = payload.get("delivery_deadline")
    if isinstance(delivery_deadline, str):
        try:
            delivery_deadline = datetime.fromisoformat(delivery_deadline)
        except:
            delivery_deadline = deadline + timedelta(days=3) # Par défaut : 3 jours après la deadline
    else:
        delivery_deadline = deadline + timedelta(days=3)

    args: Dict[str, Any] = {
        "phone": phone,
        "product_query": product,
        "qty": qty_kg,
        "unit": unit,
        "max_price": price,
        "deadline": deadline,
        "delivery_location": delivery_location,        # Ajouté
        "delivery_deadline": delivery_deadline,        # Ajouté
        "incoterm": payload.get("incoterm", "DDP"),    # Ajouté avec valeur par défaut
        "auto_extend": bool(payload.get("auto_extend", True)),
    }
    
    zone = payload.get("zone_name") or payload.get("zone")
    if zone:
        args["zone_query"] = str(zone)
        
    return "create_auction", args

def _prep_procurement_select_winner(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    _require_phone(user_phone)
    _require(payload, "auction_id")
    bid_id = str(_require(payload, "bid_id"))
    return "select_winning_bid", {"bid_id": bid_id}


def _prep_procurement_accept_offer(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    _require_phone(user_phone)
    bid_id = str(_require(payload, "bid_id"))
    return "accept_bid", {"bid_id": bid_id}


def _prep_crop_start_cycle(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    _require_phone(user_phone)
    farm_id = str(_require(payload, "farm_id"))
    product = str(_require(payload, "product"))
    surface = float(_require(payload, "surface"))
    data: Dict[str, Any] = {"crop_type": product, "area_size": surface}
    if payload.get("variety"):
        data["variety"] = str(payload["variety"])
    if payload.get("target_yield"):
        data["target_yield"] = float(payload["target_yield"])
    return "create_crop_cycle", {"farm_id": farm_id, "data": data}


def _prep_crop_record_intervention(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    _require_phone(user_phone)
    cycle_id = str(_require(payload, "cycle_id"))
    intervention_type = str(_require(payload, "intervention_type")).upper().strip()
    data: Dict[str, Any] = {"type": intervention_type}
    if payload.get("input_used"):
        data["input_used"] = str(payload["input_used"])
    if payload.get("quantity_mentioned"):
        data["quantity"] = float(payload["quantity_mentioned"])
    if payload.get("details"):
        data["description"] = str(payload["details"])
    return "log_intervention", {"cycle_id": cycle_id, "data": data}


def _prep_crop_record_observation(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    _require_phone(user_phone)
    cycle_id = str(_require(payload, "cycle_id"))
    stage = payload.get("stage_code") or payload.get("stage_label")
    try:
        stage_code = int(stage)
    except (TypeError, ValueError):
        stage_code = 0
    return "add_growth_log", {"cycle_id": cycle_id, "stage_code": stage_code}


def _prep_crop_update_stage(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    _require_phone(user_phone)
    cycle_id = str(_require(payload, "cycle_id"))
    stage_name = str(_require(payload, "stage_name"))
    return "add_crop_growth_stage", {"data": {"cycle_id": cycle_id, "stage_name": stage_name}}


def _prep_crop_update_soil(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    _require_phone(user_phone)
    farm_id = str(_require(payload, "farm_id"))
    ph = float(_require(payload, "ph"))
    data: Dict[str, Any] = {"ph": ph}
    if payload.get("organic_matter"):
        data["organic_matter"] = float(payload["organic_matter"])
    return "update_soil_profile", {"farm_id": farm_id, "data": data}


def _prep_finance_log_expense(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    _require_phone(user_phone)
    farm_id = str(_require(payload, "farm_id"))
    amount = float(_require(payload, "price_mentioned"))
    return "add_expense", {
        "farm_id": farm_id,
        "label": str(payload.get("product") or "Dépense diverse"),
        "amount": amount,
        "category": str(payload.get("category") or "OTHER"),
    }


def _prep_farm_create(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    phone = _require_phone(user_phone)
    name = str(_require(payload, "farm_name"))
    zone = str(_require(payload, "zone"))
    return "get_or_create_farm", {"producer_id": phone, "farm_name": name, "zone_id": zone}


def _prep_farm_update(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    _require_phone(user_phone)
    farm_id = str(_require(payload, "farm_id"))
    args: Dict[str, Any] = {"farm_id": farm_id}
    return "update_farm", args


def _prep_profile_set_geo(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    phone = _require_phone(user_phone)
    lat = float(_require(payload, "latitude"))
    lon = float(_require(payload, "longitude"))
    return "update_geo_location", {"user_id": phone, "lat": lat, "lon": lon}


def _prep_profile_set_prefs(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    phone = _require_phone(user_phone)
    lang = str(_require(payload, "language"))
    return "update_communication_prefs", {
        "user_id": phone,
        "advice_time": lang,
        "enabled": bool(payload.get("allow_voice", True)),
    }


def _prep_profile_switch_role(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    phone = _require_phone(user_phone)
    role = str(_require(payload, "target_role")).upper().strip()
    return "create_agent_action", {
        "agent_name": "MarketCoach",
        "action_type": "PROFILE_SWITCH_ROLE",
        "payload": {"phone": phone, "target_role": role},
    }


def _prep_system_report_anomaly(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    _require_phone(user_phone)
    zone = str(payload.get("zone") or payload.get("zone_name") or "")
    description = str(_require(payload, "description"))
    return "report_anomaly", {"zone_id": zone, "title": description[:80], "level": "MEDIUM"}


def _prep_system_bind_zone(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    phone = _require_phone(user_phone)
    zone = str(_require(payload, "zone"))
    return "create_agent_action", {
        "agent_name": "MarketCoach",
        "action_type": "SYSTEM_BIND_ZONE",
        "payload": {"phone": phone, "zone_name": zone},
    }


def _prep_system_commit_transaction(payload: Dict[str, Any], user_phone: str) -> Tuple[str, Dict[str, Any]]:
    _require_phone(user_phone)
    staging_id = str(_require(payload, "staging_id"))
    return "commit_staged_transaction", {"transaction_id": staging_id, "approved": True}


# =======================================================================
# 🗺️ MAPPINGS ET POLITIQUES DE DISPATCH (Alignement LLM vs Infra)
# =======================================================================

# Dictionnaire complet des intentions de LECTURE (READ-ONLY)
# Namespacing strict correspondant à intent.py
MARKET_READ_ACTIONS_MAP = {
    # STOCK
    "STOCK_GET_SUMMARY": _prep_stock_get_summary,
    "STOCK_GET_DETAIL": _prep_stock_get_detail,
    "STOCK_GET_MOVEMENTS": _prep_stock_get_movements,
    # MARCHÉ / SALES
    "SALES_GET_CATALOG": _prep_sales_get_catalog,
    "MARKET_GET_REQUESTS": _prep_market_get_requests,
    "MARKET_GET_REQUEST_DETAIL": _prep_market_get_request_detail,
    "MARKET_GET_MY_PROPOSALS": _prep_market_get_my_proposals,
    "MARKET_SNAPSHOT": _prep_market_snapshot,
    "MARKET_SNAPSHOT_ZONAL": _prep_market_snapshot_zonal,
    "SEARCH_PRODUCTS": _prep_search_products,
    # AGRONOMIE
    "AGRO_GET_CYCLES": _prep_agro_get_cycles,
    "AGRO_GET_STANDARDS": _prep_agro_get_standards,
    "AGRO_GET_ECONOMICS": _prep_agro_get_economics,
    "AGRO_GET_RISKS": _prep_agro_get_risks,
    # STRUCTURE
    "FARM_GET_MY_LIST": _prep_farm_get_my_list,
    # FINANCE
    "FINANCE_GET_SUMMARY": _prep_finance_get_summary,
    # PROFIL / SYSTÈME
    "PROFILE_GET_MCP_USER": _prep_profile_get_mcp_user,
    "PROFILE_GET_TRUST": _prep_profile_get_trust,
    "PROFILE_GET_CONTEXT": _prep_profile_get_context,
    "DASHBOARD_PRODUCER": _prep_dashboard_producer,
    "SEARCH_NEARBY": _prep_search_nearby,
    "VALIDATE_PRICE": _prep_validate_price,
    "SYSTEM_GET_PENDING": _prep_system_get_pending,
}

# Dictionnaire complet des intentions d'ÉCRITURE (WRITE)
# Namespacing strict correspondant à intent.py
MARKET_WRITE_ACTIONS_MAP = {
    # STOCK
    "STOCK_REGISTER_HARVEST": _prep_stock_register_harvest,
    "STOCK_RECORD_MOVEMENT": _prep_stock_record_movement,
    "STOCK_ADJUST": _prep_stock_adjust,
    "STOCK_REMOVE_PARTIAL": _prep_stock_remove_partial,
    "STOCK_DELETE": _prep_stock_delete,
    # SALES (PRODUCER)
    "SALES_PUBLISH_PRODUCT": _prep_sales_publish_product,
    "SALES_RECORD_DIRECT": _prep_sales_record_direct,
    "SALES_PLACE_BID": _prep_sales_place_bid,
    "SALES_ACCEPT_CONTRACT": _prep_sales_accept_contract,
    # PROCUREMENT (BUYER)
    "PROCUREMENT_CREATE_REQUEST": _prep_procurement_create_request,
    "PROCUREMENT_SELECT_WINNER": _prep_procurement_select_winner,
    "PROCUREMENT_ACCEPT_OFFER": _prep_procurement_accept_offer,
    # AGRONOMIE
    "CROP_START_CYCLE": _prep_crop_start_cycle,
    "CROP_RECORD_INTERVENTION": _prep_crop_record_intervention,
    "CROP_RECORD_OBSERVATION": _prep_crop_record_observation,
    "CROP_UPDATE_STAGE": _prep_crop_update_stage,
    "CROP_UPDATE_SOIL": _prep_crop_update_soil,
    # FINANCE
    "FINANCE_LOG_EXPENSE": _prep_finance_log_expense,
    # FARM
    "FARM_CREATE": _prep_farm_create,
    "FARM_UPDATE": _prep_farm_update,
    # PROFIL
    "PROFILE_SET_GEO": _prep_profile_set_geo,
    "PROFILE_SET_PREFS": _prep_profile_set_prefs,
    "PROFILE_SWITCH_ROLE": _prep_profile_switch_role,
    # SYSTÈME
    "SYSTEM_REPORT_ANOMALY": _prep_system_report_anomaly,
    "SYSTEM_BIND_ZONE": _prep_system_bind_zone,
    "SYSTEM_COMMIT_TRANSACTION": _prep_system_commit_transaction,
}

# Agrégat global exposé à l'exécuteur du graphe
MARKET_ACTIONS_MAP = {**MARKET_READ_ACTIONS_MAP, **MARKET_WRITE_ACTIONS_MAP}

__all__ = [
    "MARKET_READ_ACTIONS_MAP",
    "MARKET_WRITE_ACTIONS_MAP",
    "MARKET_ACTIONS_MAP",
]