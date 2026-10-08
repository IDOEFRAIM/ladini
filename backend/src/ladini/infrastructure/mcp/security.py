from __future__ import annotations

import asyncio
import contextvars
import importlib
import json
import logging
import os
import re
import time
import uuid
from collections import deque
from enum import Enum
from typing import (
    Any,
    Callable,
    Coroutine,
    Dict,
    Iterable,
    List,
    Optional,
    Protocol,
)

from pydantic import BaseModel, Field

logger = logging.getLogger("MCP.Core.Security")
_REQUEST_ID_CTX: contextvars.ContextVar[str] = contextvars.ContextVar(
    "mcp_request_id", default=""
)


class PermissionScope(str, Enum):
    DB_READ_ONLY = "DB_READ_ONLY"
    DB_DATA_WRITE = "DB_DATA_WRITE"
    DB_SCHEMA_MODIFY = "DB_SCHEMA_MODIFY"


TOOL_SCOPE_MAP: dict[str, PermissionScope] = {
    "get_user_profile": PermissionScope.DB_READ_ONLY,
    "get_user_by_phone": PermissionScope.DB_READ_ONLY,
    "list_products": PermissionScope.DB_READ_ONLY,
    "search_products": PermissionScope.DB_READ_ONLY,
    "get_orders": PermissionScope.DB_READ_ONLY,
    "get_pending_actions": PermissionScope.DB_READ_ONLY,
    "get_open_auctions": PermissionScope.DB_READ_ONLY,
    "get_farm_stocks": PermissionScope.DB_READ_ONLY,
    "get_stocks": PermissionScope.DB_READ_ONLY,
    "get_stock_movements": PermissionScope.DB_READ_ONLY,
    "get_stock_history": PermissionScope.DB_READ_ONLY,
    "normalize_unit": PermissionScope.DB_READ_ONLY,
    "guess_category": PermissionScope.DB_READ_ONLY,
    "get_producer_dashboard": PermissionScope.DB_READ_ONLY,
    "get_clients": PermissionScope.DB_READ_ONLY,
    "get_farms": PermissionScope.DB_READ_ONLY,
    "get_expense_summary": PermissionScope.DB_READ_ONLY,
    "get_expenses": PermissionScope.DB_READ_ONLY,
    "get_user_context_state": PermissionScope.DB_READ_ONLY,
    "list_market_matches": PermissionScope.DB_READ_ONLY,
    "db_status": PermissionScope.DB_READ_ONLY,
    "identify_or_create_user": PermissionScope.DB_DATA_WRITE,
    "complete_user_profile": PermissionScope.DB_DATA_WRITE,
    "create_product": PermissionScope.DB_DATA_WRITE,
    "add_product_photo": PermissionScope.DB_DATA_WRITE,
    "add_bid_photo": PermissionScope.DB_DATA_WRITE,
    "add_auction_photo": PermissionScope.DB_DATA_WRITE,
    "create_order": PermissionScope.DB_DATA_WRITE,
    "create_auction": PermissionScope.DB_DATA_WRITE,
    "update_auction_fields": PermissionScope.DB_DATA_WRITE,
    # Approvisionnement récurrent (Phase 2/4)
    "create_recurring_need": PermissionScope.DB_DATA_WRITE,
    "create_recurring_needs": PermissionScope.DB_DATA_WRITE,
    "update_recurring_need": PermissionScope.DB_DATA_WRITE,
    "accept_match_proposal": PermissionScope.DB_DATA_WRITE,
    "mark_order_delivery_status": PermissionScope.DB_DATA_WRITE,
    "record_order_reception": PermissionScope.DB_DATA_WRITE,
    "list_my_deliverable_orders": PermissionScope.DB_READ_ONLY,
    "list_my_recurring_needs": PermissionScope.DB_READ_ONLY,
    "ensure_next_recurring_occurrence": PermissionScope.DB_DATA_WRITE,
    "refresh_recurring_need_matching": PermissionScope.DB_DATA_WRITE,
    "get_recurring_need_detail": PermissionScope.DB_READ_ONLY,
    "get_recurring_start_policy": PermissionScope.DB_READ_ONLY,
    "place_bid": PermissionScope.DB_DATA_WRITE,
    "update_stock_with_movement": PermissionScope.DB_DATA_WRITE,
    "prepare_transaction_staging": PermissionScope.DB_DATA_WRITE,
    "commit_staged_transaction": PermissionScope.DB_DATA_WRITE,
    "create_agent_action": PermissionScope.DB_DATA_WRITE,
    "update_action_status": PermissionScope.DB_DATA_WRITE,
    "get_or_create_farm": PermissionScope.DB_DATA_WRITE,
    "add_stock": PermissionScope.DB_DATA_WRITE,
    "remove_stock": PermissionScope.DB_DATA_WRITE,
    "record_sale": PermissionScope.DB_DATA_WRITE,
    "add_expense": PermissionScope.DB_DATA_WRITE,
    # (2026-09-05, Phase 6B — audit des mutations prenant `order_id` seul) :
    # `update_order_status` RETIRÉ de la carte des scopes, donc désormais
    # refusé par le fail-closed de `runtime.py::call_tool`.
    #
    # `ProducerMgmtMixin.update_order_status(order_id, new_status,
    # payment_status)` écrit `Order.status` ET `Order.payment_status` à des
    # valeurs ARBITRAIRES, sans AUCUN contrôle de propriété (ni téléphone, ni
    # producteur, ni acheteur) et sans garde de statut. Elle contournait donc
    # entièrement les garanties construites autour de la clôture
    # (`confirm_delivery_and_payment`), de l'annulation producteur
    # (`cancel_confirmed_order`) et de l'annulation acheteur
    # (`cancel_pending_order`) — n'importe quelle commande pouvait être
    # marquée PAID/COMPLETED/CANCELLED par un appelant MCP quelconque.
    #
    # Recherche exhaustive : ZÉRO appelant dans tout le dépôt (seuls cette
    # entrée et un commentaire la mentionnaient). La méthode elle-même n'est
    # pas supprimée (pas de suppression de code métier sans nécessité) — elle
    # est simplement rendue inatteignable. Verrouillé par
    # `tests/architecture/test_order_mutations_require_ownership.py`.
    # "update_order_status": PermissionScope.DB_DATA_WRITE,
    "update_farm": PermissionScope.DB_DATA_WRITE,
    "register_surplus_offer": PermissionScope.DB_DATA_WRITE,
    "upsert_user_context_state": PermissionScope.DB_DATA_WRITE,
    "create_market_match": PermissionScope.DB_DATA_WRITE,
    "migrate_schema": PermissionScope.DB_SCHEMA_MODIFY,
    "drop_table": PermissionScope.DB_SCHEMA_MODIFY,
    "alter_table": PermissionScope.DB_SCHEMA_MODIFY,
    "search_agronomy_docs": PermissionScope.DB_READ_ONLY,
    "search_past_interactions": PermissionScope.DB_READ_ONLY,
    "persist_conversation": PermissionScope.DB_DATA_WRITE,
    # Moderation / anti-abuse
    "get_account_status": PermissionScope.DB_READ_ONLY,
    "get_last_interactive_outbound": PermissionScope.DB_READ_ONLY,
    "get_prohibited_terms": PermissionScope.DB_READ_ONLY,
    "record_moderation_strike": PermissionScope.DB_DATA_WRITE,
    "record_demand_signal": PermissionScope.DB_DATA_WRITE,
    "record_campaign_interest": PermissionScope.DB_DATA_WRITE,
    # Escrow (Paydunya) — explicite plutôt que de laisser le guess automatique
    # décider, vu la sensibilité (argent bloqué / débloqué).
    "initiate_escrow_payment": PermissionScope.DB_DATA_WRITE,
    "verify_delivery_otp": PermissionScope.DB_DATA_WRITE,
    # (2026-09-05, Phase 7 — audit final des mutations exposées) : ces deux
    # opérations sont SYSTÈME, jamais conversationnelles, et leurs appelants
    # réels n'utilisent PAS MCP :
    #   - `mark_escrow_paid(invoice_token)` : appelée directement via
    #     `AgriDatabaseService()` par la tâche IPN Paydunya
    #     (`flows/buyer/preorder_payment.py::reconcile_invoice`, déclenchée
    #     par `workers/payments/paydunya_ipn_task.py`). Exposée en MCP, elle
    #     permettait de déclarer un paiement reçu (`payment_status=ESCROWED`
    #     + notifications) sans passer par le fournisseur de paiement.
    #   - `expire_pending_payments()` : cron, appelée directement par
    #     `workers/crons/order_expiry.py`. Exposée en MCP, elle permettait
    #     d'expirer/annuler EN MASSE les commandes en attente de paiement,
    #     sans aucun acteur ni périmètre.
    # Aucune des deux n'a de contrôle de propriété (par nature : l'une
    # s'authentifie par le token d'invoice, l'autre est un balayage global).
    # Retirées de la carte des scopes -> refusées par le fail-closed de
    # `runtime.py::call_tool`. Les appels système, eux, sont inchangés.
    # "mark_escrow_paid": PermissionScope.DB_DATA_WRITE,
    # "expire_pending_payments": PermissionScope.DB_DATA_WRITE,
    "list_producer_escrowed_orders": PermissionScope.DB_READ_ONLY,
    # --- Audit 2026-08 : outils exposés (auto-découverts via
    # `h.py::_compute_exposed_methods`) mais jusqu'ici absents d'ici, donc
    # entièrement dépendants du guess heuristique de `_autofill_tool_scopes`.
    # Vérifiés un par un comme réellement appelés ailleurs dans le code
    # (gateway MCP, INTENT_CONFIG, flows) avant d'être déclarés ici. Deux
    # étaient mal classés par le guess : `get_or_create_client` (préfixe
    # "get" → deviné READ_ONLY alors qu'il écrit si le client n'existe pas)
    # et `ensure_performance_indexes` (deviné WRITE alors que c'est un
    # DDL — création d'index).
    "get_auction_bids": PermissionScope.DB_READ_ONLY,
    "get_auctions": PermissionScope.DB_READ_ONLY,
    "get_auctions_bids": PermissionScope.DB_READ_ONLY,
    "get_available_zones": PermissionScope.DB_READ_ONLY,
    "get_buyer_orders_dashboard": PermissionScope.DB_READ_ONLY,
    "get_market_snapshot": PermissionScope.DB_READ_ONLY,
    "get_my_active_bids": PermissionScope.DB_READ_ONLY,
    "get_my_products": PermissionScope.DB_READ_ONLY,
    "get_offer_reservations": PermissionScope.DB_READ_ONLY,
    "get_producer_auctions": PermissionScope.DB_READ_ONLY,
    "get_producer_farm": PermissionScope.DB_READ_ONLY,
    "get_producer_orders": PermissionScope.DB_READ_ONLY,
    "get_producer_stocks": PermissionScope.DB_READ_ONLY,
    "get_product_category_unit_config": PermissionScope.DB_READ_ONLY,
    "get_transaction_summary": PermissionScope.DB_READ_ONLY,
    "get_zone_by_name": PermissionScope.DB_READ_ONLY,
    "get_zone_hierarchy_by_name": PermissionScope.DB_READ_ONLY,
    "list_producer_productions": PermissionScope.DB_READ_ONLY,
    "check_price_anomaly": PermissionScope.DB_READ_ONLY,
    "validate_stock_availability_atomic": PermissionScope.DB_READ_ONLY,
    "get_or_create_client": PermissionScope.DB_DATA_WRITE,
    "adjust_stock": PermissionScope.DB_DATA_WRITE,
    "cancel_pending_order": PermissionScope.DB_DATA_WRITE,
    # (2026-09-04, audit Order(DRAFT) orphelin PREORDER) : méthode déjà
    # présente sur `BuyerMixin` — auto-exposée par introspection
    # (`protocols/mcp/servers/h.py::EXPOSED_METHODS`), mais jamais autorisée
    # ici (fail-closed, voir `infrastructure/mcp/runtime.py`) ni jamais
    # appelée depuis la conversation (`_cancel_preorder` ne transitionnait
    # que le `PreorderDraft` applicatif, jamais l'`Order` Postgres sous-jacent).
    "cancel_preorder_draft": PermissionScope.DB_DATA_WRITE,
    "close_negotiation_session": PermissionScope.DB_DATA_WRITE,
    # (2026-09-04, clôture F1 — paiement à la livraison) : nouvelle méthode
    # sur `ProducerMgmtMixin`.
    "confirm_delivery_and_payment": PermissionScope.DB_DATA_WRITE,
    "confirm_preorder_draft": PermissionScope.DB_DATA_WRITE,
    "create_farm": PermissionScope.DB_DATA_WRITE,
    "create_preorder_draft": PermissionScope.DB_DATA_WRITE,
    "create_user_profile": PermissionScope.DB_DATA_WRITE,
    "declare_future_production": PermissionScope.DB_DATA_WRITE,
    "initiate_negotiation_session": PermissionScope.DB_DATA_WRITE,
    "reserve_future_offer": PermissionScope.DB_DATA_WRITE,
    "select_winning_bid": PermissionScope.DB_DATA_WRITE,
    "update_bid_price": PermissionScope.DB_DATA_WRITE,
    "update_communication_prefs": PermissionScope.DB_DATA_WRITE,
    "update_geo_location": PermissionScope.DB_DATA_WRITE,
    "update_negotiation_offer": PermissionScope.DB_DATA_WRITE,
    "update_product_price_and_qty": PermissionScope.DB_DATA_WRITE,
    # (2026-09-04, Product Completeness Phase 2) : retrait d'un produit du
    # catalogue — `SALES_UNPUBLISH_PRODUCT`. Sans cette entrée, le
    # fail-closed du TOOL_SCOPE_MAP refuserait l'appel.
    "delete_product": PermissionScope.DB_DATA_WRITE,
    # (2026-09-04, Phase 5 — décision produit #1) : annulation producteur
    # d'une commande confirmée.
    "cancel_confirmed_order": PermissionScope.DB_DATA_WRITE,
    # (2026-09-13, confirmation explicite producteur) : miroir en écriture
    # de `cancel_confirmed_order` ci-dessus — le producteur accepte
    # explicitement une commande en attente.
    "confirm_order_by_producer": PermissionScope.DB_DATA_WRITE,
    "update_production_fields": PermissionScope.DB_DATA_WRITE,
}


