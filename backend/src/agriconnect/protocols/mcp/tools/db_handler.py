"""DB-backed MCP tools (moved from providers/resource_provider).

This module registers many MCP tools against the shared runtime MCP. It
depends on `agriconnect.infrastructure.mcp.runtime` for the `mcp` instance
and runtime helpers.
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

# Import MCP registration helpers from core infrastructure
from agriconnect.infrastructure.mcp.runtime import mcp, runtime, _log_call


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
async def create_agent_action(agent_name: str, action_type: str, payload: dict, user_id: str | None = None, priority: str | None = "MEDIUM", order_id: str | None = None, ai_reasoning: str | None = None, ctx=None) -> str:
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
        await ctx.info(f"update_action_status id={action_id} â†’ {new_status}")
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

    try:
        await runtime.ensure_initialized()
    except Exception as exc_init:
        return GenericResult(status="error", message=f"DB init failed: {exc_init}").model_dump_json()

    db = runtime.db
    if db is None:
        return GenericResult(status="error", message="DB not initialized").model_dump_json()

    try:
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
async def create_product(producer_id: str, name: str, price: float, quantity_for_sale: float, unit: str = "KG", category_label: str = "CÃ©rÃ©ales", sub_category_id: str | None = None, description: str | None = None, local_names: Any = None, ctx=None) -> str:
    if ctx:
        await ctx.info(f"create_product {name} for producer={producer_id}")
    db = runtime.db
    if db is None:
        return GenericResult(status="error", message="DB not initialized").model_dump_json()
    try:
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
    return await get_stocks(farm_id=farm_id, ctx=ctx)
