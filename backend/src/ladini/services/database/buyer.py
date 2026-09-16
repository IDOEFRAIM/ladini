import logging
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import case, desc, func, literal, or_, select, update
from sqlalchemy.orm import joinedload, selectinload

from ladini.core.formatting import fmt_num as _fmt_num

# Import des modèles alignés sur le schéma
from ladini.domain.models import (
    Auction,
    Bid,
    BuyerProfile,
    BuyerType,
    Delivery,
    Farm,
    MarketOffer,
    Order,
    OrderItem,
    Producer,
    Product,
    SubCategory,
    TrustScore,
    User,
    Zone,
)
from ladini.domain.order_policy import (
    validate_minimum_order_quantity,
)
from ladini.domain.pricing_tiers import (
    PricingTierError,
    compute_line,
    resolve_stock_debit,
    resolve_tier,
)

from .base import BaseMixin
from .common import normalize_phone
from .errors import BusinessRuleException
from .search import fuzzy_match, similarity_rank

logger = logging.getLogger("ladini.services.database.buyer")


def _naive_utc(dt_value: Optional[datetime]) -> Optional[datetime]:
    """Datetime naïf UTC pour écriture DB (colonnes sans timezone)."""
    if not isinstance(dt_value, datetime):
        return None
    if dt_value.tzinfo is not None:
        return dt_value.astimezone(timezone.utc).replace(tzinfo=None)
    return dt_value


_LIVESTOCK_KEYWORDS: tuple[str, ...] = (
    "chevre",
    "chevres",
    "chevrettes",
    "mouton",
    "moutons",
    "ovin",
    "ovins",
    "caprin",
    "caprins",
    "bovin",
    "bovins",
    "vache",
    "vaches",
    "boeuf",
    "boeufs",
    "taureau",
    "taureaux",
    "veau",
    "veaux",
    "poulet",
    "poulets",
    "poule",
    "poules",
    "volaille",
    "volailles",
    "canard",
    "canards",
    "dinde",
    "dindes",
    "dindon",
    "dindons",
    "pintade",
    "pintades",
    "porc",
    "porcs",
    "cochon",
    "cochons",
    "porcelet",
    "porcelets",
    "lapin",
    "lapins",
    "cobaye",
    "cobayes",
    "ane",
    "anes",
    "cheval",
    "chevaux",
    "poussins",
    "poussin",
)


def _normalize_ascii_lower(text: Optional[str]) -> str:
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return stripped.lower()


def _guess_display_unit(product_name: Optional[str], db_unit: Optional[str]) -> str:
    unit = (db_unit or "").strip().upper()
    if unit in {"KILOGRAMME", "KILOGRAMMES", "KGS"}:
        unit = "KG"
    if unit and unit not in {"", "KG"}:
        return unit
    normalized_name = _normalize_ascii_lower(product_name)
    if normalized_name and any(
        keyword in normalized_name for keyword in _LIVESTOCK_KEYWORDS
    ):
        return "TETE"
    return unit or "KG"


