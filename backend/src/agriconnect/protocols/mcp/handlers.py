"""MCP Handlers — thin adapter layer mapping MCP tool calls to AgriDatabaseService.

Every handler:
  1. Validates / normalises its inputs.
  2. Delegates to a single ``AgriDatabaseService`` method.
  3. Catches ``DatabaseServiceError`` / ``IntegrityError`` and returns a
     clear textual error instead of crashing.
  4. Wraps results in ``GenericResult`` JSON.

The module exposes:
  - ``TOOL_HANDLERS``: dict[str, Callable] used by ``server.py`` to register tools.
  - ``TOOL_DESCRIPTIONS``: dict[str, str] auto-populated from service docstrings.
"""
from __future__ import annotations

import csv
import inspect
import json
import logging
import datetime as _dt
import types as _types
import uuid
from typing import Any, Dict, Union, get_args, get_origin
from functools import wraps # <--- INDISPENSABLE

from agriconnect.core.models import GenericResult
from agriconnect.services.database.database_service import AgriDatabaseService

logger = logging.getLogger("MCP.Handlers")

# ---------------------------------------------------------------------------
# Singleton service — instantiated once, reused by every handler.
# ---------------------------------------------------------------------------
_db: AgriDatabaseService | None = None


def _get_db() -> AgriDatabaseService:
    """Return the shared AgriDatabaseService singleton (lazy init)."""
    global _db
    if _db is None:
        _db = AgriDatabaseService()
    return _db


def get_db_service() -> AgriDatabaseService:
    """Public accessor for external callers that need the singleton."""
    return _get_db()


# ---------------------------------------------------------------------------
# EXPOSED_METHODS — auto-generated from AgriDatabaseService public async API
# ---------------------------------------------------------------------------
def _compute_exposed_methods() -> list[str]:
    methods: set[str] = set()
    for name, member in inspect.getmembers(AgriDatabaseService):
        if name.startswith("_"):
            continue
        if inspect.iscoroutinefunction(member):
            methods.add(name)
    return sorted(methods)


EXPOSED_METHODS = _compute_exposed_methods()


# ---------------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------------
def _ok(data: Any) -> str:
    return GenericResult(status="ok", data=data).model_dump_json()


def _error(message: str) -> str:
    return GenericResult(status="error", message=message).model_dump_json()


def _safe(coro_name: str):
    """Decorator that catches service exceptions and returns a JSON error."""
    def decorator(fn):
        @wraps(fn) # <--- C'EST LA CLÉ : Copie la signature originale sur le wrapper
        async def wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except AgriDatabaseService.IntegrityError as exc:
                logger.warning("IntegrityError in %s: %s", coro_name, exc)
                return _error(f"Contrainte d'intégrité violée : {exc}")
            except AgriDatabaseService.DatabaseServiceError as exc:
                logger.error("DatabaseServiceError in %s: %s", coro_name, exc)
                return _error(f"Erreur base de données : {exc}")
            except Exception as exc:
                logger.exception("Unexpected error in %s", coro_name)
                return _error(f"Erreur inattendue : {exc}")
        
        # Plus besoin de copier __name__, __doc__, etc. manuellement, 
        # @wraps le fait mieux que nous.
        return wrapper
    return decorator


def _wrap_auto_result(res: Any) -> str:
    """Wrap service results into GenericResult JSON.

    Some service methods return structured error dicts instead of raising.
    For auto-generated handlers, we translate those into GenericResult(error)
    when we can detect them.
    """
    if isinstance(res, dict):
        status = str(res.get("status") or "").upper()
        if res.get("error") or status in {"FAILED", "REJECTED", "NOT_FOUND", "ALREADY_HANDLED", "ERROR"}:
            msg = res.get("message") or res.get("error") or "Erreur"
            return GenericResult(status="error", data=res, message=str(msg)).model_dump_json()
    return _ok(res)


