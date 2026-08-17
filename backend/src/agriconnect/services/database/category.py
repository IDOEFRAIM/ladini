from __future__ import annotations

import logging
import unicodedata
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy import Numeric, cast, desc, func, select, update

from agriconnect.domain.models import (
    Category,
    Producer,
    Product,
    StandardPrice,
    SubCategory,
    User,
    Zone,
)

from .search import fuzzy_match, similarity_rank

logger = logging.getLogger("AgriConnect.DatabaseService.Public")


def normalize_text(text: str) -> str:
    """Nettoie le texte : minuscule, sans accents, sans espaces inutiles."""
    if not text:
        return ""
    text = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in text if unicodedata.category(c) != "Mn").strip()


def _is_confident_category_match(search_term: str, matched_name: str) -> bool:
    """Même garde-fou que `cart_service.py::_is_confident_product_match` —
    un match qui n'est QUE trigram (fuzzy_match, seuil bas volontaire pour
    tolérer les fautes de frappe) entre le terme cherché et le nom trouvé
    n'est fiable que si l'un contient littéralement l'autre. Sans ça, un
    produit hors catalogue (ex: "riz" si aucune sous-catégorie ne le
    référence) pouvait fuzzy-matcher une sous-catégorie sans rapport et
    laisser croire au prix d'un tout autre produit. Petite duplication
    volontaire (pas de dépendance DB → couche agent) — voir
    [[buyer-search-fuzzy-match-safety-2026-08]]."""

    def _norm(text: Any) -> str:
        t = str(text or "").lower().strip()
        t = t.replace("œ", "oe").replace("æ", "ae")
        t = unicodedata.normalize("NFKD", t)
        return "".join(c for c in t if not unicodedata.combining(c))

    term, name = _norm(search_term), _norm(matched_name)
    if not term or not name:
        return False
    return term in name or name in term