# Préfixes de LECTURE explicites. Tout ce qui ne commence pas par l'un d'eux
# (et n'est pas déjà mappé) est considéré comme une ÉCRITURE par défaut :
# fail-safe vers le chemin de contrôle plutôt que d'auto-autoriser un outil
# inconnu potentiellement destructeur en READ_ONLY.
_READ_PREFIXES = (
    "get",
    "list",
    "search",
    "fetch",
    "read",
    "guess",
    "normalize",
    "check",
    "validate",
    "count",
    "find",
    "resolve",
    "db_status",
)


def _guess_scope(tool_name: str) -> PermissionScope:
    name = tool_name.lower()
    if name.startswith(("migrate", "drop", "alter", "truncate")):
        return PermissionScope.DB_SCHEMA_MODIFY
    if name.startswith(_READ_PREFIXES):
        return PermissionScope.DB_READ_ONLY
    # Défaut fail-safe : écriture (scrutiny), jamais lecture auto-autorisée.
    return PermissionScope.DB_DATA_WRITE


_scopes_filled = False


def _autofill_tool_scopes() -> None:
    """Audit tools exposed by handlers against declared scopes — no longer fills.

    Historically this did ``TOOL_SCOPE_MAP.setdefault(tool, _guess_scope(tool))``
    for every tool exposed by ``h.py`` (auto-discovered via introspection of
    every public async method on ``AgriDatabaseService``). Because that
    discovery is exhaustive, EVERY exposed tool ended up present in
    ``TOOL_SCOPE_MAP`` — which meant the fail-closed guard in
    ``runtime.py::call_tool`` (``if name not in TOOL_SCOPE_MAP: raise
    PermissionDenied``) could never fire for any of them: it was decorative.
    The heuristic also mis-scored real tools (``get_or_create_client`` guessed
    READ_ONLY off its "get" prefix despite writing; ``ensure_performance_indexes``
    guessed WRITE despite being schema DDL) — see the 2026-08 audit entries
    added to ``TOOL_SCOPE_MAP`` above.

    Now this only LOGS undeclared tools loudly (a visible drift signal — a new
    async method added to ``AgriDatabaseService`` without an explicit scope is
    exactly the gap that produced the two bugs above) instead of silently
    authorizing them. Anything still undeclared correctly falls through to the
    fail-closed ``PermissionDenied`` path.

    Called lazily on first use (via ``ensure_scopes_filled``) instead of at
    import time to avoid cascading imports (h.py → AgriDatabaseService → all
    mixins) when security.py is merely imported.
    """
    global _scopes_filled
    if _scopes_filled:
        return
    _scopes_filled = True

    handlers = None
    last_error: Exception | None = None
    for module_path in (
        "ladini.protocols.mcp.servers.h",
        "ladini.protocols.mcp.handlers",
    ):
        try:
            module = importlib.import_module(module_path)
            handlers = getattr(module, "TOOL_HANDLERS", None)
        except Exception as exc:  # pragma: no cover - defensive
            last_error = exc
            continue
        if handlers:
            break

    if not handlers:
        logger.debug("TOOL_HANDLERS import failed; cannot audit scopes: %s", last_error)
        return

    undeclared = sorted(set(handlers.keys()) - set(TOOL_SCOPE_MAP.keys()))
    if undeclared:
        logger.warning(
            "MCP_SCOPE_GAP | %d outil(s) exposé(s) sans PermissionScope déclaré "
            "— désormais refusés (fail-closed) tant qu'ils ne sont pas ajoutés "
            "à TOOL_SCOPE_MAP : %s",
            len(undeclared),
            ", ".join(undeclared),
        )


