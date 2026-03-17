"""Shared implementations for AgriDB MCP tools.

These functions are the pure logic extracted from the MCP server. They
accept a `db` service instance and return the JSON string response expected
by the MCP wrapper functions.

Imports from the server module are performed lazily inside functions to avoid
import cycles between the server and tools modules.
"""
from __future__ import annotations

import json
import csv
from typing import Any, Dict, List, Optional
import uuid

from agriconnect.core.models import (
    GenericResult,
    CreateProductInput,
    PrepareTransactionInput,
    UpdateStockInput,
    CreateAgentActionInput,
    CreateAuctionInput,
    _wrap,
)

# Import MCP registration helpers from the minimal app and infrastructure modules
from agriconnect.protocols.mcp.app import mcp
from agriconnect.protocols.mcp.infrastructure import runtime, _log_call


# --- Thin MCP wrappers (register against `mcp`) --------------------------------
@mcp.tool()
async def get_user_profile(user_id: str, ctx=None) -> str:
    if ctx:
        await ctx.info(f"get_user_profile user_id={user_id}")
    db = runtime.db
    if db is None:
        return GenericResult(status="error", message="DB not initialized").model_dump_json()
    try:
        profile = await db.get_user_by_id(user_id)
        if not profile:
            res = GenericResult(status="error", message="Utilisateur introuvable.").model_dump_json()
        else:
            res = GenericResult(status="ok", data=profile).model_dump_json()
    except Exception as exc:
        res = GenericResult(status="error", message=str(exc)).model_dump_json()
    _log_call("get_user_profile", {"user_id": user_id}, res)
    return res


@mcp.tool()
async def get_user_by_phone(phone: str, ctx=None) -> str:
    if ctx:
        await ctx.info(f"get_user_by_phone phone={phone}")
    db = runtime.db
    if db is None:
        return GenericResult(status="error", message="DB not initialized").model_dump_json()
    try:
        profile = await db.get_user_by_phone(phone)
        if not profile:
            res = GenericResult(status="error", message="Utilisateur introuvable.").model_dump_json()
        else:
            res = GenericResult(status="ok", data=profile).model_dump_json()
    except Exception as exc:
        res = GenericResult(status="error", message=str(exc)).model_dump_json()
    _log_call("get_user_by_phone", {"phone": phone}, res)
    return res


@mcp.tool()
async def identify_or_create_user(phone: str, name: str = "", zone_id: str = "", ctx=None) -> str:
    if ctx:
        await ctx.info(f"identify_or_create_user phone={phone}")
    db = runtime.db
    if db is None:
        return GenericResult(status="error", message="DB not initialized").model_dump_json()
    try:
        raw = await db.identify_or_create_user(phone, name or None, zone_id or None)
        res = GenericResult(status="ok", data=raw).model_dump_json()
    except Exception as exc:
        res = GenericResult(status="error", message=str(exc)).model_dump_json()
    _log_call("identify_or_create_user", {"phone": phone, "name": name, "zone_id": zone_id}, res)
    return res


@mcp.tool()
async def update_stock_with_movement(farm_id: str, item_name: str, quantity_change: float, reason: str, ctx=None) -> str:
    if ctx:
        await ctx.info(f"update_stock farm_id={farm_id} item={item_name} delta={quantity_change}")
    db = runtime.db
    if db is None:
        return GenericResult(status="error", message="DB not initialized").model_dump_json()
    try:
        raw = await db.adjust_stock(farm_id=farm_id, item_name=item_name, quantity_change=quantity_change, reason=reason)
        res = GenericResult(status="ok", data=raw).model_dump_json()
    except Exception as exc:
        res = GenericResult(status="error", message=str(exc)).model_dump_json()
    _log_call("update_stock_with_movement", {"farm_id": farm_id, "item_name": item_name}, res)
    return res


