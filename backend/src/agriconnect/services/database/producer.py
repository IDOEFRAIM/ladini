from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Literal, Union
from decimal import Decimal

from sqlalchemy import select, update, delete, and_, or_, desc, func
from sqlalchemy.orm import selectinload, joinedload, load_only, with_loader_criteria, aliased
from sqlalchemy.ext.asyncio import AsyncSession

from agriconnect.domain.models import (
    Producer,
    Farm,
    Stock,
    StockMovement,
    User,
    Product,
    MarketOffer,
    Order,
    OrderItem,
    Client,
    Expense,
    SubCategory,
    Category,
    BuyerProfile,
)
from .base import BaseMixin
from .common import clean_text, positive_float, normalize_phone, clamp_limit

logger = logging.getLogger("agriconnect.services.producer_mgmt")

MovementType = Literal['IN', 'OUT', 'WASTE']


_BOOL_TRUE = {"1", "true", "on", "yes", "y", "oui", "vrai"}


def _as_uuid(value: Any, field: str) -> uuid.UUID:
    if value in (None, ""):
        raise ValueError(f"{field} est obligatoire")
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{field} doit être un UUID valide") from exc


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _BOOL_TRUE:
        return True
    if text in {"0", "false", "off", "no", "non", "n", "faux"}:
        return False
    return default


def _coerce_float(value: Any, field: str, *, allow_zero: bool = True, positive: bool = False, default: Optional[float] = None) -> Optional[float]:
    if value in (None, ""):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} doit être numérique") from exc
    if positive and parsed < 0:
        raise ValueError(f"{field} doit être positif")
    if not allow_zero and parsed == 0:
        raise ValueError(f"{field} ne peut pas être zéro")
    return parsed


def _parse_datetime(value: Any, field: str, *, required: bool = False) -> Optional[datetime]:
    if value in (None, ""):
        if required:
            raise ValueError(f"{field} est obligatoire")
        return None
    if isinstance(value, datetime):
        dt_value = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt_value = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"{field} doit être une date ISO (YYYY-MM-DD ou YYYY-MM-DDTHH:MM)") from exc
    if dt_value.tzinfo is None:
        dt_value = dt_value.replace(tzinfo=timezone.utc)
    return dt_value