def ensure_scopes_filled() -> None:
    """Public trigger for lazy scope initialization."""
    if not _scopes_filled:
        _autofill_tool_scopes()


SENSITIVE_COLUMNS = frozenset(
    {
        "password_hash",
        "password",
        "hashed_password",
        "email",
        "token",
        "secret",
        "api_key",
        "id_rsa",
        "private_key",
        "refresh_token",
        "access_token",
        "credit_card",
        "ssn",
        "otp",
    }
)

SQL_INJECTION_PATTERNS = [
    r"(?i)\bDROP\s+TABLE\b",
    r"(?i)\bDELETE\s+FROM\b",
    r"(?i)\bTRUNCATE\b",
    r"(?i)\bALTER\s+TABLE\b",
    r"(?i)\bOR\s+1\s*=\s*1",
    r"(?i)\bUNION\s+(ALL\s+)?SELECT\b",
]

SQL_INJECTION_REGEX = [re.compile(pattern) for pattern in SQL_INJECTION_PATTERNS]

SENSITIVE_FILE_PATTERNS = [r"\.env", r"id_rsa", r"\.pem$", r"\.key$", r"credentials"]


class MCPServerKind(str, Enum):
    DB = "db"


class MCPToolMeta(BaseModel):
    name: str
    server: MCPServerKind
    scope: PermissionScope
    description: str = ""
    timeout_seconds: float = Field(default=15.0, ge=0.1)
    retries: int = Field(default=1, ge=0, le=5)


