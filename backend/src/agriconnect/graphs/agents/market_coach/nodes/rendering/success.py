"""Rendering — SUCCESS / COMPLETED (résultats d'outils, listes, catalogues).

Ordre de rendu (préservé du monolithe historique) :
1. ``formatted_menu`` pré-rendu par l'outil (prioritaire, jamais écrasé)
2. Sections structurées farms/catalog/upcoming_cycles (forme get_stocks)
3. Catalogue acheteur (search_products)
4. Liste plate générique (CAS 2)
5. Dict simple {farm_name, stocks} (CAS 2B)
6. Gabarits transactionnels par goal (CAS 3, uniquement si rien d'autre)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from agriconnect.graphs.agents.market_coach.nodes.rendering.common import (
    RenderContext,
    apply_corrections,
    fmt_date,
    fmt_num,
    list_menu_component,
    status_component,
    unwrap_execution_result,
)


# =====================================================================
# SECTION RENDERERS
# =====================================================================

def _format_future_cycle_line(cycle: Dict[str, Any]) -> str:
    """Ligne descriptive pour un MarketOffer futur (culture/élevage)."""
    label = (
        cycle.get("display_label")
        or cycle.get("product_label")
        or cycle.get("species")
        or "Production future"
    )
    production_type = str(cycle.get("production_type") or "CROP").upper()
    emoji = "🐄" if production_type == "LIVESTOCK" else "🌱"
    status = (cycle.get("status") or "EN PRÉPARATION").upper()

    details: List[str] = []
    qty = cycle.get("available_quantity")
    unit = str(cycle.get("unit") or "").upper()
    if qty not in (None, ""):
        details.append(f"{fmt_num(qty)} {unit}".strip())
    price = cycle.get("price_per_unit")
    if price not in (None, ""):
        details.append(f"{fmt_num(price)} FCFA/{unit}".strip())
    harvest = fmt_date(cycle.get("expected_harvest_date") or cycle.get("estimated_available_at"))
    if harvest:
        details.append(f"disponible le {harvest}")
    if cycle.get("preorder_enabled"):
        details.append("précommande active")

    details_text = " | ".join(details)
    suffix = f" — {details_text}" if details_text else ""
    return f"{emoji} {label} — statut {status}{suffix}"


def _render_farm_sections(farms_dict: Dict[str, Any]) -> Tuple[str, List[Dict[str, str]]]:
    if not farms_dict:
        return "", []
    lines = ["📋 *Voici l'état de vos stocks par exploitation :*\n"]
    options: List[Dict[str, str]] = []
    index_counter = 1
    for farm_id, farm_info in farms_dict.items():
        if not isinstance(farm_info, dict):
            continue
        farm_name = farm_info.get("farm_name") or farm_info.get("name") or "Exploitation sans nom"
        location = farm_info.get("location") or "Zone non spécifiée"
        lines.append(f"🏡 *{farm_name}* ({location})")
        stocks = farm_info.get("stocks") or []
        if not stocks:
            lines.append("  _Aucun produit stocké actuellement dans cette exploitation._")
        for stock in stocks:
            item_name = stock.get("item_name") or stock.get("product_name") or "Produit"
            qty = fmt_num(stock.get("quantity", 0))
            unit = str(stock.get("unit") or "KG").upper()
            stock_id = stock.get("stock_id") or stock.get("id") or farm_id
            label_item = f"{item_name} : {qty} {unit}"
            lines.append(f"  {index_counter}️⃣ {label_item}")
            options.append({
                "index": str(index_counter),
                "label": f"{farm_name} - {label_item}",
                "value": str(stock_id),
            })
            index_counter += 1

        cycles = farm_info.get("upcoming_cycles") or []
        for cycle in cycles[:2]:
            lines.append(f"  {index_counter}️⃣ {_format_future_cycle_line(cycle)}")
            stock_id = cycle.get("offer_id") or cycle.get("market_offer_id") or farm_id
            label_item = cycle.get("display_label") or cycle.get("product_label") or cycle.get("species") or "Production future"
            options.append({
                "index": str(index_counter),
                "label": f"{farm_name} - {label_item}",
                "value": str(stock_id),
            })
            index_counter += 1
        if len(cycles) > 2:
            lines.append(f"    … +{len(cycles) - 2} autre(s) cycle(s) en préparation")

        lines.append("")

    if options:
        lines.append("❓ *Que souhaitez-vous faire ?* Indiquez le numéro d'un lot pour le mettre en vente ou le modifier.")
    return "\n".join(lines).strip(), options


def _render_catalog_section(catalog: List[Dict[str, Any]]) -> Tuple[str, List[Dict[str, str]]]:
    if not catalog:
        return "", []
    lines = ["📦 *Produits listés dans votre catalogue :*\n"]
    options: List[Dict[str, str]] = []
    for i, product in enumerate(catalog, start=1):
        name = product.get("name") or product.get("product_name") or f"Produit {i}"
        code = product.get("short_code")
        price = product.get("price")
        unit = str(product.get("unit") or "KG").upper()
        qty = product.get("quantity") or product.get("quantity_for_sale")
        status = (product.get("status") or "DISPONIBLE").upper()
        line_header = f"{i}️⃣ *{name}*"
        if code:
            line_header += f" (Réf: #{code})"
        lines.append(line_header)
        details = []
        if price is not None:
            details.append(f"💰 {fmt_num(price)} FCFA/{unit}")
        if qty not in (None, ""):
            details.append(f"⚖️ {fmt_num(qty)} {unit} dispo")
        details.append(f"Statut : {status}")
        lines.append("  " + " | ".join(details))
        options.append({
            "index": str(i),
            "label": f"{name} ({fmt_num(qty or 0)} {unit})",
            "value": str(product.get("product_id") or product.get("id") or product.get("short_code") or i),
        })
    lines.append("👉 *Mentionnez un numéro pour ouvrir ce produit ou ajuster prix/quantité.*")
    return "\n".join(lines).strip(), options


def _render_cycles_section(cycles: List[Dict[str, Any]]) -> str:
    if not cycles:
        return ""
    deduped: Dict[str, Dict[str, Any]] = {}
    for raw_cycle in cycles:
        if not isinstance(raw_cycle, dict):
            continue
        cycle_id = str(raw_cycle.get("offer_id") or raw_cycle.get("cycle_id") or raw_cycle.get("id") or len(deduped))
        if cycle_id in deduped:
            continue
        deduped[cycle_id] = raw_cycle
    if not deduped:
        return ""
    lines = ["🌱 *Cultures en cours / futures récoltes :*"]
    for cycle in deduped.values():
        farm_name = cycle.get("farm_name") or "ferme"
        lines.append(f"• {_format_future_cycle_line(cycle)} ({farm_name})")
    return "\n".join(lines)


def _render_flat_list(items: List[Dict[str, Any]]) -> Tuple[str, List[Dict[str, str]]]:
    lines = ["📋 *Voici les éléments trouvés correspondant à votre demande :*\n"]
    options_ui: List[Dict[str, str]] = []

    for i, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            item = {}
        name = (
            item.get("name")
            or item.get("item_name")
            or item.get("product_name")
            or f"Élément {i}"
        )
        details_parts: List[str] = []
        entity_id = item.get("id")

        if "size" in item:
            entity_id = item.get("farm_id") or entity_id
            loc = item.get("location")
            details_parts.append(f"{fmt_num(item['size'])} ha")
            if loc:
                details_parts.append(str(loc))
            stocks = item.get("stocks") or []
            if isinstance(stocks, list) and stocks:
                stock_tokens: List[str] = []
                for s in stocks[:3]:
                    s_name = s.get("item_name") or s.get("product_name") or "Produit"
                    s_qty = fmt_num(s.get("quantity") or 0)
                    s_unit = str(s.get("unit") or "KG").upper()
                    stock_tokens.append(f"{s_name} {s_qty} {s_unit}")
                if len(stocks) > 3:
                    stock_tokens.append(f"+{len(stocks) - 3} autre(s)")
                details_parts.append("Stocks: " + ", ".join(stock_tokens))
        elif "quantity" in item:
            entity_id = item.get("stock_id") or item.get("product_id") or entity_id
            qty = fmt_num(item.get("quantity"))
            unit = str(item.get("unit") or "KG").upper()
            details_parts.append(f"{qty} {unit}")
            if item.get("price") is not None:
                details_parts.append(f"{fmt_num(item.get('price'))} FCFA")
        elif "price" in item or "target_price" in item:
            entity_id = item.get("auction_id") or item.get("bid_id") or entity_id
            price = fmt_num(item.get("price") or item.get("target_price"))
            details_parts.append(f"{price} FCFA")

        entity_id = str(entity_id or i)
        details_str = f" ({', '.join(details_parts)})" if details_parts else ""
        label_complet = f"{name}{details_str}"
        lines.append(f"{i}️⃣ {label_complet}")
        options_ui.append({
            "index": str(i),
            "label": label_complet,
            "value": entity_id,
        })

    lines.append("\n👉 *Faites votre choix en tapant le numéro correspondant.*")
    return "\n".join(lines), options_ui


def _render_buyer_catalog_sections(items: List[Dict[str, Any]]) -> str:
    if not items:
        return ""

    direct_items: List[Dict[str, Any]] = []
    future_items: List[Dict[str, Any]] = []

    for item in items:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source_type") or item.get("availability_kind") or "").upper()
        if not source and item.get("estimated_available_at"):
            source = "FUTURE"
        bucket = future_items if source == "FUTURE" else direct_items
        bucket.append(item)

    sections: List[str] = []

    if direct_items:
        lines = ["🌐 *Produits disponibles immédiatement :*"]
        for idx, product in enumerate(direct_items, start=1):
            name = product.get("name") or product.get("product_name") or f"Produit {idx}"
            price = product.get("price")
            unit = str(product.get("unit") or "KG").upper()
            vendor = product.get("vendor") or product.get("producer_name") or "Producteur"
            zone = product.get("zone_name") or product.get("zone")
            price_label = f"{fmt_num(price)} FCFA/{unit}" if price not in (None, "") else "Prix communiqué par le producteur"
            line = f"{idx}️⃣ *{name}* — {price_label} — {vendor}"
            if zone:
                line += f" ({zone})"
            available_qty = product.get("available_quantity")
            if available_qty not in (None, ""):
                line += f" | ⚖️ {fmt_num(available_qty)} {unit}"
            lines.append(line)
        sections.append("\n".join(lines))

    if future_items:
        lines = ["⏳ *Productions futures (précommandes ouvertes) :*"]
        for product in future_items:
            name = product.get("name") or product.get("product_name") or "Production future"
            price = product.get("price") or product.get("price_per_unit")
            unit = str(product.get("unit") or "KG").upper()
            eta = fmt_date(product.get("estimated_available_at")) or product.get("estimated_available_at") or "date à confirmer"
            vendor = product.get("vendor") or product.get("producer_name") or "Producteur"
            price_label = f"{fmt_num(price)} FCFA/{unit}" if price not in (None, "") else "Prix communiqué lors de la confirmation"
            lines.append(f"• *{name}* — {price_label} — livré vers {eta} ({vendor})")
        sections.append("\n".join(lines))

    return "\n\n".join([section for section in sections if section]).strip()


def _transactional_fallback_text(goal: str, salutation: str, payload: Dict[str, Any]) -> str:
    """Gabarits par goal quand l'outil ne renvoie ni message ni collection."""
    prod_name = payload.get("product") or payload.get("product_name") or "votre demande"
    qty = fmt_num(payload.get("quantity") or payload.get("quantity_desired") or "")
    unit = str(payload.get("unit") or payload.get("unit_desired") or "").upper()
    q_info = f" pour {qty} {unit}" if qty else ""

    g = (goal or "").upper()
    if g == "BUYER_ADD_TO_CART":
        return (
            f"🛒 {salutation}*{prod_name}*{q_info} a été ajouté à votre panier. "
            "Tapez *précommander* pour valider ou ajoutez un autre produit."
        )
    if g.startswith("BUYER_PREORDER"):
        return (
            f"✅ {salutation}Votre précommande{q_info} pour *{prod_name}* est enregistrée. "
            "Vous recevrez le récapitulatif complet dans un instant."
        )
    if g.startswith("PROCUREMENT_") or "AUCTION" in g:
        return f"✅ {salutation}Votre appel d'offres{q_info} de *{prod_name}* a été enregistré avec succès."
    if "PUBLISH" in g or "SELL" in g:
        return f"✅ {salutation}Votre offre de vente{q_info} de *{prod_name}* a bien été publiée sur le marché."
    if "BID" in g:
        price_bid = fmt_num(payload.get("price"))
        p_info = f" à {price_bid} FCFA" if price_bid else ""
        return f"✅ {salutation}Votre proposition de prix{p_info} pour *{prod_name}* a bien été transmise."
    if any(tok in g for tok in ("LIST", "GET", "CHECK", "SEARCH", "VIEW", "DASHBOARD", "SNAPSHOT")):
        # Goal de LECTURE sans contenu : rester neutre et honnête.
        return f"{salutation}Je n'ai rien trouvé à afficher pour cette demande pour le moment."
    return (
        f"{salutation}C'est noté. Dites-moi ce que vous souhaitez faire "
        "(voir vos commandes, publier un produit, lancer un appel d'offres…)."
    )