@mcp.tool()
async def prepare_transaction_staging(product_id: str, quantity_kg: float, price_fcfa_per_unit: float, buyer_phone: str, zone_id: str = "", source: str = "WHATSAPP", ctx=None) -> str:
    if ctx:
        await ctx.info(f"prepare_transaction product={product_id} qty={quantity_kg}")
    db = runtime.db
    if db is None:
        return GenericResult(status="error", message="DB not initialized").model_dump_json()
    try:
        raw = await db.prepare_transaction_staging({
            "product_id": product_id,
            "quantity_kg": quantity_kg,
            "price_fcfa_per_unit": price_fcfa_per_unit,
            "buyer_phone": buyer_phone,
            "zone_id": zone_id or None,
            "source": source,
        })
        res = GenericResult(status="ok", data=raw).model_dump_json()
    except Exception as exc:
        res = GenericResult(status="error", message=str(exc)).model_dump_json()
    _log_call("prepare_transaction_staging", {"product_id": product_id, "buyer_phone": buyer_phone}, res)
    return res


@mcp.tool()
async def create_order(product_id: str, quantity: float, buyer_phone: str, buyer_name: str = "", zone_id: str = "", source: str = "WHATSAPP", buyer_id: str = "", organization_id: str = "", payment_method: str = "CASH", ctx=None) -> str:
    if ctx:
        await ctx.info(f"create_order product={product_id} qty={quantity}")
    db = runtime.db
    if db is None:
        return GenericResult(status="error", message="DB not initialized").model_dump_json()
    try:
        raw = await db.create_order(product_id=product_id, quantity=quantity, buyer_phone=buyer_phone, buyer_name=buyer_name or None, zone_id=zone_id or None, source=source, buyer_id=buyer_id or None, organization_id=organization_id or None, payment_method=payment_method)
        if isinstance(raw, dict) and raw.get("error"):
            res = GenericResult(status="error", data=raw, message=raw.get("error")).model_dump_json()
        else:
            res = GenericResult(status="ok", data=raw).model_dump_json()
    except Exception as exc:
        res = GenericResult(status="error", message=str(exc)).model_dump_json()
    _log_call("create_order", {"product_id": product_id, "buyer_phone": buyer_phone}, res)
    return res


@mcp.tool()
async def commit_staged_transaction(transaction_id: str, approved: bool = True, ctx=None) -> str:
    if ctx:
        await ctx.info(f"commit_staged_transaction tx={transaction_id} approved={approved}")
    db = runtime.db
    if db is None:
        return GenericResult(status="error", message="DB not initialized").model_dump_json()
    try:
        raw = await db.commit_staged_transaction(transaction_id, approved=approved)
        res = GenericResult(status="ok", data=raw).model_dump_json()
    except Exception as exc:
        res = GenericResult(status="error", message=str(exc)).model_dump_json()
    _log_call("commit_staged_transaction", {"transaction_id": transaction_id}, res)
    return res


@mcp.tool()
async def get_orders(buyer_id: str = "", buyer_phone: str = "", status: str = "", limit: int = 20, ctx=None) -> str:
    if ctx:
        await ctx.info(f"get_orders buyer_id={buyer_id} limit={limit}")
    db = runtime.db
    if db is None:
        return GenericResult(status="error", message="DB not initialized").model_dump_json()
    try:
        raw = await db.get_orders(buyer_id=buyer_id or None, buyer_phone=buyer_phone or None, status=status or None, limit=limit)
        res = GenericResult(status="ok", data=raw).model_dump_json()
    except Exception as exc:
        res = GenericResult(status="error", message=str(exc)).model_dump_json()
    _log_call("get_orders", {"buyer_id": buyer_id, "buyer_phone": buyer_phone}, res)
    return res