class ToolExecutionMeta(BaseModel):
    request_id: str
    user_id: str
    tool_name: str
    duration_ms: float
    timed_out: bool = False
    token_estimate: int = 0


class ToolExecutionEnvelope(BaseModel):
    ok: bool
    data: dict[str, Any] = Field(default_factory=dict)
    error: str = ""
    meta: ToolExecutionMeta


def _normalize_tool_output(data: Any) -> dict:
    """Normalise une réponse brute d'outil vers un dict exploitable.

    Extrait de l'ancien ``MCPPermissionClient._normalize_output`` (audit
    MCP/AGUI 2026-08-26 — ce moteur de risque était du code mort en
    production, mais CETTE méthode était appelée par
    ``ToolExecutionPolicy.execute``, le chemin réellement actif). Fonction
    module-level pour ne plus dépendre d'une classe supprimée.
    """
    if isinstance(data, dict):
        return data
    if isinstance(data, str):
        try:
            parsed = json.loads(data)
            return parsed if isinstance(parsed, dict) else {"result": parsed}
        except Exception:
            return {"result": data}
    if isinstance(data, (list, tuple)):
        return {"items": list(data)}
    return {"result": str(data)}


class ToolRateLimiter:
    """In-memory per-user per-tool rate limiter (sliding window with eviction)."""

    def __init__(
        self, max_calls: int = 60, window_seconds: float = 60.0, max_keys: int = 2048
    ) -> None:
        self.max_calls = max(1, int(max_calls))
        self.window_seconds = max(1.0, float(window_seconds))
        self.max_keys = max(128, int(max_keys))
        self._events: dict[str, deque[float]] = {}

    def check(self, key: str) -> tuple[bool, str]:
        now = time.monotonic()
        events = self._events.setdefault(key, deque())
        cutoff = now - self.window_seconds
        while events and events[0] < cutoff:
            events.popleft()
        if len(events) >= self.max_calls:
            return (
                False,
                f"rate_limit_exceeded:{self.max_calls}/{int(self.window_seconds)}s",
            )
        events.append(now)
        if len(self._events) > self.max_keys:
            self._prune_stale(now)
        return True, "ok"

    def _prune_stale(self, now: float) -> None:
        stale_keys = [
            k
            for k, v in self._events.items()
            if not v or v[-1] < now - (self.window_seconds * 5)
        ]
        for key in stale_keys:
            self._events.pop(key, None)


