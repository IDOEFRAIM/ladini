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

from typing import Any, Callable, Dict, List, Optional, Tuple

from ladini.graphs.agents.market_coach.nodes.rendering.common import (
    RenderContext,
    apply_corrections,
    fmt_date,
    fmt_num,
    list_menu_component,
    status_component,
    unwrap_execution_result,
)
from ladini.graphs.agents.market_coach.services.text_pagination import (
    MAX_CHARS_PER_PAGE,
    MAX_ITEMS_PER_PAGE,
    paginate_item_blocks,
)
from ladini.services.pending_photo_target import (
    set_pending_auction_photo as _set_pending_auction_photo,
)
from ladini.services.pending_photo_target import (
    set_pending_bid_photo as _set_pending_bid_photo,
)
from ladini.services.pending_photo_target import (
    set_pending_product_photo as _set_pending_product_photo,
)
from ladini.services.search_results_cache import (
    store_results as _store_search_photo_results,
)


def _cache_numbered_items_with_photos(
    phone: str,
    items: List[Dict[str, Any]],
    id_key: str,
    label_fn: Callable[[Dict[str, Any]], str],
) -> bool:
    """Met en cache un menu numéroté (bids/enchères) pour la commande
    "photos <numéro>" — même mécanisme que le catalogue de recherche
    (services/search_results_cache.py). Renvoie True si au moins un élément
    a des photos (pour savoir s'il faut afficher le hint)."""
    if not phone or not items:
        return False
    entries: Dict[str, Dict[str, Any]] = {}
    has_photos = False
    for idx, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        images = item.get("images") or []
        if images:
            has_photos = True
        entries[str(idx)] = {
            "id": item.get(id_key),
            "name": label_fn(item),
            "images": images,
        }
    if entries:
        _store_search_photo_results(phone, entries)
    return has_photos


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
    harvest = fmt_date(
        cycle.get("expected_harvest_date") or cycle.get("estimated_available_at")
    )
    if harvest:
        details.append(f"disponible le {harvest}")
    if cycle.get("preorder_enabled"):
        details.append("précommande active")

    details_text = " | ".join(details)
    suffix = f" — {details_text}" if details_text else ""
    return f"{emoji} {label} — statut {status}{suffix}"


# Pagination des listes volumineuses (stocks, catalogue) — voir
# services/text_pagination.py pour le détail (module partagé, réutilisé par
# services/domain/cart_service.py pour le même besoin côté acheteur).
_STOCK_LIST_MAX_ITEMS_PER_PAGE = MAX_ITEMS_PER_PAGE
_STOCK_LIST_MAX_CHARS_PER_PAGE = MAX_CHARS_PER_PAGE
_paginate_item_blocks = paginate_item_blocks


def _render_farm_sections(
    farms_dict: Dict[str, Any],
) -> Tuple[str, List[Dict[str, str]]]:
    if not farms_dict:
        return "", []
    header = "📋 *Voici l'état de vos stocks par exploitation :*"
    item_blocks: List[str] = []
    options: List[Dict[str, str]] = []
    index_counter = 1
    for farm_id, farm_info in farms_dict.items():
        if not isinstance(farm_info, dict):
            continue
        farm_name = (
            farm_info.get("farm_name")
            or farm_info.get("name")
            or "Exploitation sans nom"
        )
        location = farm_info.get("location") or "Zone non spécifiée"
        farm_header = f"🏡 *{farm_name}* ({location})"
        stocks = farm_info.get("stocks") or []
        if not stocks:
            item_blocks.append(
                f"{farm_header}\n  _Aucun produit stocké actuellement dans cette exploitation._"
            )
        for stock in stocks:
            item_name = stock.get("item_name") or stock.get("product_name") or "Produit"
            qty = fmt_num(stock.get("quantity", 0))
            unit = str(stock.get("unit") or "KG").upper()
            stock_id = stock.get("stock_id") or stock.get("id") or farm_id
            label_item = f"{item_name} : {qty} {unit}"
            item_blocks.append(f"{farm_header}\n  {index_counter}️⃣ {label_item}")
            options.append(
                {
                    "index": str(index_counter),
                    "label": f"{farm_name} - {label_item}",
                    "value": str(stock_id),
                }
            )
            index_counter += 1

        cycles = farm_info.get("upcoming_cycles") or []
        for cycle in cycles[:2]:
            item_blocks.append(
                f"{farm_header}\n  {index_counter}️⃣ {_format_future_cycle_line(cycle)}"
            )
            stock_id = cycle.get("offer_id") or cycle.get("market_offer_id") or farm_id
            label_item = (
                cycle.get("display_label")
                or cycle.get("product_label")
                or cycle.get("species")
                or "Production future"
            )
            options.append(
                {
                    "index": str(index_counter),
                    "label": f"{farm_name} - {label_item}",
                    "value": str(stock_id),
                }
            )
            index_counter += 1
        if len(cycles) > 2:
            item_blocks.append(
                f"{farm_header}\n    … +{len(cycles) - 2} autre(s) cycle(s) en préparation"
            )

    footer = (
        "❓ *Que souhaitez-vous faire ?* Indiquez le numéro d'un lot pour le mettre en vente ou le modifier."
        if options
        else ""
    )
    if footer:
        item_blocks.append(footer)
    return _paginate_item_blocks(header, item_blocks), options