def _make_auto_handler(tool_name: str):
    """Create a handler that forwards MCP calls to AgriDatabaseService.<tool_name>."""
    service_method = getattr(AgriDatabaseService, tool_name, None)
    if service_method is None or not inspect.iscoroutinefunction(service_method):
        return None

    @_safe(tool_name)
    @wraps(service_method)
    async def _handler(**kwargs) -> str:
        db = _get_db()
        bound = getattr(db, tool_name)
        raw = await bound(**kwargs)
        return _wrap_auto_result(raw)

    # Make the tool name stable for logs/introspection.
    _handler.__name__ = tool_name
    return _handler


def _type_to_json_schema(annotation: Any) -> dict[str, Any]:
    """Best-effort Python typing -> JSON Schema fragment."""
    if annotation is inspect.Parameter.empty or annotation is Any:
        return {}

    origin = get_origin(annotation)
    args = get_args(annotation)

    # Optional[T] / Union[T1, T2, None]
    if origin in (Union, _types.UnionType):
        non_none = [a for a in args if a is not type(None)]  # noqa: E721
        if len(non_none) == 1:
            base = _type_to_json_schema(non_none[0])
            return {"anyOf": [base, {"type": "null"}]}
        return {"anyOf": [_type_to_json_schema(a) for a in non_none] + [{"type": "null"}]}

    # Containers
    if origin in (list, tuple, set, frozenset):
        item_schema = _type_to_json_schema(args[0]) if args else {}
        return {"type": "array", "items": item_schema}
    if origin in (dict, Dict):
        # JSON object keys are strings; only describe values.
        value_schema = _type_to_json_schema(args[1]) if len(args) == 2 else {}
        return {"type": "object", "additionalProperties": value_schema}

    # Common scalars
    if annotation is str:
        return {"type": "string"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation in (uuid.UUID,):
        return {"type": "string", "format": "uuid"}
    if annotation in (_dt.datetime,):
        return {"type": "string", "format": "date-time"}
    if annotation in (_dt.date,):
        return {"type": "string", "format": "date"}
    if annotation in (_dt.time,):
        return {"type": "string", "format": "time"}

    # Fallback
    return {}


# ---------------------------------------------------------------------------
# Handlers — Auth
# ---------------------------------------------------------------------------
@_safe("get_user_profile")
async def get_user_profile(user_id: str) -> str:
    """Récupère le profil complet d'un utilisateur par son identifiant."""
    db = _get_db()
    profile = await db.get_user_by_id(user_id)
    if not profile:
        return _error("Utilisateur introuvable.")
    return _ok(profile)


@_safe("get_user_by_phone")
async def get_user_by_phone(phone: str) -> str:
    """Recherche un utilisateur par son numéro de téléphone."""
    db = _get_db()
    profile = await db.get_user_by_phone(phone)
    if not profile:
        return _error("Utilisateur introuvable.")
    return _ok(profile)


@_safe("identify_or_create_user")
async def identify_or_create_user(
    phone: str, name: str = "", zone_id: str = ""
) -> str:
    """Identifie un utilisateur existant ou en crée un nouveau."""
    db = _get_db()
    raw = await db.identify_or_create_user(phone, name or None, zone_id or None)
    return _ok(raw)


# ---------------------------------------------------------------------------
# Handlers — Farms
# ---------------------------------------------------------------------------
@_safe("get_farms")
async def get_farms(producer_id: str) -> str:
    """Liste les fermes d'un producteur."""
    db = _get_db()
    raw = await db.get_farms(producer_id)
    return _ok(raw)


# ---------------------------------------------------------------------------
# Handlers — Stocks
# ---------------------------------------------------------------------------
@_safe("get_stocks")
async def get_stocks(farm_id: str) -> str:
    """Liste les stocks d'une ferme."""
    db = _get_db()
    raw = await db.get_stocks(farm_id)
    return _ok(raw)


async def get_farm_stocks(farm_id: str) -> str:
    """Alias pour get_stocks — liste les stocks d'une ferme."""
    return await get_stocks(farm_id=farm_id)


@_safe("add_stock")
async def add_stock(
    farm_id: str,
    item_name: str,
    quantity: float,
    unit: str = "KG",
    stock_type: str = "HARVEST",
    reason: str = "Ajout via agent",
) -> str:
    """Ajoute du stock dans une ferme."""
    db = _get_db()
    raw = await db.add_stock(
        farm_id=farm_id,
        item_name=item_name,
        quantity=quantity,
        unit=unit,
        stock_type=stock_type,
        reason=reason,
    )
    return _ok(raw)


@_safe("remove_stock")
async def remove_stock(
    farm_id: str,
    item_name: str,
    quantity: float,
    reason: str = "Retrait",
    movement_type: str = "OUT",
) -> str:
    """Retire du stock d'une ferme."""
    db = _get_db()
    raw = await db.remove_stock(
        farm_id=farm_id,
        item_name=item_name,
        quantity=quantity,
        reason=reason,
        movement_type=movement_type,
    )
    if isinstance(raw, dict) and raw.get("error"):
        return _error(raw["error"])
    return _ok(raw)


@_safe("record_sale")
async def record_sale(
    phone: str,
    product_name: str,
    quantity: float,
    total_price: float,
    unit: str = "KG",
) -> str:
    """Enregistre une vente directe en d?cr?mentant le stock correspondant."""
    db = _get_db()
    if not hasattr(db, "remove_stock"):
        return _error("Fonctionnalit? d'enregistrement de vente indisponible")
    raw = await db.remove_stock(
        farm_id=phone,
        item_name=product_name,
        quantity=quantity,
        reason=f"Vente directe  montant encaiss?: {total_price} FCFA",
        movement_type="OUT",
    )
    return _ok({
        "sale": {
            "product_name": product_name,
            "quantity": quantity,
            "unit": unit,
            "total_price": total_price,
        },
        "stock_update": raw,
    })


@_safe("update_stock_with_movement")
async def update_stock_with_movement(
    farm_id: str, item_name: str, quantity_change: float, reason: str
) -> str:
    """Ajuste le stock d'une ferme avec un mouvement (positif ou négatif)."""
    db = _get_db()
    raw = await db.adjust_stock(
        farm_id=farm_id,
        item_name=item_name,
        quantity_change=quantity_change,
        reason=reason,
    )
    return _ok(raw)


@_safe("get_stock_movements")
async def get_stock_movements(stock_id: str, limit: int = 20) -> str:
    """Liste les mouvements de stock."""
    db = _get_db()
    raw = await db.get_stock_movements(stock_id=stock_id, limit=limit)
    return _ok(raw)


# ---------------------------------------------------------------------------
# Handlers — Products
# ---------------------------------------------------------------------------
@_safe("list_products")
async def list_products(producer_id: str) -> str:
    """Liste les produits d'un producteur."""
    db = _get_db()
    prod_id = producer_id.strip() if isinstance(producer_id, str) else producer_id
    try:
        uuid.UUID(prod_id)
    except Exception:
        return _error("Format de producer_id invalide")
    raw = await db.list_products(prod_id)
    return _ok(raw)


@_safe("search_products")
async def search_products(
    product_name: str, zone_id: str | None = None, limit: int = 10
) -> str:
    """Recherche des produits par nom et zone."""
    db = _get_db()
    raw = await db.search_products(product_name=product_name, zone_id=zone_id, limit=limit)
    return _ok(raw)


@_safe("get_market_snapshot")
async def get_market_snapshot(zone: str | None = None, zone_name: str | None = None) -> str:
    """Analyse globale des prix et disponibilit?s du march?."""
    db = _get_db()
    if not hasattr(db, "get_market_snapshot"):
        return _error("Fonctionnalit? d'analyse de march? indisponible")
    zone_query = zone_name or zone
    raw = await db.get_market_snapshot(zone_query=zone_query)
    return _ok(raw)



@_safe("create_product")
async def create_product(
    producer_id: str,
    name: str,
    price: float,
    quantity_for_sale: float,
    unit: str = "KG",
    category_label: str = "Céréales",
    sub_category_id: str | None = None,
    description: str | None = None,
    local_names: Any = None,
) -> str:
    """Crée un nouveau produit pour un producteur."""
    db = _get_db()
    names_list = _parse_local_names(local_names)
    raw = await db.create_product(
        producer_id=producer_id,
        name=name,
        price=price,
        quantity_for_sale=quantity_for_sale,
        unit=unit,
        category_label=category_label,
        sub_category_id=sub_category_id or None,
        description=description or None,
        local_names=names_list,
    )
    if isinstance(raw, dict) and raw.get("error"):
        return _error(raw["error"])
    return _ok(raw)


def _parse_local_names(local_names: Any) -> list[str] | None:
    """Normalize local_names from various input formats."""
    if not local_names:
        return None
    if isinstance(local_names, list):
        return [str(n).strip() for n in local_names if str(n).strip()]
    if isinstance(local_names, str):
        try:
            parsed = json.loads(local_names)
            if isinstance(parsed, list):
                return [str(n).strip() for n in parsed if str(n).strip()]
        except Exception:
            pass
        try:
            reader = csv.reader([local_names])
            return [n.strip() for n in next(reader) if n.strip()]
        except Exception:
            return [n.strip() for n in local_names.split(",") if n.strip()]
    return None


# ---------------------------------------------------------------------------
# Handlers — Orders
# ---------------------------------------------------------------------------
@_safe("create_order")
async def create_order(
    product_id: str,
    quantity: float,
    buyer_phone: str,
    buyer_name: str = "",
    zone_id: str = "",
    source: str = "WHATSAPP",
    buyer_id: str = "",
    organization_id: str = "",
    payment_method: str = "CASH",
) -> str:
    """Crée une commande pour un produit."""
    db = _get_db()
    raw = await db.create_order(
        product_id=product_id,
        quantity=quantity,
        buyer_phone=buyer_phone,
        buyer_name=buyer_name or None,
        zone_id=zone_id or None,
        source=source,
        buyer_id=buyer_id or None,
        organization_id=organization_id or None,
        payment_method=payment_method,
    )
    if isinstance(raw, dict) and raw.get("error"):
        return _error(raw["error"])
    return _ok(raw)


@_safe("get_orders")
async def get_orders(
    buyer_id: str = "",
    buyer_phone: str = "",
    status: str = "",
    limit: int = 20,
) -> str:
    """Liste les commandes, filtrées par acheteur ou statut."""
    db = _get_db()
    raw = await db.get_orders(
        buyer_id=buyer_id or None,
        buyer_phone=buyer_phone or None,
        status=status or None,
        limit=limit,
    )
    return _ok(raw)


@_safe("update_order_status")
async def update_order_status(
    order_id: str, new_status: str, payment_status: str | None = None
) -> str:
    """Met à jour le statut d'une commande."""
    db = _get_db()
    raw = await db.update_order_status(
        order_id=order_id,
        new_status=new_status,
        payment_status=payment_status or None,
    )
    if raw is None:
        return _error("Commande introuvable")
    return _ok(raw)


# ---------------------------------------------------------------------------
# Handlers — Transactions / Staging
# ---------------------------------------------------------------------------
@_safe("prepare_transaction_staging")
async def prepare_transaction_staging(
    product_id: str,
    quantity_kg: float,
    price_fcfa_per_unit: float,
    buyer_phone: str,
    zone_id: str = "",
    source: str = "WHATSAPP",
) -> str:
    """Prépare une transaction en staging (validation avant commit)."""
    db = _get_db()
    raw = await db.prepare_transaction_staging(
        {
            "product_id": product_id,
            "quantity_kg": quantity_kg,
            "price_fcfa_per_unit": price_fcfa_per_unit,
            "buyer_phone": buyer_phone,
            "zone_id": zone_id or None,
            "source": source,
        }
    )
    return _ok(raw)


@_safe("commit_staged_transaction")
async def commit_staged_transaction(
    transaction_id: str, approved: bool = True
) -> str:
    """Confirme ou rejette une transaction en staging."""
    db = _get_db()
    raw = await db.commit_staged_transaction(transaction_id, approved=approved)
    return _ok(raw)


# ---------------------------------------------------------------------------
# Handlers — Agent Actions
# ---------------------------------------------------------------------------
@_safe("create_agent_action")
async def create_agent_action(
    agent_name: str,
    action_type: str,
    payload: dict,
    user_id: str | None = None,
    priority: str | None = "MEDIUM",
    order_id: str | None = None,
    ai_reasoning: str | None = None,
) -> str:
    """Crée une action d'agent pour validation humaine ou exécution."""
    db = _get_db()
    raw = await db.create_agent_action(
        agent_name=agent_name,
        action_type=action_type,
        payload=payload,
        user_id=user_id or None,
        priority=priority,
        order_id=order_id or None,
        ai_reasoning=ai_reasoning or None,
    )
    return _ok(raw)


@_safe("get_pending_actions")
async def get_pending_actions(agent_name: str = "", limit: int = 20) -> str:
    """Liste les actions en attente de validation."""
    db = _get_db()
    raw = await db.get_pending_actions(
        agent_name=agent_name or None, limit=limit
    )
    return _ok(raw)


@_safe("update_action_status")
async def update_action_status(
    action_id: str,
    new_status: str,
    admin_notes: str | None = None,
    validated_by_id: str | None = None,
) -> str:
    """Met à jour le statut d'une action d'agent."""
    db = _get_db()
    raw = await db.update_action_status(
        action_id,
        new_status,
        admin_notes=admin_notes or None,
        validated_by_id=validated_by_id or None,
    )
    if raw is None:
        return _error("Action introuvable")
    return _ok(raw)


# ---------------------------------------------------------------------------
# Handlers — Auctions
# ---------------------------------------------------------------------------
@_safe("create_auction")
async def create_auction(
    phone: str,
    product_query: str,
    qty: float,
    unit: str,
    max_price: float,
    deadline: str,
    zone_query: str | None = None,
    description: str | None = None,
    auto_extend: bool = True,
) -> str:
    """Crée une enchère Phone-First pour un acheteur."""
    db = _get_db()
    if not hasattr(db, "create_auction"):
        return _error("Fonctionnalité enchères pas encore disponible")
    raw = await db.create_auction(
        phone=phone,
        product_query=product_query,
        qty=qty,
        unit=unit,
        max_price=max_price,
        deadline=deadline,
        zone_query=zone_query or None,
        description=description or None,
        auto_extend=auto_extend,
    )
    return _ok(raw)


@_safe("place_bid")
async def place_bid(
    auction_id: str,
    phone: str,
    offered_price: float,
    message: str | None = None,
) -> str:
    """Place une offre producteur sur une enchère via téléphone."""
    db = _get_db()
    if not hasattr(db, "place_bid"):
        return _error("Fonctionnalité enchères pas encore disponible")
    raw = await db.place_bid(
        auction_id=auction_id,
        phone=phone,
        offered_price=offered_price,
        message=message or None,
    )
    if isinstance(raw, dict) and raw.get("status") == "error":
        return _error(raw.get("message") or "Impossible d'enregistrer l'offre")
    return _ok(raw)


@_safe("get_auctions")
async def get_auctions(
    phone: str | None = None,
    product_name: str | None = None,
    zone_name: str | None = None,
    status: str = "OPEN",
    view_mode: str = "MARKETPLACE",
) -> str:
    """Liste les appels d'offres disponibles avec menu WhatsApp indexé."""
    db = _get_db()
    if not hasattr(db, "get_auctions"):
        return _error("Fonctionnalité enchères pas encore disponible")
    raw = await db.get_auctions(
        phone=phone or None,
        product_name=product_name or None,
        zone_name=zone_name or None,
        status=status,
        view_mode=view_mode,
    )
    return _ok(raw)


@_safe("get_auctions_bids")
async def get_auctions_bids(phone: str | None = None, status: str = "OPEN") -> str:
    """Liste les propositions reçues par un acheteur avec menu WhatsApp indexé."""
    db = _get_db()
    if not hasattr(db, "get_auctions_bids"):
        return _error("Fonctionnalité enchères pas encore disponible")
    raw = await db.get_auctions_bids(phone=phone or None, status=status)
    return _ok(raw)


@_safe("get_my_active_bids")
async def get_my_active_bids(phone: str) -> str:
    """Liste les propositions envoyées par le producteur et leurs statuts."""
    db = _get_db()
    if not hasattr(db, "get_my_active_bids"):
        return _error("Fonctionnalité enchères pas encore disponible")
    raw = await db.get_my_active_bids(phone=phone)
    return _ok(raw)


@_safe("accept_bid")
async def accept_bid(bid_id: str) -> str:
    """Accepte une proposition reçue."""
    db = _get_db()
    if not hasattr(db, "accept_bid"):
        return _error("Fonctionnalité enchères pas encore disponible")
    raw = await db.accept_bid(bid_id=bid_id)
    return _ok(raw)


@_safe("select_winning_bid")
async def select_winning_bid(bid_id: str) -> str:
    """Sélectionne l'offre gagnante et génère la commande."""
    db = _get_db()
    if not hasattr(db, "select_winning_bid"):
        return _error("Fonctionnalité enchères pas encore disponible")
    raw = await db.select_winning_bid(bid_id=bid_id)
    return _ok(raw)


@_safe("get_open_auctions")
async def get_open_auctions(
    zone_id: str | None = None, limit: int = 20
) -> str:
    """Liste les enchères ouvertes."""
    db = _get_db()
    if not hasattr(db, "get_open_auctions"):
        return _error("Fonctionnalité enchères pas encore disponible")
    raw = await db.get_open_auctions(zone_id=zone_id or None, limit=limit)
    return _ok(raw)


# ---------------------------------------------------------------------------
# Handlers — Utils
# ---------------------------------------------------------------------------
@_safe("normalize_unit")
async def normalize_unit(quantity: float, unit: str) -> str:
    """Convertit une quantité dans son unité normalisée (kg)."""
    db = _get_db()
    raw = await db.normalize_unit(quantity=quantity, unit=unit)
    return _ok({"value": raw})


@_safe("guess_category")
async def guess_category(product_name: str) -> str:
    """Devine la catégorie d'un produit à partir de son nom."""
    db = _get_db()
    raw = await db.guess_category(product_name=product_name)
    return _ok({"value": raw})


# ---------------------------------------------------------------------------
# Handlers — Dashboards
# ---------------------------------------------------------------------------
@_safe("get_producer_dashboard")
async def get_producer_dashboard(producer_id: str) -> str:
    """Récupère le tableau de bord complet d'un producteur."""
    db = _get_db()
    raw = await db.get_producer_dashboard(producer_id)
    return _ok(raw)

@_safe("get_or_create_farm")
async def get_or_create_farm(producer_id: str, farm_name: str = "Ma ferme", zone_id: str = None,) -> str:
    """Récupère ou crée une ferme pour un producteur."""
    db = _get_db()
    raw = await db.get_or_create_farm(producer_id, farm_name, zone_id)
    return _ok(raw)


# =================================================================
# CROP & AGRONOMIC HANDLERS
# =================================================================

@_safe("create_crop_cycle")
async def create_crop_cycle(farm_id: str, data: Dict[str, Any]) -> str:
    """Crée un nouveau cycle de culture pour une ferme donnée."""
    db = _get_db()
    raw = await db.create_crop_cycle(farm_id=farm_id, data=data)
    return _ok(raw)

@_safe("get_crop_cycle_context")
async def get_crop_cycle_context(cycle_id: str) -> str:
    """
    Récupère tout l'historique d'un cycle (interventions, croissance, recommandations).
    C'est la fonction clé pour donner du contexte à l'IA.
    """
    db = _get_db()
    raw = await db.get_cycle_with_context(cycle_id=cycle_id)
    return _ok(raw)

@_safe("log_crop_intervention")
async def log_crop_intervention(cycle_id: str, data: Dict[str, Any]) -> str:
    """Enregistre une action technique (semis, irrigation, fertilisation, traitement)."""
    db = _get_db()
    raw = await db.log_intervention(cycle_id=cycle_id, data=data)
    return _ok(raw)

@_safe("add_growth_observation")
async def add_growth_observation(cycle_id: str, stage_code: int, image_url: str = None) -> str:
    """Enregistre une observation visuelle du stade de croissance BBCH."""
    db = _get_db()
    raw = await db.add_growth_log(cycle_id=cycle_id, stage_code=stage_code, image_url=image_url)
    return _ok(raw)

@_safe("get_yield_analysis")
async def get_yield_analysis(cycle_id: str) -> str:
    """Analyse les performances : rendement cible vs rendement attendu."""
    db = _get_db()
    raw = await db.get_yield_performance_metrics(cycle_id=cycle_id)
    return _ok(raw)

@_safe("check_growth_health")
async def check_growth_health(cycle_id: str) -> str:
    """Vérifie si la culture suit son cycle normal ou si elle subit un stress (retard)."""
    db = _get_db()
    raw = await db.check_growth_compliance(cycle_id=cycle_id)
    return _ok(raw)

@_safe("get_irrigation_nutrition_needs")
async def get_irrigation_nutrition_needs(cycle_id: str) -> str:
    """Calcule les besoins immédiats en eau (mm/jour) et azote (kg/ha)."""
    db = _get_db()
    raw = await db.get_instant_resource_needs(cycle_id=cycle_id)
    return _ok(raw)

@_safe("get_sanitary_alerts")
async def get_sanitary_alerts(farm_id: str) -> str:
    """Récupère les alertes actives sur les pestes et maladies pour la ferme."""
    db = _get_db()
    raw = await db.get_active_sanitary_risks(farm_id=farm_id)
    return _ok(raw)

@_safe("manage_agent_memory")
async def manage_agent_memory(user_id: str, last_topic: str = None, prefs: Dict = None) -> str:
    """Gère la mémoire contextuelle de l'agent pour ce producteur."""
    db = _get_db()
    if last_topic or prefs:
        raw = await db.update_agent_memory(user_id=user_id, last_topic=last_topic, prefs=prefs)
    else:
        raw = await db.get_agent_memory(user_id=user_id)
    return _ok(raw)

@_safe("analyze_climate_impact")
async def analyze_climate_impact(cycle_id: str) -> str:
    """Analyse l'impact du stress thermique sur le cycle actuel."""
    db = _get_db()
    raw = await db.analyze_thermal_stress(cycle_id=cycle_id)
    return _ok(raw)


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------
TOOL_HANDLERS: Dict[str, Any] = {
    "get_user_profile": get_user_profile,
    "get_user_by_phone": get_user_by_phone,
    "identify_or_create_user": identify_or_create_user,
    "get_farms": get_farms,
    "get_stocks": get_stocks,
    "get_farm_stocks": get_farm_stocks,
    "add_stock": add_stock,
    "remove_stock": remove_stock,
    "update_stock_with_movement": update_stock_with_movement,
    "get_stock_movements": get_stock_movements,
    "list_products": list_products,
    "search_products": search_products,
    "get_market_snapshot": get_market_snapshot,
    "create_product": create_product,
    "record_sale": record_sale,
    "create_order": create_order,
    "get_orders": get_orders,
    "update_order_status": update_order_status,
    "prepare_transaction_staging": prepare_transaction_staging,
    "commit_staged_transaction": commit_staged_transaction,
    "create_agent_action": create_agent_action,
    "get_pending_actions": get_pending_actions,
    "update_action_status": update_action_status,
    "create_auction": create_auction,
    "place_bid": place_bid,
    "get_auctions": get_auctions,
    "get_auctions_bids": get_auctions_bids,
    "get_my_active_bids": get_my_active_bids,
    "accept_bid": accept_bid,
    "select_winning_bid": select_winning_bid,
    "get_open_auctions": get_open_auctions,
    "normalize_unit": normalize_unit,
    "guess_category": guess_category,
    "get_producer_dashboard": get_producer_dashboard,
    "get_or_create_farm": get_or_create_farm,
    
    # --- Gestion des Cycles de Culture ---
    "create_crop_cycle": create_crop_cycle,
    "get_crop_cycle_context": get_crop_cycle_context,
    "check_growth_health": check_growth_health,
    "get_yield_analysis": get_yield_analysis,

    # --- Opérations et Observations Terrain ---
    "log_crop_intervention": log_crop_intervention,
    "add_growth_observation": add_growth_observation,
    "get_irrigation_nutrition_needs": get_irrigation_nutrition_needs,
    "analyze_climate_impact": analyze_climate_impact,

    # --- Intelligence et Risques ---
    "get_active_sanitary_risks": get_sanitary_alerts,
    "manage_agent_memory": manage_agent_memory,
}


# Auto-register every public async method from AgriDatabaseService.
# Manual handlers above always take precedence.
for _tool_name in EXPOSED_METHODS:
    if _tool_name not in TOOL_HANDLERS:
        _auto = _make_auto_handler(_tool_name)
        if _auto is not None:
            TOOL_HANDLERS[_tool_name] = _auto


def get_tool_descriptions() -> Dict[str, str]:
    """Build tool descriptions from handler docstrings."""
    return {
        name: (inspect.getdoc(fn) or name).split("\n")[0]
        for name, fn in TOOL_HANDLERS.items()
    }


TOOL_DESCRIPTIONS = get_tool_descriptions()


# Dans agriconnect/protocols/mcp/handlers.py

def get_tool_schemas() -> Dict[str, Dict[str, Any]]:
    schemas: Dict[str, Dict[str, Any]] = {}
    for name, fn in TOOL_HANDLERS.items():
        sig = inspect.signature(fn)
        properties: Dict[str, Any] = {}
        required: list[str] = []
        additional_properties: bool | dict[str, Any] | None = None

        for param_name, param in sig.parameters.items():
            if param_name in {"args", "self", "cls", "session"}:
                continue

            # **kwargs
            if param.kind == inspect.Parameter.VAR_KEYWORD:
                additional_properties = True
                continue

            # *args
            if param.kind == inspect.Parameter.VAR_POSITIONAL:
                continue

            prop_def: Dict[str, Any] = {"description": param_name}
            prop_def.update(_type_to_json_schema(param.annotation))
            properties[param_name] = prop_def

            if param.default is inspect.Parameter.empty:
                required.append(param_name)

        tool_desc = (inspect.getdoc(fn) or name).split("\n")[0]
        schema: Dict[str, Any] = {
            "type": "object",
            "description": tool_desc,
            "properties": properties,
            "required": required,
        }
        if additional_properties is not None:
            schema["additionalProperties"] = additional_properties

        schemas[name] = schema

    return schemas

# Ajoutez cette ligne juste en dessous de TOOL_DESCRIPTIONS
TOOL_SCHEMAS= get_tool_schemas()


if __name__ == "__main__":
    # Test rapide pour vérifier que les outils sont bien accessibles
    for tool_name, handler in TOOL_HANDLERS.items():
        print(f"Tool: {tool_name}, Description: {TOOL_DESCRIPTIONS.get(tool_name)}, Schema: {TOOL_SCHEMAS.get(tool_name)} type::{type(TOOL_SCHEMAS.get(tool_name))}")