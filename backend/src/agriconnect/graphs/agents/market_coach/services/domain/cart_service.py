from __future__ import annotations

"""Cart domain service — isolates buyer cart orchestration helpers."""

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("AgriConnect.Market.CartService")

from agriconnect.graphs.agents.market_coach.flows.common.menu_contracts import (
    MenuOption,
    MenuRequest,
)
from agriconnect.graphs.agents.market_coach.flows.common.menu_text import (
    render_cart_actions_hint,
    render_selection_prompt,
)
from agriconnect.graphs.agents.market_coach.services.mcp.gateway import (
    AgentActionGateway,
    PreorderGateway,
    ProductGateway,
    StockGateway,
)
from agriconnect.graphs.agents.market_coach.utils import MarketRuntime, is_success_response

from .buyer_common import SUPPORT_FOOTER, with_support_footer


@dataclass
class CartDomainService:
    mc_runtime: MarketRuntime

    async def resolve_product_vendors(
        self,
        phone: str,
        product_name: str,
    ) -> Tuple[List[Dict[str, Any]], bool]:
        if not product_name:
            return [], False
        res = await ProductGateway(self.mc_runtime).search_products(
            product=str(product_name),
            phone=str(phone),
        )
        if not is_success_response(res):
            return [], False
        results = res.get("results") or (res.get("data") or {}).get("results") or []
        if not results:
            return [], False

        vendors: List[Dict[str, Any]] = []
        seen_ids: set[str] = set()
        for item in results:
            pid = str(item.get("id") or "")
            producer_id = item.get("producer_id") or item.get("vendor_id") or ""
            key = f"{pid}:{producer_id}"
            if key in seen_ids:
                continue
            seen_ids.add(key)
            source_type = _infer_source_type(item)
            vendors.append(
                {
                    "product_id": pid,
                    # Pour une PRODUCTION FUTURE, `id` renvoyé par search_products est
                    # l'id du MarketOffer → requis par reserve_future_offer.
                    "market_offer_id": pid if source_type == "FUTURE" else None,
                    "crop_cycle_id": item.get("crop_cycle_id"),
                    "name": _clean_product_name(item.get("name") or product_name),
                    "price": float(item.get("price") or 0.0),
                    "unit": str(item.get("unit") or "KG").upper(),
                    "vendor": item.get("vendor"),
                    "vendor_name": item.get("vendor_name")
                    or item.get("producer_name")
                    or item.get("vendor")
                    or "Producteur",
                    "producer_id": producer_id,
                    "zone": item.get("zone") or item.get("zone_name") or "",
                    "source_type": source_type,
                    "is_auction": source_type == "AUCTION",
                    "available_qty": item.get("available_quantity")
                    or item.get("quantity_for_sale"),
                    "estimated_available_at": item.get("estimated_available_at"),
                }
            )
        return vendors, len(vendors) > 1

    def build_product_selection_menu(
        self,
        product_name: str,
        vendors: List[Dict[str, Any]],
        *,
        extra_context: Optional[Dict[str, Any]] = None,
        post_hint: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], MenuRequest]:
        lines = [f"🔍 *Producteurs disponibles pour « {product_name} » :*\n"]
        options: List[MenuOption] = []

        for i, v in enumerate(vendors, start=1):
            source_tag = ""
            if v.get("source_type") == "PROCUREMENT":
                source_tag = " 📋 Appel d'offres"
            elif v.get("source_type") == "FUTURE":
                eta = v.get("estimated_available_at") or "date à confirmer"
                source_tag = f" ⏳ Future ({eta})"
            elif v.get("is_auction"):
                source_tag = " 🏷️ Enchère"

            zone_info = f" ({v['zone']})" if v.get("zone") else ""
            qty_info = f" — Dispo: {v['available_qty']}" if v.get("available_qty") else ""
            label = (
                f"{v['vendor_name']}{zone_info} — {v['price']} FCFA/{v['unit']}"
                f"{qty_info}{source_tag}"
            )
            lines.append(f"*{i}.* {label}")
            options.append(MenuOption(index=str(i), label=label, value=v.get("producer_id") or str(i)))

        lines.append(render_selection_prompt(noun="producteur"))
        if post_hint:
            lines.append(post_hint.strip())
        menu_text = "\n".join(lines)

        menu = MenuRequest(
            title=f"Choix producteur — {product_name}",
            options=options,
            kind="product_vendor",
            metadata={"product_name": product_name},
            preformatted_text=menu_text,
        )

        vendor_context = {
            "product": product_name,
            "vendors": vendors,
            "available_mapping_kind": "product_vendor",
        }
        if extra_context:
            vendor_context.update(extra_context)

        state_patch: Dict[str, Any] = {
            "status": "WAITING_INPUT",
            "expected_input": "SELECTION",
            "response_strategy": "SELECTION_MENU",
            "final_response": menu_text,
            "ag_ui_component": None,
            "vendor_selection_context": vendor_context,
            "pending_menu": menu,
        }
        return state_patch, menu

    @staticmethod
    def recompute_cart_meta(cart: List[Dict[str, Any]]) -> Dict[str, Any]:
        total = sum(float(line.get("line_total") or 0.0) for line in cart)
        return {"total_amount": round(total, 2), "currency": "XOF", "items_count": len(cart)}

    @staticmethod
    def format_pending_draft(pending: Optional[Dict[str, Any]]) -> Optional[str]:
        if not pending or pending.get("__reset__"):
            return None
        product = pending.get("product")
        if not product:
            return None
        qty = pending.get("quantity")
        unit = pending.get("unit")
        details = ""
        if qty is not None and qty not in ("", []):
            details = f" — {qty}"
            if unit:
                details += f" {unit}"
        elif unit:
            details = f" — {unit}"
        return (
            f"✏️ *En cours d'ajout* : {product}{details}\n"
            "_(Complétez les infos pour l'ajouter au panier.)_"
        )

    def render_cart_menu(
        self,
        cart: List[Dict[str, Any]],
        meta: Dict[str, Any],
        pending_draft: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        pending_line = self.format_pending_draft(pending_draft)
        if not cart:
            base_msg = (
                "🛒 Votre panier est vide.\n\n"
                "Pour commencer, indiquez un produit et une quantité.\n"
                "💡 _Exemple : « 50 kg de maïs » ou « je cherche du riz »_"
            )
            if pending_line:
                base_msg = "\n".join(["🛒 Votre panier est encore vide.", pending_line])
            return {
                "final_response": base_msg,
                "ag_ui_component": None,
            }

        lines = ["🛒 *Votre panier actuel :*"]
        options = []
        has_auction_items = False
        for i, line in enumerate(cart, start=1):
            source_type = str(line.get("source_type") or "DIRECT").upper()
            source_label = SOURCE_TYPE_LABELS.get(source_type, "🌐 Catalogue")
            vendor_info = f" — _{line.get('vendor_name')}_" if line.get("vendor_name") else ""
            notification_badge = ""
            if line.get("notification_status") == "PENDING_RESPONSE":
                notification_badge = " ⏳"
            elif line.get("notification_status") == "RESPONDED":
                notification_badge = " ✅"

            lines.append(
                f"\n*{i}. {line.get('name')}* ({source_label}){vendor_info}{notification_badge}\n"
                f"   {_fmt_num(line.get('quantity'))} {line.get('unit')} × {_fmt_num(line.get('price'))} = "
                f"*{_fmt_num(line.get('line_total'))} FCFA*"
            )
            options.append({"index": str(i), "label": f"{line.get('name')} (x{line.get('quantity')})"})
            if line.get("is_auction"):
                has_auction_items = True

        lines.append(f"\n💰 *Total estimé : {_fmt_num(meta.get('total_amount'))} {meta.get('currency')}*")
        lines.append(render_cart_actions_hint(has_auction_items))

        if pending_line:
            lines.append("\n" + pending_line)
        cart_text = "\n".join(lines)
        return {
            "final_response": cart_text,
            "ag_ui_component": None,
            "pending_menu": MenuRequest(
                title="Votre panier",
                options=[MenuOption(index=o["index"], label=o["label"]) for o in options],
                kind="cart",
                metadata={"actions": ["precommander", "ajouter", "négocier", "annuler"]},
                preformatted_text=cart_text,
            ),
        }

    async def add_to_cart_with_ref(
        self,
        phone: str,
        product_name: str,
        quantity: Any,
        ref: Dict[str, Any],
        cart: List[Dict[str, Any]],
        state: Dict[str, Any],
    ) -> Dict[str, Any]:
        try:
            qty = float(quantity)
        except (TypeError, ValueError):
            qty = 0.0

        if qty <= 0:
            return {
                "status": "WAITING_INPUT",
                "expected_input": "QUANTITY",
                "response_strategy": "ASK_MISSING_FIELD",
                "missing_fields": ["quantity"],
                "final_response": (
                    f"📦 Quelle quantité de *{product_name}* souhaitez-vous ?\n"
                    "💡 _Exemples : 50 kg, 2 sacs, 100 kg..._"
                ),
                "ag_ui_component": None,
            }

        source_type = str(ref.get("source_type") or "DIRECT").upper()
        if source_type == "FUTURE":
            # PRODUCTION FUTURE → vraie RÉSERVATION (précommande) au lieu d'un
            # simple message informatif. Aucun débit de stock : reserve_future_offer
            # incrémente MarketOffer.reserved_quantity et crée un Order(PREORDER)
            # lié via market_offer_id, puis notifie le producteur.
            offer_id = ref.get("market_offer_id") or ref.get("offer_id") or ref.get("id")
            if not offer_id:
                return {
                    "status": "COMPLETED",
                    "response_strategy": "SUCCESS",
                    "final_response": (
                        f"⏳ *{ref.get('name') or product_name}* est une production future, "
                        "mais je n'ai pas pu retrouver sa référence pour la réserver. Réessayez la sélection."
                    ),
                    "vendor_selection_context": None,
                }

            reservation = await PreorderGateway(self.mc_runtime).reserve_future_offer(
                buyer_phone=phone,
                market_offer_id=str(offer_id),
                quantity=qty,
                desired_price=ref.get("price") or ref.get("price_per_unit"),
            )
            reservation = _unwrap_tool_envelope(reservation)

            if not is_success_response(reservation):
                msg = reservation.get("message") or "Cette production n'a pas pu être réservée pour le moment."
                return {
                    "status": "COMPLETED",
                    "response_strategy": "ERROR",
                    "final_response": msg,
                    "vendor_selection_context": None,
                    "preorder_workflow": {"__reset__": True},
                }

            return {
                "status": "COMPLETED",
                "response_strategy": "SUCCESS",
                "final_response": reservation.get("message")
                or f"✅ Précommande enregistrée pour *{product_name}*.",
                "preorder_workflow": {"__reset__": True},
                "vendor_selection_context": None,
                "transaction_payload": {"__reset__": True},
            }

        resolved_pid = ref.get("product_id") or ref.get("id")
        check = await StockGateway(self.mc_runtime).validate_stock_availability(
            product_id=resolved_pid,
            quantity=qty,
            unit=ref.get("unit"),
            buyer_phone=phone,
        )

        # Defensive unwrap: some transports wrap the DB dict inside an execution
        # envelope ({"ok": ..., "data": {...}, "error": ...}). If we don't unwrap,
        # is_success_response() sees no top-level "status" and no "message",
        # producing a bogus "Stock insuffisant" with no numbers.
        check = _unwrap_tool_envelope(check)

        logger.info(
            "STOCK_CHECK | product_id=%s qty=%s available=%s status=%s reason=%s msg=%s",
            resolved_pid, qty,
            check.get("available_quantity"),
            check.get("status"),
            check.get("reason"),
            check.get("message"),
        )

        if not is_success_response(check):
            fallback = check.get("fallback") or []
            recos = [
                {
                    "type": "alternative_product",
                    "product_id": alt.get("product_id"),
                    "name": alt.get("name"),
                    "price": alt.get("price"),
                    "available_quantity": alt.get("available_quantity"),
                    "unit": alt.get("unit"),
                }
                for alt in fallback
            ]

            reason = str(check.get("reason") or "").lower()
            available = check.get("available_quantity")
            display_name = ref.get("name") or product_name
            unit_lbl = str(ref.get("unit") or check.get("unit") or "KG")

            # Genuine shortage (vendor exists but not enough) OR product vanished →
            # propose an enchère so producers can commit to supply the demand.
            if reason == "product_not_found":
                msg = check.get("message") or f"Le produit « {display_name} » n'existe plus dans le catalogue."
            elif available is not None:
                msg = (
                    f"📉 Stock insuffisant pour « *{display_name}* » : "
                    f"seulement *{_fmt_num(available)} {unit_lbl}* disponible(s) sur "
                    f"*{_fmt_num(qty)} {unit_lbl}* demandé(s)."
                )
            else:
                # Envelope/technical error — do not pretend it's a stock shortage.
                msg = check.get("message") or (
                    "⚠️ La vérification du stock n'a pas abouti. Veuillez réessayer."
                )

            escalation = (
                "\n\n🙋 Souhaitez-vous lancer un *appel d'offres* pour que les producteurs "
                "s'engagent à fournir cette quantité ?\n"
                "👉 Répondez *oui* pour lancer, ou *non* pour autre chose."
            )
            wm = dict(state.get("working_memory") or {})
            wm.update({
                "buyer_request_waiting_choice": True,
                "buyer_request_catalog_checked": True,
                "buyer_request_last_product": display_name,
            })
            payload_seed = {"product": display_name}
            if qty:
                payload_seed["quantity"] = qty
            if ref.get("unit"):
                payload_seed["unit"] = ref.get("unit")

            return {
                "status": "WAITING_INPUT",
                "expected_input": "CONFIRMATION",
                "response_strategy": "ASK_MISSING_FIELD",
                "final_response": msg + ("\n\n💡 Des alternatives sont disponibles." if recos else "") + escalation,
                "fallback_recommendations": recos,
                "preorder_workflow": {"phase": "CART"},
                "vendor_selection_context": None,
                "current_goal": "BUYER_REQUEST",
                "goal_status": "ACTIVE",
                "working_memory": wm,
                "transaction_payload": payload_seed,
            }

        price = float(check.get("unit_price") or ref.get("price") or 0.0)
        line: Dict[str, Any] = {
            "product_id": ref.get("product_id") or ref.get("id"),
            "name": ref.get("name") or product_name,
            "quantity": qty,
            "unit": str(check.get("unit") or ref.get("unit") or "KG"),
            "price": price,
            "line_total": round(price * qty, 2),
            "producer_id": ref.get("producer_id") or check.get("producer_id"),
            "vendor_name": ref.get("vendor_name"),
            "source_type": source_type,
            "is_auction": ref.get("is_auction", False),
            "status": "VALIDATED",
            "notification_id": None,
            "notification_status": None,
        }

        notification_payload = {
            "producer_id": str(line.get("producer_id") or ""),
            "buyer_phone": phone,
            "product_id": str(line.get("product_id") or ""),
            "product_name": str(line.get("name") or ""),
            "quantity": qty,
            "unit": line.get("unit"),
            "source_type": source_type,
        }

        notification_result = await AgentActionGateway(self.mc_runtime).create_action(
            agent_name="MarketCoach",
            action_type="notify_interested_buyer",
            payload=notification_payload,
        )
        if is_success_response(notification_result):
            line["notification_id"] = notification_result.get("action_id") or notification_result.get("id")
            line["notification_status"] = "PENDING_RESPONSE"

        cart = [
            c
            for c in cart
            if c.get("status") != "DRAFT" and c.get("product_id") not in ("DRAFT", line["product_id"])
        ]
        cart.append(line)
        meta = self.recompute_cart_meta(cart)
        render = self.render_cart_menu(cart, meta)

        response = {
            "active_cart": cart,
            "status": "COMPLETED",
            "response_strategy": "SELECTION_MENU",
            "cart_meta": meta,
            "preorder_workflow": {"phase": "CART"},
            "fallback_recommendations": [],
            "draft_payload": {"__reset__": True},
            "transaction_payload": {"__reset__": True},
            "vendor_selection_context": None,
        }
        response.update(render)
        return response


SOURCE_TYPE_LABELS: Dict[str, str] = {
    "DIRECT": "🌐 Catalogue",
    "FUTURE": "⏳ Future production",
    "AUCTION": "🏷️ Enchère – prix négociable",
    "PROCUREMENT": "📋 Appel d'offres",
}


_GEO_TAG_RE = __import__("re").compile(r"\s*\((?:🌐|📍)[^)]*\)\s*$")


def _clean_product_name(name: Any) -> str:
    """Strip the geo tag the search bakes into names ('tomates (🌐 National)').

    Without this the cart shows a double tag: 'tomates (🌐 National) (🌐 Catalogue)'.
    We remove a trailing '(🌐 …)' / '(📍 …)' so the stored name is clean.
    """
    s = str(name or "").strip()
    return _GEO_TAG_RE.sub("", s).strip() or s


def _fmt_num(value: Any) -> str:
    """Format a numeric value without a trailing ``.0`` (225.0 → '225')."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(f)) if f == int(f) else str(f)


def _unwrap_tool_envelope(result: Any) -> Dict[str, Any]:
    """Return the domain dict from a possibly-enveloped tool response.

    Some transports return the raw DB dict (``{"status": "success", ...}``),
    others wrap it in an execution envelope (``{"ok": bool, "data": {...},
    "error": ...}``). Without unwrapping, an ``ok=False`` envelope has no
    top-level ``status``/``message`` and is silently misread as an empty
    failure — surfacing a bogus "stock insuffisant" with no numbers.
    """
    if not isinstance(result, dict):
        return {"status": "error", "message": "Réponse outil invalide."}
    # Already a domain dict.
    if "status" in result or "message" in result or "available_quantity" in result:
        return result
    # Execution envelope shape.
    if "ok" in result or "data" in result:
        data = result.get("data")
        if isinstance(data, dict) and data:
            merged = dict(data)
            if result.get("error") and "message" not in merged:
                merged.setdefault("message", str(result.get("error")))
            merged.setdefault("status", "success" if result.get("ok") else "error")
            return merged
        # Empty data → propagate the envelope error as a domain error.
        return {
            "status": "success" if result.get("ok") else "error",
            "message": str(result.get("error") or "") or None,
        }
    return result


def _infer_source_type(product_record: Dict[str, Any]) -> str:
    src = str(product_record.get("source_type") or product_record.get("type") or "").upper()
    if src in ("AUCTION", "PROCUREMENT", "FUTURE"):
        return src
    if product_record.get("crop_cycle_id"):
        return "FUTURE"
    if product_record.get("auction_id") or product_record.get("is_auction"):
        return "AUCTION"
    return "DIRECT"


__all__ = [
    "CartDomainService",
    "SUPPORT_FOOTER",
    "with_support_footer",
    "SOURCE_TYPE_LABELS",
]