def _render_catalog_section(
    catalog: List[Dict[str, Any]],
) -> Tuple[str, List[Dict[str, str]]]:
    if not catalog:
        return "", []
    header = "📦 *Produits listés dans votre catalogue :*"
    item_blocks: List[str] = []
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
        details = []
        raw_tiers = product.get("pricing_tiers")
        is_tiered = isinstance(raw_tiers, list) and any(isinstance(t, dict) for t in raw_tiers)
        # INVARIANT : `Product.price` d'un produit à paliers est un champ de COMPATIBILITÉ legacy (prix
        # brut du 1er palier), PAS la vérité commerciale — jamais affiché comme « X FCFA/{unité} ».
        if price is not None and not is_tiered:
            details.append(f"💰 {fmt_num(price)} FCFA/{unit}")
        if qty not in (None, ""):
            details.append(f"⚖️ {fmt_num(qty)} {unit} dispo")
        details.append(f"Statut : {status}")
        block = f"{line_header}\n  " + " | ".join(details)
        # Déclinaisons de prix/conditionnement (2026-08-30) : `price`/`qty`
        # ci-dessus ne sont qu'un résumé représentatif (1er tarif / somme des
        # quantités, voir memory.py) — sans ceci, un producteur avec
        # plusieurs tarifs ("5 L à 500f, 10 L à 900f") voyait SEULEMENT le
        # 1er dans son propre catalogue, comme si le 2e avait disparu, alors
        # que le récapitulatif de confirmation les montrait bien groupés.
        tiers = product.get("pricing_tiers")
        if isinstance(tiers, list) and tiers:
            tier_lines = []
            for tier in tiers:
                if not isinstance(tier, dict):
                    continue
                t_qty = tier.get("quantity")
                t_unit = str(tier.get("unit") or "").strip()
                t_price = tier.get("price")
                if t_qty in (None, "", [], {}) or not t_unit or t_price in (None, "", [], {}):
                    continue
                label = f"{fmt_num(t_qty)} {t_unit}"
                packaging = tier.get("packaging")
                if packaging:
                    label += f" ({packaging})"
                tier_lines.append(f"    • {label} — {fmt_num(t_price)} FCFA")
            if tier_lines:
                block += "\n  Conditionnements :\n" + "\n".join(tier_lines)
        item_blocks.append(block)
        options.append(
            {
                "index": str(i),
                "label": f"{name} ({fmt_num(qty or 0)} {unit})",
                "value": str(
                    product.get("product_id")
                    or product.get("id")
                    or product.get("short_code")
                    or i
                ),
            }
        )
    item_blocks.append(
        "✏️ _Pour changer le prix, la quantité, le nom ou l'unité d'un produit, tapez *modifier un produit*._"
    )
    return _paginate_item_blocks(header, item_blocks), options


def _render_cycles_section(
    cycles: List[Dict[str, Any]],
) -> Tuple[str, List[Dict[str, str]]]:
    """Audit UX interactive 2026-08-27 : construit désormais aussi les
    options de sélection (`ListMenu`), en plus du texte — c'était le seul
    des rendus structurés de ce fichier à n'en jamais produire, condamnant
    toute précommande/production future à un pavé de texte brut sur
    WhatsApp (voir farms/catalog ci-dessus, qui en construisaient déjà)."""
    if not cycles:
        return "", []
    deduped: Dict[str, Dict[str, Any]] = {}
    for raw_cycle in cycles:
        if not isinstance(raw_cycle, dict):
            continue
        cycle_id = str(
            raw_cycle.get("offer_id")
            or raw_cycle.get("cycle_id")
            or raw_cycle.get("id")
            or len(deduped)
        )
        if cycle_id in deduped:
            continue
        deduped[cycle_id] = raw_cycle
    if not deduped:
        return "", []
    lines = ["🌱 *Cultures en cours / futures récoltes :*"]
    options: List[Dict[str, str]] = []
    for cycle_id, cycle in deduped.items():
        farm_name = cycle.get("farm_name") or "ferme"
        # Index numéroté (audit UX interactive 2026-08-27) : cette section
        # utilisait une puce "•" simple, seule exception aux index emoji
        # numérotés déjà utilisés partout ailleurs dans ce fichier (farms,
        # catalog, flat_list) — incohérent avec les `options` sélectionnables
        # juste en dessous, qui ELLES étaient déjà numérotées.
        lines.append(
            f"{len(options) + 1}️⃣ {_format_future_cycle_line(cycle)} ({farm_name})"
        )
        label = (
            cycle.get("display_label")
            or cycle.get("product_label")
            or cycle.get("species")
            or "Production future"
        )
        options.append(
            {
                "index": str(len(options) + 1),
                "label": f"{label} ({farm_name})",
                "value": cycle_id,
            }
        )
    lines.append(
        "\n✏️ _Pour changer le prix, la quantité, le nom ou la date d'un lot, tapez *modifier une production*._"
    )
    return "\n".join(lines), options