class BuyerMixin(BaseMixin):
    """
    BuyerMixin - Couche Métier Acheteur.
    Strictement DRY : s'appuie exclusivement sur les résolutions de profils de BaseMixin.
    """

    # ─── SECTION 1 : CONTEXTES ET COMPORTEMENTS LLM ───────────────────────

    async def get_buyer_context(self, phone: str) -> Dict[str, Any]:
        """Génère le contexte d'activité complet à destination des agents LangGraph."""
        try:
            # Consommation directe de la méthode héritée de BaseMixin
            user_obj, profile_obj = await self.get_buyer_profile(phone=phone)
        except Exception:
            return {
                "role": "new_user",
                "message": "Profil acheteur introuvable.",
                "user_info": None,
            }

        # Résolution du nom de la zone si elle est présente sur l'user
        zone_name = "Inconnue"
        if user_obj.zone_id:
            zone_obj = await self.session.scalar(
                select(Zone.name).where(Zone.id == user_obj.zone_id)
            )
            if zone_obj:
                zone_name = zone_obj

        active_orders = await self.get_active_orders_context_by_phone(phone=phone)

        return {
            "role": "existing_buyer",
            "user_info": {
                "id": str(profile_obj.id),
                "name": getattr(profile_obj, "establishment_name", None)
                or user_obj.name
                or "Acheteur",
                "zone": zone_name,
                "zone_id": str(user_obj.zone_id) if user_obj.zone_id else None,
            },
            "recent_history": active_orders[:3],
            "can_order": user_obj.zone_id is not None,
        }

    # ─── SECTION 2 : GESTION DES FLUX (COMMANDES & LIVRAISONS) ──────────────

    async def get_active_orders_context_by_phone(
        self, phone: str
    ) -> List[Dict[str, Any]]:
        """Récupère le statut des commandes en cours d'un acheteur."""
        current_session = self.session
        if not current_session:
            return []
        try:
            clean_phone = normalize_phone(phone)
            ACTIVE_STATUSES = ["PENDING", "CONFIRMED", "PAID", "SHIPPED", "PICKED_UP"]

            stmt = (
                select(Order)
                .join(BuyerProfile, Order.buyer_id == BuyerProfile.id)
                .join(User, BuyerProfile.user_id == User.id)
                .options(
                    selectinload(Order.items).joinedload(OrderItem.product),
                    selectinload(Order.delivery).joinedload(Delivery.agent),
                )
                .where(User.phone == clean_phone, Order.status.in_(ACTIVE_STATUSES))
                .order_by(desc(Order.created_at))
            )

            result = await current_session.execute(stmt)
            orders = result.unique().scalars().all()

            return [
                {
                    "id": str(o.id),
                    "status": o.status,
                    "total": o.total_amount,
                    "items": [
                        {"name": i.product.name, "qty": i.quantity} for i in o.items
                    ]
                    if o.items
                    else [],
                    "date": o.created_at.isoformat() if o.created_at else None,
                }
                for o in orders
            ]
        except Exception as e:
            logger.error(f"Erreur context commandes pour {phone}: {e}")
            return []

    # ─── SECTION 3 : RECHERCHE GÉOLOCALISÉE ───────────────────────────

    async def search_products(
        self, product: str, phone: str, limit: int = 15
    ) -> Dict[str, Any]:
        """Recherche unifiée Buyer : catalogue immédiat + productions futures.

        Retourne un schéma homogène pour permettre au flow buyer de distinguer
        clairement les produits disponibles (DIRECT) et les précommandes futures
        (FUTURE / MarketOffer) sans heuristique textuelle.
        """
        current_session = self.session
        if not current_session:
            return {
                "status": "error",
                "message": "Session de base de données indisponible.",
            }

        try:
            clean_product = str(product).strip()
            if not clean_product:
                return {
                    "status": "error",
                    "message": "Le nom du produit à rechercher est vide.",
                }

            # Résolution du profil de l'acheteur
            user_obj, _ = await self.get_buyer_profile(phone=phone)
            target_uuid = user_obj.zone_id if user_obj else None
            parent_id = None

            if target_uuid:
                parent_id = await current_session.scalar(
                    select(Zone.parent_id).where(Zone.id == target_uuid)
                )

            # Construction dynamique des scores de proximité
            conditions = []
            if target_uuid:
                conditions.append((Producer.zone_id == target_uuid, 1))
            if parent_id:
                conditions.append((Zone.parent_id == parent_id, 2))

            priority_score = (
                case(*conditions, else_=3).label("priority")
                if conditions
                else literal(3).label("priority")
            )

            catalog_stmt = (
                select(
                    Product.id,
                    Product.name,
                    Product.price,
                    Product.quantity_for_sale,
                    Product.unit,
                    Product.images,
                    # (2026-08-30) sans cette colonne, un produit à paliers
                    # multiples (`pricing_tiers`) devenait invisible à
                    # l'acheteur — même bug déjà corrigé côté producteur sur
                    # `get_stocks` (colonne persistée mais jamais SELECTionnée
                    # ici). Voir domain/pricing_tiers.py.
                    Product.pricing_tiers,
                    Producer.id.label("producer_id"),
                    User.name.label("producer_name"),
                    Zone.name.label("zone_name"),
                    priority_score,
                    # (2026-09-02) Seuil minimum de commande — politique
                    # PLATEFORME portée par le TYPE de produit
                    # (`SubCategory`), jamais par `Product`. Voir
                    # domain/order_policy.py. `outerjoin` : un produit sans
                    # `sub_category_id` (legacy) doit rester achetable —
                    # NULL ici = aucune règle, jamais un blocage silencieux.
                    SubCategory.minimum_order_quantity,
                    SubCategory.minimum_order_unit,
                )
                .outerjoin(Producer, Producer.id == Product.producer_id)
                .outerjoin(User, User.id == Producer.user_id)
                .outerjoin(Zone, Zone.id == Producer.zone_id)
                .outerjoin(SubCategory, SubCategory.id == Product.sub_category_id)
                .where(
                    # Recherche floue trigram (« tomte » → « tomate ») au lieu du
                    # substring strict qui bloquait l'agent sur une faute de frappe.
                    fuzzy_match(Product.name, clean_product),
                    Product.quantity_for_sale > 0,
                    # (2026-09-04, Product Completeness Phase 2) : `is_available`
                    # est DÉJÀ le drapeau « ne fait pas partie du catalogue
                    # public » — posé par `product.py::delete_product`
                    # (archivage d'un produit retiré, historique préservé) ET
                    # par `marketplace.py::record_sale` (produit fantôme créé
                    # pour ancrer une vente directe). Jusqu'ici la recherche
                    # acheteur ne l'interrogeait pas : ces lignes n'étaient
                    # exclues QUE par `quantity_for_sale > 0`, donc une simple
                    # mise à jour de quantité (`SALES_UPDATE_PRODUCT`, qui ne
                    # regarde pas ce drapeau) ressuscitait silencieusement un
                    # produit retiré. Colonne `nullable=False, default=True` —
                    # aucun risque d'exclure des lignes legacy.
                    Product.is_available.is_(True),
                )
                .order_by("priority", Product.price.asc())
                .limit(limit)
            )

            future_priority_score = (
                case(*conditions, else_=3).label("priority")
                if conditions
                else literal(3).label("priority")
            )
            future_stmt = (
                select(
                    MarketOffer.id,
                    MarketOffer.product_label,
                    MarketOffer.species,
                    MarketOffer.production_type,
                    MarketOffer.price_per_unit,
                    MarketOffer.available_quantity,
                    MarketOffer.estimated_available_at,
                    Producer.id.label("producer_id"),
                    User.name.label("producer_name"),
                    Zone.name.label("zone_name"),
                    future_priority_score,
                    # (2026-09-02) Même politique plateforme que le catalogue
                    # direct — voir domain/order_policy.py. `MarketOffer` a
                    # son propre `sub_category_id`.
                    SubCategory.minimum_order_quantity,
                    SubCategory.minimum_order_unit,
                )
                .outerjoin(Farm, Farm.id == MarketOffer.farm_id)
                .outerjoin(Producer, Producer.id == Farm.producer_id)
                .outerjoin(User, User.id == Producer.user_id)
                .outerjoin(Zone, Zone.id == Producer.zone_id)
                .outerjoin(SubCategory, SubCategory.id == MarketOffer.sub_category_id)
                .where(
                    or_(
                        fuzzy_match(MarketOffer.product_label, clean_product),
                        fuzzy_match(MarketOffer.species, clean_product),
                    ),
                    MarketOffer.is_public.is_(True),
                    MarketOffer.preorder_enabled.is_(True),
                    MarketOffer.available_quantity > 0,
                )
                .order_by("priority", MarketOffer.price_per_unit.asc())
                .limit(limit)
            )

            catalog_rows = (
                (await current_session.execute(catalog_stmt)).mappings().all()
            )
            future_rows = (await current_session.execute(future_stmt)).mappings().all()

            if not catalog_rows and not future_rows:
                return {
                    "status": "success",
                    "message": f"Désolé, aucun produit correspondant à '{clean_product}' n'est disponible.",
                }

            combined_results: List[Dict[str, Any]] = []

            for row in catalog_rows:
                is_local = (
                    target_uuid is not None
                    and row.get("priority")
                    and int(row["priority"]) <= 2
                )
                tag = "📍 Local" if is_local else "🌐 National"

                unit_label = _guess_display_unit(row["name"], row["unit"])

                combined_results.append(
                    {
                        "id": str(row["id"]),
                        "name": f"{row['name']} ({tag})",
                        "price": float(row["price"]),
                        "priority": int(row.get("priority") or 3),
                        "unit": unit_label,
                        "vendor": row["producer_name"] or "Producteur Anonyme",
                        "vendor_name": row["producer_name"] or "Producteur Anonyme",
                        "producer_name": row["producer_name"] or "Producteur Anonyme",
                        "producer_id": str(row.get("producer_id") or ""),
                        "zone_name": row.get("zone_name"),
                        "is_local": is_local,
                        "source_type": "DIRECT",
                        "availability_kind": "CATALOG",
                        "available_quantity": float(
                            row.get("quantity_for_sale") or 0.0
                        ),
                        # Voir services/search_results_cache.py — permet à l'acheteur
                        # de demander "photos <numéro>" pour un résultat de recherche.
                        "images": list(row.get("images") or []),
                        "pricing_tiers": row.get("pricing_tiers"),
                        "minimum_order_quantity": (
                            float(row["minimum_order_quantity"])
                            if row.get("minimum_order_quantity") is not None
                            else None
                        ),
                        "minimum_order_unit": row.get("minimum_order_unit"),
                    }
                )

            for row in future_rows:
                estimated = row.get("estimated_available_at")
                estimated_iso = estimated.isoformat() if estimated else None
                crop_name = (
                    row.get("product_label") or row.get("species") or clean_product
                )
                is_local = (
                    target_uuid is not None
                    and row.get("priority")
                    and int(row["priority"]) <= 2
                )
                tag = "📍 Local" if is_local else "🌐 National"
                display_name = f"{crop_name} ({tag} • ⏳ Future)"

                combined_results.append(
                    {
                        "id": str(row["id"]),
                        "name": display_name,
                        "price": float(row.get("price_per_unit") or 0.0),
                        "priority": int(row.get("priority") or 3),
                        "unit": _guess_display_unit(
                            crop_name,
                            row.get("production_type") == "LIVESTOCK"
                            and "TETE"
                            or "KG",
                        ),
                        "vendor": row.get("producer_name") or "Producteur Anonyme",
                        "vendor_name": row.get("producer_name") or "Producteur Anonyme",
                        "producer_name": row.get("producer_name")
                        or "Producteur Anonyme",
                        "producer_id": str(row.get("producer_id") or ""),
                        "zone_name": row.get("zone_name"),
                        "is_local": is_local,
                        "source_type": "FUTURE",
                        "availability_kind": "FUTURE",
                        "estimated_available_at": estimated_iso,
                        "available_quantity": float(
                            row.get("available_quantity") or 0.0
                        ),
                        "crop_cycle_id": str(row["id"]),
                        "images": [],  # productions futures : pas de photo avant récolte
                        "minimum_order_quantity": (
                            float(row["minimum_order_quantity"])
                            if row.get("minimum_order_quantity") is not None
                            else None
                        ),
                        "minimum_order_unit": row.get("minimum_order_unit"),
                    }
                )

            combined_results.sort(
                key=lambda r: (
                    int(r.get("priority") or 3),
                    0 if r.get("source_type") == "DIRECT" else 1,
                    float(r.get("price") or 0.0),
                )
            )
            formatted_results = []
            for idx, item in enumerate(combined_results[: max(1, int(limit))], 1):
                item["display_index"] = idx
                item.pop("priority", None)
                formatted_results.append(item)

            return {
                "status": "success",
                "results": formatted_results,
                "search_info": "Inclut produits disponibles (catalogue) et productions futures (précommande).",
            }

        except Exception as e:
            logger.error(
                f"[Marketplace Search] Erreur critique: {str(e)}", exc_info=True
            )
            return {
                "status": "error",
                "message": "Erreur technique lors de la recherche.",
            }

    # ─── SECTION 4 : CRÉATION TRANSACTIONNELLE DE COMMANDES ──────────────────

    async def finalize_multi_order(
        self, items: List[Dict[str, Any]], phone: str
    ) -> Dict[str, Any]:
        """Crée une commande ferme multi-produits avec Row-level locking strict."""
        current_session = self.session
        if not current_session:
            raise BusinessRuleException("Session indisponible.")

        # Appel direct à BaseMixin, plus de duplication locale
        user_obj, profile_obj = await self.get_buyer_profile(phone=phone)

        if not user_obj.zone_id:
            raise BusinessRuleException("Veuillez configurer votre zone de livraison.")

        # Validation stricte AVANT le tri anti-deadlock : un product_id absent,
        # non-UUID ou mal formé rendrait l'ordre lexicographique indéterminé et
        # annulerait la garantie d'acquisition de verrous dans un ordre stable
        # entre transactions concurrentes.
        for item in items:
            raw_id = item.get("product_id")
            if not raw_id or not isinstance(raw_id, (str, uuid.UUID)):
                raise BusinessRuleException(
                    "Identifiant produit manquant ou invalide dans le panier.",
                    reason="invalid_product_id",
                )
            try:
                uuid.UUID(str(raw_id))
            except (TypeError, ValueError):
                raise BusinessRuleException(
                    f"Identifiant produit invalide : {raw_id!r}.",
                    reason="invalid_product_id",
                ) from None

        # Tri déterministe par product_id avant tout FOR UPDATE : deux requêtes
        # concurrentes achetant les mêmes produits dans un ordre différent
        # forment un cycle de verrous → deadlock. Le tri garantit un ordre
        # d'acquisition identique pour toutes les transactions simultanées.
        sorted_items = sorted(items, key=lambda x: str(x.get("product_id")))

        new_order = Order(
            id=uuid.uuid4(),
            buyer_id=profile_obj.id,
            zone_id=user_obj.zone_id,
            total_amount=0.0,
            status="PENDING",
            payment_status="PENDING",
            delivery_status="PENDING",
            source="WHATSAPP",
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        current_session.add(new_order)
        await current_session.flush()

        running_total = 0.0
        summary_items = []

        for item in sorted_items:
            p_id = item.get("product_id")
            qty = float(item.get("quantity", 0))
            if qty <= 0:
                continue

            p_uuid = uuid.UUID(p_id) if isinstance(p_id, str) else p_id

            # Verrou exclusif d'écriture (Race-Condition Proof)
            product = await current_session.scalar(
                select(Product).where(Product.id == p_uuid).with_for_update()
            )

            if not product:
                raise BusinessRuleException(
                    "Un produit sélectionné n'est plus disponible."
                )

            # (2026-08-30) Article à palier — re-résout le tarif contre les
            # `pricing_tiers` LIVE du produit (pas la valeur mise en cache
            # dans la ligne de panier au moment de l'ajout) : même philosophie
            # anti-péremption que le repli `product.price` déjà en place pour
            # les articles à tarif unique. `qty` reste le nombre de paquets
            # (affichage), `base_unit_quantity` la quantité réelle à débiter.
            # Voir domain/pricing_tiers.py.
            tier_id = item.get("tier_id")
            base_unit_quantity = None
            if tier_id:
                try:
                    tier = resolve_tier(product.pricing_tiers, str(tier_id))
                    computed = compute_line(tier, int(qty))
                except PricingTierError as exc:
                    raise BusinessRuleException(str(exc)) from exc
                price_at_sale = tier.price
                line_total = computed.price_total
                base_unit_quantity = computed.base_unit_quantity
                stock_debit = base_unit_quantity
            else:
                price_at_sale = float(product.price)
                line_total = price_at_sale * qty
                stock_debit = qty

            # (2026-09-02) Seuil minimum de commande — RE-VALIDÉ ici contre la
            # règle LIVE (jamais la valeur vue au moment de l'ajout au
            # panier), même philosophie anti-péremption que `pricing_tiers`
            # ci-dessus : un admin a pu relever le seuil ("50 KG -> 100 KG")
            # entre l'ajout au panier et cette confirmation de commande — la
            # commande finale doit respecter la règle EN VIGUEUR maintenant,
            # jamais celle capturée plus tôt côté état conversationnel. Voir
            # domain/order_policy.py (source unique, même fonction que
            # `cart_service.py::add_to_cart_with_ref`).
            min_check = None
            if product.sub_category_id:
                sub_row = (
                    await current_session.execute(
                        select(
                            SubCategory.minimum_order_quantity,
                            SubCategory.minimum_order_unit,
                        ).where(SubCategory.id == product.sub_category_id)
                    )
                ).first()
                if sub_row is not None:
                    min_check = validate_minimum_order_quantity(
                        minimum_order_quantity=(
                            float(sub_row[0]) if sub_row[0] is not None else None
                        ),
                        minimum_order_unit=sub_row[1],
                        total_quantity=stock_debit,
                        total_unit=str(product.unit or "KG"),
                    )
            if min_check is not None and not min_check.passed:
                if min_check.reason == "UNIT_INCOMPATIBLE":
                    raise BusinessRuleException(
                        f"Seuil minimum de commande pour {product.name} défini en "
                        f"{min_check.minimum_unit}, incompatible avec {product.unit}.",
                        reason="minimum_order_unit_incompatible",
                    )
                raise BusinessRuleException(
                    f"La quantité minimale pour {product.name} est de "
                    f"{min_check.minimum_in_total_unit:g} {product.unit} "
                    f"(commande actuelle : {stock_debit:g} {product.unit}).",
                    reason="below_minimum_order_quantity",
                )

            if product.quantity_for_sale < stock_debit:
                raise BusinessRuleException(
                    f"Stock insuffisant pour {product.name} (Dispo: {product.quantity_for_sale}).",
                    reason="insufficient_stock",
                )

            running_total += line_total

            current_session.add(
                OrderItem(
                    id=uuid.uuid4(),
                    order_id=new_order.id,
                    product_id=product.id,
                    quantity=qty,
                    price_at_sale=price_at_sale,
                    tier_id=str(tier_id) if tier_id else None,
                    base_unit_quantity=base_unit_quantity,
                )
            )

            # `stock_debit` est un `float` (`qty`/`base_unit_quantity`
            # ci-dessus) ; `quantity_for_sale` est une colonne `Numeric`
            # (chargée en `decimal.Decimal`) — `Decimal -= float` lève
            # `TypeError` (même classe de bug que le recrédit au moment de
            # l'annulation, `cancel_confirmed_order`/`cancel_pending_order`,
            # incident réel 2026-09-15). Conversion explicite avant
            # l'arithmétique, jamais d'opérateur augmenté sur la colonne
            # Decimal directement.
            product.quantity_for_sale = (
                float(product.quantity_for_sale or 0.0) - stock_debit
            )
            summary_items.append(f"{product.name} (x{qty} {product.unit or 'u'})")

        if not summary_items:
            raise BusinessRuleException("Le panier ne contient aucun article valide.")

        new_order.total_amount = running_total
        await current_session.flush()

        return {
            "status": "success",
            "order_id": str(new_order.id),
            "order_number": str(new_order.id)[:8].upper(),
            "total_amount": running_total,
            "summary": ", ".join(summary_items),
            "message": f"✅ Commande enregistrée ! Total: {running_total} FCFA.",
        }

    # ─── SECTION 5 : FEEDBACK & RÉPUTATION ───────────────────────────────────

    async def rate_delivery(
        self, order_id: str, rating: int, comment: str
    ) -> Dict[str, Any]:
        """Évalue une livraison et ajuste le TrustScore de l'agent."""
        current_session = self.session
        if not current_session:
            raise BusinessRuleException("Session indisponible.")

        o_uuid = uuid.UUID(order_id) if isinstance(order_id, str) else order_id

        delivery = await current_session.scalar(
            select(Delivery)
            .where(Delivery.order_id == o_uuid)
            .options(joinedload(Delivery.agent))
        )

        if not delivery or not delivery.delivery_agent_id or not delivery.agent:
            raise BusinessRuleException(
                "Aucun agent de livraison associé à cette commande."
            )

        score_impact = 0.5 if rating >= 4 else (-0.5 if rating <= 2 else 0.0)

        if score_impact != 0.0:
            trust_score = await current_session.scalar(
                select(TrustScore).where(TrustScore.user_id == delivery.agent.user_id)
            )

            if trust_score:
                trust_score.reliability_index = (
                    float(trust_score.reliability_index or 0.0) + score_impact
                )
                trust_score.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            else:
                current_session.add(
                    TrustScore(
                        id=uuid.uuid4(),
                        user_id=delivery.agent.user_id,
                        reliability_index=5.0 + score_impact,
                        updated_at=datetime.now(timezone.utc).replace(tzinfo=None),
                    )
                )

            await current_session.flush()

        return {
            "status": "success",
            "message": "Merci pour votre retour ! Pris en compte.",
        }

    async def list_buyer_types(self) -> Dict[str, Any]:
        """Liste les typologies/segments d'acheteurs de la plateforme."""
        current_session = self.session
        if not current_session:
            return {"status": "error", "message": "Session indisponible."}
        result = await current_session.execute(
            select(BuyerType).order_by(BuyerType.name)
        )
        types = [{"id": str(bt.id), "name": bt.name} for bt in result.scalars().all()]
        return {"status": "success", "data": types}

    async def set_buyer_type(self, buyer_id: str, type_id: str) -> Dict[str, Any]:
        """Assigne une catégorie de ciblage commercial au profil de l'acheteur."""
        current_session = self.session
        if not current_session:
            return {"status": "error", "message": "Session indisponible."}
        b_uuid = uuid.UUID(buyer_id) if isinstance(buyer_id, str) else buyer_id
        t_uuid = uuid.UUID(type_id) if isinstance(type_id, str) else type_id

        await current_session.execute(
            update(BuyerProfile)
            .where(BuyerProfile.id == b_uuid)
            .values(buyer_type_id=t_uuid)
        )
        await current_session.flush()
        return {
            "status": "success",
            "data": {"buyer_id": str(b_uuid), "type_id": str(t_uuid)},
        }

    async def get_buyer_orders_dashboard(self, phone: str) -> Dict[str, Any]:
        """Génère un tableau de bord WhatsApp des commandes en cours pour cet acheteur."""
        current_session = self.session
        if not current_session:
            return {"status": "error", "message": "Session indisponible."}
        try:
            # Phone-First : Résolution du profil via BaseMixin
            _, profile_obj = await self.get_buyer_profile(phone=phone)

            # Statuts traduits avec des emojis explicites pour l'utilisateur WhatsApp
            status_map = {
                "PENDING": "⏳ En attente de validation",
                # (2026-09-13, confirmation explicite producteur) : la
                # commande a débité le stock mais attend encore que le
                # producteur confirme pouvoir l'honorer — distinct de
                # CONFIRMED, qui signifie désormais cet engagement pris.
                "PENDING_PRODUCER_CONFIRMATION": "⏳ En attente de confirmation du producteur",
                "CONFIRMED": "✅ Confirmée par le producteur (Préparation)",
                "PAID": "💰 Payée (En attente d'expédition)",
                "SHIPPED": "🚛 En cours de route",
                "PICKED_UP": "📍 Arrivée au point de collecte",
                "DELIVERED": "📦 Livrée avec succès",
                # (2026-09-04, clôture F1 — paiement à la livraison) : gap
                # réel comblé ici — `Order.status="COMPLETED"` (déjà utilisé
                # par `record_sale`, désormais aussi par
                # `confirm_delivery_and_payment`) n'avait AUCUNE entrée ici,
                # contrairement à `order_tracking.py::ORDER_STATUS_MAP` qui
                # l'a déjà (incohérence entre les deux surfaces de lecture,
                # mandat §18) — retombait sur le repli générique
                # `f"🔄 Status: {order.status}"`.
                "COMPLETED": "✅ Livrée et payée à la livraison",
                "CANCELLED": "❌ Annulée",
            }

            stmt = (
                select(Order)
                .options(selectinload(Order.items).joinedload(OrderItem.product))
                .where(
                    Order.buyer_id == profile_obj.id,
                    # (2026-09-04, audit Order(DRAFT) orphelin) : un
                    # `Order(status="DRAFT")` (ou "SUPERSEDED", même famille)
                    # n'est PAS encore une commande — c'est un brouillon de
                    # checkout, éventuellement abandonné. Sans cette
                    # exclusion, `status_map` (ci-dessus, aucune entrée pour
                    # DRAFT/SUPERSEDED) retombait sur le fallback générique
                    # (`f"🔄 Status: {order.status}"`) et affichait un
                    # brouillon annulé/remplacé comme une VRAIE commande dans
                    # le tableau de bord de l'acheteur.
                    Order.status.notin_(["DRAFT", "SUPERSEDED"]),
                )
                .order_by(desc(Order.created_at))
            )
            result = await current_session.execute(stmt)
            orders = result.unique().scalars().all()

            if not orders:
                return {
                    "status": "success",
                    "formatted_menu": "📦 *Vos Commandes :*\n\nVous n'avez pas encore passé de commande sur Ladini. Tapez *Marketplace* pour voir les produits disponibles !",
                }

            menu_lines = ["📦 *SUIVI DE VOS COMMANDES :*"]
            mapping_cache = {}

            for idx, order in enumerate(orders, start=1):
                items_summary = ", ".join(
                    [
                        f"{item.product.name} (x{_fmt_num(item.quantity)})"
                        for item in order.items
                    ]
                )
                display_status = status_map.get(
                    order.status.upper(), f"🔄 Status: {order.status}"
                )
                date_str = order.created_at.strftime("%d/%m/%Y")

                line = (
                    f"\n*{idx}. Commande #{str(order.id)[:8].upper()}* ({date_str})\n"
                    f"🛒 Articles : {items_summary}\n"
                    f"💵 Total : *{_fmt_num(order.total_amount)} CFA*\n"
                    f"📊 État : {display_status}\n"
                )
                menu_lines.append(line)
                mapping_cache[str(idx)] = str(order.id)

            # (2026-09-11) Pas de footer "répondez avec le numéro..." ICI —
            # `order_tracking.py::list_orders` (le SEUL appelant WhatsApp de
            # cette méthode) ajoute déjà `render_selection_prompt(noun=
            # "commande")`, un footer harmonisé partagé par tous les menus
            # acheteur. En avoir un second ici produisait DEUX instructions
            # de fin quasi identiques mais formulées différemment dans le
            # même message ("répondez avec le numéro correspondant" ET
            # "Répondez avec le numéro de votre commande, ou annuler pour
            # quitter") — signalé comme confus par un utilisateur réel.

            return {
                "status": "success",
                "formatted_menu": "\n".join(menu_lines),
                "mapping": mapping_cache,
            }
        except Exception as e:
            logger.error(f"Erreur get_buyer_orders_dashboard pour {phone}: {e}")
            return {
                "status": "error",
                "message": "Impossible de charger votre suivi de commande.",
            }

    async def cancel_pending_order(
        self, order_id: str, phone: str, reason: Optional[str] = None
    ) -> Dict[str, Any]:
        """Annule une commande et recrédite les stocks des produits associés.

        (2026-09-04, audit produit post-F1-F4 — "BUYER_CANCEL_ORDER, un
        goal réellement câblé, jamais atteignable") : GAP RÉEL fermé ici —
        malgré son nom, cette fonction ne guardait QUE `status=="PENDING"`.
        Recherche exhaustive : AUCUN chemin conversationnel vivant ne crée
        jamais un `Order` à ce statut (`"PENDING"` n'est que le défaut de
        colonne, systématiquement écrasé — `create_preorder_draft` pose
        `"DRAFT"`, `select_winning_bid` pose `"CONFIRMED"` directement).
        `BUYER_CANCEL_ORDER` était donc un goal déclaré, routé, avec un
        handler réel — mais qui échouait TOUJOURS pour toute commande
        qu'un acheteur voudrait réellement annuler. Le garde couvre
        désormais aussi `"CONFIRMED"` (le statut RÉEL d'une commande pas
        encore clôturée) — jamais `"DRAFT"` (chemin dédié,
        `cancel_preorder_draft`), jamais `"COMPLETED"`/`"CANCELLED"`
        (verrous FOR UPDATE déjà en place, sérialise proprement contre
        `confirm_delivery_and_payment`, F1, qui exige exactement le MÊME
        statut `"CONFIRMED"` — le premier à committer gagne, l'autre
        retombe sur son propre garde, jamais un état incohérent).
        """
        current_session = self.session
        if not current_session:
            raise BusinessRuleException("Session indisponible.")

        user_obj, profile_obj = await self.get_buyer_profile(phone=phone)
        o_uuid = uuid.UUID(order_id) if isinstance(order_id, str) else order_id

        # Récupération sécurisée avec verrouillage exclusif
        stmt = (
            select(Order)
            .options(selectinload(Order.items).joinedload(OrderItem.product))
            .where(Order.id == o_uuid, Order.buyer_id == profile_obj.id)
            .with_for_update()
        )
        order = await current_session.scalar(stmt)

        if not order:
            raise BusinessRuleException("Commande introuvable ou non autorisée.")
        order_status_up = order.status.upper()
        if order_status_up not in ("PENDING", "CONFIRMED", "PENDING_PRODUCER_CONFIRMATION"):
            raise BusinessRuleException(
                f"Impossible d'annuler une commande déjà en statut : {order.status}.",
                reason="not_cancellable",
            )
        # (2026-09-13, confirmation explicite producteur) : le producteur est
        # déjà informé de la commande dès sa création (`PENDING_PRODUCER_
        # CONFIRMATION`, notification immédiate) — pas seulement une fois
        # `CONFIRMED` — il doit donc être prévenu de l'annulation acheteur
        # dans les deux cas, pour ne pas confirmer une commande déjà annulée.
        was_confirmed = order_status_up in ("CONFIRMED", "PENDING_PRODUCER_CONFIRMATION")

        # Restitution physique des stocks aux producteurs — vide (donc
        # sans effet, correctement) pour une commande RFQ : `select_winning_bid`
        # ne crée jamais d'`OrderItem` (voir l'audit Auction/Bid), aucun
        # stock catalogue n'est débité à la confirmation d'une telle commande.
        for item in order.items:
            if item.product:
                prod_stmt = (
                    select(Product)
                    .where(Product.id == item.product_id)
                    .with_for_update()
                )
                product = await current_session.scalar(prod_stmt)
                if product:
                    # `resolve_stock_debit` renvoie un `float` ; `quantity_for_sale`
                    # est une colonne `Numeric` (chargée en `decimal.Decimal`) —
                    # `Decimal += float` lève `TypeError` (même incident réel
                    # que son miroir producteur, `services/database/
                    # producer.py::cancel_confirmed_order`, 2026-09-15).
                    # Même parade déjà établie plus haut dans ce fichier
                    # (`available = float(product.quantity_for_sale or 0.0)`
                    # avant l'arithmétique, ligne ~2204) : convertir
                    # explicitement avant d'opérer, jamais d'opérateur
                    # augmenté sur la colonne Decimal directement.
                    product.quantity_for_sale = float(
                        product.quantity_for_sale or 0.0
                    ) + resolve_stock_debit(item)

        order.status = "CANCELLED"
        order.cancellation_role = "BUYER"
        normalized_reason = None
        if reason:
            candidate = reason.strip()
            if candidate:
                normalized_reason = candidate
                existing_desc = order.delivery_desc or ""
                reason_log = f"[CancelReason] {normalized_reason}"
                order.delivery_desc = (
                    f"{existing_desc}\n{reason_log}" if existing_desc else reason_log
                )

        # Notification producteur(s) — UNIQUEMENT si la commande était déjà
        # `CONFIRMED` (un producteur ne "sait" qu'une commande PENDING
        # existe de toute façon — cas structurellement mort, voir
        # docstring). Même résolution dual-origine que
        # `ProducerMgmtMixin.confirm_delivery_and_payment` (F1) : PREORDER
        # via `OrderItem`→`Product.producer_id`, RFQ via
        # `Order.winning_bid_id`→`Bid.producer_id`. Outbox, même
        # transaction, jamais un envoi direct.
        if was_confirmed:
            producer_phones: set[str] = set()
            for item in order.items or []:
                if item.product and item.product.producer_id:
                    prod_row = (
                        await current_session.execute(
                            select(User.phone)
                            .join(Producer, Producer.user_id == User.id)
                            .where(Producer.id == item.product.producer_id)
                            .limit(1)
                        )
                    ).first()
                    if prod_row and prod_row[0]:
                        producer_phones.add(prod_row[0])
            if order.winning_bid_id and not producer_phones:
                bid_prod_row = (
                    await current_session.execute(
                        select(User.phone)
                        .join(Producer, Producer.user_id == User.id)
                        .join(Bid, Bid.producer_id == Producer.id)
                        .where(Bid.id == order.winning_bid_id)
                        .limit(1)
                    )
                ).first()
                if bid_prod_row and bid_prod_row[0]:
                    producer_phones.add(bid_prod_row[0])

            if producer_phones:
                from ladini.workers.outbox import templates as _outbox_templates
                from ladini.workers.repositories import outbox_repo as _outbox_repo

                order_number = str(order.id)[:8].upper()
                await _outbox_repo.enqueue(
                    current_session,
                    [
                        {
                            "channel": "WHATSAPP",
                            "recipient_phone": phone_,
                            "template_key": _outbox_templates.ORDER_CANCELLED_BY_BUYER_PRODUCER,
                            "payload": {"order_number": order_number},
                            "dedupe_key": f"ORDER_CANCELLED_PRODUCER:{order.id}:{phone_}",
                        }
                        for phone_ in producer_phones
                    ],
                )

        await current_session.flush()

        # Anti-abus : au-delà de MAX_CANCELLATIONS annulations acheteur, on
        # bloque le compte (déblocage manuel via service client).
        blocked_now = await self._enforce_cancellation_limit(
            current_session, profile_obj.id, user_obj
        )

        # Message précis : une commande issue d'un appel d'offres (RFQ/Auction)
        # n'a jamais débité de stock catalogue (aucun `OrderItem`) — ne pas
        # prétendre une restitution qui n'a pas eu lieu.
        stock_note = (
            " Les stocks ont été restitués aux agriculteurs."
            if order.items
            else ""
        )
        base_msg = (
            f"❌ La commande #{str(order.id)[:8].upper()} a été annulée.{stock_note}"
            + (f"\n📝 Raison : {normalized_reason}" if normalized_reason else "")
        )
        if blocked_now:
            base_msg += (
                "\n\n🔒 *Votre compte a été bloqué* suite à des annulations répétées. "
                "Contactez le *service client* pour le débloquer."
            )
        return {
            "status": "success",
            "account_blocked": bool(blocked_now),
            "message": base_msg,
        }

    async def _enforce_cancellation_limit(
        self, session, buyer_profile_id, user_obj
    ) -> bool:
        """Bloque le compte si les annulations acheteur dépassent le seuil.

        Retourne True uniquement si le blocage vient d'être appliqué (pour
        informer l'acheteur). Best-effort : n'interrompt jamais l'annulation.
        """
        try:
            from ladini.services.database.moderation import MAX_CANCELLATIONS

            count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(Order)
                    .where(
                        Order.buyer_id == buyer_profile_id,
                        func.upper(Order.status) == "CANCELLED",
                        func.upper(func.coalesce(Order.cancellation_role, ""))
                        == "BUYER",
                    )
                )
                or 0
            )
            if count <= MAX_CANCELLATIONS or user_obj is None:
                return False

            current = str(getattr(user_obj, "account_status", "") or "").upper()
            if current in {"BLOCKED", "BANNED"}:
                return False  # déjà restreint : ne pas re-notifier / ne pas rétrograder

            user_obj.account_status = "BLOCKED"
            user_obj.blocked_reason = "Annulations répétées de commandes."
            # Colonne TIMESTAMP WITHOUT TIME ZONE → datetime naïf obligatoire.
            user_obj.blocked_at = datetime.now(timezone.utc).replace(tzinfo=None)
            await session.flush()
            logger.warning(
                "Compte %s BLOQUÉ : %s annulations (> %s).",
                getattr(user_obj, "id", "?"),
                count,
                MAX_CANCELLATIONS,
            )
            return True
        except Exception:
            logger.warning("Enforcement du plafond d'annulations échoué", exc_info=True)
            return False

    async def estimate_delivery_cost(
        self, phone: str, product_ids: List[str]
    ) -> Dict[str, Any]:
        """Calcule une estimation transparente des frais de transport du panier vers la zone de l'acheteur."""
        current_session = self.session
        if not current_session:
            return {"status": "error", "message": "Session indisponible."}
        try:
            user_obj, _ = await self.get_buyer_profile(phone=phone)
            if not user_obj.zone_id:
                return {
                    "status": "error",
                    "message": "Veuillez configurer votre zone pour estimer la livraison.",
                }

            buyer_zone_id = user_obj.zone_id
            total_transport_estimate = 0.0
            number_of_pickup_points = 0

            for p_id in product_ids:
                p_uuid = uuid.UUID(p_id) if isinstance(p_id, str) else p_id

                # Récupération de la zone de production
                stmt = (
                    select(Producer.zone_id)
                    .join(Product, Product.producer_id == Producer.id)
                    .where(Product.id == p_uuid)
                )
                producer_zone_id = await current_session.scalar(stmt)

                if not producer_zone_id:
                    continue

                number_of_pickup_points += 1

                # Heuristique intelligente de routage logistique ouest-africain
                if producer_zone_id == buyer_zone_id:
                    total_transport_estimate += (
                        1500.0  # Même ville / village (Circuit ultra-court)
                    )
                else:
                    # Vérification si même région (même parent logistique)
                    p_parent = await current_session.scalar(
                        select(Zone.parent_id).where(Zone.id == producer_zone_id)
                    )
                    b_parent = await current_session.scalar(
                        select(Zone.parent_id).where(Zone.id == buyer_zone_id)
                    )

                    if p_parent and p_parent == b_parent:
                        total_transport_estimate += (
                            4000.0  # Transit régional (Inter-communal)
                        )
                    else:
                        total_transport_estimate += (
                            12000.0  # Transit national / Longue distance
                        )

            formatted_text = (
                f"🚛 *ESTIMATION LOGISTIQUE LADINI :*\n\n"
                f"📍 Destination : _{user_obj.name}_\n"
                f"🏢 Nombre de fermes de ramassage : *{number_of_pickup_points}*\n"
                f"💰 Coût estimé du transport : *{total_transport_estimate} CFA*\n\n"
                f"_💡 Note : Ce tarif est une estimation automatique basée sur les circuits courts. Le prix définitif vous sera confirmé lors de l'attribution du livreur._"
            )

            return {
                "status": "success",
                "estimated_cost": total_transport_estimate,
                "formatted_text": formatted_text,
            }
        except Exception as e:
            logger.error(f"Erreur estimation transport pour {phone}: {e}")
            return {
                "status": "error",
                "message": "Calcul logistique indisponible temporairement.",
            }

    # ─── SECTION 6 : TUNNEL TRANSACTIONNEL "GRADE ENTREPRISE" ────────────────
    # Outils MCP alignés sur les modèles Product, Stock, Order/OrderItem,
    # Auction, Bid. Chaque outil retourne TOUJOURS un dict structuré
    # (status SUCCESS/FAILURE/error) et alimente, en cas d'échec métier, une
    # clé `fallback` exploitée par le buyer_flow pour proposer une alternative.

    @staticmethod
    def _to_uuid(value: Any) -> Optional[uuid.UUID]:
        """Convertit une valeur en UUID, tolérante aux None / formats invalides."""
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return value
        try:
            return uuid.UUID(str(value))
        except (TypeError, ValueError):
            return None

    async def validate_stock_availability_atomic(
        self,
        product_id: str,
        quantity: float,
        unit: Optional[str] = None,
        buyer_phone: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Vérifie et verrouille la disponibilité réelle d'un produit avant précommande.

        Pose un verrou ligne (FOR UPDATE) sur le produit pour éviter les
        survente en conditions concurrentes. En cas de stock insuffisant,
        propose des alternatives (mêmes produits chez d'autres producteurs).
        """
        current_session = self.session
        if not current_session:
            return {
                "status": "error",
                "message": "Session de base de données indisponible.",
            }

        p_uuid = self._to_uuid(product_id)
        if p_uuid is None:
            return {"status": "error", "message": "Identifiant produit invalide."}

        try:
            requested = float(quantity)
        except (TypeError, ValueError):
            return {"status": "error", "message": "Quantité demandée invalide."}
        if requested <= 0:
            return {
                "status": "error",
                "message": "La quantité demandée doit être strictement positive.",
            }

        try:
            product = await current_session.scalar(
                select(Product).where(Product.id == p_uuid).with_for_update()
            )
            if not product:
                return {
                    "status": "error",
                    "reason": "product_not_found",
                    "message": "Ce produit n'existe plus dans le catalogue.",
                    "fallback": [],
                }

            available = float(product.quantity_for_sale or 0.0)
            if available >= requested:
                response_unit = _guess_display_unit(product.name, unit or product.unit)
                return {
                    "status": "success",
                    "product_id": str(product.id),
                    "product_name": product.name,
                    "available_quantity": available,
                    "requested_quantity": requested,
                    "unit": response_unit,
                    "unit_price": float(product.price or 0.0),
                    "producer_id": str(product.producer_id)
                    if product.producer_id
                    else None,
                    "message": f"{requested} {response_unit} de {product.name} disponibles.",
                }

            # Stock insuffisant → recherche d'alternatives (résilience).
            # Recherche floue trigram : ratisse plus large que le substring
            # strict pour proposer de vraies alternatives (haute sensibilité —
            # mieux vaut un faux positif que zéro fallback pour l'agent).
            alt_rows = await current_session.execute(
                select(
                    Product.id,
                    Product.name,
                    Product.price,
                    Product.quantity_for_sale,
                    Product.unit,
                )
                .where(
                    fuzzy_match(Product.name, product.name),
                    Product.id != product.id,
                    Product.quantity_for_sale >= requested,
                )
                .order_by(
                    similarity_rank(Product.name, product.name), Product.price.asc()
                )
                .limit(3)
            )
            fallback = [
                {
                    "product_id": str(r.id),
                    "name": r.name,
                    "price": float(r.price or 0.0),
                    "available_quantity": float(r.quantity_for_sale or 0.0),
                    "unit": _guess_display_unit(r.name, r.unit),
                }
                for r in alt_rows.mappings().all()
            ]

            return {
                "status": "error",
                "reason": "insufficient_stock",
                "product_id": str(product.id),
                "product_name": product.name,
                "available_quantity": available,
                "requested_quantity": requested,
                "message": (
                    f"Stock insuffisant pour {product.name} : "
                    f"{available} disponible(s) sur {requested} demandé(s)."
                ),
                "fallback": fallback,
            }
        except Exception as e:
            logger.error(
                f"validate_stock_availability_atomic({product_id}): {e}", exc_info=True
            )
            return {
                "status": "error",
                "message": "Erreur technique lors de la vérification du stock.",
            }

    async def reserve_future_offer(
        self,
        buyer_phone: str,
        market_offer_id: str,
        quantity: Any,
        desired_price: Any = None,
    ) -> Dict[str, Any]:
        """Réserve une quantité sur une PRODUCTION FUTURE (MarketOffer précommandable).

        Contrairement à une commande catalogue, on NE débite AUCUN stock produit :
        une précommande future se contente de RÉSERVER la quantité sur l'offre
        (``MarketOffer.reserved_quantity``) jusqu'à ce que le producteur valide la
        récolte. On crée un ``Order`` lié via ``market_offer_id`` (type PREORDER,
        statut PENDING) et on notifie le producteur (outbox). Idempotence protégée
        par la capacité disponible (available − reserved).
        """
        current_session = self.session
        if not current_session:
            raise BusinessRuleException("Session indisponible.")

        try:
            qty = float(quantity)
        except (TypeError, ValueError):
            qty = 0.0
        if qty <= 0:
            raise BusinessRuleException(
                "La quantité à réserver doit être supérieure à 0."
            )

        try:
            o_uuid = uuid.UUID(str(market_offer_id))
        except (TypeError, ValueError):
            raise BusinessRuleException("Référence d'offre invalide.") from None

        user_obj, profile_obj = await self.get_buyer_profile(phone=buyer_phone)

        # Verrou de ligne : évite deux réservations concurrentes qui
        # sur-réserveraient au-delà de la capacité disponible.
        offer = await current_session.scalar(
            select(MarketOffer).where(MarketOffer.id == o_uuid).with_for_update()
        )
        if not offer:
            raise BusinessRuleException("Cette production n'est plus disponible.")
        if not bool(offer.preorder_enabled) or not bool(offer.is_public):
            raise BusinessRuleException(
                "Cette production n'accepte pas encore de précommande."
            )
        if str(offer.status or "").upper() in {"CLOSED", "CANCELLED", "SOLD_OUT"}:
            raise BusinessRuleException(
                f"Cette production n'est plus ouverte (statut : {offer.status})."
            )

        available = float(offer.available_quantity or 0.0)
        reserved = float(offer.reserved_quantity or 0.0)
        remaining = available - reserved
        if qty > remaining + 1e-9:
            raise BusinessRuleException(
                f"Il ne reste que {_fmt_num(remaining)} {offer.unit or 'unité'} réservable(s) "
                f"sur cette production (vous demandez {_fmt_num(qty)}).",
                reason="insufficient_capacity",
            )

        zone_uuid = user_obj.zone_id
        unit_price = None
        try:
            unit_price = float(desired_price) if desired_price is not None else None
        except (TypeError, ValueError):
            unit_price = None
        if unit_price is None:
            unit_price = (
                float(offer.price_per_unit) if offer.price_per_unit is not None else 0.0
            )
        total = round(unit_price * qty, 2)

        eta = offer.estimated_available_at or offer.expected_harvest_date

        new_order = Order(
            id=uuid.uuid4(),
            buyer_id=profile_obj.id,
            market_offer_id=offer.id,
            zone_id=zone_uuid,
            customer_name=getattr(profile_obj, "establishment_name", None)
            or user_obj.name,
            customer_phone=normalize_phone(buyer_phone, required=False),
            total_amount=total,
            subtotal=total,
            status="PENDING",
            payment_status="PENDING",
            delivery_status="PENDING",
            payment_method="CASH",
            source="WHATSAPP",
            order_type="PREORDER",
            is_agent_order=True,
            expected_fulfillment_date=_naive_utc(eta),
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        current_session.add(new_order)

        # Réservation : on incrémente la quantité réservée SANS toucher au stock.
        offer.reserved_quantity = reserved + qty
        offer.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await current_session.flush()

        # Notifie le producteur via l'outbox (même transaction que la réservation).
        notified = await self._notify_producer_reservation(offer, qty, total)

        eta_txt = eta.strftime("%d/%m/%Y") if isinstance(eta, datetime) else "à venir"
        price_txt = _fmt_num(unit_price) if unit_price else "prix à confirmer"
        return {
            "status": "success",
            "order_id": str(new_order.id),
            "data": {
                "order_id": str(new_order.id),
                "market_offer_id": str(offer.id),
                "product": offer.product_label,
                "quantity": qty,
                "unit": offer.unit or "KG",
                "unit_price": unit_price,
                "total": total,
                "eta": eta.isoformat() if isinstance(eta, datetime) else None,
                "remaining_after": round(remaining - qty, 3),
            },
            "message": (
                f"✅ *Précommande enregistrée* pour {_fmt_num(qty)} {offer.unit or 'unité'} de "
                f"*{offer.product_label}* ({price_txt} FCFA/unité).\n"
                f"📅 Disponibilité prévue : *{eta_txt}*.\n"
                f"💰 Total estimé : *{_fmt_num(total)} FCFA*.\n\n"
                + (
                    "🔔 Le producteur a été notifié de votre réservation."
                    if notified
                    else "Le producteur sera informé de votre réservation."
                )
            ),
        }

    async def _notify_producer_reservation(
        self, offer: MarketOffer, qty: float, total: float
    ) -> bool:
        """Enfile une notif producteur (« un acheteur a réservé X ») via l'outbox.

        Dans la MÊME transaction que la réservation : si le commit échoue, la
        notif n'est pas enfilée non plus. dedupe_key = order-agnostique par
        (offer, quantité cumulée) évité → on autorise plusieurs notifs (une par
        réservation) via un suffixe temporel.
        """
        try:
            prod_row = (
                await self.session.execute(
                    select(User.phone)
                    .join(Producer, Producer.user_id == User.id)
                    .where(Producer.id == offer.producer_id)
                    .limit(1)
                )
            ).first()
            prod_phone = prod_row[0] if prod_row else None
            if not prod_phone:
                return False

            from ladini.workers.outbox import templates as _tpl
            from ladini.workers.repositories import outbox_repo as _outbox_repo

            eta = offer.estimated_available_at or offer.expected_harvest_date
            await _outbox_repo.enqueue(
                self.session,
                [
                    {
                        "channel": "WHATSAPP",
                        "recipient_phone": prod_phone,
                        "template_key": _tpl.PREORDER_RESERVED_PRODUCER,
                        "payload": {
                            "product": offer.product_label,
                            "quantity": float(qty),
                            "unit": offer.unit or "KG",
                            "total": float(total),
                            "reserved_total": float(offer.reserved_quantity or 0.0),
                            "available": float(offer.available_quantity or 0.0),
                            "eta": eta.strftime("%d/%m/%Y")
                            if isinstance(eta, datetime)
                            else None,
                        },
                        "dedupe_key": f"PREORDER_RESERVED:{offer.id}:{datetime.now(timezone.utc).replace(tzinfo=None).timestamp()}",
                    }
                ],
            )
            return True
        except Exception as exc:  # pragma: no cover - non bloquant
            logger.warning(
                "[Preorder] notif producteur échouée (non bloquant): %s", exc
            )
            return False

    async def create_preorder_draft(
        self,
        buyer_phone: str,
        cart_items: List[Dict[str, Any]],
        expected_fulfillment_date: Optional[str] = None,
        payment_method: str = "CASH",
        delivery_zone_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Crée une commande brouillon (PREORDER) multi-items sans débiter les stocks.

        Le brouillon matérialise le panier en Order(status=DRAFT, order_type=PREORDER)
        + OrderItem. Les stocks ne sont PAS décrémentés tant que la précommande
        n'est pas convertie en commande ferme (preorder_converted_at).
        """
        current_session = self.session
        if not current_session:
            raise BusinessRuleException("Session indisponible.")
        if not cart_items:
            raise BusinessRuleException(
                "Le panier est vide, impossible de créer une précommande."
            )

        user_obj, profile_obj = await self.get_buyer_profile(phone=buyer_phone)

        # `Order.zone_id` est nullable en base (orders/models.py) — bloquer
        # toute la précommande faute de zone est une décision métier, pas une
        # contrainte technique. Ça transformait un détail logistique
        # (affinable plus tard, à la livraison) en cul-de-sac complet pour
        # l'acheteur : la précommande entière échouait sans jamais proposer
        # d'issue. On dégrade gracieusement — zone absente = zone_uuid=None,
        # la précommande se crée quand même.
        zone_uuid = self._to_uuid(delivery_zone_id) or user_obj.zone_id

        fulfillment_dt = None
        if expected_fulfillment_date:
            try:
                fulfillment_dt = datetime.fromisoformat(str(expected_fulfillment_date))
            except ValueError:
                fulfillment_dt = None
        if (
            fulfillment_dt is not None
            and getattr(fulfillment_dt, "tzinfo", None) is not None
        ):
            fulfillment_dt = fulfillment_dt.astimezone(timezone.utc).replace(
                tzinfo=None
            )

        # (2026-09-05, Phase 6A — « une commande par producteur ») : un panier
        # peut légitimement mélanger plusieurs producteurs (le vendeur est
        # choisi PAR PRODUIT, cf. `cart_service.resolve_product_vendors`).
        # Jusqu'ici tout atterrissait dans UNE commande, alors que la
        # responsabilité (livrer, encaisser, annuler) est PAR PRODUCTEUR : un
        # producteur pouvait donc clôturer ou annuler la part d'un autre.
        # Désormais une commande par producteur, toutes corrélées par
        # `checkout_group_id` — ce qui rend les contrôles de propriété
        # existants (`confirm_delivery_and_payment`, `cancel_confirmed_order`)
        # corrects PAR CONSTRUCTION, sans le moindre correctif chez eux.
        # `checkout_group_id` ne porte AUCUN état : après confirmation, chaque
        # commande vit sa vie indépendamment.
        checkout_group_id = uuid.uuid4()
        orders_by_producer: Dict[str, Order] = {}
        totals_by_producer: Dict[str, float] = {}

        async def _order_for_producer(producer_id: Any) -> Order:
            """Commande du producteur, créée à la première ligne RETENUE.

            Créée tardivement (après validation de la ligne) pour ne jamais
            produire de commande vide si tous les articles d'un producteur
            sont écartés (palier périmé, seuil minimum non atteint)."""
            key = str(producer_id or "")
            existing = orders_by_producer.get(key)
            if existing is not None:
                return existing
            order = Order(
                id=uuid.uuid4(),
                buyer_id=profile_obj.id,
                zone_id=zone_uuid,
                customer_name=getattr(profile_obj, "establishment_name", None)
                or user_obj.name,
                customer_phone=normalize_phone(buyer_phone, required=False),
                total_amount=0.0,
                subtotal=0.0,
                status="DRAFT",
                payment_status="PENDING",
                delivery_status="PENDING",
                payment_method="CASH",
                source="WHATSAPP",
                order_type="PREORDER",
                is_agent_order=True,
                expected_fulfillment_date=fulfillment_dt,
                checkout_group_id=checkout_group_id,
                created_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
            current_session.add(order)
            await current_session.flush()
            orders_by_producer[key] = order
            totals_by_producer[key] = 0.0
            return order

        running_total = 0.0
        summary_items: List[str] = []
        unresolved: List[str] = []
        # (2026-09-04, audit CART→CHECKOUT) : items RÉELLEMENT écrits en base
        # (prix/palier RÉSOLUS SERVEUR, jamais le panier tel quel) — c'est ce
        # que l'appelant (`bootstrap_preorder_draft`) doit utiliser pour bâtir
        # le récapitulatif affiché à l'acheteur, jamais les valeurs figées au
        # moment de l'ajout au panier (voir la note "affichage ≠ exécution"
        # ci-dessous). Un panier vu 5 minutes plus tôt peut légitimement
        # différer du prix catalogue ACTUEL — ce champ garantit que ce que
        # l'acheteur confirme est EXACTEMENT ce que cette transaction a écrit.
        resolved_items: List[Dict[str, Any]] = []

        for item in cart_items:
            p_uuid = self._to_uuid(item.get("product_id"))
            try:
                qty = float(item.get("quantity") or 0)
            except (TypeError, ValueError):
                qty = 0.0
            if p_uuid is None or qty <= 0:
                continue

            product = await current_session.scalar(
                select(Product).where(Product.id == p_uuid)
            )
            if not product:
                unresolved.append(str(item.get("product_id")))
                continue

            tier_id = item.get("tier_id")
            # `order_id` n'est PAS posé ici : la commande du producteur n'est
            # créée qu'une fois la ligne définitivement retenue (voir
            # `_order_for_producer`), pour ne jamais créer de commande vide.
            order_item_kwargs: Dict[str, Any] = {
                "id": uuid.uuid4(),
                "product_id": product.id,
            }

            if tier_id:
                # (2026-09-04, audit CART→CHECKOUT) : GAP RÉEL fermé ici — ce
                # panier a résolu un PALIER (`domain/pricing_tiers.py`) au
                # moment de l'ajout, mais `qty` reçu ici est un NOMBRE DE
                # PAQUETS, jamais une quantité en unité de base — AVANT ce
                # correctif, cette boucle traitait TOUT item comme un produit
                # à tarif unique (`product.price * qty`), ce qui pour un
                # article à palier facturait/débitait le nombre de PAQUETS au
                # PRIX/UNITÉ DE BASE (ex: 3 bidons de 10L facturés comme 3 L
                # au lieu de 3 bidons au tarif du palier — sous-facturation
                # ET sous-débit de stock massifs). `resolve_tier`/`compute_line`
                # (déjà utilisés côté panier, `services/domain/cart_service.py`)
                # sont réutilisés ici SERVEUR pour re-résoudre le MÊME palier
                # de façon AUTORITATIVE — jamais confiance au prix envoyé par
                # le client. `OrderItem.tier_id`/`base_unit_quantity`
                # (colonnes déjà existantes, JAMAIS peuplées jusqu'ici) sont
                # désormais posées — c'est exactement CE que
                # `resolve_stock_debit`/`confirm_preorder_draft` attendaient
                # déjà en aval (voir leur docstring).
                try:
                    tier = resolve_tier(product.pricing_tiers, str(tier_id))
                    if not float(qty).is_integer():
                        raise PricingTierError(
                            f"Nombre de paquets invalide pour {product.name} (qty={qty})."
                        )
                    computed = compute_line(tier, int(qty))
                except PricingTierError as exc:
                    logger.warning(
                        "create_preorder_draft: palier %s introuvable/invalide pour "
                        "produit %s (panier périmé ?) — article ignoré : %s",
                        tier_id, p_uuid, exc,
                    )
                    unresolved.append(str(item.get("product_id")))
                    continue

                price = float(tier.price)  # prix PAR PAQUET, jamais par unité de base
                line_total = computed.price_total
                order_item_kwargs["quantity"] = qty
                order_item_kwargs["price_at_sale"] = price
                order_item_kwargs["tier_id"] = tier.tier_id
                order_item_kwargs["base_unit_quantity"] = computed.base_unit_quantity
                resolved_items.append(
                    {
                        "product_id": str(product.id),
                        "name": product.name,
                        "quantity": qty,
                        "unit": tier.unit,
                        "packaging": tier.packaging,
                        "tier_quantity": tier.quantity,
                        "base_unit_quantity": computed.base_unit_quantity,
                        "tier_id": tier.tier_id,
                        "price": price,
                        "line_total": round(line_total, 2),
                        "producer_id": str(product.producer_id) if product.producer_id else None,
                    }
                )
                summary_items.append(
                    f"{product.name} (x{int(qty)} {tier.packaging or tier.unit})"
                )
            else:
                price = float(product.price or 0.0)
                line_total = price * qty
                order_item_kwargs["quantity"] = qty
                order_item_kwargs["price_at_sale"] = price
                resolved_items.append(
                    {
                        "product_id": str(product.id),
                        "name": product.name,
                        "quantity": qty,
                        "unit": product.unit or "KG",
                        "price": price,
                        "line_total": round(line_total, 2),
                        "producer_id": str(product.producer_id) if product.producer_id else None,
                    }
                )
                summary_items.append(f"{product.name} (x{qty} {product.unit or 'KG'})")

            # (2026-09-04, audit CART→CHECKOUT boundary closure) : GAP RÉEL
            # fermé ici — le seuil minimum de commande (`governance.sub_categories`,
            # SEULE source canonique, voir `domain/order_policy.py`) n'était
            # revalidé qu'à l'AJOUT au panier (`cart_service.py::add_to_cart_with_ref`),
            # jamais ici au moment du snapshot serveur faisant foi. Un panier
            # composé AVANT qu'un admin ne relève le seuil (ex: 50 KG -> 100 KG)
            # pouvait ainsi produire une précommande en dessous du minimum EN
            # VIGUEUR — exactement le type de divergence "cart dit valide,
            # checkout dit valide, alors que la règle actuelle dit invalide"
            # que ce chantier doit fermer. Réutilise la MÊME fonction pure et
            # la MÊME source (`SubCategory`) que `finalize_multi_order`
            # (référence déjà correcte, jamais câblée) — aucune nouvelle
            # primitive. Un item qui échoue est écarté (jamais silencieusement
            # sous-facturé) et journalisé dans `unresolved_items`, comme un
            # palier périmé juste au-dessus — `bootstrap_preorder_draft` en
            # informe explicitement l'acheteur (voir son propre correctif).
            total_qty_for_policy = (
                computed.base_unit_quantity if tier_id else qty
            )
            policy_unit = tier.unit if tier_id else (product.unit or "KG")
            if getattr(product, "sub_category_id", None):
                sub_row = (
                    await current_session.execute(
                        select(
                            SubCategory.minimum_order_quantity,
                            SubCategory.minimum_order_unit,
                        ).where(SubCategory.id == product.sub_category_id)
                    )
                ).first()
                if sub_row is not None:
                    min_check = validate_minimum_order_quantity(
                        minimum_order_quantity=(
                            float(sub_row[0]) if sub_row[0] is not None else None
                        ),
                        minimum_order_unit=sub_row[1],
                        total_quantity=total_qty_for_policy,
                        total_unit=str(policy_unit),
                    )
                    if not min_check.passed:
                        logger.warning(
                            "create_preorder_draft: seuil minimum non atteint pour "
                            "produit %s (qty=%s unit=%s, reason=%s) — article ignoré",
                            p_uuid, total_qty_for_policy, policy_unit, min_check.reason,
                        )
                        resolved_items.pop()
                        summary_items.pop()
                        unresolved.append(str(item.get("product_id")))
                        continue

            running_total += line_total
            producer_key = str(product.producer_id or "")
            target_order = await _order_for_producer(product.producer_id)
            order_item_kwargs["order_id"] = target_order.id
            totals_by_producer[producer_key] = (
                totals_by_producer.get(producer_key, 0.0) + line_total
            )
            current_session.add(OrderItem(**order_item_kwargs))

        if not summary_items or not orders_by_producer:
            raise BusinessRuleException(
                "Aucun article valide dans le panier pour la précommande.",
                reason="no_valid_items",
            )

        # Total PAR COMMANDE — jamais le total du checkout : le producteur A
        # ne doit jamais voir (ni encaisser) le montant du producteur B.
        for producer_key, order in orders_by_producer.items():
            order_total = round(totals_by_producer.get(producer_key, 0.0), 2)
            order.subtotal = order_total
            order.total_amount = order_total
        await current_session.flush()

        # Commande PRIMAIRE = première créée. Elle porte l'identité du
        # brouillon (`PreorderDraft.order_id`, colonne `preorder_drafts.order_id`)
        # pour que toute la machinerie CAS/idempotence/reprise existante
        # continue de fonctionner à l'identique. Le groupe complet voyage à
        # côté, dans `order_ids`.
        ordered_orders = list(orders_by_producer.values())
        primary_order = ordered_orders[0]
        orders_payload = [
            {
                "order_id": str(o.id),
                "order_number": str(o.id)[:8].upper(),
                "producer_id": producer_key or None,
                "total_amount": float(o.total_amount or 0.0),
                "items": [
                    it
                    for it in resolved_items
                    if str(it.get("producer_id") or "") == producer_key
                ],
            }
            for producer_key, o in orders_by_producer.items()
        ]

        if len(orders_payload) > 1:
            breakdown = " · ".join(
                f"{len(o['items'])} article(s) → {_fmt_num(o['total_amount'])} FCFA"
                for o in orders_payload
            )
            message = (
                f"📝 Précommande brouillon créée — {len(summary_items)} article(s) "
                f"répartis en {len(orders_payload)} commandes (une par producteur) : "
                f"{breakdown}. Total {_fmt_num(running_total)} FCFA."
            )
        else:
            message = (
                f"📝 Précommande brouillon créée — {len(summary_items)} article(s), "
                f"total estimé {running_total} FCFA."
            )

        return {
            "status": "success",
            "order_id": str(primary_order.id),
            "preorder_id": str(primary_order.id),
            "order_number": str(primary_order.id)[:8].upper(),
            "order_type": "PREORDER",
            "checkout_group_id": str(checkout_group_id),
            "order_ids": [str(o.id) for o in ordered_orders],
            "orders": orders_payload,
            "subtotal": running_total,
            "total_amount": running_total,
            "currency": primary_order.currency or "XOF",
            "items_count": len(summary_items),
            "items": resolved_items,
            "summary": ", ".join(summary_items),
            "unresolved_items": unresolved,
            "message": message,
        }

    async def initiate_negotiation_session(
        self,
        buyer_phone: str,
        product_id: str,
        offered_price: float,
        quantity: Optional[float] = None,
        message: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Ouvre une session de négociation acheteur→producteur via un Auction ancré.

        Matérialise l'offre comme un Auction(status=OPEN) rattaché à la
        sous-catégorie du produit ciblé. Le prix proposé devient
        max_price_per_unit, et le prix catalogue du producteur sert de
        référence (seller_minimum). Retourne un negotiation_id réutilisable.
        """
        current_session = self.session
        if not current_session:
            raise BusinessRuleException("Session indisponible.")

        p_uuid = self._to_uuid(product_id)
        if p_uuid is None:
            raise BusinessRuleException("Identifiant produit invalide.")
        try:
            offer = float(offered_price)
        except (TypeError, ValueError):
            raise BusinessRuleException("Prix proposé invalide.") from None
        if offer <= 0:
            raise BusinessRuleException(
                "Le prix proposé doit être strictement positif."
            )

        user_obj, profile_obj = await self.get_buyer_profile(phone=buyer_phone)

        product = await current_session.scalar(
            select(Product).where(Product.id == p_uuid)
        )
        if not product:
            raise BusinessRuleException(
                "Produit introuvable, impossible d'ouvrir une négociation.",
                reason="product_not_found",
                fallback=[],
            )
        if not product.sub_category_id:
            raise BusinessRuleException(
                "Ce produit n'est pas catégorisé, négociation impossible pour l'instant.",
                reason="missing_subcategory",
                fallback=[],
            )

        seller_minimum = float(product.price or 0.0)
        qty = None
        try:
            qty = float(quantity) if quantity is not None else None
        except (TypeError, ValueError):
            qty = None
        qty = qty or float(product.quantity_for_sale or 1.0) or 1.0

        zone_name = "À convenir"
        if user_obj.zone_id:
            zname = await current_session.scalar(
                select(Zone.name).where(Zone.id == user_obj.zone_id)
            )
            if zname:
                zone_name = zname

        deadline = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=7)
        new_auction = Auction(
            id=uuid.uuid4(),
            buyer_id=profile_obj.id,
            sub_category_id=product.sub_category_id,
            quantity=qty,
            unit=(product.unit or "KG").upper(),
            max_price_per_unit=offer,
            description=(
                message
                or f"Négociation sur {product.name} (prix proposé: {offer} FCFA)."
            ),
            delivery_location=zone_name,
            delivery_deadline=deadline,
            deadline=deadline,
            target_zone_id=user_obj.zone_id,
            status="OPEN",
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        current_session.add(new_auction)
        await current_session.flush()

        price_gap = round(seller_minimum - offer, 2)
        return {
            "status": "success",
            "negotiation_status": "PENDING",
            "negotiation_id": str(new_auction.id),
            "auction_id": str(new_auction.id),
            "product_id": str(product.id),
            "product_name": product.name,
            "producer_id": str(product.producer_id) if product.producer_id else None,
            "buyer_offer": offer,
            "seller_minimum": seller_minimum,
            "price_gap": price_gap,
            "quantity": qty,
            "unit": (product.unit or "KG").upper(),
            "next_expected_action": "SELLER_RESPONSE",
            "message": (
                f"🤝 Négociation ouverte sur {product.name} : vous proposez {offer} FCFA "
                f"(prix catalogue {seller_minimum} FCFA)."
            ),
        }

    async def get_transaction_summary(
        self,
        buyer_phone: Optional[str] = None,
        order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Vue agrégée d'une transaction (commande + items + négociation liée).

        Si `order_id` est fourni, retourne le détail de cette commande. Sinon,
        retourne la dernière transaction en cours de l'acheteur identifié par
        `buyer_phone`.
        """
        current_session = self.session
        if not current_session:
            return {"status": "error", "message": "Session indisponible."}

        try:
            stmt = (
                select(Order)
                .options(selectinload(Order.items).joinedload(OrderItem.product))
                .order_by(desc(Order.created_at))
                .limit(1)
            )

            o_uuid = self._to_uuid(order_id)
            if o_uuid is not None:
                stmt = stmt.where(Order.id == o_uuid)
            elif buyer_phone:
                _, profile_obj = await self.get_buyer_profile(phone=buyer_phone)
                # (2026-09-04, audit Order(DRAFT) orphelin) : "la dernière
                # transaction" ne doit JAMAIS être un brouillon de checkout
                # abandonné/remplacé (DRAFT/SUPERSEDED) — sans cette
                # exclusion, un acheteur demandant "où en est ma commande ?"
                # juste après avoir annulé ou recomposé son panier se voyait
                # répondre sur ce brouillon plutôt que sur sa VRAIE dernière
                # commande (`order_tracking.py::check_order_status`, le seul
                # appelant réel de cette branche sans `order_id` explicite).
                # Portée volontairement limitée à CETTE branche : un
                # `order_id` explicite reste consultable tel quel (support/
                # debug), voir la branche `o_uuid is not None` ci-dessus.
                stmt = stmt.where(
                    Order.buyer_id == profile_obj.id,
                    Order.status.notin_(["DRAFT", "SUPERSEDED"]),
                )
            else:
                return {
                    "status": "error",
                    "message": "Fournir order_id ou buyer_phone.",
                }

            order = await current_session.scalar(stmt)

            # Ownership check (P0-SEC-004) : la branche order_id ci-dessus ne
            # filtre QUE sur Order.id — un buyer_phone fourni en plus (comme
            # le fait TOUJOURS check_order_status) doit être vérifié
            # explicitement contre l'acheteur réel de la commande, sinon
            # n'importe quel order_id connu (numéroté séquentiellement,
            # devinable) expose la commande d'un autre acheteur.
            if order is not None and o_uuid is not None and buyer_phone:
                _, requester_profile = await self.get_buyer_profile(phone=buyer_phone)
                if order.buyer_id != requester_profile.id:
                    order = None

            if not order:
                return {
                    "status": "success",
                    "message": "Aucune transaction trouvée.",
                    "data": None,
                }

            items = [
                {
                    "product_id": str(it.product_id),
                    "product_name": it.product.name if it.product else "?",
                    "quantity": float(it.quantity or 0.0),
                    "unit_price": float(it.price_at_sale or 0.0),
                    "line_total": float(it.quantity or 0.0)
                    * float(it.price_at_sale or 0.0),
                }
                for it in (order.items or [])
            ]

            negotiation = None
            if order.auction_id:
                auction = await current_session.scalar(
                    select(Auction).where(Auction.id == order.auction_id)
                )
                if auction:
                    negotiation = {
                        "auction_id": str(auction.id),
                        "status": auction.status,
                        "max_price_per_unit": float(auction.max_price_per_unit or 0.0),
                        "quantity": float(auction.quantity or 0.0),
                        "winner_bid_id": str(auction.winner_bid_id)
                        if auction.winner_bid_id
                        else None,
                    }

            return {
                "status": "success",
                "data": {
                    "order_id": str(order.id),
                    "order_number": str(order.id)[:8].upper(),
                    "order_type": order.order_type,
                    "status": order.status,
                    "payment_status": order.payment_status,
                    "delivery_status": order.delivery_status,
                    "subtotal": float(order.subtotal or 0.0),
                    "delivery_fee": float(order.delivery_fee or 0.0),
                    "total_amount": float(order.total_amount or 0.0),
                    "currency": order.currency or "XOF",
                    "items": items,
                    "negotiation": negotiation,
                    "created_at": order.created_at.isoformat()
                    if order.created_at
                    else None,
                },
                "message": (
                    f"Commande #{str(order.id)[:8].upper()} — {order.status} — "
                    f"{float(order.total_amount or 0.0)} {order.currency or 'XOF'}."
                ),
            }
        except Exception as e:
            logger.error(
                f"get_transaction_summary(order={order_id}, phone={buyer_phone}): {e}",
                exc_info=True,
            )
            return {
                "status": "error",
                "message": "Erreur technique lors de la récupération de la transaction.",
            }

    async def confirm_preorder_draft(
        self,
        buyer_phone: str,
        preorder_id: str,
        delivery_lat: Optional[float] = None,
        delivery_lon: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Convertit une précommande brouillon (Order.status=DRAFT) en commande ferme.

        - Verrouille la commande et les produits (FOR UPDATE) pour éviter la survente.
        - Débite `Product.quantity_for_sale`.
        - Marque `preorder_converted_at` et passe le statut en `CONFIRMED`.
        - `delivery_lat`/`delivery_lon` (optionnels) figent le point GPS de
          livraison sur la commande — voir [[gps-delivery-burkina-faso-2026-08]].
        """
        current_session = self.session
        if not current_session:
            raise BusinessRuleException("Session indisponible.")

        if delivery_lat is not None and delivery_lon is not None:
            from ladini.core.geofencing import is_within_burkina_faso

            if not is_within_burkina_faso(delivery_lat, delivery_lon):
                raise BusinessRuleException(
                    "Le point de livraison est hors du Burkina Faso.",
                    reason="out_of_country",
                )

        o_uuid = self._to_uuid(preorder_id)
        if o_uuid is None:
            raise BusinessRuleException("Identifiant précommande invalide.")

        _, profile_obj = await self.get_buyer_profile(phone=buyer_phone)

        stmt = (
            select(Order)
            .options(selectinload(Order.items).joinedload(OrderItem.product))
            .where(Order.id == o_uuid, Order.buyer_id == profile_obj.id)
            .with_for_update()
        )
        order = await current_session.scalar(stmt)
        if not order:
            raise BusinessRuleException("Précommande introuvable ou non autorisée.")

        if str(order.status or "").upper() != "DRAFT":
            raise BusinessRuleException(
                f"Cette précommande n'est pas en brouillon (statut={order.status}).",
                reason="not_draft",
            )

        # (2026-09-05, Phase 6A) : confirmation GROUPÉE — un checkout mixte a
        # produit une commande par producteur, l'acheteur ne confirme qu'UNE
        # fois. On récupère ici les commandes sœurs (même `checkout_group_id`,
        # même acheteur, encore `DRAFT`). Verrou `FOR UPDATE` et ordre
        # déterministe par `id` : deux confirmations concurrentes du même
        # groupe se sérialisent au lieu de s'entrelacer.
        # Commande sans groupe (mono-producteur, RFQ, ou commande antérieure
        # à ce modèle) : le groupe vaut exactement `[order]`, comportement
        # historique strictement inchangé.
        group_orders: List[Order] = [order]
        if order.checkout_group_id is not None:
            siblings = (
                (
                    await current_session.execute(
                        select(Order)
                        .options(selectinload(Order.items).joinedload(OrderItem.product))
                        .where(
                            Order.checkout_group_id == order.checkout_group_id,
                            Order.buyer_id == profile_obj.id,
                            Order.id != order.id,
                            func.upper(Order.status) == "DRAFT",
                        )
                        .order_by(Order.id)
                        .with_for_update()
                    )
                )
                .scalars()
                .unique()
                .all()
            )
            group_orders.extend(siblings)
        group_orders.sort(key=lambda o: str(o.id))

        # Vérification / débit stock atomique par produit
        insufficient: List[Dict[str, Any]] = []
        running_total = 0.0
        totals_by_order: Dict[str, float] = {str(o.id): 0.0 for o in group_orders}

        # (2026-09-04, audit CART→CHECKOUT) : tri déterministe par
        # `product_id` AVANT tout `FOR UPDATE` — même garde-fou anti-deadlock
        # DÉJÀ présent dans `finalize_multi_order` (voir sa docstring : "deux
        # requêtes concurrentes achetant les mêmes produits dans un ordre
        # différent forment un cycle de verrous"), jamais appliqué ici
        # jusqu'à cet audit. Deux précommandes concurrentes portant les MÊMES
        # 2 produits, mais ajoutés au panier dans l'ordre inverse (buyer A :
        # tomates puis maïs ; buyer B : maïs puis tomates), pouvaient
        # verrouiller `Product` dans un ordre opposé — Postgres détecte et
        # annule l'une des deux transactions (`deadlock_detected`), jamais de
        # corruption de données, mais un échec évitable au lieu d'une simple
        # sérialisation. Réutilise le motif déjà PROUVÉ ailleurs dans ce
        # fichier, aucune nouvelle primitive.
        # Le tri porte sur l'UNION des articles du groupe (pas commande par
        # commande) : c'est l'ordre d'acquisition des verrous `Product` qui
        # doit être global pour conserver la garantie anti-deadlock.
        sorted_items = sorted(
            (
                (item, o)
                for o in group_orders
                for item in (o.items or [])
            ),
            key=lambda pair: str(pair[0].product_id or ""),
        )

        for item, item_order in sorted_items:
            if not item.product_id:
                continue

            product = await current_session.scalar(
                select(Product).where(Product.id == item.product_id).with_for_update()
            )
            if not product:
                insufficient.append(
                    {
                        "product_id": str(item.product_id),
                        "reason": "product_not_found",
                    }
                )
                continue

            requested = float(item.quantity or 0.0)
            # Quantité en unité de BASE à débiter — diffère de `requested`
            # (le nombre de paquets/palier affiché à l'acheteur) dès qu'un
            # `tier_id` est impliqué. Voir domain/pricing_tiers.py.
            stock_debit = resolve_stock_debit(item)
            available = float(product.quantity_for_sale or 0.0)
            if available < stock_debit:
                insufficient.append(
                    {
                        "product_id": str(product.id),
                        "name": product.name,
                        "requested": stock_debit,
                        "available": available,
                        "unit": (product.unit or "KG").upper(),
                    }
                )
                continue

            product.quantity_for_sale = available - stock_debit
            line_total = float(item.price_at_sale or 0.0) * requested
            running_total += line_total
            # Chaque montant est imputé à SA commande — le total d'un
            # producteur n'inclut jamais les lignes d'un autre.
            totals_by_order[str(item_order.id)] = (
                totals_by_order.get(str(item_order.id), 0.0) + line_total
            )

        if insufficient:
            raise BusinessRuleException(
                "Stock insuffisant sur un ou plusieurs articles — précommande non confirmée.",
                reason="insufficient_stock",
                details=insufficient,
            )

        # Postgres column is TIMESTAMP WITHOUT TIME ZONE → store naive UTC
        converted_at = datetime.now(timezone.utc).replace(tzinfo=None)
        # (2026-09-13, confirmation explicite producteur) : la commande
        # n'est plus directement `CONFIRMED` à ce stade — elle entre dans un
        # état intermédiaire (`PENDING_PRODUCER_CONFIRMATION`) tant que le
        # producteur n'a pas explicitement confirmé pouvoir l'honorer
        # (`ProducerMgmtMixin.confirm_order_by_producer`) ou l'a refusée
        # (`cancel_confirmed_order`, désormais aussi valide depuis cet état).
        # Le stock reste débité dès CE tour (comme avant) — un refus recrédite
        # via le même mécanisme que l'annulation post-confirmation.
        for grp_order in group_orders:
            grp_order.preorder_converted_at = converted_at
            grp_order.status = "PENDING_PRODUCER_CONFIRMATION"
            grp_order.payment_status = grp_order.payment_status or "PENDING"
            grp_total = round(totals_by_order.get(str(grp_order.id), 0.0), 2)
            grp_order.subtotal = grp_total
            grp_order.total_amount = grp_total
            if delivery_lat is not None and delivery_lon is not None:
                grp_order.gps_lat = delivery_lat
                grp_order.gps_lng = delivery_lon
        await current_session.flush()

        # (2026-09-04, F2 — notification producteur préorder direct) : GAP
        # RÉEL fermé ici — ce chemin (paiement à la livraison,
        # `ESCROW_PAYMENT_ENABLED=False`) n'enfilait AUCUNE notification
        # producteur, contrairement au chemin escrow
        # (`EscrowMixin.mark_escrow_paid`, déclenché par l'IPN, notifie déjà
        # via `ESCROW_PAYMENT_SECURED_PRODUCER`). Réutilise EXACTEMENT le
        # même motif de collecte multi-producteurs (un panier peut contenir
        # des articles de plusieurs producteurs, chacun notifié une seule
        # fois — `dedupe_key` scopé par `(order.id, phone)`, même convention
        # que l'escrow) — jamais "paiement reçu" (rien n'a été payé en
        # ligne ici) : un nouveau template dédié au paiement à la livraison,
        # jamais le template escrow réutilisé à tort.
        from ladini.workers.outbox import templates as _outbox_templates
        from ladini.workers.repositories import outbox_repo as _outbox_repo

        # (2026-09-05, Phase 6A) : une notification PAR COMMANDE, portant le
        # montant de CETTE commande. Un producteur ne reçoit donc jamais le
        # total du checkout d'un autre. La collecte reste multi-producteurs
        # par commande pour couvrir les commandes ANCIENNES (antérieures au
        # split, volontairement conservées telles quelles — grandfathering).
        entries: List[Dict[str, Any]] = []
        all_producer_phones: set[str] = set()
        for grp_order in group_orders:
            grp_number = str(grp_order.id)[:8].upper()
            grp_phones: set[str] = set()
            for item in grp_order.items or []:
                if item.product and item.product.producer_id:
                    prod_row = (
                        await current_session.execute(
                            select(User.phone)
                            .join(Producer, Producer.user_id == User.id)
                            .where(Producer.id == item.product.producer_id)
                            .limit(1)
                        )
                    ).first()
                    if prod_row and prod_row[0]:
                        grp_phones.add(prod_row[0])
            # (2026-09-11) `items_summary` : le producteur recevait jusqu'ici
            # UNIQUEMENT un code de commande + un montant ("Nouvelle commande
            # confirmée ! #65280745 — 1000000 CFA"), sans savoir CE QUE
            # l'acheteur a commandé — signalé comme peu pertinent par un
            # producteur réel (il doit ouvrir l'app pour savoir quoi
            # préparer). On transmet donc le détail produit/quantité déjà
            # chargé sur `grp_order.items` (mêmes lignes utilisées juste
            # au-dessus pour résoudre les producteurs), formaté avec
            # `_fmt_num` (jamais de quantité brute genre "50.000" ambigüe).
            items_summary = ", ".join(
                f"{item.product.name} (x{_fmt_num(item.quantity)})"
                for item in (grp_order.items or [])
                if item.product
            ) or "articles divers"
            entries.extend(
                {
                    "channel": "WHATSAPP",
                    "recipient_phone": phone,
                    "template_key": _outbox_templates.PREORDER_CONFIRMED_PRODUCER,
                    "payload": {
                        "order_number": grp_number,
                        "amount": float(grp_order.total_amount or 0.0),
                        "currency": grp_order.currency or "XOF",
                        "items_summary": items_summary,
                    },
                    "dedupe_key": f"PREORDER_CONFIRMED_PRODUCER:{grp_order.id}:{phone}",
                }
                for phone in grp_phones
            )
            all_producer_phones |= grp_phones

        if entries:
            await _outbox_repo.enqueue(current_session, entries)

        order_number = str(order.id)[:8].upper()
        confirmed_payload = [
            {
                "order_id": str(o.id),
                "order_number": str(o.id)[:8].upper(),
                "total_amount": float(o.total_amount or 0.0),
            }
            for o in group_orders
        ]
        if len(group_orders) > 1:
            detail = " · ".join(
                f"#{o['order_number']} {_fmt_num(o['total_amount'])} FCFA"
                for o in confirmed_payload
            )
            message = (
                f"✅ Précommande confirmée — {len(group_orders)} commandes "
                f"(une par producteur) : {detail}. "
                f"Total {_fmt_num(running_total)} {order.currency or 'XOF'}. "
                "Chaque producteur vous livrera et encaissera sa part séparément."
            )
        else:
            message = (
                f"✅ Précommande confirmée. Commande #{order_number} "
                f"pour {float(order.total_amount or 0.0)} {order.currency or 'XOF'}."
            )

        return {
            "status": "success",
            "order_id": str(order.id),
            "order_number": order_number,
            "total_amount": float(order.total_amount or 0.0),
            "checkout_total": round(running_total, 2),
            "orders": confirmed_payload,
            "currency": order.currency or "XOF",
            "message": message,
            # (2026-09-13, incident WhatsApp #3 — utilisateur double-rôle) :
            # les numéros producteurs notifiés ci-dessus doivent voir leur
            # PROCHAINE réponse ("confirmer"/"annuler") traitée comme
            # PRODUCER, même si leur workspace conversationnel (unique par
            # numéro, partagé buyer/producteur) est resté figé en mode BUYER
            # par une session antérieure. Ce processus MCP n'a volontairement
            # pas accès à Redis (service à privilège minimal, DB uniquement)
            # — l'indice d'ÉTAT correspondant (`pending_role_hint:{phone}`)
            # est donc posé par l'APPELANT côté worker (qui, lui, a accès à
            # Redis), voir `flows/buyer/preorder_confirmation.py`, jamais ici.
            "producer_phones_notified": sorted(all_producer_phones),
        }

    async def cancel_preorder_draft(
        self,
        buyer_phone: str,
        preorder_id: str,
        reason: Optional[str] = None,
        target_status: str = "CANCELLED",
    ) -> Dict[str, Any]:
        """Annule (ou remplace) une précommande brouillon sans impacter les
        stocks (non débitée).

        (2026-09-04, audit Order(DRAFT) orphelin) : `target_status` distingue
        deux issues DIFFÉRENTES pour le même `Order(status=DRAFT)` abandonné :
        `"CANCELLED"` (défaut, comportement historique inchangé — l'acheteur
        a explicitement annulé sa précommande) vs `"SUPERSEDED"` (le cycle
        "ajouter d'autres produits" a fait naître un NOUVEAU
        `Order(status=DRAFT)` pour le MÊME `PreorderDraft` — l'ancien n'a
        jamais été rejeté par l'acheteur, il a simplement été remplacé).
        `cancellation_role`/`delivery_desc` (champs sémantiquement liés à un
        VRAI rejet acheteur) ne sont posés QUE sur `target_status="CANCELLED"`.
        """
        current_session = self.session
        if not current_session:
            raise BusinessRuleException("Session indisponible.")

        o_uuid = self._to_uuid(preorder_id)
        if o_uuid is None:
            raise BusinessRuleException("Identifiant précommande invalide.")

        _, profile_obj = await self.get_buyer_profile(phone=buyer_phone)

        stmt = (
            select(Order)
            .where(Order.id == o_uuid, Order.buyer_id == profile_obj.id)
            .with_for_update()
        )
        order = await current_session.scalar(stmt)
        if not order:
            raise BusinessRuleException("Précommande introuvable ou non autorisée.")

        if str(order.status or "").upper() != "DRAFT":
            raise BusinessRuleException(
                f"Impossible d'annuler une précommande non brouillon (statut={order.status}).",
                reason="not_draft",
            )

        # (2026-09-05, Phase 7 — audit « outils acceptant un statut
        # arbitraire ») : `target_status` était écrit TEL QUEL dans
        # `Order.status`. Les appelants légitimes ne passent que
        # `"CANCELLED"` ou `"SUPERSEDED"`, mais cette méthode est exposée
        # comme outil MCP : un appelant direct pouvait faire passer SON
        # brouillon à n'importe quel statut (`"COMPLETED"` par exemple),
        # sans confirmation, sans débit de stock ni paiement. Liste blanche
        # explicite — même classe de faille que `update_order_status`.
        target_status = str(target_status or "").upper()
        if target_status not in ("CANCELLED", "SUPERSEDED"):
            raise BusinessRuleException(
                "Statut cible non autorisé pour l'abandon d'un brouillon "
                f"(reçu : {target_status}).",
                reason="invalid_target_status",
            )

        # (2026-09-05, Phase 6A) : un checkout mixte a produit une commande
        # par producteur — abandonner le brouillon doit les emporter TOUTES,
        # sinon les commandes sœurs resteraient `DRAFT` orphelines (exactement
        # le problème fermé par l'audit Order(DRAFT) orphelin). Sans groupe
        # (mono-producteur, RFQ, commande antérieure) : une seule commande,
        # comportement historique inchangé.
        group_orders: List[Order] = [order]
        if order.checkout_group_id is not None:
            siblings = (
                (
                    await current_session.execute(
                        select(Order)
                        .where(
                            Order.checkout_group_id == order.checkout_group_id,
                            Order.buyer_id == profile_obj.id,
                            Order.id != order.id,
                            func.upper(Order.status) == "DRAFT",
                        )
                        .order_by(Order.id)
                        .with_for_update()
                    )
                )
                .scalars()
                .unique()
                .all()
            )
            group_orders.extend(siblings)

        for grp_order in group_orders:
            grp_order.status = target_status
            if target_status == "CANCELLED":
                grp_order.cancellation_role = "BUYER"
            if reason:
                grp_order.delivery_desc = (
                    grp_order.delivery_desc or ""
                ) + f"\n[CancelReason] {reason}"
        await current_session.flush()

        verb = "annulée" if target_status == "CANCELLED" else "remplacée"
        if len(group_orders) > 1:
            refs = ", ".join(f"#{str(o.id)[:8].upper()}" for o in group_orders)
            message = f"❌ Précommande {verb} — {len(group_orders)} commandes : {refs}."
        else:
            message = f"❌ Précommande #{str(order.id)[:8].upper()} {verb}."
        return {
            "status": "success",
            "order_id": str(order.id),
            "order_ids": [str(o.id) for o in group_orders],
            "order_status": target_status,
            "message": message,
        }

    async def update_negotiation_offer(
        self,
        buyer_phone: str,
        negotiation_id: str,
        new_price: float,
    ) -> Dict[str, Any]:
        """Met à jour le prix plafond (max_price_per_unit) d'une négociation (Auction)."""
        current_session = self.session
        if not current_session:
            raise BusinessRuleException("Session indisponible.")

        a_uuid = self._to_uuid(negotiation_id)
        if a_uuid is None:
            raise BusinessRuleException("Identifiant négociation invalide.")
        try:
            price = float(new_price)
        except (TypeError, ValueError):
            raise BusinessRuleException("Nouveau prix invalide.") from None
        if price <= 0:
            raise BusinessRuleException(
                "Le nouveau prix doit être strictement positif."
            )

        _, profile_obj = await self.get_buyer_profile(phone=buyer_phone)
        stmt = (
            select(Auction)
            .where(Auction.id == a_uuid, Auction.buyer_id == profile_obj.id)
            .with_for_update()
        )
        auction = await current_session.scalar(stmt)
        if not auction:
            raise BusinessRuleException("Négociation introuvable ou non autorisée.")

        if str(auction.status or "").upper() not in {"OPEN"}:
            raise BusinessRuleException(
                f"Négociation déjà clôturée (statut={auction.status}).",
                reason="auction_closed",
            )

        old = float(auction.max_price_per_unit or 0.0)
        auction.max_price_per_unit = price
        # (2026-09-04, audit Auction/Bid — décision `Auction.version`) :
        # incrément retiré. C'était le SEUL écrivain de cette colonne dans
        # tout le dépôt, et aucun lecteur nulle part (ni CAS optimiste — cette
        # fonction protège déjà sa cohérence via `.with_for_update()`
        # pessimiste ci-dessus —, ni affichage, ni réconciliation). La
        # colonne reste en base (voir `Auction.version` dans
        # `domain/orders/models.py` pour la justification complète : pas de
        # suppression de colonne sans confirmer qu'aucun lecteur externe au
        # dépôt n'existe), mais plus aucun code n'écrit dedans.
        await current_session.flush()

        return {
            "status": "success",
            "negotiation_id": str(auction.id),
            "old_price": old,
            "new_price": price,
            "message": f"🔁 Offre mise à jour : {old} → {price} FCFA/{auction.unit}.",
        }

    async def close_negotiation_session(
        self,
        buyer_phone: str,
        negotiation_id: str,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Clôture une négociation (Auction) côté acheteur (annulation)."""
        current_session = self.session
        if not current_session:
            raise BusinessRuleException("Session indisponible.")

        a_uuid = self._to_uuid(negotiation_id)
        if a_uuid is None:
            raise BusinessRuleException("Identifiant négociation invalide.")

        _, profile_obj = await self.get_buyer_profile(phone=buyer_phone)
        stmt = (
            select(Auction)
            .where(Auction.id == a_uuid, Auction.buyer_id == profile_obj.id)
            .with_for_update()
        )
        auction = await current_session.scalar(stmt)
        if not auction:
            raise BusinessRuleException("Négociation introuvable ou non autorisée.")

        if str(auction.status or "").upper() != "OPEN":
            raise BusinessRuleException(
                f"Négociation déjà clôturée (statut={auction.status}).",
                reason="already_closed",
            )

        auction.status = "CANCELLED"
        auction.cancelled_at = datetime.now(timezone.utc).replace(tzinfo=None)
        if reason:
            auction.cancellation_reason = str(reason)
        await current_session.flush()

        return {
            "status": "success",
            "negotiation_id": str(auction.id),
            "message": "❌ Négociation annulée.",
        }