class ToolExecutionPolicy:
    """Central policy for sanitation, timeout, rate-limit and audit metadata."""

    def __init__(
        self,
        registry: Optional[MCPToolRegistry] = None,
        max_calls_per_minute: int = 60,
        default_timeout_seconds: float = 30.0,
    ) -> None:
        self._registry = registry or get_registry()
        self._limiter = ToolRateLimiter(
            max_calls=max_calls_per_minute, window_seconds=60.0
        )
        self._default_timeout = max(0.1, float(default_timeout_seconds))
        self._overrides: dict[str, float] = {}

    async def execute(
        self,
        tool_name: str,
        handler: Callable[..., Coroutine[Any, Any, Any]],
        arguments: Optional[Dict[str, Any]] = None,
        *,
        user_id: str = "anonymous",
        request_id: str = "",
        timeout_seconds: Optional[float] = None,
    ) -> dict[str, Any]:
        arguments = self.sanitize_arguments(arguments or {})
        rid = request_id or _REQUEST_ID_CTX.get() or str(uuid.uuid4())
        _REQUEST_ID_CTX.set(rid)

        logger.info(f"TOOL_NAME:{tool_name} - TIMEOUT_SECONDS:{timeout_seconds}")
        limiter_key = f"{user_id}:{tool_name}"
        allowed, reason = self._limiter.check(limiter_key)
        if not allowed:
            raise PermissionDenied(tool_name, reason)

        meta = self._registry.get_tool(tool_name)
        # per-call override > env overrides > registry meta > default
        if timeout_seconds is not None:
            timeout = float(timeout_seconds)
        elif tool_name in getattr(self, "_overrides", {}):
            timeout = float(self._overrides[tool_name])
        else:
            timeout = float(meta.timeout_seconds if meta else self._default_timeout)
        start = time.monotonic()

        try:
            raw = await asyncio.wait_for(handler(**arguments), timeout=timeout)

            normalized = _normalize_tool_output(raw)
            elapsed = round((time.monotonic() - start) * 1000, 1)
            envelope = ToolExecutionEnvelope(
                ok=True,
                data=normalized,
                meta=ToolExecutionMeta(
                    request_id=rid,
                    user_id=user_id,
                    tool_name=tool_name,
                    duration_ms=elapsed,
                    timed_out=False,
                    token_estimate=self._estimate_tokens(normalized),
                ),
            )
            logger.info(
                "TOOL_AUDIT|%s",
                json.dumps(envelope.model_dump(mode="json"), ensure_ascii=False),
            )
            return envelope.model_dump()
        except asyncio.TimeoutError as exc:
            elapsed = round((time.monotonic() - start) * 1000, 1)
            envelope = ToolExecutionEnvelope(
                ok=False,
                data={},
                error=f"timeout_after_{timeout:.1f}s",
                meta=ToolExecutionMeta(
                    request_id=rid,
                    user_id=user_id,
                    tool_name=tool_name,
                    duration_ms=elapsed,
                    timed_out=True,
                    token_estimate=0,
                ),
            )
            logger.warning(
                "TOOL_TIMEOUT|%s",
                json.dumps(envelope.model_dump(mode="json"), ensure_ascii=False),
            )
            raise ToolExecutionTimeout(tool_name, timeout) from exc

    @staticmethod
    def sanitize_arguments(arguments: Dict[str, Any]) -> Dict[str, Any]:
        def _clean(v: Any) -> Any:
            if isinstance(v, str):
                cleaned = v.replace("\x00", "").strip()
                if len(cleaned) > 4000:
                    cleaned = cleaned[:4000]
                return cleaned
            if isinstance(v, dict):
                return {str(k): _clean(val) for k, val in v.items()}
            if isinstance(v, list):
                return [_clean(x) for x in v]
            return v

        return _clean(arguments)

    @staticmethod
    def _estimate_tokens(payload: dict[str, Any]) -> int:
        try:
            s = json.dumps(payload, ensure_ascii=False)
            return max(1, len(s) // 4)
        except Exception:
            return 0


_GLOBAL_EXECUTION_POLICY: Optional[ToolExecutionPolicy] = None


def get_execution_policy() -> ToolExecutionPolicy:
    global _GLOBAL_EXECUTION_POLICY
    ensure_scopes_filled()
    if _GLOBAL_EXECUTION_POLICY is None:
        default_env = os.getenv("MCP_DEFAULT_TOOL_TIMEOUT")
        try:
            default_timeout = float(default_env) if default_env is not None else 30.0
        except Exception:
            default_timeout = 30.0
        _GLOBAL_EXECUTION_POLICY = ToolExecutionPolicy(
            default_timeout_seconds=default_timeout
        )
        # Load optional per-tool overrides from env var JSON: {"search_agronomy_docs": 60}
        overrides_raw = os.getenv("MCP_TOOL_TIMEOUT_OVERRIDES")
        if overrides_raw:
            try:
                parsed = json.loads(overrides_raw)
                if isinstance(parsed, dict):
                    for k, v in parsed.items():
                        try:
                            _GLOBAL_EXECUTION_POLICY._overrides[str(k)] = float(v)
                        except Exception:
                            pass
            except Exception:
                logger.warning(
                    "Invalid MCP_TOOL_TIMEOUT_OVERRIDES; must be JSON mapping tool->seconds"
                )
    return _GLOBAL_EXECUTION_POLICY


class MCPToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, MCPToolMeta] = {}
        self.register_defaults()

    def register_defaults(self) -> None:
        for name, scope in TOOL_SCOPE_MAP.items():
            self._tools[name] = MCPToolMeta(
                name=name,
                server=MCPServerKind.DB,
                scope=scope,
                timeout_seconds=90.0,
                retries=1,
            )

    def has_tool(self, tool_name: str) -> bool:
        return tool_name in self._tools

    def get_tool(self, tool_name: str) -> Optional[MCPToolMeta]:
        return self._tools.get(tool_name)

    def list_tools(
        self, server: Optional[MCPServerKind] = None
    ) -> list[dict[str, Any]]:
        items = []
        for meta in self._tools.values():
            if server and meta.server != server:
                continue
            items.append(meta.model_dump())
        items.sort(key=lambda x: x["name"])
        return items

    def sync_discovered_tools(
        self, server: MCPServerKind, discovered: Iterable[dict[str, Any]]
    ) -> None:
        """Enregistre les outils découverts dynamiquement — fail-closed.

        Audit MCP/AGUI 2026-08-26 : attribuait auparavant `DB_DATA_WRITE`
        par défaut à tout outil absent de `TOOL_SCOPE_MAP`, une posture
        fail-OPEN isolée dans un fichier par ailleurs délibérément
        fail-closed (voir `_autofill_tool_scopes`, qui ne fait que loguer
        et refuse). Un outil non cartographié n'est désormais PAS
        enregistré du tout — `has_tool()`/`get_tool()` le traitent comme
        inconnu, et tout appelant (ex: `MCPManager.call_tool`) le rejette
        en amont plutôt que de lui accorder implicitement des privilèges
        d'écriture.
        """
        for item in discovered:
            name = str(item.get("name")).strip()
            if not name or name in self._tools:
                continue
            scope = TOOL_SCOPE_MAP.get(name)
            if scope is None:
                logger.warning(
                    "MCP_SCOPE_GAP | outil découvert '%s' absent de TOOL_SCOPE_MAP "
                    "— non enregistré (fail-closed) tant qu'un scope explicite "
                    "n'est pas déclaré.",
                    name,
                )
                continue
            self._tools[name] = MCPToolMeta(
                name=name,
                server=server,
                scope=scope,
                description=str(item.get("description")),
                timeout_seconds=30.0,
                retries=1,
            )