@mcp.tool()
async def create_agent_action(agent_name: str, action_type: str, payload: dict, user_id: str | None = None, priority: str = "MEDIUM", order_id: str | None = None, ai_reasoning: str | None = None, ctx=None) -> str:
    if ctx:
        await ctx.info(f"create_agent_action agent={agent_name} type={action_type}")
    db = runtime.db
    if db is None:
        return GenericResult(status="error", message="DB not initialized").model_dump_json()
    try:
        raw = await db.create_agent_action(agent_name=agent_name, action_type=action_type, payload=payload, user_id=user_id or None, priority=priority, order_id=order_id or None, ai_reasoning=ai_reasoning or None)
        res = GenericResult(status="ok", data=raw).model_dump_json()
    except Exception as exc:
        res = GenericResult(status="error", message=str(exc)).model_dump_json()
    _log_call("create_agent_action", {"agent_name": agent_name, "action_type": action_type}, res)
    return res


@mcp.tool()
async def get_pending_actions(agent_name: str = "", limit: int = 20, ctx=None) -> str:
    if ctx:
        await ctx.info(f"get_pending_actions agent={agent_name}")
    db = runtime.db
    if db is None:
        return GenericResult(status="error", message="DB not initialized").model_dump_json()
    try:
        raw = await db.get_pending_actions(agent_name=agent_name or None, limit=limit)
        res = GenericResult(status="ok", data=raw).model_dump_json()
    except Exception as exc:
        res = GenericResult(status="error", message=str(exc)).model_dump_json()
    _log_call("get_pending_actions", {"agent_name": agent_name}, res)
    return res


@mcp.tool()
async def update_action_status(action_id: str, new_status: str, admin_notes: str | None = None, validated_by_id: str | None = None, ctx=None) -> str:
    if ctx:
        await ctx.info(f"update_action_status id={action_id} → {new_status}")
    db = runtime.db
    if db is None:
        return GenericResult(status="error", message="DB not initialized").model_dump_json()
    try:
        raw = await db.update_action_status(action_id, new_status, admin_notes=admin_notes or None, validated_by_id=validated_by_id or None)
        if raw is None:
            res = GenericResult(status="error", message="action not found").model_dump_json()
        else:
            res = GenericResult(status="ok", data=raw).model_dump_json()
    except Exception as exc:
        res = GenericResult(status="error", message=str(exc)).model_dump_json()
    _log_call("update_action_status", {"action_id": action_id, "new_status": new_status}, res)
    return res


@mcp.tool()
async def list_products(producer_id: str, ctx=None) -> str:
    if ctx:
        await ctx.info(f"list_products producer_id={producer_id}")

    # Ensure the runtime has an initialized DB engine/session. This is
    # safe to call from an existing event loop and will call `init_db()`
    # if needed.
    try:
        await runtime.ensure_initialized()
    except Exception as exc_init:
        return GenericResult(status="error", message=f"DB init failed: {exc_init}").model_dump_json()

    db = runtime.db
    if db is None:
        return GenericResult(status="error", message="DB not initialized").model_dump_json()

    try:
        # sanitize input: trim whitespace and validate UUID format
        prod_id = producer_id.strip() if isinstance(producer_id, str) else producer_id
        try:
            import uuid

            _ = uuid.UUID(prod_id)
        except Exception:
            return GenericResult(status="error", message="Invalid producer_id format").model_dump_json()

        raw = await db.list_products(prod_id)
        res = GenericResult(status="ok", data=raw).model_dump_json()
    except Exception as exc:
        res = GenericResult(status="error", message=str(exc)).model_dump_json()

    _log_call("list_products", {"producer_id": producer_id}, res)
    return res


@mcp.tool()
async def search_products(product_name: str, zone_id: str | None = None, limit: int = 10, ctx=None) -> str:
    if ctx:
        await ctx.info(f"search_products name={product_name} zone={zone_id}")
    db = runtime.db
    if db is None:
        return GenericResult(status="error", message="DB not initialized").model_dump_json()
    try:
        raw = await db.search_products(product_name, zone_id=zone_id or None, limit=limit)
        res = GenericResult(status="ok", data=raw).model_dump_json()
    except Exception as exc:
        res = GenericResult(status="error", message=str(exc)).model_dump_json()
    _log_call("search_products", {"product_name": product_name}, res)
    return res


