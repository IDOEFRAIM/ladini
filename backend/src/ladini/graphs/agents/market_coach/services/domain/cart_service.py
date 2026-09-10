"""Cart domain service — isolates buyer cart orchestration helpers."""
from __future__ import annotations

import logging
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from ladini.domain.order_policy import (
    validate_minimum_order_quantity,
)
from ladini.domain.pricing_tiers import (
    PACK_UNIT_CONTENT,
    PACK_UNIT_FOREIGN,
    PricingTierError,
    classify_pack_count_unit,
    compute_line,
    resolve_tier,
)
from ladini.domain.quantity_unit import convert_quantity, normalize_unit
from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    set_pending_interaction,
)
from ladini.graphs.agents.market_coach.flows.common.menu_contracts import (
    MenuOption,
    MenuRequest,
)
from ladini.graphs.agents.market_coach.flows.common.menu_text import (
    render_cart_actions_hint,
    render_selection_prompt,
)
from ladini.graphs.agents.market_coach.services.mcp.gateway import (
    AgentActionGateway,
    PreorderGateway,
    ProductGateway,
    StockGateway,
)
from ladini.graphs.agents.market_coach.services.text_pagination import (
    paginate_item_blocks,
)
from ladini.graphs.agents.market_coach.utils import (
    MarketRuntime,
    is_success_response,
    unwrap_tool_envelope,
)
from ladini.services.search_results_cache import (
    store_results as _store_search_photo_results,
)

from .buyer_common import SUPPORT_FOOTER, with_support_footer

logger = logging.getLogger("Ladini.Market.CartService")


class ProductLookupUnavailable(Exception):
    """La recherche catalogue a échoué TECHNIQUEMENT (outil MCP indisponible,
    timeout, panne transitoire) — à ne JAMAIS confondre avec « aucun vendeur
    pour ce produit ».

    Chantier résilience 2026-08 : `resolve_product_vendors` renvoyait
    `([], False)` dans les DEUX cas. Les appelants
    (`flows/buyer/cart.py`, `flows/buyer/procurement.py`) rendent une liste
    vide par « 📭 Le produit X n'est pas disponible dans notre catalogue » —
    donc une simple panne backend affirmait à l'acheteur, à tort, que le
    produit n'existe pas, et l'orientait vers un appel d'offres inutile pour
    un produit pourtant bien en stock. Exactement le piège que
    `flows/producer/flow.py` documente déjà pour ses propres listes
    (« NE JAMAIS confondre une erreur technique avec une liste réellement
    vide ») — la leçon n'avait jamais été appliquée côté acheteur.
    """