def _utc_naive(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _normalize_offer_payload(payload: Dict[str, Any], *, is_future: bool = False) -> Dict[str, Any]:
    """Normalise un payload de déclaration d'offre marché (MarketOffer).

    ``is_future=True`` (déclaration de production FUTURE / précommandable) impose
    une sémantique distincte d'un produit catalogue livrable :
      - une date de disponibilité (``estimated_available_at`` ou
        ``expected_harvest_date``) est OBLIGATOIRE — une future récolte sans
        date fiable n'est pas précommandable ;
      - ``preorder_enabled`` et ``is_public`` par défaut à True (on veut exposer
        l'offre à la demande) ;
      - ``current_stock`` forcé à 0 : une production future n'a PAS de stock réel
        (ne pas recycler ``available_quantity`` comme si c'était du disponible) ;
      - ``status`` par défaut ``AVAILABLE`` (visible/réservable), pas ``DRAFT``.
    """
    if not isinstance(payload, dict):
        raise ValueError("Le payload doit être un objet JSON")

    normalized: Dict[str, Any] = {}
    normalized["farm_id"] = _as_uuid(payload.get("farm_id"), "farm_id")

    production_type = str(payload.get("production_type") or payload.get("type") or "CROP").strip().upper()
    if production_type not in {"CROP", "LIVESTOCK"}:
        raise ValueError("production_type doit valoir 'CROP' ou 'LIVESTOCK'")
    normalized["production_type"] = production_type

    # product_label : nom commercial de l'offre (ex-crop_type)
    product_label = (
        payload.get("product_label")
        or payload.get("crop_type")
        or payload.get("product")
        or payload.get("culture")
        or (payload.get("species") if production_type == "LIVESTOCK" else None)
    )
    if not product_label:
        raise ValueError("product_label (nom du produit) est obligatoire")
    normalized["product_label"] = str(product_label).strip()

    normalized["species"] = payload.get("species") or None
    normalized["breed"] = payload.get("breed") or payload.get("race") or None
    normalized["unit"] = str(payload.get("unit") or payload.get("unit_mentioned") or ("HEAD" if production_type == "LIVESTOCK" else "KG")).upper()

    available_qty = (
        payload.get("available_quantity")
        or payload.get("quantity")
        or payload.get("quantity_mentioned")
        or 0.0
    )
    normalized["available_quantity"] = positive_float(available_qty, "available_quantity", allow_zero=True)
    normalized["reserved_quantity"] = _coerce_float(payload.get("reserved_quantity"), "reserved_quantity", allow_zero=True, positive=True, default=0.0) or 0.0
    # Une production FUTURE n'a pas de stock réel : ne pas recopier available_quantity.
    _stock_default = 0.0 if is_future else normalized["available_quantity"]
    normalized["current_stock"] = _coerce_float(payload.get("current_stock"), "current_stock", allow_zero=True, positive=True, default=_stock_default)

    _raw_price = _coerce_float(payload.get("price_per_unit") or payload.get("price_mentioned"), "price_per_unit", allow_zero=True, positive=True)
    normalized["price_per_unit"] = round(_raw_price, 2) if _raw_price is not None else None

    normalized["preorder_enabled"] = _coerce_bool(payload.get("preorder_enabled"), default=is_future)
    normalized["is_public"] = _coerce_bool(payload.get("is_public"), default=is_future)
    normalized["status"] = str(payload.get("status") or ("AVAILABLE" if is_future else "DRAFT")).strip().upper()

    normalized["estimated_available_at"] = _parse_datetime(payload.get("estimated_available_at"), "estimated_available_at")
    normalized["expected_harvest_date"] = _parse_datetime(payload.get("expected_harvest_date"), "expected_harvest_date")
    if normalized["estimated_available_at"] is None:
        normalized["estimated_available_at"] = normalized["expected_harvest_date"]

    # ETA obligatoire pour une production future : sans date fiable, pas de précommande.
    if is_future and normalized["estimated_available_at"] is None:
        raise ValueError(
            "Une date de disponibilité prévue est requise pour une production future "
            "(estimated_available_at ou expected_harvest_date)."
        )

    normalized["sub_category_id"] = payload.get("sub_category_id")
    return normalized


def _offer_to_payload(offer: MarketOffer, farm: Optional[Farm] = None) -> Dict[str, Any]:
    def _iso(dt_value: Optional[datetime]) -> Optional[str]:
        return dt_value.isoformat() if isinstance(dt_value, datetime) else None

    farm_name = farm.name if farm else getattr(getattr(offer, "farm", None), "name", None)
    farm_id = str(farm.id) if farm else str(getattr(offer, "farm_id", ""))

    return {
        "offer_id": str(offer.id),
        "farm_id": farm_id,
        "farm_name": farm_name or "Ferme",
        "product_label": offer.product_label,
        "production_type": (offer.production_type or "CROP").upper(),
        "species": offer.species,
        "breed": offer.breed,
        "status": (offer.status or "DRAFT").upper(),
        "available_quantity": float(offer.available_quantity) if offer.available_quantity is not None else None,
        "reserved_quantity": float(offer.reserved_quantity) if offer.reserved_quantity is not None else 0.0,
        "current_stock": float(offer.current_stock) if offer.current_stock is not None else None,
        "unit": (offer.unit or "KG").upper(),
        "price_per_unit": float(offer.price_per_unit) if offer.price_per_unit is not None else None,
        "preorder_enabled": bool(offer.preorder_enabled),
        "is_public": bool(offer.is_public),
        "estimated_available_at": _iso(offer.estimated_available_at),
        "expected_harvest_date": _iso(offer.expected_harvest_date),
        "display_label": offer.product_label or offer.species or "Production",
    }


class ProducerMgmtMixin(BaseMixin):
    """
    Mixin centralisé pour la gestion des exploitations, des catalogues de produits,
    des finances et des flux de stocks transactionnels via agent MCP.
    """
    # ✅ LA SIGNATURE CORRIGÉE : Plus de paramètre "session" dans les parenthèses
    async def guess_category(self, product_name: str) -> str:
        """
        Analyse le nom d'un produit et interroge la base de données 
        via self.session pour trouver la catégorie parente correspondante.
        """
        if not product_name:
            return "AUTRES"

        name_clean = product_name.strip()

        try:
            # 1. Résolution dynamique via les sous-catégories existantes en BD
            stmt = (
                select(Category.name)
                .join(SubCategory, SubCategory.category_id == Category.id)
                .where(SubCategory.name.ilike(f"%{name_clean}%"))
                .limit(1)
            )
            category_name = await self.session.scalar(stmt)

            if category_name:
                return category_name.upper()

            # 2. Deuxième chance : Correspondance directe avec une catégorie principale
            stmt_alt = select(Category.name).limit(100)
            categories_res = await self.session.scalars(stmt_alt)
            all_categories = categories_res.all()

            for cat in all_categories:
                if cat.upper() in name_clean.upper():
                    return cat.upper()

        except Exception as e:
            logger.error(f"⚠️ Erreur lors de la résolution de la catégorie pour '{product_name}': {e}")

        # 3. Fallback de sécurité
        MAPPING_DE_SECOURS = {
            "CÉRÉALES": ["MAÏS", "RIZ", "MIL", "SORGHO", "FONIO"],
            "LÉGUMES": ["TOMATE", "OIGNON", "PIMENT", "CHOU", "GOMBO", "CAROTTE"],
            "FRUITS": ["MANGUE", "ORANGE", "CITRON", "BANANE", "PAPAYE"],
            "ANIMAUX": ["POULET", "BOEUF", "MOUTON", "CHÈVRE", "OEUF"]
        }

        for categorie, mots_cles in MAPPING_DE_SECOURS.items():
            if any(mot in name_clean.upper() for mot in mots_cles):
                return categorie

        return "AUTRES"

    # ─── SECTION 1 : GESTION DES FERMES (FARMS) ───────────────────────────
    async def get_or_create_farm(
        self,
        producer_id: str | None = None,
        farm_name: str = "Ma ferme",
        zone_id: str | None = None,
        *,
        phone: str | None = None,
    ) -> Dict[str, Any]:
        """
        Récupère la ferme existante d'un producteur ou en crée une nouvelle par défaut via self.session.
        """
        resolved_phone = await self._resolve_producer_phone(phone=phone, producer_id=producer_id)

        # 1. Résolution de l'identité complète (User + Producer)
        row = await self._fetch_user_entities(phone=resolved_phone)
        if not row:
            raise ValueError(f"Aucun utilisateur trouvé pour le numéro {resolved_phone}")
        
        user_obj, producer_obj, _, _, _ = row

        # 2. Création du Producer s'il n'existe pas encore
        if not producer_obj:
            logger.info("ℹ️ Profil producteur manquant pour l'utilisateur %s, création automatique...", user_obj.id)
            producer_obj = Producer(
                id=user_obj.id,
                user_id=user_obj.id,
                business_name=user_obj.name or "Mon Agrobusiness"
            )
            self.session.add(producer_obj)
            await self.session.flush()

        # 3. Recherche de la première ferme existante
        stmt = select(Farm).where(Farm.producer_id == producer_obj.id).limit(1)
        farm = (await self.session.execute(stmt)).scalars().first()
        
        if farm:
            logger.info("ℹ️ Ferme existante trouvée pour le producteur %s", resolved_phone)
            return farm.to_dict()

        # 4. Création d'une ferme par défaut si aucune n'existe
        farm = Farm(
            id=uuid.uuid4(), 
            name=farm_name, 
            producer_id=producer_obj.id, 
            zone_id=uuid.UUID(str(zone_id)) if zone_id else None
        )
        self.session.add(farm)
        await self.session.flush()
        await self.session.refresh(farm)
        
        logger.info("✅ Ferme créée automatiquement : %s pour le numéro %s", farm_name, resolved_phone)
        return farm.to_dict()


    async def create_farm(
        self,
        name: str,
        location: str = None,
        size: float = None,
        zone_id: str = None,
        *,
        producer_id: str | None = None,
        phone: str | None = None,
    ) -> Dict[str, Any]:
        """
        Crée explicitement une ferme via self.session en s'appuyant sur le profil existant.
        """
        phone = await self._resolve_producer_phone(phone=phone, producer_id=producer_id)
        name = clean_text(name, "name", required=True)

        row = await self._fetch_user_entities(phone=phone)
        if not row:
            raise ValueError(f"Impossible de créer une ferme : aucun utilisateur avec le numéro {phone}")

        user_obj, producer_obj, _, _, _ = row

        if not producer_obj:
            logger.info("Profil producteur manquant pour %s, creation automatique...", phone)
            producer_obj = Producer(
                id=user_obj.id,
                user_id=user_obj.id,
                business_name=user_obj.name or "Mon Agrobusiness"
            )
            self.session.add(producer_obj)
            await self.session.flush()

        new_farm = Farm(
            id=uuid.uuid4(),
            producer_id=producer_obj.id,
            name=name,
            location=location,
            size=positive_float(size),
            zone_id=uuid.UUID(str(zone_id)) if zone_id else None
        )
        self.session.add(new_farm)
        await self.session.flush()
        await self.session.refresh(new_farm)
        return new_farm.to_dict()

    async def update_farm(
        self,
        phone: str | None = None,
        farm_id: str | None = None,
        *,
        producer_id: str | None = None,
        **kwargs,
    ) -> Optional[Dict[str, Any]]:
        """
        Met à jour dynamiquement les attributs d'une ferme.
        Prend farm_id en priorité, sinon cherche la première ferme du producteur.
        """
        try:
            # 1. Identification de la ferme
            resolved_phone: str | None = None
            if farm_id:
                stmt = select(Farm).where(Farm.id == uuid.UUID(str(farm_id)))
            else:
                resolved_phone = await self._resolve_producer_phone(phone=phone, producer_id=producer_id)
                row = await self._fetch_user_entities(phone=resolved_phone)
                if not row or not row[1]:
                    raise ValueError(f"Aucun profil producteur pour {resolved_phone}")
                stmt = select(Farm).where(Farm.producer_id == row[1].id).limit(1)
            
            farm = (await self.session.execute(stmt)).scalars().first()
            if not farm:
                identity = resolved_phone or phone or "<inconnu>"
                logger.warning("Ferme non trouvée pour %s / %s", identity, farm_id)
                return None

            # 2. Mise à jour dynamique des champs autorisés
            PROTECTED = ("id", "producer_id", "created_at", "updated_at")
            for key, value in kwargs.items():
                if key not in PROTECTED and hasattr(farm, key):
                    if key == "zone_id" and value:
                        setattr(farm, key, uuid.UUID(str(value)))
                    elif key == "size":
                        setattr(farm, key, positive_float(value))
                    else:
                        setattr(farm, key, value)

            await self.session.flush()
            await self.session.refresh(farm)
            return farm.to_dict()

        except Exception as e:
            logger.error(f"Erreur critique lors de l'update de la ferme ({phone}): {e}", exc_info=True)
            raise

    async def get_farms(
        self,
        producer_id: str | None = None,
        *,
        phone: str | None = None,
    ) -> Dict[str, Any]:
        """Retourne la liste structurée des fermes du producteur en résolvant l'identité."""

        try:
            resolved_phone = await self._resolve_producer_phone(phone=phone, producer_id=producer_id)
        except ValueError as exc:
            return {"status": "error", "message": str(exc), "data": []}

        farms_response = await self.get_producer_farm(resolved_phone)
        if isinstance(farms_response, dict):
            return farms_response

        # Sécurise le format si la méthode de base évolue vers une liste brute.
        return {
            "status": "success",
            "count": len(farms_response),
            "data": farms_response,
        }

    # ─── SECTION 2 : GESTION DU CATALOGUE PRODUITS ───────────────────────
    async def create_product(
            self, 
            name: str, 
            price: float, 
            quantity_for_sale: float, 
            unit: str = "KG", 
            category_label: str = None, 
            sub_category_id: str = None, 
            description: str = None, 
            local_names: dict = None,
            *,
            producer_id: str | None = None, 
            phone: str | None = None
        ) -> Dict[str, Any]:
            """
            Ajoute un produit au catalogue public de vente du producteur via self.session.
            """
            phone = await self._resolve_producer_phone(phone=phone, producer_id=producer_id)
            phone = clean_text(phone, "phone", required=True)
            name = clean_text(name, "name", required=True)
            price = positive_float(price, "price", allow_zero=True)
            quantity_for_sale = positive_float(quantity_for_sale, "quantity_for_sale", allow_zero=True)
            unit = clean_text(unit, "unit", required=False, max_length=20) or "KG"

            try:
                profile_res = await self.get_producer_profile(phone)
                
                if not profile_res or profile_res[0] is None:
                    logger.warning(f"⚠️ Échec create_product : Le numéro {phone} n'a pas de profil producteur.")
                    return {
                        "status": "error",
                        "message": f"Création impossible : Le numéro {phone} n'est rattaché à aucun compte producteur actif."
                    }
                
                user, producer = profile_res
                
                if not producer or not producer.id:
                    return {'status': 'error', 'message': "Profil producteur invalide ou corrompu. Pas d'id disponible."}
                    
                product_id = str(uuid.uuid4())
                short_code = product_id[:8].upper()
                
                if not sub_category_id:
                    sub_cat_stmt = select(SubCategory.id).where(SubCategory.name.ilike(f"%{name.strip()}%")).limit(1)
                    sub_category_id = await self.session.scalar(sub_cat_stmt)
                
                category_label = (
                    clean_text(category_label, "category_label", required=False) 
                    or await self.guess_category(product_name=name)
                )
                
                product = Product(
                    id=uuid.UUID(product_id), 
                    short_code=short_code, 
                    name=name.strip(), 
                    price=float(price), 
                    unit=unit.upper().strip(), 
                    quantity_for_sale=float(quantity_for_sale), 
                    producer_id=producer.id,
                    category_label=category_label, 
                    sub_category_id=uuid.UUID(str(sub_category_id)) if sub_category_id else None, 
                    description=description.strip() if description else None, 
                    local_names=local_names,
                    created_at=datetime.now(),
                    updated_at=datetime.now()
                )
                
                self.session.add(product)
                await self.session.flush()

                return {
                    "status": "success",
                    "product_id": product_id,
                    "short_code": short_code,
                    "message": f"🎉 Le produit *{name}* a été ajouté avec succès à votre catalogue de vente !",
                    "data": {
                        "product_id": product_id, 
                        "short_code": short_code, 
                        "name": name, 
                        "price_fcfa": price, 
                        "quantity": quantity_for_sale, 
                        "unit": unit,
                        "category_label": category_label
                    }
                }
                    
            except Exception as e:
                logger.error(f"❌ Erreur système dans create_product : {str(e)}")
                return {"status": "error", "message": f"Erreur technique lors du stockage du produit : {str(e)}"}

    async def list_products(self, producer_id: str | None = None, *, phone: str | None = None) -> Dict[str, Any]:
        """
        Vision Phone-First : Liste tous les produits via self.session.
        """
        try:
            clean_phone = await self._resolve_producer_phone(phone=phone, producer_id=producer_id)
            profile_res = await self.get_producer_profile(clean_phone)
            if not profile_res or profile_res[0] is None:
                return {"status": "error", "message": "Votre compte producteur n'est pas identifié."}
                
            user, producer = profile_res

            stmt = (
                select(Product)
                .where(Product.producer_id == producer.id)
                .order_by(desc(Product.created_at))
            )
            result = await self.session.execute(stmt)
            products = result.scalars().all()

            if not products:
                return {
                    "status": "success",
                    "count": 0,
                    "message": "📦 Votre catalogue est actuellement vide. Pour ajouter un produit, écrivez par exemple : 'Je veux vendre 50 kg de riz à 1200 CFA le kg'."
                }

            products_list = []
            menu_lines = ["📦 *Votre catalogue de produits en vente :*"]
            mapping_cache = {}

            for i, p in enumerate(products, start=1):
                price_val = float(p.price) if isinstance(p.price, Decimal) else p.price
                products_list.append({
                    "product_id": str(p.id),
                    "short_code": p.short_code,
                    "name": p.name,
                    "price": price_val,
                    "quantity": p.quantity_for_sale,
                    "unit": p.unit
                })

                line = (
                    f"\n*{i}. {p.name}* (Réf: #{p.short_code})\n"
                    f"💰 Prix : *{price_val} CFA/{p.unit}*\n"
                    f"⚖️ Stock dispo : {p.quantity_for_sale} {p.unit}"
                )
                menu_lines.append(line)
                mapping_cache[str(i)] = str(p.id)

            menu_lines.append("\n_Pour modifier un prix ou une quantité, mentionnez simplement le nom du produit ou son numéro._")

            return {
                "status": "success",
                "count": len(products_list),
                "formatted_menu": "\n".join(menu_lines),
                "mapping": mapping_cache,
                "data": products_list
            }

        except Exception as e:
            logger.error(f"❌ Erreur lors du listing des produits : {str(e)}")
            return {"status": "error", "message": "Impossible d'accéder à votre catalogue pour le moment."}

    # ─── SECTION 3 : LOGIQUE DU STOCK TRANSACTIONNEL ──────────────────────

    # NB : `add_stock`/`remove_stock`/`adjust_stock` vivent désormais UNIQUEMENT
    # dans `MarketplaceMixin` (services/database/marketplace.py) — signature
    # `farm_id` directe, cohérente avec l'architecture d'auto-résolution
    # `FARM_CRITICAL_GOALS` (le farm_id est déjà résolu avant l'appel du tool).
    # Ce mixin en définissait des doublons `phone`-based (repli sur la 1ʳᵉ ferme
    # du producteur) — dead code, jamais atteint via `AgriDatabaseService`
    # (le MRO liste `MarketplaceMixin` avant `ProducerMgmtMixin`). Voir
    # [[farm-autoprovision-critical-goals]].

    async def get_stocks(self, phone: str | None = None, *, producer_id: str | None = None, **kwargs) -> Dict[str, Any]:
        """Récupère les stocks, le catalogue et les futures récoltes d'un producteur."""

        try:
            clean_phone = await self._resolve_producer_phone(phone=phone, producer_id=producer_id)
            logger.debug(f"[get_stocks] Extraction des stocks pour le téléphone : {clean_phone}")

            producer_uuid: uuid.UUID | None = None
            if producer_id:
                try:
                    producer_uuid = uuid.UUID(str(producer_id))
                except (TypeError, ValueError):
                    producer_uuid = None

            if producer_uuid is None:
                profile_res = await self.get_producer_profile(clean_phone)
                if not profile_res or profile_res[0] is None:
                    raise ValueError("Profil producteur introuvable pour la consultation des stocks.")

                _user_obj, producer_obj = profile_res
                producer_uuid = getattr(producer_obj, "id", None)

            safe_catalog_limit = clamp_limit(kwargs.get("catalog_limit"), default=30, maximum=200)
            farm_limit = clamp_limit(kwargs.get("farm_limit"), default=20, maximum=50)
            stock_limit = clamp_limit(kwargs.get("stock_limit"), default=500, maximum=5000)
            cycle_limit = clamp_limit(kwargs.get("cycle_limit"), default=150, maximum=2000)
            date_cutoff = datetime.utcnow() - timedelta(days=1)
            inactive_statuses = ("HARVESTED", "CANCELLED", "ABANDONED")

            if producer_uuid:
                farm_stmt = (
                    select(Farm.id, Farm.name, Farm.location, Farm.size)
                    .where(Farm.producer_id == producer_uuid)
                    .limit(farm_limit)
                )
            else:
                farm_stmt = (
                    select(Farm.id, Farm.name, Farm.location, Farm.size)
                    .join(Farm.producer)
                    .join(Producer.user)
                    .where(User.phone == clean_phone)
                    .limit(farm_limit)
                )

            farm_rows = (await self.session.execute(farm_stmt)).all()
            farm_ids: list[uuid.UUID] = [row[0] for row in farm_rows if row and row[0] is not None]

            # Fallback: filtering farms by producer_id can miss them when the
            # profile resolves a different producer row than the one that owns
            # the farms. Retry via the reliable User.phone join (identical to
            # get_producer_farm) and realign producer_uuid so the catalog query
            # below targets the correct producer too.
            if not farm_rows:
                fallback_rows = (await self.session.execute(
                    select(Farm.id, Farm.name, Farm.location, Farm.size, Farm.producer_id)
                    .join(Farm.producer)
                    .join(Producer.user)
                    .where(User.phone == clean_phone)
                    .limit(farm_limit)
                )).all()
                if fallback_rows:
                    farm_rows = [(r[0], r[1], r[2], r[3]) for r in fallback_rows]
                    farm_ids = [r[0] for r in fallback_rows if r and r[0] is not None]
                    recovered_producer_id = next((r[4] for r in fallback_rows if r and r[4]), None)
                    if recovered_producer_id is not None:
                        producer_uuid = recovered_producer_id
                    logger.info(
                        "[get_stocks] Farm fallback via phone join recovered %d farm(s) for %s",
                        len(farm_rows), clean_phone,
                    )

            catalog_snapshot: List[Dict[str, Any]] = []
            if producer_uuid:
                product_stmt = (
                    select(Product)
                    .options(
                        load_only(
                            Product.id,
                            Product.name,
                            Product.short_code,
                            Product.price,
                            Product.unit,
                            Product.quantity_for_sale,
                            Product.is_available,
                            Product.updated_at,
                        )
                    )
                    .where(Product.producer_id == producer_uuid)
                    .order_by(desc(Product.updated_at))
                    .limit(safe_catalog_limit)
                )
                product_result = await self.session.execute(product_stmt)
                products = product_result.scalars().all()
                for prod in products:
                    price_val = float(prod.price) if isinstance(prod.price, Decimal) else prod.price
                    catalog_snapshot.append(
                        {
                            "product_id": str(prod.id),
                            "name": prod.name,
                            "short_code": prod.short_code,
                            "price": price_val,
                            "unit": prod.unit,
                            "quantity": prod.quantity_for_sale,
                            "status": "AVAILABLE" if getattr(prod, "is_available", True) else "HIDDEN",
                            "updated_at": prod.updated_at.isoformat() if getattr(prod, "updated_at", None) else None,
                        }
                    )

            global_cycles: List[Dict[str, Any]] = []

            if not farm_rows:
                return {
                    "status": "success",
                    "message": f"Aucune exploitation trouvée pour le numéro {clean_phone}.",
                    "data": {
                        "farms": {},
                        "catalog": catalog_snapshot,
                        "upcoming_cycles": [],
                    },
                }

            stocks_farm_by_farm: Dict[str, Dict[str, Any]] = {}
            farm_name_by_id: Dict[uuid.UUID, str] = {}
            farm_loc_by_id: Dict[uuid.UUID, str] = {}
            farm_size_by_id: Dict[uuid.UUID, float] = {}

            for farm_id, farm_name, farm_location, farm_size in farm_rows:
                farm_name_by_id[farm_id] = str(farm_name) if farm_name else "Ferme sans nom"
                farm_loc_by_id[farm_id] = str(farm_location) if farm_location else "Non spécifiée"
                farm_size_by_id[farm_id] = float(farm_size) if farm_size else 0.0
                stocks_farm_by_farm[str(farm_id)] = {
                    "farm_name": farm_name_by_id[farm_id],
                    "location": farm_loc_by_id[farm_id],
                    "size": farm_size_by_id[farm_id],
                    "stocks": [],
                    "upcoming_cycles": [],
                }

            if farm_ids:
                stock_stmt = (
                    select(Stock.id, Stock.farm_id, Stock.item_name, Stock.quantity, Stock.unit)
                    .where(Stock.farm_id.in_(farm_ids))
                    .order_by(Stock.farm_id, Stock.item_name)
                    .limit(stock_limit)
                )
                stock_rows = (await self.session.execute(stock_stmt)).all()
                for stock_id, farm_id, item_name, quantity, unit in stock_rows:
                    bucket = stocks_farm_by_farm.get(str(farm_id))
                    if bucket is None:
                        continue
                    bucket["stocks"].append(
                        {
                            "stock_id": str(stock_id),
                            "item_name": str(item_name or ""),
                            "quantity": float(quantity) if quantity else 0.0,
                            "unit": unit or "KG",
                        }
                    )

                offer_stmt = (
                    select(
                        MarketOffer.id,
                        MarketOffer.farm_id,
                        MarketOffer.product_label,
                        MarketOffer.production_type,
                        MarketOffer.species,
                        MarketOffer.breed,
                        MarketOffer.status,
                        MarketOffer.estimated_available_at,
                        MarketOffer.expected_harvest_date,
                        MarketOffer.available_quantity,
                        MarketOffer.reserved_quantity,
                        MarketOffer.current_stock,
                        MarketOffer.unit,
                        MarketOffer.price_per_unit,
                        MarketOffer.preorder_enabled,
                        MarketOffer.is_public,
                    )
                    .where(
                        MarketOffer.farm_id.in_(farm_ids),
                        MarketOffer.status.notin_(inactive_statuses),
                        or_(
                            MarketOffer.estimated_available_at.is_(None),
                            MarketOffer.estimated_available_at >= date_cutoff,
                            MarketOffer.expected_harvest_date >= date_cutoff,
                        ),
                    )
                    .limit(cycle_limit)
                )
                offer_rows = (await self.session.execute(offer_stmt)).all()

                for (
                    offer_id,
                    farm_id,
                    product_label,
                    production_type,
                    species,
                    breed,
                    status,
                    estimated_available_at,
                    expected_harvest_date,
                    available_quantity,
                    reserved_quantity,
                    current_stock,
                    unit,
                    price_per_unit,
                    preorder_enabled,
                    is_public,
                ) in offer_rows:
                    farm_name = farm_name_by_id.get(farm_id)
                    payload = {
                        "offer_id": str(offer_id),
                        "farm_id": str(farm_id),
                        "farm_name": farm_name or "Ferme",
                        "product_label": product_label,
                        "production_type": (production_type or "CROP").upper(),
                        "species": species,
                        "breed": breed,
                        "status": (status or "DRAFT").upper(),
                        "estimated_available_at": estimated_available_at.isoformat() if isinstance(estimated_available_at, datetime) else None,
                        "expected_harvest_date": expected_harvest_date.isoformat() if isinstance(expected_harvest_date, datetime) else None,
                        "available_quantity": float(available_quantity) if available_quantity is not None else None,
                        "reserved_quantity": float(reserved_quantity) if reserved_quantity is not None else 0.0,
                        "current_stock": float(current_stock) if current_stock is not None else None,
                        "unit": (unit or "KG").upper(),
                        "price_per_unit": float(price_per_unit) if price_per_unit is not None else None,
                        "preorder_enabled": bool(preorder_enabled),
                        "is_public": bool(is_public),
                        "display_label": product_label or species or "Production",
                    }
                    bucket = stocks_farm_by_farm.get(str(farm_id))
                    if bucket is not None:
                        bucket["upcoming_cycles"].append(payload)
                    global_cycles.append(payload)

            return {
                "status": "success",
                "message": f"Stocks de {len(farm_rows)} ferme(s) récupérés avec succès.",
                "data": {
                    "farms": stocks_farm_by_farm,
                    "catalog": catalog_snapshot,
                    "upcoming_cycles": global_cycles,
                },
            }

        except Exception as e:
            logger.error(f"❌ Erreur SQL de jointure dans get_stocks : {str(e)}", exc_info=True)
            raise e



    async def declare_future_production(
        self,
        payload: Dict[str, Any],
        *,
        phone: str | None = None,
        producer_id: str | None = None,
    ) -> Dict[str, Any]:
        """Déclare une production future (culture ou élevage) prête pour les précommandes."""

        resolved_phone = await self._resolve_producer_phone(phone=phone, producer_id=producer_id)
        normalized = _normalize_offer_payload(payload, is_future=True)

        farm = await self.session.get(Farm, normalized["farm_id"])
        if not farm:
            raise ValueError("Ferme introuvable pour l'identifiant fourni")

        user_row = await self._fetch_user_entities(resolved_phone)
        if not user_row or not user_row[1]:
            raise ValueError("Profil producteur introuvable pour ce numero")
        producer_obj = user_row[1]
        if farm.producer_id != producer_obj.id:
            raise ValueError("Cette exploitation n'appartient pas a votre profil producteur")

        offer = MarketOffer(
            id=uuid.uuid4(),
            farm_id=farm.id,
            producer_id=producer_obj.id,
            product_label=normalized["product_label"],
            production_type=normalized.get("production_type"),
            species=normalized.get("species"),
            breed=normalized.get("breed"),
            unit=normalized.get("unit"),
            available_quantity=normalized.get("available_quantity") or 0.0,
            reserved_quantity=normalized.get("reserved_quantity") or 0.0,
            current_stock=normalized.get("current_stock") or normalized.get("available_quantity") or 0.0,
            price_per_unit=normalized.get("price_per_unit"),
            preorder_enabled=normalized.get("preorder_enabled", False),
            is_public=normalized.get("is_public", False),
            status=normalized.get("status", "DRAFT"),
            estimated_available_at=_utc_naive(normalized.get("estimated_available_at")),
            expected_harvest_date=_utc_naive(normalized.get("expected_harvest_date")),
            sub_category_id=uuid.UUID(str(normalized["sub_category_id"])) if normalized.get("sub_category_id") else None,
        )

        self.session.add(offer)
        await self.session.flush()
        await self.session.refresh(offer)

        snapshot = _offer_to_payload(offer, farm)
        label = snapshot.get("display_label") or "production"
        return {
            "status": "success",
            "message": f"Offre '{label}' enregistree sur {snapshot.get('farm_name')}.",
            "data": snapshot,
        }


    async def get_offer_reservations(
        self,
        *,
        phone: str | None = None,
        producer_id: str | None = None,
        market_offer_id: str | None = None,
    ) -> Dict[str, Any]:
        """Liste les précommandes (réservations) reçues sur les productions futures.

        Vue producteur de la boucle de réservation : pour chaque offre future
        précommandable, agrège les Order(type=PREORDER) liés via market_offer_id,
        avec quantité réservée et acheteurs. Filtrable sur une offre précise.
        """
        try:
            resolved_phone = await self._resolve_producer_phone(phone=phone, producer_id=producer_id)
        except ValueError as exc:
            return {"status": "error", "message": str(exc), "data": []}

        profile_res = await self.get_producer_profile(resolved_phone)
        if not profile_res or not profile_res[1]:
            return {"status": "error", "message": "Profil producteur introuvable.", "data": []}
        producer_obj = profile_res[1]

        buyer_user = aliased(User)
        stmt = (
            select(
                MarketOffer.id.label("offer_id"),
                MarketOffer.product_label,
                MarketOffer.unit,
                MarketOffer.available_quantity,
                MarketOffer.reserved_quantity,
                MarketOffer.estimated_available_at,
                Order.id.label("order_id"),
                Order.total_amount,
                Order.status.label("order_status"),
                Order.created_at.label("reserved_at"),
                buyer_user.name.label("buyer_name"),
                buyer_user.phone.label("buyer_phone"),
            )
            .join(Order, Order.market_offer_id == MarketOffer.id)
            .outerjoin(BuyerProfile, Order.buyer_id == BuyerProfile.id)
            .outerjoin(buyer_user, BuyerProfile.user_id == buyer_user.id)
            .where(
                MarketOffer.producer_id == producer_obj.id,
                Order.order_type == "PREORDER",
            )
            .order_by(MarketOffer.estimated_available_at.asc(), Order.created_at.desc())
        )
        if market_offer_id:
            try:
                stmt = stmt.where(MarketOffer.id == uuid.UUID(str(market_offer_id)))
            except (TypeError, ValueError):
                return {"status": "error", "message": "Référence d'offre invalide.", "data": []}

        rows = (await self.session.execute(stmt)).all()

        if not rows:
            return {
                "status": "success",
                "count": 0,
                "data": [],
                "message": "Aucune précommande sur vos productions futures pour le moment.",
            }

        offers: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            oid = str(r.offer_id)
            bucket = offers.setdefault(oid, {
                "market_offer_id": oid,
                "product": r.product_label,
                "unit": (r.unit or "KG"),
                "available_quantity": float(r.available_quantity or 0.0),
                "reserved_quantity": float(r.reserved_quantity or 0.0),
                "eta": r.estimated_available_at.isoformat() if isinstance(r.estimated_available_at, datetime) else None,
                "reservation_count": 0,
                "reservations": [],
            })
            bucket["reservation_count"] += 1
            bucket["reservations"].append({
                "order_id": str(r.order_id),
                "buyer_name": r.buyer_name or "Acheteur",
                "buyer_phone": r.buyer_phone,
                "total": float(r.total_amount or 0.0),
                "status": (r.order_status or "PENDING"),
                "reserved_at": r.reserved_at.isoformat() if isinstance(r.reserved_at, datetime) else None,
            })

        data = list(offers.values())
        lines = ["📊 *Réservations sur vos productions futures :*"]
        for off in data:
            eta_txt = ""
            if off["eta"]:
                try:
                    eta_txt = f" · 📅 {datetime.fromisoformat(off['eta']).strftime('%d/%m/%Y')}"
                except ValueError:
                    pass
            lines.append(
                f"\n🌱 *{off['product']}*{eta_txt}\n"
                f"🔔 {off['reservation_count']} précommande(s) — "
                f"*{off['reserved_quantity']:g}/{off['available_quantity']:g} {off['unit']}* réservé(s)"
            )

        return {
            "status": "success",
            "count": len(data),
            "data": data,
            "formatted_menu": "\n".join(lines),
        }

    # ─── SECTION 4 : VISION COMMANDES PRODUCTEUR ───────────────────────
    async def get_producer_orders(
        self,
        *,
        phone: str | None = None,
        producer_id: str | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """Retourne les commandes associées au producteur (catalogue + précommandes)."""

        try:
            resolved_phone = await self._resolve_producer_phone(phone=phone, producer_id=producer_id)
        except ValueError as exc:
            return {"status": "error", "message": str(exc), "data": []}

        profile_res = await self.get_producer_profile(resolved_phone)
        if not profile_res or not profile_res[1]:
            return {"status": "error", "message": "Profil producteur introuvable pour ce numéro.", "data": []}

        producer_obj = profile_res[1]
        safe_limit = clamp_limit(limit, default=20, maximum=50)
        status_filter = (status or "").strip().upper()

        product_order_ids = (
            select(OrderItem.order_id)
            .join(Product, Product.id == OrderItem.product_id)
            .where(Product.producer_id == producer_obj.id)
            .distinct()
        )
        cycle_order_ids = (
            select(Order.id)
            .join(MarketOffer, MarketOffer.id == Order.market_offer_id)
            .join(Farm, Farm.id == MarketOffer.farm_id)
            .where(Farm.producer_id == producer_obj.id)
            .distinct()
        )

        candidate_ids = select(Order.id).where(
            or_(
                Order.id.in_(product_order_ids),
                Order.id.in_(cycle_order_ids),
            )
        )

        if status_filter:
            candidate_ids = candidate_ids.where(func.upper(Order.status) == status_filter)

        order_ids = (
            candidate_ids.order_by(desc(Order.created_at)).limit(safe_limit * 2)
        ).subquery()

        stmt = (
            select(Order)
            .options(
                selectinload(Order.items).selectinload(OrderItem.product),
                selectinload(Order.offer).selectinload(MarketOffer.farm),
            )
            .where(Order.id.in_(select(order_ids.c.id)))
            .order_by(desc(Order.created_at))
        )

        if status_filter:
            stmt = stmt.where(func.upper(Order.status) == status_filter)

        result = await self.session.execute(stmt)
        orders = result.scalars().unique().all()

        if not orders:
            message = (
                f"Aucune commande avec le statut '{status_filter}' n'a été trouvée."
                if status_filter
                else "Aucune commande n'a encore été passée auprès de vos produits."
            )
            return {"status": "success", "count": 0, "message": message, "data": []}

        status_icons = {
            "PENDING": "🟡",
            "CONFIRMED": "🟢",
            "COMPLETED": "✅",
            "DELIVERED": "✅",
            "IN_PROGRESS": "🔵",
            "CANCELLED": "❌",
            "FAILED": "❌",
        }

        def _fmt_amount(amount: float | None, currency: str | None = "XOF") -> str:
            if amount in (None, ""):
                return "—"
            pretty = f"{float(amount):,.0f}".replace(",", " ").replace(".", ",")
            return f"{pretty} {currency or 'XOF'}"

        payload: List[Dict[str, Any]] = []
        menu_lines = ["📦 *Vos commandes récentes :*"]
        mapping: Dict[str, str] = {}

        for idx, order in enumerate(orders, start=1):
            relevant_items: List[Dict[str, Any]] = []
            for item in order.items or []:
                product = item.product
                if not product or getattr(product, "producer_id", None) != producer_obj.id:
                    continue
                relevant_items.append(
                    {
                        "product_id": str(product.id),
                        "product_name": product.name,
                        "quantity": float(item.quantity or 0.0),
                        "unit": (product.unit or "UNITE").upper(),
                        "price_at_sale": float(item.price_at_sale or 0.0),
                    }
                )

            cycle_context = None
            if not relevant_items and order.offer and order.offer.farm:
                if order.offer.farm.producer_id == producer_obj.id:
                    cycle_label = order.offer.product_label or order.offer.species or "Production future"
                    cycle_context = {
                        "product_id": str(order.offer.id),
                        "product_name": cycle_label,
                        "quantity": float(order.offer.available_quantity or 0.0),
                        "unit": (order.offer.unit or "KG").upper(),
                        "source": "MARKET_OFFER",
                    }
                    relevant_items.append(cycle_context)

            status_value = (order.status or "PENDING").upper()
            status_label = f"{status_icons.get(status_value, '🧾')} {status_value.title()}"
            order_code = (order.whatsapp_id or str(order.id))[:8].upper()
            amount_label = _fmt_amount(order.total_amount, order.currency)
            buyer_label = order.customer_name or "Acheteur"
            buyer_phone = order.customer_phone or "-"
            source_type = "PREORDER" if order.market_offer_id else (order.order_type or "STANDARD").upper()

            summary_items = ", ".join(
                f"{item['product_name']} ({item['quantity']:.0f} {item['unit']})"
                for item in relevant_items
                if item.get("quantity") is not None
            )
            if not summary_items:
                summary_items = cycle_context["product_name"] if cycle_context else "—"

            menu_lines.append(
                f"\n*{idx}. {status_label}* · {amount_label}\n"
                f"{summary_items}\n"
                f"👤 {buyer_label} ({buyer_phone}) · Réf: #{order_code}"
            )
            mapping[str(idx)] = str(order.id)

            payload.append(
                {
                    "order_id": str(order.id),
                    "reference": order_code,
                    "status": status_value,
                    "delivery_status": (order.delivery_status or "PENDING").upper(),
                    "payment_status": (order.payment_status or "PENDING").upper(),
                    "created_at": order.created_at.isoformat() if order.created_at else None,
                    "total_amount": float(order.total_amount or 0.0),
                    "currency": order.currency or "XOF",
                    "buyer_name": buyer_label,
                    "buyer_phone": buyer_phone,
                    "order_type": source_type,
                    "items": relevant_items,
                }
            )

        return {
            "status": "success",
            "count": len(payload),
            "formatted_menu": "\n".join(menu_lines),
            "mapping": mapping,
            "data": payload,
        }


    async def update_production_visibility(
        self,
        cycle_id: str,
        *,
        phone: str | None = None,
        producer_id: str | None = None,
        is_public: bool | None = None,
        preorder_enabled: bool | None = None,
    ) -> Dict[str, Any]:
        """Permet d'activer/désactiver l'exposition d'un lot de production future."""

        resolved_phone = await self._resolve_producer_phone(phone=phone, producer_id=producer_id)
        cycle_uuid = _as_uuid(cycle_id, "cycle_id")

        stmt = (
            select(MarketOffer, Farm)
            .join(Farm, Farm.id == MarketOffer.farm_id)
            .where(MarketOffer.id == cycle_uuid)
        )
        result = await self.session.execute(stmt)
        row = result.first()
        if not row:
            raise ValueError("Cycle introuvable pour l'identifiant fourni")
        cycle, farm = row

        user_row = await self._fetch_user_entities(resolved_phone)
        if not user_row or not user_row[1]:
            raise ValueError("Profil producteur introuvable")
        if farm.producer_id != user_row[1].id:
            raise ValueError("Vous n'êtes pas autorisé à modifier cette production")

        if is_public is not None:
            cycle.is_public = bool(is_public)
        if preorder_enabled is not None:
            cycle.preorder_enabled = bool(preorder_enabled)

        await self.session.flush()
        snapshot = _offer_to_payload(cycle, farm)
        return {
            "status": "success",
            "message": "Visibilite mise a jour.",
            "data": snapshot,
        }



    async def get_producer_stocks(
        self,
        phone: Optional[str] = None,
        producer_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Retourne un inventaire aplati (liste d'items) pour l'agent conversationnel."""

        try:
            lookup_phone = await self._resolve_producer_phone(phone=phone, producer_id=producer_id)
        except ValueError:
            return {
                "status": "error",
                "message": "Identité producteur introuvable pour la consultation des stocks.",
                "data": [],
            }

        base_res = await self.get_stocks(phone=lookup_phone)
        if (base_res or {}).get("status") != "success":
            return base_res

        raw_data = base_res.get("data") or {}
        farms = raw_data.get("farms", {}) if isinstance(raw_data, dict) else {}
        if not isinstance(farms, dict):
            return base_res

        flattened: List[Dict[str, Any]] = []
        for farm_id, metadata in farms.items():
            stocks = metadata.get("stocks") or []
            if not isinstance(stocks, list):
                continue
            for entry in stocks:
                flattened.append(
                    {
                        "stock_id": entry.get("stock_id"),
                        "item_name": entry.get("item_name"),
                        "quantity": entry.get("quantity"),
                        "unit": entry.get("unit", "KG"),
                        "farm_id": farm_id,
                        "farm_name": metadata.get("farm_name"),
                        "location": metadata.get("location"),
                    }
                )

        return {
            "status": "success",
            "count": len(flattened),
            "data": flattened,
        }

    async def add_stock_movement(
        self, 
        phone: str, 
        stock_id: any, 
        mtype: str, 
        quantity: float, 
        reason: str = None
    ) -> any:
        """
        Enregistre un mouvement de stock granulaire (IN/OUT) avec vérification 
        stricte des permissions du producteur.
        """
        from sqlalchemy.orm import joinedload
        from agriconnect.domain.models import Stock, Farm, Producer, User, StockMovement

        # 1. Préparation de la requête avec verrou ciblé
        stmt = (
            select(Stock)
            .where(Stock.id == stock_id)
            .options(
                joinedload(Stock.farm)
                .joinedload(Farm.producer)
                .joinedload(Producer.user)
            )
            .with_for_update(of=Stock)
        )
        
        res = await self.session.execute(stmt)
        
        # 2. Extraction immédiate de l'objet unique
        stock_obj = res.unique().scalar_one_or_none()
        
        # 3. Barrière de sécurité et validation des droits d'accès
        if not stock_obj:
            raise ValueError("Stock introuvable.")
            
        if not stock_obj.farm or not stock_obj.farm.producer or not stock_obj.farm.producer.user:
            raise PermissionError("Structure de propriété du stock incomplète en base de données.")
            
        if stock_obj.farm.producer.user.phone != phone:
            raise PermissionError("Accès refusé au stock ciblé.")
            
        # 4. Application de la logique métier (Incrémentation ou Décrémentation)
        if mtype == "IN":
            stock_obj.quantity += quantity
        elif mtype == "OUT":
            if stock_obj.quantity < quantity:
                raise ValueError(f"Stock insuffisant. Disponible : {stock_obj.quantity} {stock_obj.unit}")
            stock_obj.quantity -= quantity
        else:
            raise ValueError("Type de mouvement invalide. Utilisez 'IN' ou 'OUT'.")
            
        # 5. Création de la ligne d'historique (Parfaitement alignée avec ton modèle BDD)
        movement = StockMovement(
            stock_id=stock_obj.id,
            type=mtype,
            quantity=quantity,
            reason=reason
        )
        
        self.session.add(movement)
        await self.session.flush() # Enregistre sans clore la transaction globale
        
        # À la fin de add_stock_movement :
        return {
            "status": "success",
            "data": {
                "stock_id": str(stock_obj.id),
                "item_name": stock_obj.item_name,
                "old_quantity": stock_obj.quantity - quantity if mtype == "IN" else stock_obj.quantity + quantity,
                "new_quantity": stock_obj.quantity,
                "unit": stock_obj.unit,
            },
        }

    # NB : `get_stock_movements` vit désormais UNIQUEMENT dans `MarketplaceMixin`
    # (services/database/marketplace.py) — dead code ici (jamais atteint via
    # AgriDatabaseService). Voir [[farm-autoprovision-critical-goals]].

    async def delete_stock(self, phone: str, stock_id: any) -> bool:
        from sqlalchemy.orm import joinedload
        from agriconnect.domain.models import Stock, Farm, Producer, User

        stmt = (
            select(Stock)
            .where(Stock.id == stock_id)
            .options(joinedload(Stock.farm).joinedload(Farm.producer).joinedload(Producer.user))
        )
        res = await self.session.execute(stmt)
        stock_obj = res.unique().scalar_one_or_none() # 🚀 On extrait TOUT DE SUITE

        if not stock_obj or not stock_obj.farm or not stock_obj.farm.producer or not stock_obj.farm.producer.user:
            raise ValueError("Stock introuvable ou droits insuffisants.")

        if stock_obj.farm.producer.user.phone != phone:
            raise ValueError("Stock introuvable ou droits insuffisants.")

        await self.session.delete(stock_obj)
        await self.session.flush()
        return True

    # ─── SECTION 4 : SUIVI COMMERCIAL & CRM (CLIENTS / ORDERS) ───────────

    async def update_order_status(self, order_id: str, new_status: str, payment_status: str = None) -> Optional[Dict[str, Any]]:
        """
        Met à jour de manière sécurisée le statut d'une commande via self.session.
        """
        stmt = select(Order).where(Order.id == uuid.UUID(order_id)).with_for_update()
        result = await self.session.execute(stmt)
        order = result.scalar_one_or_none()
        
        if not order:
            logger.warning(f"Commande introuvable : {order_id}")
            return None
            
        order.status = new_status
        if payment_status:
            order.payment_status = payment_status
            
        await self.session.flush()
        await self.session.refresh(order)
        return order.to_dict()

    async def get_or_create_client(
        self,
        name: str,
        client_phone: str,
        email: str = None,
        location: str = None,
        *,
        producer_id: str | None = None,
        phone: str | None = None,
    ) -> Dict[str, Any]:
        """
        Identifie ou ajoute un client via self.session.
        """
        name = clean_text(name, "name", required=True)
        client_phone = clean_text(client_phone, "client_phone", required=True, max_length=40)
        
        try:
            resolved_phone = await self._resolve_producer_phone(phone=phone, producer_id=producer_id)
            _, producer = await self.get_producer_profile(resolved_phone)

            stmt = select(Client).where(Client.producer_id == producer.id, Client.phone == client_phone)
            result = await self.session.execute(stmt)
            client = result.scalar_one_or_none()

            if client:
                return {"status": "success", "data": client.to_dict()}

            client = Client(id=uuid.uuid4(), name=name, phone=client_phone, email=email, location=location, producer_id=producer.id)
            self.session.add(client)
            await self.session.flush()
            await self.session.refresh(client)
            return {"status": "success", "data": client.to_dict()}
        except ValueError as e:
            return {"status": "error", "message": str(e)}

    async def get_clients(self, producer_id: str | None = None, *, phone: str | None = None) -> Union[List[Dict[str, Any]], Dict[str, str]]:
        """
        Liste les clients via self.session.
        """
        try:
            resolved_phone = await self._resolve_producer_phone(phone=phone, producer_id=producer_id)
            _, producer = await self.get_producer_profile(resolved_phone)
            stmt = select(Client).where(Client.producer_id == producer.id).order_by(desc(Client.total_spent))
            result = await self.session.execute(stmt)
            return {"status": "success", "data": [c.to_dict() for c in result.scalars()]}
        except ValueError as e:
            return {"status": "error", "message": str(e)}

    # ─── SECTION 5 : FINANCE & SUIVI AGRONOMIQUE (EXPENSES / CROPS / SURPLUS) ───
    # NB : `add_expense`/`get_expenses`/`get_expense_summary` vivent désormais
    # UNIQUEMENT dans `MarketplaceMixin` (services/database/marketplace.py) —
    # signature `farm_id` directe, cohérente avec l'architecture
    # `FARM_CRITICAL_GOALS`. Doublons `phone`-based ici — dead code, jamais
    # atteint via `AgriDatabaseService`. Voir [[farm-autoprovision-critical-goals]].

    async def get_market_offers(self, phone: str) -> Union[List[Dict[str, Any]], Dict[str, str]]:
        """Liste les offres de marché (productions futures) de la première ferme du producteur."""
        try:
            farms_payload = await self.get_producer_farm(phone)
            if isinstance(farms_payload, dict):
                farms_data = farms_payload.get("data") or []
            else:
                farms_data = farms_payload

            if not farms_data:
                msg = (farms_payload.get("message") if isinstance(farms_payload, dict) else None) or "Aucune exploitation trouvée pour ce producteur."
                return {"status": "error", "message": msg}

            first_farm = farms_data[0]
            if isinstance(first_farm, dict):
                raw_farm_id = first_farm.get("farm_id") or first_farm.get("id")
            else:
                raw_farm_id = getattr(first_farm, "id", None)
            if not raw_farm_id:
                return {"status": "error", "message": "Impossible de déterminer l'exploitation principale."}

            farm_id = uuid.UUID(str(raw_farm_id))
            stmt = select(MarketOffer).where(MarketOffer.farm_id == farm_id)
            result = await self.session.execute(stmt)
            return {"status": "success", "data": [c.to_dict() for c in result.scalars()]}
        except ValueError as e:
            return {"status": "error", "message": str(e)}
