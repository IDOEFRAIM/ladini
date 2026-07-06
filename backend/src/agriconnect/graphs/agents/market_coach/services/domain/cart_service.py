from __future__ import annotations

"""Cart domain service — isolates buyer cart orchestration helpers."""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from agriconnect.graphs.agents.market_coach.flows.common.menu_contracts import (
    MenuOption,
    MenuRequest,
)
from agriconnect.graphs.agents.market_coach.utils import MarketRuntime, is_success_response

from .buyer_common import SUPPORT_FOOTER, safe_call_tool, with_support_footer


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
        res = await safe_call_tool(
            self.mc_runtime,
            "search_products",
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
                    "crop_cycle_id": item.get("crop_cycle_id"),
                    "name": str(item.get("name") or product_name),
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

        lines.append("\n_Répondez avec le numéro du producteur choisi, ou *annuler* pour quitter._")
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
            "product_name": product_name,
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
        product = pending.get("product") or pending.get("product_name")
        if not product:
            return None
        qty = pending.get("quantity_mentioned") or pending.get("quantity")
        unit = pending.get("unit_mentioned") or pending.get("unit")
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
            base_msg = "🛒 Votre panier est vide. Indiquez un produit et une quantité pour commencer."
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
                f"   {line.get('quantity')} {line.get('unit')} × {line.get('price')} = *{line.get('line_total')} FCFA*"
            )
            options.append({"index": str(i), "label": f"{line.get('name')} (x{line.get('quantity')})"})
            if line.get("is_auction"):
                has_auction_items = True

        lines.append(f"\n💰 *Total estimé : {meta.get('total_amount')} {meta.get('currency')}*")

        actions_hint = "\n_Répondez *précommander* pour valider, ou ajoutez un autre produit._"
        if has_auction_items:
            actions_hint += "\n_Pour les articles en enchère : *négocier* pour faire une contre-offre._"
        lines.append(actions_hint)

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

        source_type = str(ref.get("source_type") or "DIRECT").upper()
        if source_type == "FUTURE":
            eta = ref.get("estimated_available_at")
            eta_hint = f" (disponible vers {eta})" if eta else ""
            return {
                "status": "COMPLETED",
                "response_strategy": "SUCCESS",
                "final_response": (
                    f"⏳ *{ref.get('name') or product_name}* est une production future{eta_hint}. "
                    "Cette option est visible pour découverte et choix producteur, mais n'est pas encore commandable en stock immédiat."
                ),
                "preorder_workflow": {"phase": "CART"},
                "vendor_selection_context": {"__reset__": True},
            }

        check = await safe_call_tool(
            self.mc_runtime,
            "validate_stock_availability_atomic",
            product_id=ref.get("product_id") or ref.get("id"),
            quantity=qty,
            unit=ref.get("unit"),
            buyer_phone=phone,
        )

        status = str(check.get("status") or "").upper()
        if status != "SUCCESS":
            fallback = check.get("fallback") or []
            msg = check.get("message") or "Stock insuffisant pour cette demande."
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
            return {
                "status": "COMPLETED",
                "response_strategy": "SUCCESS",
                "final_response": msg + ("\n\n💡 Des alternatives sont disponibles." if recos else ""),
                "fallback_recommendations": recos,
                "preorder_workflow": {"phase": "CART"},
                "vendor_selection_context": {"__reset__": True},
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

        notification_result = await safe_call_tool(
            self.mc_runtime,
            "create_agent_action",
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
            "vendor_selection_context": {"__reset__": True},
        }
        response.update(render)
        return response


SOURCE_TYPE_LABELS: Dict[str, str] = {
    "DIRECT": "🌐 Catalogue",
    "FUTURE": "⏳ Future production",
    "AUCTION": "🏷️ Enchère – prix négociable",
    "PROCUREMENT": "📋 Appel d'offres",
}


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
    "safe_call_tool",
    "SOURCE_TYPE_LABELS",
]