@mcp.tool()
async def create_product(producer_id: str, name: str, price: float, quantity_for_sale: float, unit: str = "KG", category_label: str = "Céréales", sub_category_id: str | None = None, description: str | None = None, local_names: Any = None, ctx=None) -> str:
    if ctx:
        await ctx.info(f"create_product {name} for producer={producer_id}")
    db = runtime.db
    if db is None:
        return GenericResult(status="error", message="DB not initialized").model_dump_json()
    try:
        # Attempt to normalize local_names as the old impl did
        names_list = None
        if local_names:
            if isinstance(local_names, list):
                names_list = [str(n).strip() for n in local_names if str(n).strip()]
            elif isinstance(local_names, str):
                try:
                    parsed = json.loads(local_names)
                    if isinstance(parsed, list):
                        names_list = [str(n).strip() for n in parsed if str(n).strip()]
                except Exception:
                    try:
                        reader = csv.reader([local_names])
                        names_list = [n.strip() for n in next(reader) if n.strip()]
                    except Exception:
                        names_list = [n.strip() for n in local_names.split(",") if n.strip()]
        raw = await db.create_product(producer_id=producer_id, name=name, price=price, quantity_for_sale=quantity_for_sale, unit=unit, category_label=category_label, sub_category_id=sub_category_id or None, description=description or None, local_names=names_list)
        if isinstance(raw, dict) and raw.get("error"):
            res = GenericResult(status="error", data=raw, message=raw.get("error")).model_dump_json()
        else:
            res = GenericResult(status="ok", data=raw).model_dump_json()
    except Exception as exc:
        res = GenericResult(status="error", message=str(exc)).model_dump_json()
    _log_call("create_product", {"producer_id": producer_id, "name": name}, res)
    return res


@mcp.tool()
async def create_auction(buyer_id: str, sub_category_id: str, quantity: float, max_price_per_unit: float, deadline: str, unit: str = "TONNE", target_zone_id: str | None = None, ctx=None) -> str:
    if ctx:
        await ctx.info(f"create_auction buyer={buyer_id} qty={quantity}")
    db = runtime.db
    if db is None:
        return GenericResult(status="error", message="DB not initialized").model_dump_json()
    try:
        raw = await db.create_auction(buyer_id=buyer_id, sub_category_id=sub_category_id, quantity=quantity, max_price_per_unit=max_price_per_unit, deadline=deadline, unit=unit, target_zone_id=target_zone_id or None)
        res = GenericResult(status="ok", data=raw).model_dump_json()
    except Exception as exc:
        res = GenericResult(status="error", message=str(exc)).model_dump_json()
    _log_call("create_auction", {"buyer_id": buyer_id}, res)
    return res


@mcp.tool()
async def place_bid(auction_id: str, producer_id: str, offered_price: float, ctx=None) -> str:
    if ctx:
        await ctx.info(f"place_bid auction={auction_id} price={offered_price}")
    db = runtime.db
    if db is None:
        return GenericResult(status="error", message="DB not initialized").model_dump_json()
    try:
        raw = await db.place_bid(auction_id, producer_id, offered_price)
        if isinstance(raw, dict) and raw.get("error"):
            res = GenericResult(status="error", data=raw, message=raw.get("error")).model_dump_json()
        else:
            res = GenericResult(status="ok", data=raw).model_dump_json()
    except Exception as exc:
        res = GenericResult(status="error", message=str(exc)).model_dump_json()
    _log_call("place_bid", {"auction_id": auction_id, "producer_id": producer_id}, res)
    return res


@mcp.tool()
async def get_open_auctions(zone_id: str | None = None, limit: int = 20, ctx=None) -> str:
    if ctx:
        await ctx.info(f"get_open_auctions zone={zone_id}")
    db = runtime.db
    if db is None:
        return GenericResult(status="error", message="DB not initialized").model_dump_json()
    try:
        raw = await db.get_open_auctions(zone_id=zone_id or None, limit=limit)
        res = GenericResult(status="ok", data=raw).model_dump_json()
    except Exception as exc:
        res = GenericResult(status="error", message=str(exc)).model_dump_json()
    _log_call("get_open_auctions", {"zone_id": zone_id}, res)
    return res