def _render_market_snapshot(exec_result: Dict[str, Any]) -> str:
    """Rendu dédié pour `get_market_snapshot` — remplace le rendu générique
    CAS 2 (liste plate) qui affichait des lignes vides ("Élément 1"/
    "Élément 2") pour cette forme de données, faute de savoir lire la clé
    `product`. Trois cas, dans l'ordre de priorité de
    `services/database/category.py::get_market_snapshot` :

    1. Produit demandé absent du catalogue → le dire clairement + lister
       les vraies catégories disponibles (demande explicite utilisateur,
       2026-08-15 : "l'agent doit signaler que les produits qui ne sont pas
       dans le catalogue ne peuvent pas être vendus sur la plateforme").
    2. Produit reconnu, prix résolu (admin `StandardPrice` en priorité,
       sinon moyenne des annonces réelles) → réponse ciblée à "quel est le
       prix de X ?".
    3. Vue d'ensemble multi-produits (pas de filtre produit) → un prix par
       sous-catégorie, comportement historique.

    Voir [[precommande-architecture-consolidation-2026-08]]."""
    if exec_result.get("product_in_catalog") is False:
        products = exec_result.get("available_products") or []
        categories = exec_result.get("available_categories") or []
        lines = [
            f"📭 {exec_result.get('message') or 'Ce produit n’est pas encore disponible sur notre plateforme.'}"
        ]
        if products:
            # Produits concrets + prix — plus actionnable qu'une simple
            # catégorie ("Céréales") : demande explicite utilisateur, voir
            # `get_available_products` (services/database/category.py).
            lines.append("")
            for item in products:
                if not isinstance(item, dict) or not item.get("name"):
                    continue
                price = item.get("price")
                if price is not None:
                    unit = str(item.get("unit") or "KG").upper()
                    tag = (
                        " (prix standard)"
                        if item.get("price_source") == "admin"
                        else " (prix moyen)"
                    )
                    lines.append(
                        f"🌾 *{item['name']}* — {fmt_num(price)} FCFA/{unit}{tag}"
                    )
                else:
                    lines.append(f"🌾 *{item['name']}*")
        elif categories:
            lines.append("\n🗂️ *Catégories disponibles sur Ladini :*")
            for cat in categories:
                icon = cat.get("icon") or "📦" if isinstance(cat, dict) else "📦"
                name = cat.get("name") if isinstance(cat, dict) else str(cat)
                if name:
                    lines.append(f"{icon} {name}")
        return "\n".join(lines)

    data = exec_result.get("data") or []
    if not data:
        return (
            exec_result.get("message")
            or "Aucune donnée de prix disponible pour le moment."
        )

    if len(data) == 1 and exec_result.get("product_in_catalog"):
        row = data[0]
        product_name = row.get("product") or "ce produit"
        lines = [f"📊 *Prix de référence — {product_name} :*\n"]
        if row.get("price_source") == "admin":
            unit = row.get("standard_unit") or "KG"
            zone_txt = (
                f" à {row['standard_price_zone']}"
                if row.get("standard_price_zone")
                else ""
            )
            lines.append(
                f"💰 Prix standard{zone_txt} : *{fmt_num(row.get('standard_price'))} FCFA/{unit}*"
            )
        elif row.get("avg_price") is not None:
            lines.append(
                f"💰 Prix moyen constaté chez les producteurs : *{fmt_num(row.get('avg_price'))} FCFA*"
            )
            if row.get("min_price") is not None:
                lines.append(f"   (à partir de {fmt_num(row.get('min_price'))} FCFA)")
        total_stock = row.get("total_stock")
        if total_stock:
            lines.append(f"📦 Stock total disponible : {fmt_num(total_stock)}")
        return "\n".join(lines)

    lines = ["📊 *Aperçu des prix du marché :*\n"]
    for row in data:
        name = row.get("product") or "Produit"
        avg = row.get("avg_price")
        min_p = row.get("min_price")
        if avg is not None:
            lines.append(
                f"• *{name}* — moy. {fmt_num(avg)} FCFA (min {fmt_num(min_p)} FCFA)"
            )
        else:
            lines.append(f"• *{name}*")
    return "\n".join(lines)