# =====================================================================
# HANDLER PRINCIPAL
# =====================================================================

async def render_success(ctx: RenderContext) -> Dict[str, Any]:
    state, goal, salutation, payload = ctx.state, ctx.goal, ctx.salutation, ctx.payload
    raw_exec_result = state.get("execution_result") or {}

    # Les nœuds buyer (cart, negotiation, tracking) posent final_response
    # directement sans passer par mcp_tool_executor.
    precomputed_success = state.get("final_response")
    if precomputed_success and not raw_exec_result:
        return apply_corrections(state, {
            "final_response": precomputed_success,
            "ag_ui_component": state.get("ag_ui_component"),
        })

    exec_result = unwrap_execution_result(raw_exec_result)
    tool_msg = exec_result.get("message") or ""
    tool_data = exec_result.get("data")
    if tool_data in (None, {}):
        results_payload = exec_result.get("results")
        if isinstance(results_payload, (list, dict)):
            tool_data = results_payload

    text_output = str(tool_msg) or "🌾 Opération réussie."
    ag_component: Optional[Dict[str, Any]] = None

    formatted_menu_raw = exec_result.get("formatted_menu")
    formatted_menu_text = formatted_menu_raw.strip() if isinstance(formatted_menu_raw, str) else ""
    has_formatted_menu = bool(formatted_menu_text)
    if has_formatted_menu:
        text_output = formatted_menu_text

    def _menu(title: str, options: List[Dict[str, str]], mode: str) -> Dict[str, Any]:
        metadata: Dict[str, Any] = {"mode": mode, "count": len(options)}
        if goal:
            metadata["goal"] = goal
        return list_menu_component(title, options, metadata=metadata)

    # ── Détection des formes structurées (get_stocks & co) ────────────
    farms_payload: Optional[Dict[str, Any]] = None
    catalog_payload: Optional[List[Dict[str, Any]]] = None
    cycles_payload: Optional[List[Dict[str, Any]]] = None

    if isinstance(tool_data, dict):
        farms_candidate = tool_data.get("farms")
        if isinstance(farms_candidate, dict):
            farms_payload = farms_candidate
        catalog_candidate = tool_data.get("catalog")
        if isinstance(catalog_candidate, list):
            catalog_payload = catalog_candidate
        cycles_candidate = tool_data.get("upcoming_cycles")
        if isinstance(cycles_candidate, list):
            cycles_payload = cycles_candidate

    if farms_payload is None and isinstance(tool_data, dict) and tool_data:
        sample_vals = [v for v in tool_data.values() if v is not None]
        if sample_vals and isinstance(sample_vals[0], dict):
            probe = sample_vals[0]
            if any(k in probe for k in ("stocks", "farm_name", "name", "location")):
                farms_payload = tool_data  # backward compat

    structured_sections_handled = False
    allow_structured_render = not has_formatted_menu

    # Forme get_stocks reconnue même VIDE ({} et [] sont falsy) : rendre un
    # message dédié « aucun produit ni exploitation » plutôt que le générique.
    is_catalog_shape = isinstance(tool_data, dict) and any(
        k in tool_data for k in ("farms", "catalog", "upcoming_cycles")
    )

    if allow_structured_render and (
        any([farms_payload, catalog_payload, cycles_payload]) or is_catalog_shape
    ):
        sections: List[str] = []
        if farms_payload:
            farm_text, farm_options = _render_farm_sections(farms_payload)
            if farm_text:
                sections.append(farm_text)
            if farm_options and ag_component is None:
                ag_component = _menu("Gestion des stocks", farm_options, mode="stocks")

        if catalog_payload:
            catalog_text, catalog_options = _render_catalog_section(catalog_payload)
            if catalog_text:
                sections.append(catalog_text)
            if catalog_options and ag_component is None:
                ag_component = _menu("Catalogue produits", catalog_options, mode="catalog")

        if cycles_payload:
            cycles_text = _render_cycles_section(cycles_payload)
            if cycles_text:
                sections.append(cycles_text)

        if sections:
            text_output = "\n\n".join([section for section in sections if section]).strip()
            structured_sections_handled = True
        elif not tool_msg:
            text_output = (
                f"{salutation}Vous n'avez encore aucun produit ni exploitation "
                "enregistrés. Tapez *ajouter un produit* pour commencer à vendre, "
                "ou *créer une ferme* pour configurer votre exploitation."
            )
            structured_sections_handled = True

    # ── Catalogue acheteur (search_products) ──────────────────────────
    selected_tool_name = str(state.get("selected_tool") or "").lower().strip()
    if (
        allow_structured_render
        and not structured_sections_handled
        and selected_tool_name == "search_products"
    ):
        results_payload_list: List[Dict[str, Any]] = []
        if isinstance(tool_data, list):
            results_payload_list = tool_data
        elif isinstance(exec_result.get("results"), list):
            results_payload_list = exec_result["results"]  # type: ignore[index]

        buyer_sections = _render_buyer_catalog_sections(results_payload_list)
        if buyer_sections:
            text_output = buyer_sections
            structured_sections_handled = True

    # ── CAS 2 : liste plate ───────────────────────────────────────────
    if isinstance(tool_data, list) and tool_data:
        flat_text, options_ui = _render_flat_list(tool_data)
        if allow_structured_render and not structured_sections_handled:
            text_output = flat_text

        if options_ui:
            if ag_component is None:
                ag_component = _menu("Faites votre choix", options_ui, mode="flat_list")
            else:
                kwargs = ag_component.get("kwargs") if isinstance(ag_component, dict) else None
                if isinstance(kwargs, dict) and "metadata" in kwargs:
                    kwargs["metadata"].setdefault("count", len(options_ui))

    # ── CAS 2B : dict simple {farm_name, stocks} ──────────────────────
    elif not structured_sections_handled and isinstance(tool_data, dict) and "stocks" in tool_data:
        stocks_list = tool_data.get("stocks") or []
        farm_label = tool_data.get("farm_name") or tool_data.get("name") or "cette exploitation"
        if not stocks_list:
            text_output = (
                f"Aucun produit n'est actuellement enregistré en stock pour {farm_label}. "
                "Souhaitez-vous ajouter une nouvelle récolte ?"
            )
            ag_component = status_component("info", message=text_output)

    # ── CAS 3 : fallback transactionnel unitaire ──────────────────────
    if not ag_component:
        # GUARD : ne JAMAIS écraser un menu déjà rendu (`formatted_menu`),
        # une section structurée (CAS 1) ni un message d'outil par le gabarit
        # générique — voir historique du bug « validée avec succès » parasite.
        if not tool_msg and not has_formatted_menu and not structured_sections_handled:
            text_output = _transactional_fallback_text(goal or "", salutation, payload)
        ag_component = status_component("success", message=text_output)

    auto_notice = state.get("auto_farm_notice")
    if auto_notice:
        text_output = f"{auto_notice}\n\n{text_output}"

    if state.get("error_creating_farm"):
        text_output = (
            "⚠️ La configuration automatique de votre ferme a échoué. Vous pouvez "
            "continuer, mais ajoutez votre ferme pour fiabiliser vos stocks.\n\n"
            f"{text_output}"
        )

    proactive = state.get("proactive_hint")
    if proactive:
        text_output = f"{text_output}\n\n💡 *Conseil :* {proactive}"

    return apply_corrections(state, {
        "final_response": text_output,
        "ag_ui_component": ag_component,
    })