_GLOBAL_REGISTRY: Optional[MCPToolRegistry] = None


def get_registry() -> MCPToolRegistry:
    global _GLOBAL_REGISTRY
    if _GLOBAL_REGISTRY is None:
        _GLOBAL_REGISTRY = MCPToolRegistry()
    return _GLOBAL_REGISTRY


class PermissionDenied(Exception):
    def __init__(self, tool_name: str, reason: str) -> None:
        self.tool_name = tool_name
        self.reason = reason
        super().__init__(f"[SHIELD] {tool_name}: {reason}")


class ToolExecutionTimeout(Exception):
    """Un outil a dépassé son délai. Distinct de PermissionDenied : c'est un
    problème de latence/DB, pas d'autorisation — les appelants ne doivent pas
    l'afficher comme un refus d'accès."""

    def __init__(self, tool_name: str, timeout_seconds: float) -> None:
        self.tool_name = tool_name
        self.timeout_seconds = timeout_seconds
        super().__init__(f"[TIMEOUT] {tool_name}: dépassé {timeout_seconds:.1f}s")


class HostBlockedError(Exception):
    def __init__(self, tool_name: str, agent_message: str, suggestion: str) -> None:
        self.tool_name = tool_name
        self.agent_message = agent_message
        self.suggestion = suggestion
        super().__init__(
            f"[HOST] {tool_name}: {agent_message} | Suggestion: {suggestion}"
        )