def _normalize_for_match(text: Any) -> str:
    text = str(text or "").lower().strip()
    text = text.replace("œ", "oe").replace("æ", "ae")
    text = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def _is_confident_product_match(search_term: Any, matched_name: Any) -> bool:
    """True when the matched product plausibly IS what was searched for —
    an exact/substring relationship between the (accent/case-normalized)
    search term and the matched product name.

    `search_products` (services/database/buyer.py) uses trigram fuzzy
    matching with a deliberately low threshold (0.22) to tolerate typos
    ("tomte" → "tomate") — a documented, intentional recall-over-precision
    tradeoff. But trigram similarity on short words can't distinguish a typo
    from a genuinely different word: a real incident (2026-08-13) had
    "oeufs" (eggs) fuzzy-match "Bœuf" (a live cattle listing at
    486 000 FCFA/head) — the buyer asked for a dozen eggs and ended up with
    a ~4.86M FCFA cattle reservation auto-confirmed, because the single-vendor
    "skip the menu, go straight to quantity/checkout" shortcut trusted ANY
    fuzzy match unconditionally.

    This is NOT a threshold problem — raising it doesn't reliably separate
    "oeufs"/"Bœuf" from "tomte"/"tomate" (both are short words with heavy
    trigram overlap purely from letter reuse). Instead: any match that is
    NOT a textual substring of the other is treated as low-confidence and
    routed through an explicit confirmation step before it's ever added to
    a cart or reserved — see [[buyer-search-fuzzy-match-safety-2026-08]].
    """
    term = _normalize_for_match(search_term)
    name = _normalize_for_match(matched_name)
    if not term or not name:
        return False
    return term in name or name in term


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
        try:
            res = await ProductGateway(self.mc_runtime).search_products(
                product=str(product_name),
                phone=str(phone),
            )
        except Exception as exc:
            logger.error(
                "resolve_product_vendors: search_products a échoué pour '%s': %s",
                product_name, exc,
            )
            raise ProductLookupUnavailable(str(exc)) from exc
        if not is_success_response(res):
            # Panne backend, PAS un catalogue vide — voir ProductLookupUnavailable.
            logger.error(
                "resolve_product_vendors: search_products a renvoyé un échec pour '%s': %s",
                product_name, (res or {}).get("message"),
            )
            raise ProductLookupUnavailable(str((res or {}).get("message") or "search_products failed"))
        results = res.get("results") or (res.get("data") or {}).get("results") or []
        if not results:
            return [], False

        vendors: List[Dict[str, Any]] = []
        seen_ids: set[str] = set()
        for item in results:
            pid = str(item.get("id") or "")
            # Cast str obligatoire : asyncpg renvoie souvent un uuid.UUID brut
            # pour les colonnes UUID — non casté ici, cette valeur finissait
            # telle quelle dans MenuOption.value (typé Optional[str]), un
            # contrat que le dataclass ne fait pas respecter à l'exécution.
            producer_id = str(item.get("producer_id") or item.get("vendor_id") or "")
            key = f"{pid}:{producer_id}"
            if key in seen_ids:
                continue
            seen_ids.add(key)
            source_type = _infer_source_type(item)
            matched_name = _clean_product_name(item.get("name") or product_name)
            vendors.append(
                {
                    "product_id": pid,
                    # Pour une PRODUCTION FUTURE, `id` renvoyé par search_products est
                    # l'id du MarketOffer → requis par reserve_future_offer.
                    "market_offer_id": pid if source_type == "FUTURE" else None,
                    "crop_cycle_id": item.get("crop_cycle_id"),
                    "name": matched_name,
                    # Voir _is_confident_product_match — un match qui n'est PAS un
                    # sous-texte du terme cherché (ou l'inverse) n'a une similarité
                    # trigram que par coïncidence de lettres (ex: "oeufs"/"Bœuf") et
                    # doit passer par une confirmation avant d'être traité comme
                    # résolu. Voir [[buyer-search-fuzzy-match-safety-2026-08]].
                    "match_confident": _is_confident_product_match(
                        product_name, matched_name
                    ),
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
                    # Voir services/search_results_cache.py — permet à l'acheteur
                    # de demander "photos <numéro>" pour un producteur du menu.
                    "images": item.get("images") or [],
                    "pricing_tiers": item.get("pricing_tiers"),
                    # (2026-09-02) Seuil minimum de commande — politique
                    # PLATEFORME portée par le type de produit (SubCategory),
                    # jamais par ce vendeur/produit précis. Voir
                    # domain/order_policy.py. Simple passage de contexte :
                    # `add_to_cart_with_ref` est le SEUL endroit qui décide.
                    "minimum_order_quantity": item.get("minimum_order_quantity"),
                    "minimum_order_unit": item.get("minimum_order_unit"),
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
        phone: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], MenuRequest]:
        header = f"🔍 *Producteurs disponibles pour « {product_name} » :*"
        item_blocks: List[str] = []
        options: List[MenuOption] = []
        photo_entries: Dict[str, Dict[str, Any]] = {}

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
            qty_info = (
                f" — Dispo: {v['available_qty']}" if v.get("available_qty") else ""
            )
            label = (
                f"{v['vendor_name']}{zone_info} — {v['price']} FCFA/{v['unit']}"
                f"{qty_info}{source_tag}"
            )
            item_blocks.append(f"*{i}.* {label}")
            options.append(
                MenuOption(
                    index=str(i), label=label, value=v.get("producer_id") or str(i)
                )
            )
            photo_entries[str(i)] = {
                "id": v.get("product_id"),
                "name": f"{product_name} — {v.get('vendor_name') or 'Producteur'}",
                "images": v.get("images") or [],
            }

        item_blocks.append(render_selection_prompt(noun="producteur"))
        # Même mécanisme que le catalogue de recherche brut
        # (nodes/rendering/success.py) — voir services/search_results_cache.py.
        # Numérotation PARTAGÉE avec la sélection de producteur ci-dessus
        # (memory.py::mapping_kind "product_vendor") : aucune collision
        # possible, "photos <numéro>" exige toujours le préfixe "photos ",
        # jamais un chiffre seul.
        if phone and any(entry["images"] for entry in photo_entries.values()):
            _store_search_photo_results(str(phone), photo_entries)
            item_blocks.append(
                "📸 Tapez *photos <numéro>* pour voir des photos d'un producteur."
            )
        if post_hint:
            item_blocks.append(post_hint.strip())
        # Pagination (incident 2026-08-27) : une recherche produit avec
        # beaucoup de producteurs disponibles produisait un texte non borné
        # — voir services/text_pagination.py.
        menu_text = paginate_item_blocks(header, item_blocks)

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
            **set_pending_interaction(InteractionKind.SELECTION_MENU),
            "response_strategy": "SELECTION_MENU",
            "final_response": menu_text,
            "ag_ui_component": None,
            "vendor_selection_context": vendor_context,
            # Incident réel (2026-08-31) : ce menu producteur seed toujours
            # un `vendor_selection_context` FRAIS pour LE produit recherché
            # (ex: "poulets"), mais ne touchait jamais `tier_selection_context`
            # — un palier laissé par un achat PRÉCÉDENT, totalement différent
            # et déjà validé (ex: "lait", 5L/10L bidon), survivait alors dans
            # ce canal `replace_value` (rien ne l'efface tant que personne ne
            # le réécrit explicitement) et se faisait réutiliser au tour
            # suivant dès qu'un vendeur était choisi — affichant un menu de
            # paliers avec les DONNÉES DE L'ANCIEN PRODUIT sous le nom du
            # nouveau. Toute recherche fraîche invalide structurellement tout
            # palier en attente d'un produit différent : jamais de report
            # implicite entre deux recherches.
            "tier_selection_context": None,
            "pending_menu": menu,
        }
        return state_patch, menu

    @staticmethod
    def recompute_cart_meta(cart: List[Dict[str, Any]]) -> Dict[str, Any]:
        total = sum(float(line.get("line_total") or 0.0) for line in cart)
        return {
            "total_amount": round(total, 2),
            "currency": "XOF",
            "items_count": len(cart),
        }

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
            vendor_info = (
                f" — _{line.get('vendor_name')}_" if line.get("vendor_name") else ""
            )
            notification_badge = ""
            if line.get("notification_status") == "PENDING_RESPONSE":
                notification_badge = " ⏳"
            elif line.get("notification_status") == "RESPONDED":
                notification_badge = " ✅"

            # Ligne à palier : `quantity` est un NOMBRE DE PAQUETS et `unit`
            # l'unité du CONTENU — les afficher accolés ("30 L × 900") laissait
            # croire à 30 litres alors qu'il s'agit de 30 bidons. On rend le
            # conditionnement explicite et on rappelle la quantité totale.
            if line.get("tier_id") and line.get("base_unit_quantity") is not None:
                packaging_lbl = line.get("packaging") or "paquet"
                tier_qty = line.get("tier_quantity")
                pack_lbl = (
                    f"{packaging_lbl} de {_fmt_num(tier_qty)} {line.get('unit')}"
                    if tier_qty is not None
                    else packaging_lbl
                )
                measure = (
                    f"{_fmt_num(line.get('base_unit_quantity'))} {line.get('unit')}"
                )
                lines.append(
                    f"\n*{i}. {line.get('name')}* ({source_label}){vendor_info}{notification_badge}\n"
                    f"   {_fmt_num(line.get('quantity'))} × {pack_lbl} "
                    f"({_fmt_num(line.get('price'))} FCFA) = "
                    f"*{_fmt_num(line.get('line_total'))} FCFA*\n"
                    f"   _Quantité totale : {measure}_"
                )
            else:
                lines.append(
                    f"\n*{i}. {line.get('name')}* ({source_label}){vendor_info}{notification_badge}\n"
                    f"   {_fmt_num(line.get('quantity'))} {line.get('unit')} × {_fmt_num(line.get('price'))} = "
                    f"*{_fmt_num(line.get('line_total'))} FCFA*"
                )
            options.append(
                {
                    "index": str(i),
                    "label": f"{line.get('name')} (x{line.get('quantity')})",
                }
            )
            if line.get("is_auction"):
                has_auction_items = True

        lines.append(
            f"\n💰 *Total estimé : {_fmt_num(meta.get('total_amount'))} {meta.get('currency')}*"
        )
        lines.append(render_cart_actions_hint(has_auction_items))

        if pending_line:
            lines.append("\n" + pending_line)
        cart_text = "\n".join(lines)
        return {
            "final_response": cart_text,
            "ag_ui_component": None,
            "pending_menu": MenuRequest(
                title="Votre panier",
                options=[
                    MenuOption(index=o["index"], label=o["label"]) for o in options
                ],
                kind="cart",
                metadata={
                    "actions": ["precommander", "ajouter", "négocier", "annuler"]
                },
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
        buyer_unit: Optional[str] = None,
        tier_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        try:
            qty = float(quantity)
        except (TypeError, ValueError):
            qty = 0.0

        if qty <= 0:
            return {
                "status": "WAITING_INPUT",
                **set_pending_interaction(InteractionKind.ENTER_QUANTITY, field_name="quantity"),
                "response_strategy": "ASK_MISSING_FIELD",
                "missing_fields": ["quantity"],
                "final_response": (
                    f"📦 Quelle quantité de *{product_name}* souhaitez-vous ?\n"
                    "💡 _Exemples : 50 kg, 2 sacs, 100 kg..._"
                ),
                "ag_ui_component": None,
            }

        # (2026-08-30) Sélection d'un palier de prix/conditionnement — voir
        # domain/pricing_tiers.py. `qty` devient alors un NOMBRE DE PAQUETS
        # du palier choisi (ex: "3" -> 3 bidons de 10L), jamais une quantité
        # en unité de base : la conversion d'unité juste en dessous (pensée
        # pour une quantité flate) ne s'applique donc PAS ici.
        selected_tier = None
        tier_pack_count: Optional[int] = None
        computed_line = None
        if tier_id:
            try:
                selected_tier = resolve_tier(ref.get("pricing_tiers"), tier_id)
            except PricingTierError as exc:
                return {
                    "status": "WAITING_INPUT",
                    **set_pending_interaction(InteractionKind.ENTER_QUANTITY, field_name="quantity"),
                    "response_strategy": "ASK_MISSING_FIELD",
                    "final_response": f"⚠️ {exc}",
                    "ag_ui_component": None,
                }

            # Faille A (audit 2026-09-01) : `buyer_unit` (l'unité écrite dans le
            # message de CE tour — jamais celle du payload fusionné, voir
            # flows/buyer/cart.py::_fresh_unit_this_turn) était totalement
            # ignoré sur cette branche. Un `qty` sur un palier est un NOMBRE DE
            # PAQUETS, sans dimension : toute unité de MESURE écrite par
            # l'acheteur signifie qu'il a répondu autre chose qu'un nombre de
            # paquets. On refuse et on redemande — jamais de conversion
            # silencieuse (pas de "30 L → 3 bidons" automatique, règle métier
            # explicite). Voir domain/pricing_tiers.py::classify_pack_count_unit
            # pour les trois verdicts et leurs cas limites ("3 sacs" sur un
            # palier "sac de 50 KG" = 3 paquets, pas un refus).
            tier_unit = (
                normalize_unit(selected_tier.unit)
                or str(selected_tier.unit or "").upper()
            )
            unit_verdict = classify_pack_count_unit(selected_tier, buyer_unit)
            if unit_verdict in (PACK_UNIT_CONTENT, PACK_UNIT_FOREIGN):
                vendor_label = ref.get("vendor_name") or "ce producteur"
                packaging_lbl = selected_tier.packaging or tier_unit.lower()
                requested_lbl = str(
                    normalize_unit(buyer_unit) or buyer_unit or ""
                ).lower()
                # Unité LITTÉRALE du palier ("L"), pas sa forme normalisée
                # ("LITRE") : le message doit reprendre mot pour mot le
                # libellé déjà affiché dans le menu.
                tier_lbl = f"{selected_tier.quantity:g} {selected_tier.unit}"
                if unit_verdict == PACK_UNIT_CONTENT:
                    detail = (
                        f"⚠️ *{_fmt_num(qty)} {requested_lbl}*, c'est une quantité "
                        f"totale — or ce conditionnement se commande par "
                        f"*{packaging_lbl}* de *{tier_lbl}*.\n\n"
                        f"📦 Combien de *{packaging_lbl}* de *{tier_lbl}* "
                        "souhaitez-vous ? _(répondez par un nombre, ex : 3)_"
                    )
                else:
                    detail = (
                        f"⚠️ Ce conditionnement de *{ref.get('name') or product_name}* "
                        f"chez *{vendor_label}* est vendu par *{packaging_lbl}* de "
                        f"*{tier_lbl}*, pas en {requested_lbl}. Combien de "
                        f"*{packaging_lbl}* souhaitez-vous ?"
                    )
                return {
                    "status": "WAITING_INPUT",
                    **set_pending_interaction(InteractionKind.ENTER_QUANTITY, field_name="quantity"),
                    "response_strategy": "ASK_MISSING_FIELD",
                    "final_response": detail,
                    "ag_ui_component": None,
                }

            # Faille B (audit 2026-09-01) : `int(qty)` tronquait
            # silencieusement les décimales ("2.5" -> 2 paquets, sans
            # avertir l'acheteur). On rejette explicitement au lieu de
            # deviner.
            if not float(qty).is_integer():
                packaging_lbl = selected_tier.packaging or "paquets"
                return {
                    "status": "WAITING_INPUT",
                    **set_pending_interaction(InteractionKind.ENTER_QUANTITY, field_name="quantity"),
                    "response_strategy": "ASK_MISSING_FIELD",
                    "final_response": (
                        f"⚠️ Merci d'indiquer un nombre ENTIER de "
                        f"*{packaging_lbl}* (ex : 2, pas {qty:g})."
                    ),
                    "ag_ui_component": None,
                }

            try:
                tier_pack_count = int(qty)
                computed_line = compute_line(selected_tier, tier_pack_count)
            except PricingTierError as exc:
                return {
                    "status": "WAITING_INPUT",
                    **set_pending_interaction(InteractionKind.ENTER_QUANTITY, field_name="quantity"),
                    "response_strategy": "ASK_MISSING_FIELD",
                    "final_response": f"⚠️ {exc}",
                    "ag_ui_component": None,
                }

        # Bug réel (2026-08-17) : cette listing est vendue dans SON unité
        # (`ref["unit"]`, ex: TONNE), mais l'acheteur peut avoir donné une
        # unité DIFFÉRENTE dans son message (ex: "45 kg"). Sans conversion,
        # `qty` (45) était appliqué tel quel contre le prix/unité TONNE du
        # producteur → panier affichant "45 TONNE" pour une demande de 45 kg
        # (risque de sur-tarification ~1000x). On convertit UNIQUEMENT quand
        # une équivalence universelle existe (KG<->TONNE) ; sinon on ne
        # devine jamais — on redemande explicitement dans l'unité vendue.
        ref_unit = normalize_unit(ref.get("unit")) or str(ref.get("unit") or "KG").upper()
        if selected_tier is None:
            requested_unit = normalize_unit(buyer_unit) if buyer_unit else None
            if requested_unit and requested_unit != ref_unit:
                converted = convert_quantity(qty, requested_unit, ref_unit)
                if converted is None:
                    vendor_label = ref.get("vendor_name") or "ce producteur"
                    return {
                        "status": "WAITING_INPUT",
                        **set_pending_interaction(InteractionKind.ENTER_QUANTITY, field_name="quantity"),
                        "response_strategy": "ASK_MISSING_FIELD",
                        "final_response": (
                            f"⚠️ *{ref.get('name') or product_name}* chez *{vendor_label}* est vendu "
                            f"en *{ref_unit}*, pas en {requested_unit.lower()}. "
                            f"Merci d'indiquer la quantité directement en {ref_unit.lower()} "
                            f"(ex : 50 {ref_unit.lower()})."
                        ),
                        "ag_ui_component": None,
                    }
                qty = converted

        # --- SEUIL MINIMUM DE COMMANDE (2026-09-02, politique plateforme) ---
        # Voir domain/order_policy.py. AVANT la vérification de stock (même
        # ordre que le reste du pipeline : quantité finale → règles métier →
        # stock) et pour LES DEUX flows, palier ET tarif unique — jamais
        # dérivé du prix ni de `pricing_tiers`. `total_quantity` est déjà la
        # quantité RÉELLE en unité de base (`computed_line.base_unit_quantity`
        # pour un palier — jamais `package_count` seul, voir règle 9 du
        # cahier des charges — sinon `qty` telle quelle), exactement la même
        # valeur que celle envoyée à la vérification de stock juste en
        # dessous.
        total_quantity_for_policy = (
            computed_line.base_unit_quantity if computed_line is not None else qty
        )
        minimum_check = validate_minimum_order_quantity(
            minimum_order_quantity=ref.get("minimum_order_quantity"),
            minimum_order_unit=ref.get("minimum_order_unit"),
            total_quantity=total_quantity_for_policy,
            total_unit=ref_unit,
        )
        if not minimum_check.passed:
            display_name = ref.get("name") or product_name
            if minimum_check.reason == "UNIT_INCOMPATIBLE":
                msg = (
                    f"⚠️ Le seuil minimum de commande pour *{display_name}* est "
                    f"défini en *{minimum_check.minimum_unit}*, incompatible avec "
                    f"*{ref_unit}*. Merci d'indiquer votre quantité en "
                    f"{str(minimum_check.minimum_unit or '').lower()}."
                )
            else:
                min_qty = minimum_check.minimum_in_total_unit
                base_msg = (
                    f"⚠️ La quantité minimale pour *{display_name}* est de "
                    f"*{min_qty:g} {ref_unit}*.\n\n"
                    f"Votre commande actuelle représente *{total_quantity_for_policy:g} "
                    f"{ref_unit}*."
                )
                if selected_tier is not None:
                    # Produit à palier : le nombre de PAQUETS reste la seule
                    # unité de réponse valide (voir domain/pricing_tiers.py —
                    # aucune conversion automatique quantité totale <->
                    # paquets, règle "pas de tetris"). On calcule combien de
                    # paquets suffiraient, à titre indicatif seulement — la
                    # question reste "combien de paquets ?", jamais "combien
                    # de KG ?".
                    packaging_lbl = selected_tier.packaging or "paquets"
                    min_packs = -(-min_qty // selected_tier.quantity)  # ceil
                    msg = (
                        f"{base_msg}\n\n"
                        f"📦 Combien de *{packaging_lbl}* de "
                        f"*{selected_tier.quantity:g} {selected_tier.unit}* "
                        f"souhaitez-vous ? _(il en faut au moins {min_packs:g} "
                        f"pour atteindre le minimum)_"
                    )
                else:
                    msg = (
                        f"{base_msg}\n\n"
                        f"Veuillez augmenter la quantité à au moins {min_qty:g} "
                        f"{ref_unit.lower()} pour continuer."
                    )
            # Conserve le contexte (produit/vendeur/palier déjà résolus) —
            # règle 25/27 : jamais de réinitialisation du tunnel, jamais de
            # complément automatique de la quantité. On redemande simplement
            # la quantité (ou le nombre de paquets, pour un palier — la
            # question reste scopée exactement comme avant ce refus).
            return {
                "status": "WAITING_INPUT",
                **set_pending_interaction(InteractionKind.ENTER_QUANTITY, field_name="quantity"),
                "response_strategy": "ASK_MISSING_FIELD",
                "final_response": msg,
                "ag_ui_component": None,
            }

        source_type = str(ref.get("source_type") or "DIRECT").upper()
        if source_type == "FUTURE":
            # PRODUCTION FUTURE → vraie RÉSERVATION (précommande) au lieu d'un
            # simple message informatif. Aucun débit de stock : reserve_future_offer
            # incrémente MarketOffer.reserved_quantity et crée un Order(PREORDER)
            # lié via market_offer_id, puis notifie le producteur.
            offer_id = (
                ref.get("market_offer_id") or ref.get("offer_id") or ref.get("id")
            )
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
                msg = (
                    reservation.get("message")
                    or "Cette production n'a pas pu être réservée pour le moment."
                )
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
        # La disponibilité se vérifie toujours en unité de BASE du produit —
        # `computed_line.base_unit_quantity` (déjà convertie) quand un palier
        # est choisi, sinon `qty` telle quelle (comportement inchangé).
        stock_check_qty = (
            computed_line.base_unit_quantity if computed_line is not None else qty
        )
        check = await StockGateway(self.mc_runtime).validate_stock_availability(
            product_id=resolved_pid,
            quantity=stock_check_qty,
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
            resolved_pid,
            qty,
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
                msg = (
                    check.get("message")
                    or f"Le produit « {display_name} » n'existe plus dans le catalogue."
                )
            elif available is not None:
                # Audit 2026-09-01 : ce message comparait la disponibilité (en
                # unité de BASE) au `qty` brut — qui, sur un palier, est un
                # NOMBRE DE PAQUETS. D'où des refus absurdes du type "seulement
                # 500 LITRE disponible(s) sur 60 LITRE demandé(s)" pour 60
                # bidons de 10 L (= 600 L). Les deux membres doivent être dans
                # la même unité : `stock_check_qty` est exactement la quantité
                # réellement demandée en unité de base.
                pack_detail = ""
                if selected_tier is not None and tier_pack_count is not None:
                    packaging_lbl = selected_tier.packaging or "paquet"
                    pack_detail = (
                        f" _({tier_pack_count} {packaging_lbl} de "
                        f"{selected_tier.quantity:g} {selected_tier.unit})_"
                    )
                msg = (
                    f"📉 Stock insuffisant pour « *{display_name}* » : "
                    f"seulement *{_fmt_num(available)} {unit_lbl}* disponible(s) sur "
                    f"*{_fmt_num(stock_check_qty)} {unit_lbl}* demandé(s){pack_detail}."
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
            wm.update(
                {
                    "buyer_request_waiting_choice": True,
                    "buyer_request_catalog_checked": True,
                    "buyer_request_last_product": display_name,
                }
            )
            payload_seed = {"product": display_name}
            # Même correction : l'appel d'offres proposé en repli doit porter la
            # quantité RÉELLE en unité de base (`unit` ci-dessous), pas un
            # nombre de paquets qui y serait relu comme des litres/kilos.
            if stock_check_qty:
                payload_seed["quantity"] = stock_check_qty
            if ref.get("unit"):
                payload_seed["unit"] = ref.get("unit")

            return {
                "status": "WAITING_INPUT",
                **set_pending_interaction(InteractionKind.CONFIRM_ACTION, context_ref="confirmation"),
                "response_strategy": "ASK_MISSING_FIELD",
                "final_response": msg
                + ("\n\n💡 Des alternatives sont disponibles." if recos else "")
                + escalation,
                "fallback_recommendations": recos,
                "preorder_workflow": {"phase": "CART"},
                "vendor_selection_context": None,
                "current_goal": "BUYER_REQUEST",
                "goal_status": "ACTIVE",
                "working_memory": wm,
                "transaction_payload": payload_seed,
            }

        if selected_tier is not None and computed_line is not None:
            # Palier choisi : `quantity` reste le NOMBRE DE PAQUETS (affichage
            # acheteur), `base_unit_quantity` porte la valeur réelle à débiter
            # du stock (voir domain/pricing_tiers.py::resolve_stock_debit).
            price = selected_tier.price
            line: Dict[str, Any] = {
                "product_id": ref.get("product_id") or ref.get("id"),
                "name": ref.get("name") or product_name,
                "quantity": tier_pack_count,
                "unit": selected_tier.unit,
                "packaging": selected_tier.packaging,
                "tier_id": selected_tier.tier_id,
                # Contenu d'UN paquet — nécessaire au rendu ("3 × bidon de
                # 10 L"), qui sinon ne peut pas distinguer le nombre de paquets
                # de la quantité en unité de base.
                "tier_quantity": selected_tier.quantity,
                "base_unit_quantity": computed_line.base_unit_quantity,
                "price": price,
                "line_total": round(computed_line.price_total, 2),
                "producer_id": ref.get("producer_id") or check.get("producer_id"),
                "vendor_name": ref.get("vendor_name"),
                "source_type": source_type,
                "is_auction": ref.get("is_auction", False),
                "status": "VALIDATED",
                "notification_id": None,
                "notification_status": None,
            }
        else:
            price = float(check.get("unit_price") or ref.get("price") or 0.0)
            line = {
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

        # La notification producteur doit décrire la demande dans l'unité de
        # BASE du produit (celle de son stock) — `qty` est un nombre de paquets
        # dès qu'un palier est choisi, et l'associer à `line["unit"]` (l'unité
        # du CONTENU du palier) produisait "60 L" pour 60 bidons de 10 L.
        notification_payload = {
            "producer_id": str(line.get("producer_id") or ""),
            "buyer_phone": phone,
            "product_id": str(line.get("product_id") or ""),
            "product_name": str(line.get("name") or ""),
            "quantity": stock_check_qty,
            "unit": ref_unit if selected_tier is not None else line.get("unit"),
            "source_type": source_type,
        }

        notification_result = await AgentActionGateway(self.mc_runtime).create_action(
            agent_name="MarketCoach",
            action_type="notify_interested_buyer",
            payload=notification_payload,
        )
        if is_success_response(notification_result):
            line["notification_id"] = notification_result.get(
                "action_id"
            ) or notification_result.get("id")
            line["notification_status"] = "PENDING_RESPONSE"

        cart = [
            c
            for c in cart
            if c.get("status") != "DRAFT"
            and c.get("product_id") not in ("DRAFT", line["product_id"])
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
            "tier_selection_context": None,
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
    """Filet local — l'unwrap canonique vit dans utils.unwrap_tool_envelope
    et s'applique déjà au chokepoint MarketRuntime.call_db (idempotent)."""
    if not isinstance(result, dict):
        return {"status": "error", "message": "Réponse outil invalide."}
    return unwrap_tool_envelope(result)


def _infer_source_type(product_record: Dict[str, Any]) -> str:
    src = str(
        product_record.get("source_type") or product_record.get("type") or ""
    ).upper()
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