@mcp.tool()
async def get_farms(producer_id: str, ctx=None) -> str:
    if ctx:
        await ctx.info(f"get_farms producer_id={producer_id}")
    db = runtime.db
    if db is None:
        return GenericResult(status="error", message="DB not initialized").model_dump_json()
    try:
        raw = await db.get_farms(producer_id)
        res = GenericResult(status="ok", data=raw).model_dump_json()
    except Exception as exc:
        res = GenericResult(status="error", message=str(exc)).model_dump_json()
    _log_call("get_farms", {"producer_id": producer_id}, res)
    return res


@mcp.tool()
async def get_stocks(farm_id: str, ctx=None) -> str:
    if ctx:
        await ctx.info(f"get_stocks farm_id={farm_id}")
    db = runtime.db
    if db is None:
        return GenericResult(status="error", message="DB not initialized").model_dump_json()
    try:
        raw = await db.get_stocks(farm_id)
        res = GenericResult(status="ok", data=raw).model_dump_json()
    except Exception as exc:
        res = GenericResult(status="error", message=str(exc)).model_dump_json()
    _log_call("get_stocks", {"farm_id": farm_id}, res)
    return res


@mcp.tool()
async def get_farm_stocks(farm_id: str, ctx=None) -> str:
    # Alias used by some agents
    return await get_stocks(farm_id=farm_id, ctx=ctx)


@mcp.tool()
async def add_stock(
    farm_id: str,
    item_name: str,
    quantity: float,
    unit: str = "KG",
    stock_type: str = "HARVEST",
    reason: str = "Ajout via MCP",
    warehouse_id: str | None = None,
    organization_id: str | None = None,
    ctx=None,
) -> str:
    if ctx:
        await ctx.info(f"add_stock farm_id={farm_id} item={item_name} qty={quantity}")
    db = runtime.db
    if db is None:
        return GenericResult(status="error", message="DB not initialized").model_dump_json()
    try:
        raw = await db.add_stock(
            farm_id=farm_id,
            item_name=item_name,
            quantity=quantity,
            unit=unit,
            stock_type=stock_type,
            reason=reason,
            warehouse_id=warehouse_id,
            organization_id=organization_id,
        )
        res = GenericResult(status="ok", data=raw).model_dump_json()
    except Exception as exc:
        res = GenericResult(status="error", message=str(exc)).model_dump_json()
    _log_call("add_stock", {"farm_id": farm_id, "item_name": item_name}, res)
    return res


@mcp.tool()
async def remove_stock(
    farm_id: str,
    item_name: str,
    quantity: float,
    reason: str = "Retrait",
    movement_type: str = "OUT",
    ctx=None,
) -> str:
    if ctx:
        await ctx.info(f"remove_stock farm_id={farm_id} item={item_name} qty={quantity}")
    db = runtime.db
    if db is None:
        return GenericResult(status="error", message="DB not initialized").model_dump_json()
    try:
        raw = await db.remove_stock(
            farm_id=farm_id,
            item_name=item_name,
            quantity=quantity,
            reason=reason,
            movement_type=movement_type,
        )
        res = GenericResult(status="ok", data=raw).model_dump_json()
    except Exception as exc:
        res = GenericResult(status="error", message=str(exc)).model_dump_json()
    _log_call("remove_stock", {"farm_id": farm_id, "item_name": item_name}, res)
    return res