class PreflightResult:
    def __init__(self, passed: bool, reason: str = ""):
        self.passed = passed
        self.reason = reason

    def __bool__(self) -> bool:
        return self.passed


class MCPPermissionHostApp:
    """Préfiltre de sécurité (injection SQL, SQL brut, chemins sensibles).

    Point d'application RÉEL en production via ``infrastructure/mcp/runtime.py
    ::AgriDBMCPServer._run_preflight`` (instancié avec ``client=None`` — seul
    ``_preflight_scan``/``_suggest_fix`` sont utilisés, jamais ``.execute()``).
    ``client`` n'est donc plus typé sur une classe de décision de risque
    (l'ancien ``MCPPermissionClient``, supprimé — audit MCP/AGUI 2026-08-26 :
    ce moteur de risque à trois niveaux n'était jamais câblé sur aucun chemin
    de production, voir ``nodes/confirmation_gate.py`` pour le VRAI point de
    confirmation humaine). ``.execute()`` reste utilisable avec n'importe quel
    objet duck-typé exposant ``call_tool(name, arguments)``.
    """

    def __init__(self, client: Any = None, block_raw_sql: bool = True) -> None:
        self._client = client
        self._block_raw_sql = block_raw_sql
        self._sql_re = SQL_INJECTION_REGEX
        self._file_re = [re.compile(p) for p in SENSITIVE_FILE_PATTERNS]

    async def execute(
        self, tool_name: str, arguments: Dict[str, Any] | None = None
    ) -> Any:
        arguments = arguments or {}
        pf = self._preflight_scan(tool_name, arguments)
        if not pf:
            raise HostBlockedError(
                tool_name, pf.reason, self._suggest_fix(tool_name, pf.reason)
            )
        try:
            return await self._client.call_tool(tool_name, arguments)
        except PermissionDenied as pd:
            raise HostBlockedError(
                pd.tool_name, pd.reason, self._suggest_fix(pd.tool_name, pd.reason)
            ) from pd

    def list_tools(self) -> list[dict]:
        return self._client.list_tools()

    def _preflight_scan(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> PreflightResult:
        values = self._flatten_strings(arguments)
        for val in values:
            for rx in self._sql_re:
                if rx.search(val):
                    return PreflightResult(False, f"SQL suspect pattern: {rx.pattern}")
            if self._block_raw_sql and self._looks_like_raw_sql(val):
                return PreflightResult(False, "Raw SQL is forbidden")
            for rx in self._file_re:
                if rx.search(val):
                    # Motif de fichier sensible détecté (ex: .env, id_rsa) —
                    # laissé passer (ce n'est pas forcément malveillant : un
                    # nom de fichier légitime peut matcher), mais journalisé
                    # pour investigation a posteriori plutôt que bloqué à tort.
                    logger.warning(
                        "PREFLIGHT_SENSITIVE_FILE_PATTERN | tool=%s | pattern=%s",
                        tool_name,
                        rx.pattern,
                    )
        return PreflightResult(True)

    def _suggest_fix(self, tool_name: str, reason: str) -> str:
        scope = TOOL_SCOPE_MAP.get(tool_name, PermissionScope.DB_DATA_WRITE)
        if scope == PermissionScope.DB_SCHEMA_MODIFY:
            return "Enable maintenance mode for schema changes."
        if "SQL" in reason:
            return f"Use structured arguments for tool '{tool_name}', not raw SQL."
        return f"Check arguments and retry '{tool_name}'."

    @staticmethod
    def _flatten_strings(obj: Any, acc: Optional[List[str]] = None) -> List[str]:
        if acc is None:
            acc = []
        if isinstance(obj, str):
            acc.append(obj)
        elif isinstance(obj, dict):
            for v in obj.values():
                MCPPermissionHostApp._flatten_strings(v, acc)
        elif isinstance(obj, (list, tuple)):
            for v in obj:
                MCPPermissionHostApp._flatten_strings(v, acc)
        return acc

    @staticmethod
    def _looks_like_raw_sql(val: str) -> bool:
        vu = val.upper().strip()
        starters = (
            "SELECT ",
            "INSERT ",
            "UPDATE ",
            "DELETE ",
            "DROP ",
            "ALTER ",
            "CREATE ",
            "TRUNCATE ",
        )
        if not any(vu.startswith(s) for s in starters):
            return False
        secondary = ("FROM ", "WHERE ", "SET ", "INTO ", "TABLE ", "VALUES")
        return any(s in vu for s in secondary)


class MCPSessionManager:
    def __init__(
        self,
        host: MCPPermissionHostApp,
        session_id: str = "unknown",
        trust_window_seconds: int = 300,
    ) -> None:
        self._host = host
        self.session_id = session_id
        self._trust_window = trust_window_seconds
        self._trusted_tools: Dict[str, float] = {}

    async def execute(
        self, tool_name: str, arguments: Dict[str, Any] | None = None
    ) -> Any:
        return await self._host.execute(tool_name, arguments)

    async def safe_read(
        self, tool_name: str, arguments: Dict[str, Any] | None = None
    ) -> Any:
        return await self._host.execute(tool_name, arguments)


class AsyncMCPBackend(Protocol):
    async def call_tool(self, name: str, arguments) -> Any: ...

    async def list_tools(self) -> list[dict[str, Any]]: ...


class DBInProcessBackend:
    async def call_tool(self, name: str, arguments) -> Any:
        from ladini.protocols.mcp.servers.h import TOOL_HANDLERS

        fn = TOOL_HANDLERS.get(name)
        if fn is None:
            raise ValueError(f"Unknown DB tool: {name}")
        raw = await fn(**arguments)
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except Exception:
                return {"result": raw}
        return raw

    async def list_tools(self) -> list[dict[str, Any]]:
        from ladini.protocols.mcp.servers.h import TOOL_DESCRIPTIONS

        return [{"name": n, "description": d} for n, d in TOOL_DESCRIPTIONS.items()]


class MCPManager:
    def __init__(self, registry: MCPToolRegistry | None = None) -> None:
        self.registry = registry or get_registry()

        self._backends: dict[MCPServerKind, AsyncMCPBackend] = {
            MCPServerKind.DB: DBInProcessBackend(),
        }
        self._ready = False

    async def ensure_ready(self) -> None:
        if self._ready:
            return

        # Synchronisation uniquement pour le backend DB
        db_tools = await self._backends[MCPServerKind.DB].list_tools()
        self.registry.sync_discovered_tools(MCPServerKind.DB, db_tools)

        self._ready = True

    async def call_tool(
        self, tool_name: str, arguments: Dict[str, Any] | None = None
    ) -> Any:
        await self.ensure_ready()
        arguments = arguments or {}
        meta = self.registry.get_tool(tool_name)
        if meta is None:
            raise ValueError(f"Unknown tool '{tool_name}'")

        backend = self._backends[meta.server]
        attempts = 1 + max(0, int(meta.retries))
        last_error: Optional[Exception] = None

        for attempt in range(1, attempts + 1):
            try:
                return await backend.call_tool(tool_name, arguments)
            except Exception as exc:
                last_error = exc
                if attempt >= attempts:
                    break

        raise RuntimeError(f"MCP call failed for '{tool_name}': {last_error}")

    async def list_tools(self) -> dict[str, list[dict[str, Any]]]:
        await self.ensure_ready()
        # Exposition uniquement des outils DB
        return {
            "db_tools": self.registry.list_tools(MCPServerKind.DB),
        }

# NOTE (audit MCP/AGUI 2026-08-26) : `ShieldHub` / `MCPShield` /
# `UnifiedMCPClient` ont été supprimés — ce sous-arbre construisait un moteur
# de décision de risque à trois niveaux (`RiskLevel`, `TOOL_RISK_MAP`,
# `HITL_REQUIRED`) dont le SEUL appelant dans tout le dépôt était un script de
# test manuel (`infrastructure/mcp/main.py`, adapté pour utiliser
# `AgriDBMCPServer` directement — voir ce fichier). Le chemin RÉELLEMENT
# emprunté par chaque appel d'outil de l'agent
# (`AgriDBMCPServer.call_tool`) n'a jamais consulté ce moteur : il applique
# uniquement `TOOL_SCOPE_MAP` (fail-closed) + `MCPPermissionHostApp`
# (préflight SQL/fichiers, toujours actif ci-dessus). Le garder aurait
# perpétué l'illusion d'un garde-fou de confirmation à risque au niveau
# protocole MCP qui n'a jamais existé en production. Le VRAI point de
# confirmation humaine (HITL) est `nodes/confirmation_gate.py`, en amont de
# tout appel d'outil, dans le graphe de l'agent.