class PublicProductMixin:
    """PublicProductMixin - Catalogue Public Haute Performance.

    Exploite la hiérarchie Catégories > Sous-Catégories et le maillage territorial.
    Utilise dynamiquement l'infrastructure self.session du service principal.
    """

    # ─── SECTION 1 : EXPLORATION ET TAXONOMIE ──────────────────────────────

    async def get_public_categories(self) -> List[Dict[str, Any]]:
        """Récupère les catégories contenant des produits actifs via une sous-requête EXISTS."""
        current_session = self.session
        if not current_session:
            logger.error("[Public Catalog] Session de base de données indisponible.")
            return []

        try:
            # Sous-requête optimisée pour vérifier la présence de stock sans charger les lignes
            product_exists = (
                select(1)
                .join(SubCategory, Product.sub_category_id == SubCategory.id)
                .where(
                    SubCategory.category_id == Category.id,
                    Product.quantity_for_sale > 0,
                )
                .exists()
            )

            stmt = select(Category).where(product_exists).order_by(Category.name)
            result = await current_session.execute(stmt)
            categories = result.scalars().all()

            icon_map = {
                "legumes": "🥕",
                "cereales": "🌾",
                "animaux": "🐂",
                "elevage": "🐂",
                "transforme": "📦",
                "outils": "🚜",
                "materiel": "🚜",
            }

            return [
                {
                    "id": str(c.id),
                    "name": c.name,
                    "icon": icon_map.get(normalize_text(c.name), "📦"),
                }
                for c in categories
            ]
        except Exception as e:
            logger.error(f"Erreur dans get_public_categories: {e}", exc_info=True)
            return []

    async def get_available_products(self, limit: int = 15) -> List[Dict[str, Any]]:
        """Sous-catégories concrètement disponibles (stock actif OU prix
        standard admin renseigné) avec leur prix — pour CITER des PRODUITS
        précis (pas juste des catégories larges type "Céréales") quand on
        rejette une demande hors catalogue.

        Demande explicite utilisateur (2026-08-15) : "l'agent doit pouvoir
        citer les produits qui sont permis dans le catalogue et présenter les
        prix standard quand l'utilisateur en fait le besoin" — un simple nom
        de catégorie ("Céréales") n'est pas assez actionnable ; un nom de
        produit avec son prix l'est. Voir
        [[precommande-architecture-consolidation-2026-08]]."""
        current_session = self.session
        if not current_session:
            return []
        try:
            active_stock_exists = (
                select(1)
                .where(
                    Product.sub_category_id == SubCategory.id,
                    Product.quantity_for_sale > 0,
                )
                .exists()
            )
            standard_price_exists = (
                select(1)
                .where(StandardPrice.sub_category_id == SubCategory.id)
                .exists()
            )
            stmt = (
                select(SubCategory.id, SubCategory.name)
                .where(active_stock_exists | standard_price_exists)
                .order_by(SubCategory.name)
                .limit(limit)
            )
            rows = (await current_session.execute(stmt)).all()

            results: List[Dict[str, Any]] = []
            for row in rows:
                entry: Dict[str, Any] = {"name": row.name}
                standard = await self._get_standard_price(row.id)
                if standard:
                    entry["price"] = standard["standard_price"]
                    entry["unit"] = standard.get("standard_unit") or "KG"
                    entry["price_source"] = "admin"
                else:
                    avg_price = await current_session.scalar(
                        select(
                            func.round(cast(func.avg(Product.price), Numeric), 2)
                        ).where(
                            Product.sub_category_id == row.id,
                            Product.quantity_for_sale > 0,
                        )
                    )
                    if avg_price is not None:
                        entry["price"] = float(avg_price)
                        entry["price_source"] = "market_average"
                results.append(entry)
            return results
        except Exception as e:
            logger.error(f"Erreur dans get_available_products: {e}", exc_info=True)
            return []

    async def get_public_zones(self) -> List[Dict[str, Any]]:
        """Récupère les Zones administratives disposant de stocks réels."""
        current_session = self.session
        if not current_session:
            return []

        try:
            stmt = (
                select(Zone)
                .join(Producer, Producer.zone_id == Zone.id)
                .join(Product, Product.producer_id == Producer.id)
                .where(Product.quantity_for_sale > 0)
                .distinct()
                .order_by(Zone.name)
            )
            result = await current_session.execute(stmt)
            return [{"id": str(z.id), "name": z.name} for z in result.scalars().all()]
        except Exception as e:
            logger.error(f"Erreur dans get_public_zones: {e}", exc_info=True)
            return []

    # ─── SECTION 2 : RECHERCHE ET REQUÊTES CATALOGUE ───────────────────────

    async def get_public_products(
        self,
        category_id: Optional[str] = None,
        zone_id: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Récupère les produits filtrés avec résolution des filtrages par Zone et Catégorie."""
        current_session = self.session
        if not current_session:
            return {
                "items": [],
                "total": 0,
                "status": "error",
                "message": "Session indisponible.",
            }

        try:
            stmt = select(Product).where(Product.quantity_for_sale > 0)

            # Application adaptative du filtre de zone géographique
            if zone_id and str(zone_id).strip().lower() != "all":
                try:
                    clean_zone = str(zone_id).strip()
                    z_uuid = uuid.UUID(clean_zone) if len(clean_zone) == 36 else zone_id
                    stmt = stmt.join(
                        Producer, Product.producer_id == Producer.id
                    ).where(Producer.zone_id == z_uuid)
                except ValueError:
                    pass

            # Application du filtre par taxonomie ou label textuel
            if category_id and str(category_id).strip().lower() != "all":
                try:
                    clean_cat = str(category_id).strip()
                    u_id = uuid.UUID(clean_cat) if len(clean_cat) == 36 else category_id
                    stmt = stmt.where(Product.sub_category_id == u_id)
                except ValueError:
                    stmt = stmt.where(Product.category_label.ilike(f"%{clean_cat}%"))

            # Recherche floue trigram (tolérante aux fautes : « tomte » → « tomate »).
            # Remplace l'ancien ILIKE substring strict qui bloquait l'agent sur la
            # moindre faute de frappe. Tri par pertinence quand un terme est fourni.
            search_term = str(search).strip() if search else ""
            if search_term:
                stmt = stmt.where(fuzzy_match(Product.name, search_term))
                stmt = (
                    stmt.order_by(similarity_rank(Product.name, search_term))
                    .limit(limit)
                    .offset(offset)
                )
            else:
                stmt = (
                    stmt.order_by(desc(Product.created_at)).limit(limit).offset(offset)
                )
            result = await current_session.execute(stmt)
            products = result.scalars().all()

            items = [
                {
                    "id": str(p.id),
                    "name": p.name,
                    "price": float(p.price)
                    if isinstance(p.price, Decimal)
                    else p.price,
                    "unit": str(p.unit).upper(),
                    "stock": p.quantity_for_sale,
                    "category_label": p.category_label,
                    "images": p.images if p.images else [],
                }
                for p in products
            ]

            return {"items": items, "total": len(items), "status": "success"}
        except Exception as e:
            logger.error(f"Erreur get_public_products: {str(e)}", exc_info=True)
            return {"items": [], "total": 0, "message": str(e), "status": "error"}

    async def get_market_snapshot(
        self,
        zone_query: Optional[str] = None,
        product_query: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Analyse globale des stocks et des prix moyens du marché par
        sous-catégorie, ou — si `product_query` est fourni — le prix de
        référence d'UN produit précis.

        Incident réel (2026-08-15) : sans `product_query`, "quel est le prix
        du riz ?" renvoyait TOUT le catalogue (aucun filtre produit
        n'existait), et le rendu générique affichait des lignes vides
        ("Élément 1"/"Élément 2") faute de savoir lire la clé `product`.
        Autre demande explicite du même retour : si le produit demandé n'est
        pas dans notre catalogue, le dire clairement et lister les vraies
        catégories disponibles plutôt que de laisser deviner. Voir
        [[precommande-architecture-consolidation-2026-08]].

        Ordre de préférence du prix quand `product_query` matche une
        sous-catégorie confiante (voir `_is_confident_category_match`) :
        1. `StandardPrice` (prix de référence saisi par un admin), si présent
           — pour la zone demandée si elle matche, sinon n'importe laquelle.
        2. Moyenne des prix des produits réellement en vente pour cette
           sous-catégorie (comportement historique de cette méthode).
        """
        current_session = self.session
        if not current_session:
            return {"status": "error", "message": "Session indisponible."}

        try:
            matched_sub_category_id = None
            matched_sub_category_name = None

            if product_query and product_query.strip():
                clean_product = product_query.strip()
                candidate_stmt = (
                    select(SubCategory.id, SubCategory.name)
                    .where(fuzzy_match(SubCategory.name, clean_product))
                    .order_by(similarity_rank(SubCategory.name, clean_product))
                    .limit(1)
                )
                candidate = (await current_session.execute(candidate_stmt)).first()
                if not candidate or not _is_confident_category_match(
                    clean_product, candidate.name
                ):
                    categories = await self.get_public_categories()
                    products = await self.get_available_products()
                    message = (
                        f"« {clean_product} » n'est pas encore disponible sur notre "
                        "plateforme."
                    )
                    if products:
                        message += " Voici des produits que vous pouvez commander dès maintenant :"
                    elif categories:
                        message += (
                            " Seuls les produits des catégories ci-dessous peuvent y être "
                            "vendus/achetés."
                        )
                    return {
                        "status": "success",
                        "product_in_catalog": False,
                        "message": message,
                        "available_categories": categories,
                        "available_products": products,
                    }
                matched_sub_category_id = candidate.id
                matched_sub_category_name = candidate.name

            avg_price_rounded = func.round(
                cast(func.avg(Product.price), Numeric), 2
            ).label("avg_price")

            stmt = (
                select(
                    SubCategory.id.label("sub_category_id"),
                    SubCategory.name.label("product"),
                    func.sum(Product.quantity_for_sale).label("total_stock"),
                    func.min(Product.price).label("min_price"),
                    avg_price_rounded,
                )
                .join(Product, Product.sub_category_id == SubCategory.id)
                .where(Product.quantity_for_sale > 0)
            )

            if zone_query and zone_query.strip():
                zone_query_clean = zone_query.strip()
                stmt = (
                    stmt.join(Producer, Product.producer_id == Producer.id)
                    .join(Zone, Producer.zone_id == Zone.id)
                    # Référentiel logistique : recherche floue trigram.
                    .where(fuzzy_match(Zone.name, zone_query_clean))
                )

            if matched_sub_category_id is not None:
                stmt = stmt.where(SubCategory.id == matched_sub_category_id)

            stmt = stmt.group_by(SubCategory.id, SubCategory.name).order_by(
                SubCategory.name
            )
            result = await current_session.execute(stmt)
            rows = result.all()

            if not rows:
                if matched_sub_category_id is not None:
                    # La sous-catégorie existe (produit reconnu du catalogue)
                    # mais personne n'a encore de stock à vendre — distinct
                    # de "produit hors catalogue" : on le dit clairement.
                    standard = await self._get_standard_price(
                        matched_sub_category_id, zone_query
                    )
                    if standard:
                        return {
                            "status": "success",
                            "product_in_catalog": True,
                            "zone": zone_query or "Toutes zones",
                            "data": [
                                {
                                    "product": matched_sub_category_name,
                                    "total_stock": 0,
                                    **standard,
                                }
                            ],
                        }
                    return {
                        "status": "success",
                        "product_in_catalog": True,
                        "data": [],
                        "message": (
                            f"« {matched_sub_category_name} » est un produit reconnu, mais "
                            "aucun producteur n'a de stock disponible pour l'instant "
                            f"({'zone ' + zone_query if zone_query else 'toutes zones confondues'})."
                        ),
                    }
                return {
                    "status": "success",
                    "data": [],
                    "message": f"Aucun stock disponible pour la zone '{zone_query or 'Toutes'}'",
                }

            clean_rows = []
            for row in rows:
                r_map = dict(row._mapping)
                r_map.pop("sub_category_id", None)
                if isinstance(r_map["min_price"], Decimal):
                    r_map["min_price"] = float(r_map["min_price"])
                if isinstance(r_map["avg_price"], Decimal):
                    r_map["avg_price"] = float(r_map["avg_price"])
                clean_rows.append(r_map)

            if matched_sub_category_id is not None:
                standard = await self._get_standard_price(
                    matched_sub_category_id, zone_query
                )
                if standard:
                    clean_rows[0].update(standard)
                else:
                    clean_rows[0]["price_source"] = "market_average"

            return {
                "status": "success",
                "product_in_catalog": True
                if matched_sub_category_id is not None
                else None,
                "zone": zone_query or "Toutes zones",
                "data": clean_rows,
            }
        except Exception as e:
            logger.error(f"Erreur get_market_snapshot: {str(e)}", exc_info=True)
            return {"status": "error", "message": str(e)}

    async def _get_standard_price(
        self,
        sub_category_id: Any,
        zone_query: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Prix de référence saisi par un admin (`StandardPrice`) pour cette
        sous-catégorie — priorité à la zone demandée si elle matche,
        sinon n'importe quelle zone où un admin en a renseigné un."""
        current_session = self.session
        if not current_session:
            return None
        stmt = (
            select(
                StandardPrice.price_per_unit,
                StandardPrice.unit,
                Zone.name.label("zone_name"),
            )
            .join(Zone, StandardPrice.zone_id == Zone.id)
            .where(StandardPrice.sub_category_id == sub_category_id)
        )
        if zone_query and zone_query.strip():
            zoned_stmt = stmt.where(fuzzy_match(Zone.name, zone_query.strip()))
            row = (await current_session.execute(zoned_stmt)).first()
            if row:
                return {
                    "standard_price": float(row.price_per_unit),
                    "standard_unit": row.unit,
                    "standard_price_zone": row.zone_name,
                    "price_source": "admin",
                }
        row = (await current_session.execute(stmt.order_by(Zone.name).limit(1))).first()
        if not row:
            return None
        return {
            "standard_price": float(row.price_per_unit),
            "standard_unit": row.unit,
            "standard_price_zone": row.zone_name,
            "price_source": "admin",
        }

    async def get_related_products(
        self, product_id: str, limit: int = 4
    ) -> List[Dict[str, Any]]:
        """Trouve les produits de même variété (sous-catégorie) hors produit courant."""
        current_session = self.session
        if not current_session:
            return []

        try:
            p_uuid = (
                uuid.UUID(product_id) if isinstance(product_id, str) else product_id
            )

            sub_cat_id = await current_session.scalar(
                select(Product.sub_category_id).where(Product.id == p_uuid)
            )
            if not sub_cat_id:
                return []

            stmt = (
                select(Product)
                .where(
                    Product.sub_category_id == sub_cat_id,
                    Product.id != p_uuid,
                    Product.quantity_for_sale > 0,
                )
                .limit(limit)
            )
            result = await current_session.execute(stmt)
            return [
                self._format_public_product_minimal(p) for p in result.scalars().all()
            ]
        except Exception as e:
            logger.error(
                f"Erreur get_related_products pour {product_id}: {e}", exc_info=True
            )
            return []

    async def get_voice_catalog(self, category_id: str) -> List[Dict[str, Any]]:
        """Catalogue optimisé pour les moteurs IVR ou les notes vocales WhatsApp."""
        current_session = self.session
        if not current_session:
            return []

        try:
            c_uuid = (
                uuid.UUID(category_id) if isinstance(category_id, str) else category_id
            )
            stmt = select(Product).where(
                Product.sub_category_id == c_uuid,
                Product.audio_url.isnot(None),
                Product.quantity_for_sale > 0,
            )
            result = await current_session.execute(stmt)
            return [
                self._format_public_product_minimal(p) for p in result.scalars().all()
            ]
        except Exception as e:
            logger.error(f"Erreur get_voice_catalog: {e}", exc_info=True)
            return []

    async def get_quick_contact_link(self, product_id: str) -> Optional[str]:
        """Génère le lien Click-to-Chat WhatsApp direct avec le producteur."""
        current_session = self.session
        if not current_session:
            return None

        try:
            p_uuid = (
                uuid.UUID(product_id) if isinstance(product_id, str) else product_id
            )
            stmt = (
                select(User.phone)
                .join(Producer, Producer.user_id == User.id)
                .join(Product, Product.producer_id == Producer.id)
                .where(Product.id == p_uuid)
            )
            phone = await current_session.scalar(stmt)
            return f"https://wa.me/{phone}" if phone else None
        except Exception as e:
            logger.error(
                f"Erreur get_quick_contact_link pour {product_id}: {e}", exc_info=True
            )
            return None

    # ─── SECTION 3 : GÉOLOCALISATION ET ESTIMATIONS (PLANAR / PYTHAGORE) ───

    async def search_by_proximity(
        self, lat: float, lng: float, radius_km: int = 50
    ) -> List[Dict[str, Any]]:
        """Recherche par rayon géographique via formule plane optimisée (Pas de double calcul)."""
        current_session = self.session
        if not current_session:
            return []

        try:
            distance_sq_formula = func.pow(
                (User.latitude - lat) * 111.12, 2
            ) + func.pow((User.longitude - lng) * 111.12, 2)

            stmt = (
                select(Product)
                .join(Producer, Product.producer_id == Producer.id)
                .join(User, Producer.user_id == User.id)
                .where(
                    Product.quantity_for_sale > 0, distance_sq_formula <= radius_km**2
                )
                .order_by(distance_sq_formula.asc())
            )

            result = await current_session.execute(stmt)
            return [
                self._format_public_product_minimal(p) for p in result.scalars().all()
            ]
        except Exception as e:
            logger.error(
                f"Erreur search_by_proximity (lat: {lat}, lng: {lng}): {e}",
                exc_info=True,
            )
            return []

    async def get_delivery_estimate(
        self, buyer_lat: float, buyer_lng: float, product_id: str
    ) -> Dict[str, Any]:
        """Estime la distance et calcule la projection financière des frais logistiques."""
        current_session = self.session
        if not current_session:
            return {"status": "error", "message": "Session indisponible."}

        try:
            p_uuid = (
                uuid.UUID(product_id) if isinstance(product_id, str) else product_id
            )

            distance_km_formula = func.sqrt(
                func.pow((User.latitude - buyer_lat) * 111.12, 2)
                + func.pow((User.longitude - buyer_lng) * 111.12, 2)
            ).label("distance_km")

            stmt = (
                select(Product.name, distance_km_formula)
                .join(Producer, Product.producer_id == Producer.id)
                .join(User, Producer.user_id == User.id)
                .where(Product.id == p_uuid)
            )

            result = await current_session.execute(stmt)
            row = result.fetchone()

            if not row:
                return {
                    "status": "error",
                    "message": "Produit ou producteur introuvable",
                }

            distance_km = round(float(row.distance_km), 2)
            # Règle métier logistique : 100 FCFA par Kilomètre
            estimated_cost = round(distance_km * 100, 0)

            return {
                "status": "success",
                "product_name": row.name,
                "distance_km": distance_km,
                "estimated_delivery_fee": float(estimated_cost),
                "currency": "XOF",
            }
        except Exception as e:
            logger.error(f"Erreur get_delivery_estimate: {e}", exc_info=True)
            return {"status": "error", "message": str(e)}

    async def bind_user_to_zone(
        self, user_id: str, lat: float, lng: float
    ) -> Optional[str]:
        """Associe l'utilisateur à la Zone la plus proche de ses coordonnées GPS actuelles."""
        current_session = self.session
        if not current_session:
            return None

        try:
            u_uuid = uuid.UUID(user_id) if isinstance(user_id, str) else user_id

            zone_distance_sq = func.pow((Zone.latitude - lat) * 111.12, 2) + func.pow(
                (Zone.longitude - lng) * 111.12, 2
            )
            stmt_zone = select(Zone.id).order_by(zone_distance_sq.asc()).limit(1)
            target_zone_id = await current_session.scalar(stmt_zone)

            update_values = {
                "latitude": lat,
                "longitude": lng,
                "updated_at": datetime.now(timezone.utc).replace(tzinfo=None),
            }
            if target_zone_id:
                update_values["zone_id"] = target_zone_id

            await current_session.execute(
                update(User).where(User.id == u_uuid).values(**update_values)
            )
            await current_session.flush()
            return str(target_zone_id) if target_zone_id else None
        except Exception as e:
            logger.error(f"Erreur bind_user_to_zone pour {user_id}: {e}", exc_info=True)
            return None

    # ─── SECTION 4 : FORMATAGE INTERNE ─────────────────────────────────────

    def _format_public_product_minimal(self, p: Product) -> Dict[str, Any]:
        """Formateur défensif unifié pour le catalogue public."""
        return {
            "id": str(p.id),
            "name": p.name,
            "price": float(p.price) if isinstance(p.price, Decimal) else p.price,
            "unit": str(p.unit or "KG").upper(),
            "category_label": p.category_label,
            "stock": p.quantity_for_sale,
        }