@mcp.tool()
async def add_expense(farm_id: str, label: str, amount: float, category: str = "OTHER", ctx=None) -> str:
    if ctx:
        await ctx.info(f"add_expense farm_id={farm_id} label={label} amount={amount}")
    db = runtime.db
    if db is None:
        return GenericResult(status="error", message="DB not initialized").model_dump_json()
    try:
        raw = await db.add_expense(farm_id=farm_id, label=label, amount=amount, category=category)
        res = GenericResult(status="ok", data=raw).model_dump_json()
    except Exception as exc:
        res = GenericResult(status="error", message=str(exc)).model_dump_json()
    _log_call("add_expense", {"farm_id": farm_id, "label": label}, res)
    return res


@mcp.tool()
async def get_expenses(farm_id: str, category: str | None = None, limit: int = 50, ctx=None) -> str:
    if ctx:
        await ctx.info(f"get_expenses farm_id={farm_id} limit={limit}")
    db = runtime.db
    if db is None:
        return GenericResult(status="error", message="DB not initialized").model_dump_json()
    try:
        raw = await db.get_expenses(farm_id=farm_id, category=category, limit=limit)
        res = GenericResult(status="ok", data=raw).model_dump_json()
    except Exception as exc:
        res = GenericResult(status="error", message=str(exc)).model_dump_json()
    _log_call("get_expenses", {"farm_id": farm_id, "limit": limit}, res)
    return res


@mcp.tool()
async def get_expense_summary(farm_id: str, ctx=None) -> str:
    if ctx:
        await ctx.info(f"get_expense_summary farm_id={farm_id}")
    db = runtime.db
    if db is None:
        return GenericResult(status="error", message="DB not initialized").model_dump_json()
    try:
        raw = await db.get_expense_summary(farm_id=farm_id)
        res = GenericResult(status="ok", data=raw).model_dump_json()
    except Exception as exc:
        res = GenericResult(status="error", message=str(exc)).model_dump_json()
    _log_call("get_expense_summary", {"farm_id": farm_id}, res)
    return res


@mcp.tool()
async def get_producer_dashboard(producer_id: str, ctx=None) -> str:
    if ctx:
        await ctx.info(f"get_producer_dashboard producer_id={producer_id}")
    db = runtime.db
    if db is None:
        return GenericResult(status="error", message="DB not initialized").model_dump_json()
    try:
        raw = await db.get_producer_dashboard(producer_id)
        res = GenericResult(status="ok", data=raw).model_dump_json()
    except Exception as exc:
        res = GenericResult(status="error", message=str(exc)).model_dump_json()
    _log_call("get_producer_dashboard", {"producer_id": producer_id}, res)
    return res


@mcp.tool()
async def register_surplus_offer(
    commodity: str,
    quantity: float,
    location: str,
    contact: str = "TBD",
    user_id: str | None = None,
    ctx=None,
) -> str:
    """Persist a surplus declaration in an auditable way.

    The current canonical async service does not expose a dedicated
    `surplus_offers` writer. We persist this as an `agent_action` payload,
    which keeps it in the DB with governance metadata.
    """
    if ctx:
        await ctx.info(f"register_surplus_offer commodity={commodity} qty={quantity} location={location}")
    db = runtime.db
    if db is None:
        return GenericResult(status="error", message="DB not initialized").model_dump_json()
    try:
        # Prefer dedicated surplus_offers persistence when available on the
        # async DB service. Fall back to agent_action if not implemented.
        try:
            raw = await db.create_surplus_offer(
                user_id=user_id,
                product_name=commodity,
                quantity_kg=quantity,
                price_kg=None,
                zone_id=None,
                location=location,
                channel="mcp",
            )
            res = GenericResult(status="ok", data={"record_type": "surplus_offer", "offer": raw}).model_dump_json()
        except AttributeError:
            # Older DB service does not expose create_surplus_offer
            payload = {"commodity": commodity, "quantity": quantity, "location": location, "contact": contact}
            raw = await db.create_agent_action(
                agent_name="MarketCoach",
                action_type="REGISTER_SURPLUS",
                payload=payload,
                user_id=user_id,
                priority="MEDIUM",
                ai_reasoning="Surplus declaration captured from market flow",
            )
            res = GenericResult(status="ok", data={"record_type": "agent_action", "action": raw}).model_dump_json()
    except Exception as exc:
        res = GenericResult(status="error", message=str(exc)).model_dump_json()
    _log_call("register_surplus_offer", {"commodity": commodity, "quantity": quantity, "location": location}, res)
    return res