def _render_price_check(exec_result: Dict[str, Any]) -> str:
    """Rendu dédié pour `check_price_anomaly` (VALIDATE_PRICE) — sans lui,
    ce résultat (ni `message` ni `data`, juste `is_anomaly`/`reason`/
    `reference_price`) tombait dans le gabarit transactionnel générique et
    pouvait afficher un texte totalement sans rapport (bug réel 2026-08-15,
    voir `_transactional_fallback_text`)."""
    is_anomaly = bool(exec_result.get("is_anomaly"))
    ref_price = exec_result.get("reference_price")
    reason = str(exec_result.get("reason") or "").strip()

    if is_anomaly:
        return (
            f"⚠️ {reason}"
            if reason
            else "⚠️ Ce prix semble inhabituel par rapport au marché."
        )
    if ref_price is not None:
        return f"✅ Ce prix est cohérent avec le marché (référence : {fmt_num(ref_price)} FCFA)."
    return f"✅ {reason}" if reason else "✅ Prix noté."


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
            or item.get("product")
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
        options_ui.append(
            {
                "index": str(i),
                "label": label_complet,
                "value": entity_id,
            }
        )

    lines.append("\n👉 *Faites votre choix en tapant le numéro correspondant.*")
    return "\n".join(lines), options_ui


def _render_buyer_catalog_sections(
    items: List[Dict[str, Any]],
) -> Tuple[str, List[Dict[str, str]]]:
    """Audit UX interactive 2026-08-27 : construit désormais aussi les
    options de sélection pour les produits DIRECT (déjà numérotés dans le
    texte ci-dessous). Les productions futures restent du texte descriptif
    pur, comme avant ce correctif — elles n'étaient de toute façon jamais
    numérotées, donc pas de régression sur leur affichage."""
    if not items:
        return "", []

    direct_items: List[Dict[str, Any]] = []
    future_items: List[Dict[str, Any]] = []

    for item in items:
        if not isinstance(item, dict):
            continue
        source = str(
            item.get("source_type") or item.get("availability_kind") or ""
        ).upper()
        if not source and item.get("estimated_available_at"):
            source = "FUTURE"
        bucket = future_items if source == "FUTURE" else direct_items
        bucket.append(item)

    sections: List[str] = []
    options: List[Dict[str, str]] = []

    if direct_items:
        lines = ["🌐 *Produits disponibles immédiatement :*"]
        for idx, product in enumerate(direct_items, start=1):
            name = (
                product.get("name") or product.get("product_name") or f"Produit {idx}"
            )
            price = product.get("price")
            unit = str(product.get("unit") or "KG").upper()
            vendor = (
                product.get("vendor") or product.get("producer_name") or "Producteur"
            )
            zone = product.get("zone_name") or product.get("zone")
            # Mandat B2c.4 §4-10 : `pricing_label` (search_products, certifié) porte la VRAIE base
            # du prix — un conditionnement ("500 FCFA/sachet de 0,5 L") ou un lot TOTAL_LOT ne
            # doivent jamais se retrouver ici reconstruits en "X FCFA/unité". Repli legacy
            # uniquement si l'appelant n'a pas fourni ce champ (résultat antérieur à cette phase).
            price_label = product.get("pricing_label") or (
                f"{fmt_num(price)} FCFA/{unit}"
                if price not in (None, "")
                else "Prix communiqué par le producteur"
            )
            line = f"{idx}️⃣ *{name}* — {price_label} — {vendor}"
            if zone:
                line += f" ({zone})"
            available_qty = product.get("available_quantity")
            if available_qty not in (None, ""):
                line += f" | ⚖️ {fmt_num(available_qty)} {unit}"
            lines.append(line)
            options.append(
                {
                    "index": str(idx),
                    "label": f"{name} — {vendor}",
                    "value": str(
                        product.get("id") or product.get("product_id") or idx
                    ),
                }
            )
        sections.append("\n".join(lines))

    if future_items:
        lines = ["⏳ *Productions futures (précommandes ouvertes) :*"]
        for product in future_items:
            name = (
                product.get("name")
                or product.get("product_name")
                or "Production future"
            )
            price = product.get("price") or product.get("price_per_unit")
            unit = str(product.get("unit") or "KG").upper()
            eta = (
                fmt_date(product.get("estimated_available_at"))
                or product.get("estimated_available_at")
                or "date à confirmer"
            )
            vendor = (
                product.get("vendor") or product.get("producer_name") or "Producteur"
            )
            price_label = product.get("pricing_label") or (
                f"{fmt_num(price)} FCFA/{unit}"
                if price not in (None, "")
                else "Prix communiqué lors de la confirmation"
            )
            lines.append(f"• *{name}* — {price_label} — livré vers {eta} ({vendor})")
        sections.append("\n".join(lines))

    return "\n\n".join([section for section in sections if section]).strip(), options


# Pour chaque gabarit transactionnel à fort impact ci-dessous, l'outil MCP
# qui a RÉELLEMENT dû s'exécuter pour que ce texte soit vrai. `selected_tool`
# (posé frais CHAQUE tour par `mcp_tool_executor`, jamais périmé) sert de
# garde-fou : si un outil différent (ou aucun outil connu) vient de tourner,
# le gabarit est refusé même si `goal` semble correspondre — voir le bug
# réel documenté dans la docstring de `_transactional_fallback_text`.
_FALLBACK_GOAL_REQUIRES_TOOL: Dict[str, Tuple[str, ...]] = {
    "BUYER_ADD_TO_CART": ("add_to_cart",),
    "BUYER_PREORDER": ("create_preorder",),
    "PROCUREMENT_OR_AUCTION": ("create_auction",),
    "PUBLISH_OR_SELL": ("create_product", "record_sale"),
    "BID": ("place_bid",),
    "STOCK_REGISTER_HARVEST": ("add_stock",),
}


def _transactional_fallback_text(
    goal: str,
    salutation: str,
    payload: Dict[str, Any],
    selected_tool: str = "",
) -> str:
    """Gabarits par goal quand l'outil ne renvoie ni message ni collection.

    Bug réel (2026-08-15) : un prix vérifié via VALIDATE_PRICE
    (`check_price_anomaly`, un outil READ qui ne renvoie ni `message` ni
    `data`) a été annoncé "✅ Votre appel d'offres... a été enregistré avec
    succès" — `goal` ici vient de `resolve_goal_for_ui`
    (`nodes/rendering/common.py`), une résolution TOLÉRANTE conçue pour un
    badge d'UI ("jamais UNKNOWN"), qui retombe en cascade sur
    `working_memory.active_goal`/`suspended_goal`/
    `detected_intent` dès que `current_goal` est vidé (ce que
    `mcp_tool_executor` fait systématiquement à la complétion) — l'un de ces
    champs peut porter un goal PÉRIMÉ d'une tentative précédente abandonnée
    dans la même conversation (ici : un appel d'offres pour "riz" rejeté
    juste avant par le nouveau garde-fou catalogue de `create_auction`, voir
    Round 4 dans [[precommande-architecture-consolidation-2026-08]]).
    `selected_tool` — posé frais CE tour par `mcp_tool_executor`, jamais
    périmé — est la seule source fiable de "quel outil vient RÉELLEMENT de
    s'exécuter" ; les gabarits à fort impact ci-dessous ne s'appliquent que
    s'ils correspondent à l'outil réellement appelé (`selected_tool==""`
    reste toléré pour la compatibilité des appels directs/tests qui ne le
    renseignent pas).
    """
    prod_name = payload.get("product") or payload.get("product_name") or "votre demande"
    qty = fmt_num(payload.get("quantity") or payload.get("quantity_desired") or "")
    unit = str(payload.get("unit") or payload.get("unit_desired") or "").upper()
    q_info = f" pour {qty} {unit}" if qty else ""

    def _tool_matches(key: str) -> bool:
        allowed = _FALLBACK_GOAL_REQUIRES_TOOL[key]
        return not selected_tool or selected_tool in allowed

    g = (goal or "").upper()
    if g == "BUYER_ADD_TO_CART" and _tool_matches("BUYER_ADD_TO_CART"):
        return (
            f"🛒 {salutation}*{prod_name}*{q_info} a été ajouté à votre panier. "
            "Tapez *précommander* pour valider ou ajoutez un autre produit."
        )
    if g.startswith("BUYER_PREORDER") and _tool_matches("BUYER_PREORDER"):
        if prod_name == "votre demande" or not qty:
            # Fail-closed : sans produit NI quantité réels on n'annonce JAMAIS une précommande « pour 0 pour votre demande »
            # (contexte de reprise perdu) — le panier, lui, reste intact.
            return (
                f"{salutation}Je n'ai pas pu confirmer le détail de ta précommande. Ton panier est conservé — "
                "tape *précommander* pour la valider."
            )
        return (
            f"✅ {salutation}Votre précommande{q_info} pour *{prod_name}* est enregistrée. "
            "Vous recevrez le récapitulatif complet dans un instant."
        )
    if (g.startswith("PROCUREMENT_") or "AUCTION" in g) and _tool_matches(
        "PROCUREMENT_OR_AUCTION"
    ):
        return f"✅ {salutation}Votre appel d'offres{q_info} de *{prod_name}* a été enregistré avec succès."
    if ("PUBLISH" in g or "SELL" in g) and _tool_matches("PUBLISH_OR_SELL"):
        return f"✅ {salutation}Votre offre de vente{q_info} de *{prod_name}* a bien été publiée sur le marché."
    if "BID" in g and _tool_matches("BID"):
        price_bid = fmt_num(payload.get("price"))
        p_info = f" à {price_bid} FCFA" if price_bid else ""
        return f"✅ {salutation}Votre proposition de prix{p_info} pour *{prod_name}* a bien été transmise."
    # Actions d'écriture sur le stock — incident 2026-08-27 : ces goals
    # n'avaient AUCUN gabarit dédié, donc une récolte (ou un ajustement)
    # réellement enregistrée retombait sur le message générique "C'est
    # noté. Dites-moi ce que vous souhaitez faire..." — une rupture de ton
    # juste après une action confirmée avec succès. Match EXACT (pas
    # substring) : "STOCK_" est aussi le préfixe des goals de LECTURE
    # (STOCK_GET_DETAIL/MOVEMENTS/SUMMARY), gérés par la branche LECTURE
    # ci-dessous.
    if g == "STOCK_REGISTER_HARVEST" and _tool_matches("STOCK_REGISTER_HARVEST"):
        return f"✅ {salutation}Récolte enregistrée{q_info} pour *{prod_name}*."
    if any(
        tok in g
        for tok in ("LIST", "GET", "CHECK", "SEARCH", "VIEW", "DASHBOARD", "SNAPSHOT")
    ) and (not selected_tool or selected_tool.startswith(("get_", "list_", "search_", "check_"))):
        # Goal de LECTURE sans contenu : rester neutre et honnête. Incident
        # réel (2026-08-27) : contrairement aux branches ci-dessus, cette
        # branche n'avait AUCUN garde-fou `_tool_matches` — un `goal` PÉRIMÉ
        # contenant "GET" (ex: reliquat de `get_farms` appelé par
        # context_resolver plus tôt dans la même conversation) a annoncé
        # "rien à afficher" juste après qu'un `add_stock` (575 poulets) ait
        # RÉUSSI. Le garde-fou ci-dessus n'autorise ce texte que si l'outil
        # réellement exécuté ce tour est lui-même un outil de lecture.
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
        return apply_corrections(
            state,
            {
                "final_response": precomputed_success,
                "ag_ui_component": state.get("ag_ui_component"),
            },
        )

    exec_result = unwrap_execution_result(raw_exec_result)
    tool_msg = exec_result.get("message") or ""
    tool_data = exec_result.get("data")
    if tool_data in (None, {}):
        results_payload = exec_result.get("results")
        if isinstance(results_payload, (list, dict)):
            tool_data = results_payload

    text_output = str(tool_msg) or "🌾 Opération réussie."
    ag_component: Optional[Dict[str, Any]] = None
    selected_tool_name = str(state.get("selected_tool") or "").lower().strip()
    user_phone = str(state.get("user_phone") or "").strip()

    # ── Enchères/appels d'offres : proposer d'ajouter une photo juste après
    # l'action (photo liée automatiquement à CE bid/CETTE auction dès qu'une
    # image arrive — voir services/pending_photo_target.py +
    # workers/media/product_photo_task.py). Le producteur/l'acheteur peut
    # bien sûr ignorer la proposition, rien n'est bloquant.
    if selected_tool_name == "place_bid" and user_phone and exec_result.get("bid_id"):
        _set_pending_bid_photo(user_phone, str(exec_result["bid_id"]))
        text_output += (
            "\n\n📸 Envoyez une photo de ce lot pour rassurer l'acheteur — "
            "elle sera automatiquement liée à cette offre."
        )
    elif (
        selected_tool_name == "create_auction"
        and user_phone
        and exec_result.get("auction_id")
    ):
        _set_pending_auction_photo(user_phone, str(exec_result["auction_id"]))
        text_output += "\n\n📸 Vous pouvez aussi envoyer une photo de référence pour cet appel d'offres."
    elif (
        selected_tool_name == "create_product"
        and user_phone
        and exec_result.get("product_id")
    ):
        # (2026-09-15) Demandé explicitement APRÈS la création, jamais avant
        # ni pendant : le produit est déjà publié à ce stade (moins de
        # risque de perturber la saisie), et le producteur peut enchaîner la
        # création d'autant de produits qu'il veut sans y être obligé — la
        # photo reste une simple invitation non bloquante, comme pour
        # enchères/appels d'offres ci-dessus.
        _set_pending_product_photo(user_phone, str(exec_result["product_id"]))
        text_output += (
            "\n\n📸 Voulez-vous ajouter une photo de ce produit ? Envoyez-la "
            "simplement maintenant, ou continuez avec un autre produit."
        )

    formatted_menu_raw = exec_result.get("formatted_menu")
    formatted_menu_text = (
        formatted_menu_raw.strip() if isinstance(formatted_menu_raw, str) else ""
    )
    has_formatted_menu = bool(formatted_menu_text)
    if has_formatted_menu:
        text_output = formatted_menu_text

        # ── Menus enchères/offres numérotés : "photos <numéro>" ────────
        # Même mécanisme que le catalogue de recherche et le menu "choix
        # producteur" (services/search_results_cache.py) — la numérotation du
        # menu WHATSAPP (menu_lines) et celle de `data` sont construites dans
        # la MÊME boucle côté DB (services/database/auction.py), donc
        # strictement alignées.
        _AUCTION_MENU_TOOLS: Dict[str, tuple[str, Callable[[Dict[str, Any]], str]]] = {
            "get_auctions_bids": (
                "bid_id",
                lambda it: (
                    f"{it.get('product') or 'Lot'} — {it.get('producer') or 'Producteur'}"
                ),
            ),
            "get_producer_auctions": (
                "auction_id",
                lambda it: (
                    f"{it.get('product') or 'Lot'} — {it.get('buyer_name') or 'Acheteur'}"
                ),
            ),
            "get_my_active_bids": (
                "bid_id",
                lambda it: f"{it.get('product') or 'Lot'} (votre offre)",
            ),
        }
        menu_tool_spec = _AUCTION_MENU_TOOLS.get(selected_tool_name)
        if menu_tool_spec and isinstance(tool_data, list) and user_phone:
            id_key, label_fn = menu_tool_spec
            if _cache_numbered_items_with_photos(
                user_phone, tool_data, id_key, label_fn
            ):
                text_output += (
                    "\n\n📸 Tapez *photos <numéro>* pour voir les photos d'une ligne."
                )

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
                ag_component = _menu(
                    "Catalogue produits", catalog_options, mode="catalog"
                )

        if cycles_payload:
            cycles_text, cycles_options = _render_cycles_section(cycles_payload)
            if cycles_text:
                sections.append(cycles_text)
            if cycles_options and ag_component is None:
                ag_component = _menu(
                    "Productions à venir", cycles_options, mode="cycles"
                )

        if sections:
            text_output = "\n\n".join(
                [section for section in sections if section]
            ).strip()
            structured_sections_handled = True
        elif not tool_msg:
            text_output = (
                f"{salutation}Vous n'avez encore aucun produit ni exploitation "
                "enregistrés. Tapez *ajouter un produit* pour commencer à vendre, "
                "ou *créer une ferme* pour configurer votre exploitation."
            )
            structured_sections_handled = True

    # ── Prix du marché / prix de référence (get_market_snapshot) ────────
    if (
        allow_structured_render
        and not structured_sections_handled
        and selected_tool_name == "get_market_snapshot"
    ):
        text_output = _render_market_snapshot(exec_result)
        structured_sections_handled = True

    # ── Validation de prix (check_price_anomaly / VALIDATE_PRICE) ──────
    # Outil READ qui ne renvoie ni `message` ni `data` (juste is_anomaly/
    # reason/reference_price) — sans ce rendu dédié, il tombait dans le
    # gabarit transactionnel générique CAS 3, cf. bug documenté dans
    # `_transactional_fallback_text`.
    if (
        allow_structured_render
        and not structured_sections_handled
        and selected_tool_name == "check_price_anomaly"
    ):
        text_output = _render_price_check(exec_result)
        structured_sections_handled = True

    # ── Catalogue acheteur (search_products) ──────────────────────────
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

        buyer_sections, buyer_options = _render_buyer_catalog_sections(
            results_payload_list
        )
        if buyer_sections:
            text_output = buyer_sections
            structured_sections_handled = True
            if buyer_options and ag_component is None:
                ag_component = _menu(
                    "Produits disponibles", buyer_options, mode="buyer_catalog"
                )

            # Cache les résultats DIRECT (numéro affiché -> id/photos) pour
            # que la commande WhatsApp « photos <numéro> » retrouve quel
            # produit un numéro désignait (workers/media/product_photo_task.py).
            # Même filtre DIRECT/FUTURE que `_render_buyer_catalog_sections`
            # ci-dessus, dupliqué ici à dessein pour ne pas changer le contrat
            # de retour (string, str) de cette fonction pure, verrouillé par
            # tests/nodes/test_rendering_success.py::TestRenderBuyerCatalogSections.
            direct_items = [
                item
                for item in results_payload_list
                if isinstance(item, dict)
                and str(
                    item.get("source_type") or item.get("availability_kind") or ""
                ).upper()
                == "DIRECT"
            ]
            if direct_items:
                phone = str(state.get("user_phone") or "").strip()
                entries = {
                    str(idx): {
                        "id": item.get("id"),
                        "name": item.get("name"),
                        "images": item.get("images") or [],
                    }
                    for idx, item in enumerate(direct_items, start=1)
                }
                _store_search_photo_results(phone, entries)
                if any(item.get("images") for item in direct_items):
                    text_output += "\n\n📸 Tapez *photos <numéro>* pour voir des photos d'un résultat."

    # ── CAS 2 : liste plate ───────────────────────────────────────────
    # NB : exclut get_market_snapshot — ses lignes sont des prix de référence,
    # pas des éléments sélectionnables. Sans cette exclusion, _render_flat_list
    # construit quand même un `options_ui` (append inconditionnel par ligne,
    # voir la boucle ci-dessus) et écrase `ag_component` avec un faux menu
    # « Faites votre choix » par-dessus le rendu correct de
    # _render_market_snapshot, même si `structured_sections_handled` est déjà
    # True (cette branche ne teste pas ce flag — volontaire pour search_products,
    # qui veut un menu de sélection en plus de son texte dédié).
    if (
        isinstance(tool_data, list)
        and tool_data
        and selected_tool_name != "get_market_snapshot"
    ):
        flat_text, options_ui = _render_flat_list(tool_data)
        if allow_structured_render and not structured_sections_handled:
            text_output = flat_text

        if options_ui:
            if ag_component is None:
                ag_component = _menu("Faites votre choix", options_ui, mode="flat_list")
            else:
                kwargs = (
                    ag_component.get("kwargs")
                    if isinstance(ag_component, dict)
                    else None
                )
                if isinstance(kwargs, dict) and "metadata" in kwargs:
                    kwargs["metadata"].setdefault("count", len(options_ui))

    # ── CAS 2B : dict simple {farm_name, stocks} ──────────────────────
    elif (
        not structured_sections_handled
        and isinstance(tool_data, dict)
        and "stocks" in tool_data
    ):
        stocks_list = tool_data.get("stocks") or []
        farm_label = (
            tool_data.get("farm_name") or tool_data.get("name") or "cette exploitation"
        )
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
            # Bug réel (2026-08-14) : "Votre appel d'offres pour 0 de *votre
            # demande* a été enregistré avec succès" — pour les goals à
            # formulaire (ex: PROCUREMENT_CREATE_REQUEST/appel d'offres),
            # produit/quantité/prix sont capturés dans `form_data` (voir
            # `build_procurement_escalation`), PAS dans `transaction_payload`
            # qui peut être quasi vide à ce stade (juste `resolved_id`/
            # `confirm` du dernier tour). Le payload passé au gabarit doit
            # d'abord se rabattre sur form_data, avec les valeurs non-vides
            # de `payload` en priorité (le plus récent des deux).
            #
            # Récidive réelle (2026-09-07, STOCK_REGISTER_HARVEST) : "Récolte
            # enregistrée pour 0  pour votre demande" — MÊME classe de bug,
            # goal SANS form_data cette fois. Root cause précise : `add_stock`
            # a bien reçu quantity=6000 (confirmé — l'écriture DB est
            # correcte), mais `state_cleaner_node` tourne AVANT `final_response`
            # (edge réel : response_strategy -> state_cleaner -> final_response)
            # et efface `transaction_payload` dès que `status=="COMPLETED"`
            # (reset terminal-goal, `{"__reset__": True}`) — `payload` ici est
            # donc déjà vide au moment du rendu, quel que soit le goal.
            # `confirmation_summary_payload` (posé par `confirmation_gate` au
            # tour où le récap a été affiché, JAMAIS effacé par ce reset
            # terminal) est le snapshot figé qui survit — seconde source de
            # repli, pour tout goal transactionnel qui n'utilise pas form_data.
            form_data = state.get("form_data") or {}
            confirmed_payload = state.get("confirmation_summary_payload") or {}
            effective_payload = {
                **form_data,
                **confirmed_payload,
                **{k: v for k, v in payload.items() if v not in (None, "", [], {})},
            }
            text_output = _transactional_fallback_text(
                goal or "",
                salutation,
                effective_payload,
                selected_tool_name,
            )
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

    return apply_corrections(
        state,
        {
            "final_response": text_output,
            "ag_ui_component": ag_component,
        },
    )