@mcp.resource("db://status")
async def db_status() -> str:
    return json.dumps({"server": "AgriConnect Database MCP Server", "status": "running"})


@mcp.tool()
async def persist_conversation(user_id: str, query_json: str, response_json: str, agent_type: str = "mcp_shield_audit", ctx=None) -> str:
    """Persist a conversation/audit record into the runtime DB.

    This tool is intentionally minimal: it allows clients to route audit
    writes through the MCP server so the server-side `runtime.db` handles
    persistence with the canonical schema.
    """
    db = runtime.db
    if db is None:
        return GenericResult(status="error", message="DB not initialized").model_dump_json()
    try:
        # Normalize user_id to a valid UUID string for DB insertion. If the
        # provided `user_id` is not a valid UUID, generate a placeholder.
        try:
            uuid.UUID(str(user_id))
            user_uuid = str(user_id)
        except Exception:
            user_uuid = str(uuid.uuid4())

        # AgriDatabaseService.log_conversation expects positional args
        # (user_id, query, response, agent_type, ...).
        raw = await db.log_conversation(
            user_uuid,
            query_json,
            response_json,
            agent_type=agent_type,
        )
        return GenericResult(status="ok", data=raw).model_dump_json()
    except Exception as exc:
        return GenericResult(status="error", message=str(exc)).model_dump_json()



# Explicit tool map exported for defensive discovery by the server.
# This avoids fragile introspection of FastMCP internals — servers should
# import `TOOL_MAP` to obtain a stable mapping name->callable.
TOOL_MAP = {
    "get_user_profile": get_user_profile,
    "get_user_by_phone": get_user_by_phone,
    "identify_or_create_user": identify_or_create_user,
    "update_stock_with_movement": update_stock_with_movement,
    "prepare_transaction_staging": prepare_transaction_staging,
    "create_order": create_order,
    "commit_staged_transaction": commit_staged_transaction,
    "get_orders": get_orders,
    "create_agent_action": create_agent_action,
    "get_pending_actions": get_pending_actions,
    "update_action_status": update_action_status,
    "list_products": list_products,
    "search_products": search_products,
    "create_product": create_product,
    "create_auction": create_auction,
    "place_bid": place_bid,
    "get_open_auctions": get_open_auctions,
    "get_farms": get_farms,
    "get_stocks": get_stocks,
    "get_farm_stocks": get_farm_stocks,
    "add_stock": add_stock,
    "remove_stock": remove_stock,
    "add_expense": add_expense,
    "get_expenses": get_expenses,
    "get_expense_summary": get_expense_summary,
    "get_producer_dashboard": get_producer_dashboard,
    "register_surplus_offer": register_surplus_offer,
    "persist_conversation": persist_conversation,
}

__all__ = ["TOOL_MAP"]


if __name__ == "__main__":
    """Quick CLI to test MCP tool functions in this module.

    Usage:
      python db_tools.py [tool_name] [arg1] [arg2] ...

    Defaults to calling `list_products` with a placeholder producer id.
    """
    import sys
    import asyncio

    TOOL = sys.argv[1] if len(sys.argv) > 1 else "list_products"
    ARGS = sys.argv[2:]

    async def _cli_main():
        try:
            async with runtime.lifespan():
                fn = globals().get(TOOL)
                if not fn or not asyncio.iscoroutinefunction(fn):
                    print(f"Unknown or non-async tool: {TOOL}")
                    return
                try:
                    result = await fn(*ARGS)
                    print(result)
                except TypeError as te:
                    print("Argument error:", te)
                except Exception as exc:
                    print("Error executing tool:", exc)
        except Exception as e:
            print("Runtime initialization failed:", e)

    asyncio.run(_cli_main())
