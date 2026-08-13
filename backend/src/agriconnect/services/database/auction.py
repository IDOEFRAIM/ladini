from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta
import logging
import re
import unicodedata
import uuid
from rapidfuzz import fuzz, process
from sqlalchemy import select, and_, or_, func, update, exists
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased
from agriconnect.core.formatting import fmt_num as _fmt_num
from .common import clean_text, normalize_phone
from .search import fuzzy_match, similarity_rank
from .errors import BusinessRuleException
from .moderation import _fold as _fold_for_moderation

# Importation stricte des modèles requis pour le domaine des enchères
from agriconnect.domain.models import Auction, Bid, SubCategory, Zone, BuyerProfile, User,Producer, Order, Product, Category
from .base import BaseMixin

logger = logging.getLogger("agriconnect.services.database.auction")

_FUZZY_SUBCATEGORY_THRESHOLD = 78
_MAX_PHOTOS_PER_LOT = 8  # même plafond que Product.images (product.py::_MAX_PHOTOS_PER_PRODUCT)


def _normalize_product_label(value: str) -> str:
    """Low-tech normalizer to align plural/diacritics variations for product names."""
    if value is None:
        return ""
    text = str(value).strip().lower()
    if not text:
        return ""
    text = text.replace("œ", "oe").replace("æ", "ae")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    tokens: List[str] = []
    for token in text.split():
        cleaned = "".join(ch for ch in token if ch.isalnum())
        if len(cleaned) > 3 and cleaned.endswith("s"):
            cleaned = cleaned[:-1]
        tokens.append(cleaned)
    return " ".join(t for t in tokens if t)

class AuctionMixin(BaseMixin):
    """
    AuctionMixin - Moteur exclusif du cycle de vie des Enchères et Appels d'Offres.
    
    ZÉRO FUITE DE RESPONSABILITÉ :
    - La gestion, le listing et le requêtage brut des profils (Acheteurs/Producteurs) 
      sont délégués aux mixins spécialisés de la classe hôte via 'self'.
    - Ce mixin ne contient que la logique métier liée aux tables `Auction` et `Bid`.
    """

    # ─── SECTION 1 : LOGIQUE DE RÉSOLUTION AUXILIAIRE ────────────────────
    async def _fuzzy_match_sub_category(self, product_query: str) -> Optional[SubCategory]:
        """Fallback when the exact ILIKE fails (handles accents, plurals, typos)."""
        current_session = self.session
        if not current_session:
            return None

        normalized_query = _normalize_product_label(product_query)
        if not normalized_query:
            return None

        rows: List[SubCategory] = (await current_session.execute(select(SubCategory))).scalars().all()
        if not rows:
            return None

        normalized_index: Dict[str, SubCategory] = {}
        for sub_cat in rows:
            normalized = _normalize_product_label(sub_cat.name)
            if not normalized:
                continue
            normalized_index.setdefault(normalized, sub_cat)

        direct_match = normalized_index.get(normalized_query)
        if direct_match:
            return direct_match

        match = process.extractOne(
            normalized_query,
            list(normalized_index.keys()),
            scorer=fuzz.WRatio,
        )
        if match and match[1] >= _FUZZY_SUBCATEGORY_THRESHOLD:
            resolved = normalized_index.get(match[0])
            if resolved:
                logger.info(
                    "[Auction] Fuzzy product mapping '%s' -> '%s' (score=%s)",
                    product_query,
                    resolved.name,
                    match[1],
                )
            return resolved

        return None

    async def _get_or_create_sub_category_for_rfq(self, product_query_clean: str) -> SubCategory:
        """Auto-provisionne une sous-catégorie catalogue pour un appel d'offres.

        Un appel d'offres (Auction) exprime un besoin acheteur, pas un article
        d'un catalogue déjà stabilisé — contrairement à `create_product`, il
        n'y a pas de raison métier de refuser un produit simplement parce
        qu'aucun producteur ne l'a encore déclaré. La modération anti-abus
        (termes interdits) tourne déjà en amont sur le texte brut du message
        (nodes/security_moderation.py) ; elle est revérifiée ici en défense en
        profondeur car cette méthode est aussi un outil MCP appelable
        directement (hors du flux conversationnel WhatsApp).
        """
        current_session = self.session

        terms_res = await self.get_prohibited_terms()
        terms = (terms_res or {}).get("terms") or []
        if terms:
            folded = _fold_for_moderation(product_query_clean)
            words = set(re.findall(r"[a-z0-9]+", folded))
            for term in terms:
                t = _fold_for_moderation(term)
                if not t:
                    continue
                matched = (t in folded) if " " in t else (t in words)
                if matched:
                    raise BusinessRuleException(
                        f"Produit '{product_query_clean}' non autorisé.",
                        reason="prohibited_product",
                    )

        category_name = (await self.guess_category(product_query_clean) or "AUTRES").strip().upper()
        category = await current_session.scalar(
            select(Category).where(func.upper(Category.name) == category_name)
        )
        if category is None:
            category = Category(name=category_name)
            current_session.add(category)
            await current_session.flush()

        new_sub_cat = SubCategory(category_id=category.id, name=product_query_clean.strip().title())
        current_session.add(new_sub_cat)
        try:
            await current_session.flush()
        except IntegrityError:
            # Concurrence : une autre requête a créé la même sous-catégorie au
            # même millième de seconde — on récupère celle qui a gagné.
            await current_session.rollback()
            existing = await current_session.scalar(
                select(SubCategory).where(
                    SubCategory.category_id == category.id,
                    func.lower(SubCategory.name) == product_query_clean.strip().lower(),
                )
            )
            if existing is not None:
                return existing
            raise
        logger.info(
            "[Auction] Sous-catégorie auto-provisionnée pour un appel d'offres : '%s' (catégorie=%s)",
            new_sub_cat.name, category.name,
        )
        return new_sub_cat

    async def resolve_sub_category(self, name: str) -> Optional[uuid.UUID]:
        """Recherche floue (trigram) pour récupérer l'ID d'une sous-catégorie de produit.

        Tolère les fautes de frappe agricoles (« tomte » → « tomate ») — champ
        catalogue à fort impact métier, cible explicite de la recherche floue.
        """
        current_session = self.session
        if not current_session:
            return None
        stmt = (
            select(SubCategory.id)
            .where(fuzzy_match(SubCategory.name, name))
            .order_by(similarity_rank(SubCategory.name, name))
            .limit(1)
        )
        result = await current_session.execute(stmt)
        return result.scalar()

    async def get_active_auction_for_user(self, phone: str) -> str | None:
        """Retrouve l'ID de l'enchère ouverte la plus récente pour un acheteur."""
        current_session = self.session
        if not current_session:
            logger.error("❌ Aucune session active trouvée pour get_active_auction_for_user")
            return None
            
        try:
            clean_phone = normalize_phone(phone)
            stmt = (
                select(Auction.id)
                # ✅ CORRECTION : Jointure correcte de Auction.buyer_id sur la PK BuyerProfile.id
                .join(BuyerProfile, Auction.buyer_id == BuyerProfile.id)
                # Puis jointure de la FK BuyerProfile.user_id vers la PK User.id
                .join(User, BuyerProfile.user_id == User.id)
                .where(User.phone == clean_phone, Auction.status == "OPEN")
                .order_by(Auction.created_at.desc())
                .limit(1)
            )
            res = await current_session.scalar(stmt)
            return str(res) if res else None
            
        except Exception as e:
            logger.error(f"❌ Erreur get_active_auction_for_user: {e}", exc_info=True)
            return None

    # ─── SECTION 2 : ENREGISTREMENT ET ÉDITION ───────────────────────────
    async def create_auction(
        self, 
        phone: str, 
        product_query: str, 
        qty: float, 
        unit: str, 
        max_price: float,
        deadline: Any, 
        delivery_location: str,      # Obligatoire selon votre modèle
        delivery_deadline: datetime, # Obligatoire selon votre modèle
        incoterm: str = "DDP",       # Valeur par défaut
        zone_query: Optional[str] = None, 
        description: Optional[str] = None,
        auto_extend: bool = True
    ) -> Dict[str, Any]:
        """Crée un appel d'offre (Auction) avec les contraintes logistiques respectées."""
        current_session = self.session
        if not current_session:
            raise BusinessRuleException("Session indisponible.")

        # 1. Résolution de l'acheteur
        try:
            user_obj, buyer_profile = await self.get_buyer_profile(phone=str(phone))
        except ValueError as e:
            logger.warning(f"⚠️ Échec résolution acheteur : {str(e)}")
            raise BusinessRuleException("Profil acheteur manquant.") from e

        buyer_id = buyer_profile.id
        target_zone_id = user_obj.zone_id
        final_zone_name = "Zone non spécifiée"

        product_query_clean = str(product_query or "").strip()

        # 2. Résolution du produit — recherche floue trigram (catalogue,
        # fort impact métier), triée par pertinence décroissante.
        sub_cat_stmt = (
            select(SubCategory)
            .where(fuzzy_match(SubCategory.name, product_query_clean))
            .order_by(similarity_rank(SubCategory.name, product_query_clean))
            .limit(1)
        )
        sub_cat = await current_session.scalar(sub_cat_stmt)
        if not sub_cat:
            sub_cat = await self._fuzzy_match_sub_category(product_query_clean)
        if not sub_cat:
            # Un appel d'offres exprime un BESOIN acheteur, pas la publication
            # d'un article d'un catalogue déjà stabilisé (contrairement à
            # create_product) — LADINI ne doit pas refuser de relayer une
            # demande simplement parce qu'aucun producteur n'a encore déclaré
            # ce produit précis. On auto-provisionne la sous-catégorie au lieu
            # de rejeter, tant que le produit n'est pas interdit.
            sub_cat = await self._get_or_create_sub_category_for_rfq(product_query_clean)

        # 3. Résolution zone — recherche floue trigram (référentiel logistique).
        if zone_query and zone_query.strip():
            zone_obj = await current_session.scalar(
                select(Zone)
                .where(fuzzy_match(Zone.name, zone_query.strip()))
                .order_by(similarity_rank(Zone.name, zone_query.strip()))
                .limit(1)
            )
            if zone_obj:
                target_zone_id = zone_obj.id
                final_zone_name = zone_obj.name

        # 4. Validation dates
        if isinstance(deadline, str):
            deadline = datetime.fromisoformat(deadline.replace(" ", "T"))

        now = datetime.now()
        if deadline <= now:
            raise BusinessRuleException("Date limite invalide.")

        delivery_deadline_dt = delivery_deadline
        if isinstance(delivery_deadline_dt, str):
            try:
                delivery_deadline_dt = datetime.fromisoformat(delivery_deadline_dt.replace(" ", "T"))
            except ValueError:
                delivery_deadline_dt = None
        if not isinstance(delivery_deadline_dt, datetime):
            delivery_deadline_dt = deadline + timedelta(days=3)

        # 5. Instanciation avec tous les champs requis par le modèle SQLAlchemy
        new_auction = Auction(
            id=uuid.uuid4(),
            buyer_id=buyer_id,
            sub_category_id=sub_cat.id,
            description=description.strip() if description else None,
            quantity=float(qty),
            unit=unit.upper().strip(),
            max_price_per_unit=float(max_price),
            deadline=deadline,
            # Champs logistiques qui causaient le NotNullViolationError
            incoterm=incoterm.upper().strip(),
            delivery_location=delivery_location.strip(),
            delivery_deadline=delivery_deadline_dt,
            # États par défaut
            auto_extend=auto_extend,
            status="OPEN",
            escrow_status="NONE",
            target_zone_id=target_zone_id,
            version=0,
            created_at=now,
            updated_at=now
        )

        current_session.add(new_auction)
        await current_session.flush()

        qty_txt = _fmt_num(qty)
        unit_txt = unit.upper().strip()
        price_txt = _fmt_num(max_price)
        return {
            "status": "success",
            "auction_id": str(new_auction.id),
            "message": (
                f"✅ Votre appel d'offres pour *{qty_txt} {unit_txt} de {sub_cat.name}* "
                f"a été enregistré (prix max *{price_txt} FCFA/{unit_txt}*).\n\n"
                "📢 Dès qu'un producteur propose une offre, je vous recontacte pour valider. "
                "Tapez *suivre mes appels* pour suivre l'état des réponses."
            ),
            "summary": f"📢 Appel d'offre publié pour {qty_txt} {unit_txt} de {sub_cat.name}.",
        }

    async def add_auction_photo(
        self,
        phone: str,
        auction_id: str,
        image_url: str,
        replace: bool = False,
    ) -> Dict[str, Any]:
        """Lie une photo de référence (déjà uploadée sur Supabase Storage) à
        un appel d'offres (Auction) de l'acheteur appelant — même pattern
        exact que `add_bid_photo`/`ProductMixin.add_product_photo`. Aide les
        producteurs à comprendre précisément ce qui est demandé avant de
        proposer un prix.
        """
        try:
            phone = clean_text(phone, "phone", required=True)
            auction_id = clean_text(auction_id, "auction_id", required=True)
            image_url = clean_text(image_url, "image_url", required=True, max_length=2048)
            _, buyer_profile = await self.get_buyer_profile(phone)

            stmt = (
                select(Auction)
                .where(and_(Auction.id == uuid.UUID(auction_id), Auction.buyer_id == buyer_profile.id))
                .with_for_update()
            )
            res = await self.session.execute(stmt)
            auction = res.scalar_one_or_none()

            if not auction:
                return {"status": "error", "message": "Appel d'offres introuvable ou non autorisé."}

            if replace:
                current_images = [image_url]
            else:
                current_images = list(auction.images or [])
                if image_url in current_images:
                    return {
                        "status": "success",
                        "message": "Photo déjà associée à cet appel d'offres.",
                        "data": {"auction_id": str(auction.id), "images": current_images},
                    }
                current_images.append(image_url)
                if len(current_images) > _MAX_PHOTOS_PER_LOT:
                    current_images = current_images[-_MAX_PHOTOS_PER_LOT:]

            auction.images = current_images
            await self.session.flush()
            await self.session.refresh(auction)

            logger.info(
                "AUCTION_PHOTO_ADDED: ID %s par %s (total photos: %d, replace=%s)",
                auction_id, phone, len(current_images), replace,
            )

            return {
                "status": "success",
                "message": "Photo ajoutée à votre appel d'offres avec succès.",
                "data": {"auction_id": str(auction.id), "images": current_images},
            }

        except ValueError as e:
            return {"status": "error", "message": str(e)}




    async def place_bid(
        self, 
        auction_id: str, 
        phone: str, 
        offered_price: float, 
        message: str | None = None
    ) -> Dict[str, Any]:
        """Permet à un producteur d'émettre un prix (Bid) sur un marché ouvert."""
        current_session = self.session
        if not current_session:
            raise BusinessRuleException("Session de base de données indisponible.")

        # 1. Résolution via le nouveau BaseMixin (Retourne un Tuple d'objets SQL)
        try:
            _, producer_obj = await self.get_producer_profile(phone=str(phone))
        except ValueError as e:
            logger.warning(f"⚠️ Échec de résolution producteur pour {phone} : {str(e)}")
            raise BusinessRuleException("Votre compte producteur n'est pas encore identifié.") from e

        producer_id = producer_obj.id

        # 2. Validation de la cible
        a_uuid = uuid.UUID(auction_id)
        auction = await current_session.get(Auction, a_uuid)

        if not auction:
            raise BusinessRuleException("Cette opportunité de marché n'existe plus.")

        if auction.status.upper() != "OPEN":
            raise BusinessRuleException(f"Désolé, cette enchère est fermée (Statut : {auction.status}).")

        if float(offered_price) <= 0:
            raise BusinessRuleException("Le prix proposé doit être supérieur à 0 CFA.")

        # 3. UPSERT — un producteur ne peut avoir qu'UNE offre par enchère
        #    (contrainte unique `bids_auction_producer_unique`). Si une offre
        #    existe déjà, on MET À JOUR son prix au lieu de tenter un INSERT
        #    qui lèverait IntegrityError (→ transaction empoisonnée → « erreur
        #    technique »). UX naturelle : « participer » 2× = corriger son prix.
        now = datetime.now()
        existing_bid = await current_session.scalar(
            select(Bid).where(
                Bid.auction_id == a_uuid,
                Bid.producer_id == producer_id,
            )
        )

        if existing_bid is not None:
            if str(existing_bid.status or "").upper() != "PENDING":
                raise BusinessRuleException(
                    "Vous avez déjà une offre traitée sur cette enchère, elle ne peut plus être modifiée.",
                    reason="bid_already_processed",
                )
            old_price = existing_bid.offered_price
            existing_bid.offered_price = float(offered_price)
            if message:
                existing_bid.message = message.strip()
            existing_bid.updated_at = now
            await current_session.flush()
            return {
                "status": "success",
                "bid_id": str(existing_bid.id),
                "updated": True,
                "message": (
                    f"✅ Vous aviez déjà une offre ({_fmt_num(old_price)} CFA) sur cette enchère — "
                    f"elle a été mise à jour à *{_fmt_num(offered_price)} CFA*."
                ),
            }

        new_bid = Bid(
            id=uuid.uuid4(),
            auction_id=a_uuid,
            producer_id=producer_id,
            offered_price=float(offered_price),
            is_winner=False,
            status="PENDING",
            message=message.strip() if message else None,
            created_at=now,
            updated_at=now
        )

        current_session.add(new_bid)
        await current_session.flush()

        return {
            "status": "success",
            "bid_id": str(new_bid.id),
            "message": f"✅ Votre offre de *{_fmt_num(offered_price)} CFA* a été transmise à l'acheteur avec succès."
        }

    async def add_bid_photo(
        self,
        phone: str,
        bid_id: str,
        image_url: str,
        replace: bool = False,
    ) -> Dict[str, Any]:
        """Lie une photo du lot proposé (déjà uploadée sur Supabase Storage) à
        une offre (Bid) du producteur appelant — même pattern exact que
        `ProductMixin.add_product_photo` (row lock, ownership intégrée à la
        requête, dédoublonnage sur l'URL, plafond FIFO). Donne à l'acheteur
        une preuve visuelle du lot AVANT de choisir le gagnant.
        """
        try:
            phone = clean_text(phone, "phone", required=True)
            bid_id = clean_text(bid_id, "bid_id", required=True)
            image_url = clean_text(image_url, "image_url", required=True, max_length=2048)
            _, producer = await self.get_producer_profile(phone)

            stmt = (
                select(Bid)
                .where(and_(Bid.id == uuid.UUID(bid_id), Bid.producer_id == producer.id))
                .with_for_update()
            )
            res = await self.session.execute(stmt)
            bid = res.scalar_one_or_none()

            if not bid:
                return {"status": "error", "message": "Offre introuvable ou non autorisée."}

            if replace:
                current_images = [image_url]
            else:
                current_images = list(bid.images or [])
                if image_url in current_images:
                    return {
                        "status": "success",
                        "message": "Photo déjà associée à cette offre.",
                        "data": {"bid_id": str(bid.id), "images": current_images},
                    }
                current_images.append(image_url)
                if len(current_images) > _MAX_PHOTOS_PER_LOT:
                    current_images = current_images[-_MAX_PHOTOS_PER_LOT:]

            bid.images = current_images
            await self.session.flush()
            await self.session.refresh(bid)

            logger.info(
                "BID_PHOTO_ADDED: ID %s par %s (total photos: %d, replace=%s)",
                bid_id, phone, len(current_images), replace,
            )

            return {
                "status": "success",
                "message": "Photo ajoutée à votre offre avec succès.",
                "data": {"bid_id": str(bid.id), "images": current_images},
            }

        except ValueError as e:
            return {"status": "error", "message": str(e)}

    # ─── SECTION 3 : CONSULTATIONS ET MARCHÉ (READS) ─────────────────────

    async def get_auctions(
        self, 
        phone: str | None = None,
        product_name: str | None = None, 
        zone_name: str | None = None, 
        status: str = "OPEN",
        view_mode: str = "MARKETPLACE"
    ) -> Dict[str, Any]:
        """Récupère et formate les appels d'offres actifs pour l'interface WhatsApp."""
        current_session = self.session
        if not current_session:
            return {"status": "error", "message": "Session de base de données indisponible."}
        try:
            clean_phone = normalize_phone(phone) if phone else None

            # Compteur d'offres reçues par enchère (sous-requête corrélée) : évite
            # un GROUP BY sur toute la requête tout en alimentant le badge
            # « X offre(s) » côté acheteur (vue MY_OWN). NULL-safe → 0 si aucune.
            bid_count_sq = (
                select(func.count(Bid.id))
                .where(Bid.auction_id == Auction.id)
                .correlate(Auction)
                .scalar_subquery()
                .label("bid_count")
            )
            # A minima UNE offre reçue porte une photo de lot — signal utile
            # côté acheteur MÊME quand l'enchère elle-même n'a pas de photo
            # de référence (add_auction_photo, optionnelle). Voir
            # [[auction-bid-photos-2026-08]] : sans ce flag, la liste
            # "suivre mes appels" ne pouvait jamais indiquer la présence
            # d'une photo d'offre, seulement celle de l'enchère.
            has_bid_photos_sq = (
                exists(
                    select(Bid.id).where(
                        Bid.auction_id == Auction.id,
                        func.cardinality(Bid.images) > 0,
                    )
                )
                .label("has_bid_photos")
            )

            stmt = (
                select(
                    Auction,
                    SubCategory.name.label("product_name"),
                    Zone.name.label("zone_name"),
                    BuyerProfile.establishment_name.label("buyer_name"),
                    bid_count_sq,
                    has_bid_photos_sq,
                )
                .join(SubCategory, Auction.sub_category_id == SubCategory.id)
                .outerjoin(Zone, Auction.target_zone_id == Zone.id)
                # ✅ CORRECTION : Pointage relationnel correct vers la PK BuyerProfile.id
                .outerjoin(BuyerProfile, Auction.buyer_id == BuyerProfile.id)
            )

            # Isolation sécurisée sans casser l'arbre de requêtes
            if clean_phone:
                stmt = stmt.outerjoin(User, BuyerProfile.user_id == User.id)
                if view_mode == "MY_OWN":
                    stmt = stmt.where(User.phone == clean_phone)
                else:
                    # MARKETPLACE : exclure ses propres enchères SANS éliminer
                    # celles dont l'acheteur.user est NULL (piège SQL : NULL != x
                    # vaut NULL, donc la ligne serait injustement filtrée).
                    stmt = stmt.where(
                        or_(User.phone.is_(None), User.phone != clean_phone)
                    )

            # `status` est un champ ÉNUMÉRÉ (OPEN/AWARDED/CANCELLED/EXPIRED) — égalité
            # stricte OBLIGATOIRE, jamais de recherche floue sur un champ structurel.
            stmt = stmt.where(func.upper(Auction.status) == status.strip().upper())

            # Champs catalogue/référentiel : recherche floue trigram tolérante
            # aux fautes de frappe agricoles.
            if product_name:
                stmt = stmt.where(fuzzy_match(SubCategory.name, product_name))
            if zone_name:
                stmt = stmt.where(fuzzy_match(Zone.name, zone_name))

            stmt = stmt.where(Auction.deadline > datetime.now())
            stmt = stmt.order_by(Auction.created_at.desc())
            
            results = await current_session.execute(stmt)
            rows = results.all()

            if not rows:
                return {
                    "status": "success", 
                    "count": 0, 
                    "message": "Aucun marché disponible correspondant à vos critères actuellement."
                }

            auctions_list = []
            menu_lines = [f"🔎 *Marchés disponibles ({status}) :*"]
            mapping_cache = {}

            for i, (auction, p_name, z_name, b_name, bid_count, has_bid_photos) in enumerate(rows, start=1):
                diff = auction.deadline - datetime.now()
                time_str = f"{diff.days}j {diff.seconds // 3600}h" if diff.days > 0 else f"{diff.seconds // 3600}h"
                n_bids = int(bid_count or 0)

                auctions_list.append({
                    "auction_id": str(auction.id),
                    "product": p_name,
                    "qty": auction.quantity,
                    "quantity": auction.quantity,
                    "unit": auction.unit,
                    "status": auction.status,
                    "max_price": auction.max_price_per_unit,
                    "bid_count": n_bids,
                    "images": list(auction.images or []),
                    "has_bid_photos": bool(has_bid_photos),
                })

                bids_badge = f"\n📥 Offres reçues : *{n_bids}*" if n_bids else ""
                line = (
                    f"\n*{i}. {p_name}* ({auction.quantity} {auction.unit})\n"
                    f"💰 Prix Max : *{auction.max_price_per_unit} CFA/{auction.unit}*\n"
                    f"📍 Zone : {z_name or 'Non spécifiée'}\n"
                    f"⏳ Restant : {time_str}\n"
                    f"👤 Client : {b_name or 'Acheteur AgriConnect'}{bids_badge}"
                )
                menu_lines.append(line)
                mapping_cache[str(i)] = str(auction.id)

            menu_lines.append("\n_Répondez avec le *numéro* de la ligne pour proposer votre prix (ex: *1*)._")

            return {
                "status": "success",
                "count": len(rows),
                "formatted_menu": "\n".join(menu_lines),
                "mapping": mapping_cache,
                "data": auctions_list
            }
        except Exception as e:
            logger.error(f"❌ Erreur get_auctions : {str(e)}", exc_info=True)
            return {"status": "error", "message": "Impossible de charger les marchés disponibles."}
        
    async def get_auction_bids(self, auction_id: str, phone: str | None = None) -> Dict[str, Any]:
        """Récupère l'ensemble des propositions de prix soumises sur une offre.

        ``phone`` (optionnel) : numéro de l'acheteur appelant. Non utilisé par la
        requête (filtrée par ``auction_id``) mais REQUIS pour que le runtime MCP
        dérive une identité de contexte — sans lui, ``call_tool`` refuse l'appel
        (``missing_context_identity``) et l'acheteur voit « Aucune offre » à tort.
        """
        current_session = self.session
        if not current_session:
            return {"status": "error", "message": "Session de base de données indisponible."}
        try:
            a_uuid = uuid.UUID(auction_id)

            # En-tête : fiche de l'enchère (produit, statut, quantité) — permet à
            # l'acheteur de contextualiser les offres reçues côté suivi.
            # OUTER JOIN sur SubCategory : une sous-catégorie manquante ne doit PAS
            # faire disparaître l'enchère (sinon en-tête vide → « Votre produit »).
            auction_row = (
                await current_session.execute(
                    select(Auction, SubCategory.name.label("product_name"))
                    .outerjoin(SubCategory, Auction.sub_category_id == SubCategory.id)
                    .where(Auction.id == a_uuid)
                )
            ).fetchone()
            auction_info: Dict[str, Any] = {}
            if auction_row:
                auction_obj, product_name = auction_row
                auction_info = {
                    "auction_id": str(auction_obj.id),
                    "product": product_name,
                    "product_name": product_name,
                    "status": auction_obj.status,
                    "quantity": float(auction_obj.quantity) if auction_obj.quantity is not None else None,
                    "unit": auction_obj.unit,
                    "max_price": float(auction_obj.max_price_per_unit) if auction_obj.max_price_per_unit is not None else None,
                    "images": list(auction_obj.images or []),
                }

            # LEFT OUTER JOIN Producer/User : un bid dont le lien producteur→user
            # est cassé (compte producteur incomplet / user phantom) doit tout de
            # même apparaître. Sinon le compteur de la liste (count brut) affiche
            # « 1 offre » mais ce détail (INNER JOIN) n'en montre AUCUNE.
            stmt = (
                select(Bid, User.name)
                .outerjoin(Producer, Bid.producer_id == Producer.id)
                .outerjoin(User, Producer.user_id == User.id)
                .where(Bid.auction_id == a_uuid)
                .order_by(Bid.offered_price.asc())
            )
            results = await current_session.execute(stmt)

            bids_list = []
            for bid, prod_name in results:
                bids_list.append({
                    "bid_id": str(bid.id),
                    "producer": prod_name or "Producteur Anonyme",
                    "producer_name": prod_name or "Producteur Anonyme",
                    "price": float(bid.offered_price),
                    "offered_price": float(bid.offered_price),
                    "status": str(bid.status or "PENDING").upper(),
                    "message": bid.message,
                    "delivery": "Non inclus",
                    "images": list(bid.images or []),
                })

            return {
                "status": "success",
                "count": len(bids_list),
                "auction": auction_info,
                "auction_status": auction_info.get("status"),
                "bids": bids_list
            }
        except Exception as e:
            logger.error(f"❌ Erreur get_auction_bids: {e}", exc_info=True)
            return {"status": "error", "message": f"Impossible de charger les propositions : {str(e)}"}



    # ==================================================================
    # 4. GESTION DES OFFRES PHONECENTRIC (READS & STATUTS)
    # ==================================================================

    async def get_auctions_bids(
        self, 
        phone: str | None = None, 
        status: str = "OPEN"
    ) -> Dict[str, Any]:
        """
        Vision Phone-First : Récupère les offres (Bids) reçues par un acheteur sur ses marchés.
        Délégué à l'identité de l'acheteur via le numéro de téléphone sans forcer de profil lourd.
        """
        current_session = self.session
        if not current_session:
            return {"status": "error", "message": "Erreur interne : session de base de données indisponible."}
        try:
            # Création d'un alias propre pour la table User côté producteur
            producer_user = aliased(User, name="producer_user")

            # 1. Requête de base unifiée avec toutes les jointures sémantiques indispensables
            stmt = (
                select(
                    Bid, 
                    producer_user.name.label("producer_name"),
                    SubCategory.name.label("product_name"),
                    Auction.quantity,
                    Auction.unit
                )
                .join(Auction, Bid.auction_id == Auction.id)
                .join(Producer, Bid.producer_id == Producer.id)
                .join(producer_user, Producer.user_id == producer_user.id) # Jointure sur le user du producteur
                .join(SubCategory, Auction.sub_category_id == SubCategory.id)
                .where(Auction.status == status)
                .order_by(Bid.created_at.desc())
            )

            # 2. Filtrage Phone-First ultra-robuste avec isolation de l'Acheteur
            if phone:
                clean_phone = normalize_phone(phone)
                buyer_user = aliased(User, name="buyer_user") # Alias pour le user de l'acheteur
                
                stmt = (
                    # Correction de la FK : Auction.buyer_id pointe sur BuyerProfile.id
                    stmt.join(BuyerProfile, Auction.buyer_id == BuyerProfile.id)
                    .join(buyer_user, BuyerProfile.user_id == buyer_user.id)
                    .where(buyer_user.phone == clean_phone)
                )

            results = await current_session.execute(stmt)
            rows = results.all()

            if not rows:
                return {
                    "status": "success", 
                    "count": 0, 
                    "message": "Aucune offre d'achat n'est disponible sur le marché actuellement pour votre compte."
                }

            bids_list = []
            menu_lines = ["📋 *Propositions reçues pour vos appels d'offres :*"]
            mapping_cache = {}

            # 3. Construction du menu indexé pour WhatsApp
            for i, (bid, prod_name, product_name, qty, unit) in enumerate(rows, start=1):
                clean_prod_name = prod_name or "Producteur Externe"
                
                bids_list.append({
                    "bid_id": str(bid.id),
                    "product": product_name,
                    "price": bid.offered_price,
                    "producer": clean_prod_name,
                    "images": list(bid.images or []),
                })

                line = (
                    f"\n*{i}. Lot {product_name}* ({qty} {unit})\n"
                    f"💰 Prix proposé : *{bid.offered_price} CFA*\n"
                    f"👤 Vendeur : {clean_prod_name}"
                )
                menu_lines.append(line)
                mapping_cache[str(i)] = str(bid.id)

            menu_lines.append("\n_Répondez simplement avec le *numéro* de la ligne pour accepter l'offre (ex: *1*)._")

            return {
                "status": "success",
                "count": len(bids_list),
                "formatted_menu": "\n".join(menu_lines),
                "mapping": mapping_cache,
                "data": bids_list
            }

        except Exception as e:
            logger.error(f"❌ Erreur critique get_auctions_bids: {e}", exc_info=True)
            return {"status": "error", "message": "Impossible de charger vos offres pour le moment."}


    @staticmethod
    def _derive_bid_status(bid_status: str, is_winner: bool, auction_status: str) -> tuple[str, str]:
        """Statut effectif d'une offre, dérivé du bid ET de l'état de l'enchère.

        Retourne ``(code, libellé_affichable)``. On ne se fie pas au seul
        ``bid.status`` : une offre non gagnante sur une enchère clôturée est
        *perdue*, même si le bid est resté PENDING (résilience aux états partiels).
        """
        b = str(bid_status or "").upper().strip()
        a = str(auction_status or "").upper().strip()
        if is_winner or b in {"WINNING", "WON", "ACCEPTED"}:
            return "WON", "🤝 Acceptée — Félicitations !"
        if b == "WITHDRAWN":
            return "WITHDRAWN", "↩️ Retirée"
        if b == "LOST":
            return "LOST", "❌ Non retenue"
        if a in {"CLOSED", "WON", "COMPLETED"}:
            # Enchère conclue avec un autre producteur.
            return "LOST", "❌ Non retenue"
        if a in {"EXPIRED", "CANCELLED"}:
            return a, "⌛ Enchère expirée" if a == "EXPIRED" else "❌ Enchère annulée"
        return "PENDING", "⏳ En attente de décision"

    async def get_my_active_bids(self, phone: str) -> Dict[str, Any]:
        """
        Permet à un producteur de voir l'état et l'historique des propositions (bids) qu'il a émises.
        """
        current_session = self.session
        if not current_session:
            return {"status": "error", "message": "Erreur interne : session de base de données indisponible."}
        try:
            clean_phone = normalize_phone(phone)

            stmt = (
                select(
                    Bid,
                    SubCategory.name.label("product_name"),
                    Auction.quantity,
                    Auction.unit,
                    Auction.status.label("auction_status"),
                )
                .join(Producer, Bid.producer_id == Producer.id)
                .join(User, Producer.user_id == User.id)
                .join(Auction, Bid.auction_id == Auction.id)
                .join(SubCategory, Auction.sub_category_id == SubCategory.id)
                .where(User.phone == clean_phone)
                .order_by(Bid.created_at.desc())
            )

            results = await current_session.execute(stmt)
            rows = results.all()

            if not rows:
                return {
                    "status": "success",
                    "count": 0,
                    "data": [],
                    "message": "Vous n'avez fait aucune proposition de prix pour le moment.",
                }

            menu_lines = ["📋 *Le statut de vos propositions :*"]
            data: List[Dict[str, Any]] = []
            mapping: Dict[str, str] = {}

            for i, (bid, product_name, qty, unit, auction_status) in enumerate(rows, start=1):
                status_code, friendly_status = self._derive_bid_status(
                    bid.status, bool(getattr(bid, "is_winner", False)), auction_status
                )
                bid_id = str(bid.id)
                price_txt = _fmt_num(bid.offered_price)
                data.append({
                    "bid_id": bid_id,
                    "auction_id": str(bid.auction_id),
                    "product": product_name,
                    "product_name": product_name,
                    "quantity": float(qty) if qty is not None else None,
                    "unit": unit,
                    "offered_price": float(bid.offered_price),
                    "price": float(bid.offered_price),
                    "status": status_code,
                    "status_label": friendly_status,
                    "images": list(bid.images or []),
                })
                mapping[str(i)] = bid_id

                line = (
                    f"\n*{i}. Demande de {product_name}* ({qty} {unit})\n"
                    f"💰 Votre prix : *{price_txt} CFA*\n"
                    f"📊 Statut : {friendly_status}"
                )
                menu_lines.append(line)

            return {
                "status": "success",
                "count": len(rows),
                "formatted_menu": "\n".join(menu_lines),
                "mapping": mapping,
                "data": data,
            }

        except Exception as e:
            logger.error(f"❌ Erreur get_my_active_bids: {e}", exc_info=True)
            return {"status": "error", "message": "Impossible de récupérer vos propositions."}


    async def get_producer_auctions(
        self,
        phone: str,
        scope: str = "MATCHABLE",
        product_name: Optional[str] = None,
        zone_name: Optional[str] = None,
        status: str = "OPEN",
    ) -> Dict[str, Any]:
        """Appels d'offres ouverts, vus côté PRODUCTEUR, groupés par catégorie.

        scope :
          - ``MATCHABLE`` (défaut) : uniquement les enchères dont la sous-catégorie
            correspond à ce que le producteur propose déjà au catalogue
            (``Product.sub_category_id``). S'il n'a aucun produit, on bascule
            automatiquement sur ``ALL`` (``scope_effective`` renseigne le repli).
          - ``ALL`` : toutes les enchères ouvertes du marché.
        """
        current_session = self.session
        if not current_session:
            return {"status": "error", "message": "Session de base de données indisponible."}
        try:
            clean_phone = normalize_phone(phone) if phone else None
            scope_up = str(scope or "MATCHABLE").upper().strip()

            # Sous-catégories que le producteur peut fournir (via son catalogue).
            supplied_ids: List[Any] = []
            if clean_phone:
                supplied_rows = (
                    await current_session.execute(
                        select(Product.sub_category_id)
                        .join(Producer, Product.producer_id == Producer.id)
                        .join(User, Producer.user_id == User.id)
                        .where(
                            User.phone == clean_phone,
                            Product.sub_category_id.isnot(None),
                        )
                        .distinct()
                    )
                ).scalars().all()
                supplied_ids = [sid for sid in supplied_rows if sid is not None]

            has_categories = bool(supplied_ids)
            scope_effective = scope_up
            if scope_up == "MATCHABLE" and not has_categories:
                scope_effective = "ALL"  # repli : sans catalogue, on montre tout.

            stmt = (
                select(
                    Auction,
                    SubCategory.name.label("product_name"),
                    Category.name.label("category_name"),
                    Zone.name.label("zone_name"),
                    BuyerProfile.establishment_name.label("buyer_name"),
                )
                .join(SubCategory, Auction.sub_category_id == SubCategory.id)
                .join(Category, SubCategory.category_id == Category.id)
                .outerjoin(Zone, Auction.target_zone_id == Zone.id)
                .outerjoin(BuyerProfile, Auction.buyer_id == BuyerProfile.id)
                # `status` énuméré → égalité stricte obligatoire (jamais de flou
                # sur un champ structurel).
                .where(func.upper(Auction.status) == status.strip().upper())
                .where(Auction.deadline > datetime.now())
            )

            if scope_effective == "MATCHABLE" and supplied_ids:
                stmt = stmt.where(Auction.sub_category_id.in_(supplied_ids))
            # Champs catalogue/référentiel : recherche floue trigram.
            if product_name:
                stmt = stmt.where(fuzzy_match(SubCategory.name, product_name))
            if zone_name:
                stmt = stmt.where(fuzzy_match(Zone.name, zone_name))

            stmt = stmt.order_by(Category.name.asc(), Auction.created_at.desc())

            rows = (await current_session.execute(stmt)).all()

            if not rows:
                if scope_effective == "MATCHABLE":
                    msg = (
                        "Aucun appel d'offres ouvert ne correspond à vos produits "
                        "pour le moment. Tapez *toutes les enchères* pour voir tout le marché."
                    )
                else:
                    msg = "Aucun appel d'offres ouvert sur le marché actuellement."
                return {
                    "status": "success",
                    "count": 0,
                    "data": [],
                    "scope": scope_effective,
                    "has_categories": has_categories,
                    "message": msg,
                }

            data: List[Dict[str, Any]] = []
            mapping: Dict[str, str] = {}
            header = (
                "🎯 *Appels d'offres pour vos produits :*"
                if scope_effective == "MATCHABLE"
                else "🛒 *Tous les appels d'offres ouverts :*"
            )
            menu_lines = [header]

            current_category: Optional[str] = None
            idx = 0
            for auction, p_name, cat_name, z_name, b_name in rows:
                if cat_name != current_category:
                    current_category = cat_name
                    menu_lines.append(f"\n📂 *{cat_name}*")

                idx += 1
                diff = auction.deadline - datetime.now()
                time_str = f"{diff.days}j {diff.seconds // 3600}h" if diff.days > 0 else f"{diff.seconds // 3600}h"
                qty_txt = _fmt_num(auction.quantity)
                price_txt = _fmt_num(auction.max_price_per_unit)

                data.append({
                    "auction_id": str(auction.id),
                    "product": p_name,
                    "product_name": p_name,
                    "category": cat_name,
                    "quantity": float(auction.quantity),
                    "unit": auction.unit,
                    "max_price": float(auction.max_price_per_unit),
                    "zone": z_name,
                    "buyer_name": b_name,
                    "images": list(auction.images or []),
                })
                mapping[str(idx)] = str(auction.id)

                menu_lines.append(
                    f"\n*{idx}. {p_name}* — {qty_txt} {auction.unit}\n"
                    f"💰 Prix max : *{price_txt} FCFA/{auction.unit}*\n"
                    f"📍 {z_name or 'Zone non spécifiée'} · ⏳ {time_str}\n"
                    f"👤 {b_name or 'Acheteur AgriConnect'}"
                )

            menu_lines.append(
                "\n_Répondez avec le *numéro* de l'enchère pour proposer votre prix._"
            )

            return {
                "status": "success",
                "count": len(data),
                "formatted_menu": "\n".join(menu_lines),
                "mapping": mapping,
                "data": data,
                "scope": scope_effective,
                "has_categories": has_categories,
            }
        except Exception as e:
            logger.error(f"❌ Erreur get_producer_auctions: {e}", exc_info=True)
            return {"status": "error", "message": "Impossible de charger les appels d'offres."}

    # ─── SECTION 5 : CLÔTURE ET CONVERSION EN COMMANDE ───────────────────

    async def select_winning_bid(self, bid_id: str, phone: str | None = None) -> Dict[str, Any]:
        """
        Désigne l'offre gagnante, passe l'appel d'offre à l'état 'CLOSED'
        et instancie la commande officielle (Order) au sein de la transaction courante.

        ``phone`` (optionnel) : numéro de l'acheteur appelant. REQUIS pour que le
        runtime MCP dérive une identité de contexte — sans lui, ``call_tool``
        refuse l'appel (``missing_context_identity``) → « erreur technique ».
        """
        current_session = self.session
        if not current_session:
            raise BusinessRuleException("Erreur interne : session de base de données indisponible.")

        b_id = uuid.UUID(bid_id)

        # OUTER JOIN Producer/User : un bid gagnant dont le producteur a un
        # lien User cassé ne doit PAS déclencher « Offre introuvable » (le
        # nom retombe sur « Producteur Anonyme »). Cohérent avec get_auction_bids.
        stmt = (
            select(Bid, Auction, User.name.label("producer_name"), User.phone.label("producer_phone"), SubCategory)
            .join(Auction, Bid.auction_id == Auction.id)
            .outerjoin(Producer, Bid.producer_id == Producer.id)
            .outerjoin(User, Producer.user_id == User.id)
            .join(SubCategory, Auction.sub_category_id == SubCategory.id)
            .where(Bid.id == b_id)
        )
        res = await current_session.execute(stmt)
        record = res.fetchone()

        if not record:
            raise BusinessRuleException("Offre introuvable ou retirée par le producteur.")

        bid, auction, prod_name, prod_phone, sub_cat = record

        if auction.status == "CLOSED":
            raise BusinessRuleException("Cette enchère a déjà été clôturée avec un autre partenaire.")

        # 1. Mises à jour atomiques des états du Marché
        bid.is_winner = True
        bid.status = "WINNING"
        auction.status = "CLOSED"
        auction.winner_bid_id = bid.id

        # 1b. Toutes les autres offres de cette enchère sont désormais perdues.
        #     Sans cela, un producteur non retenu resterait « En attente » à vie.
        now_loss = datetime.now()
        await current_session.execute(
            update(Bid)
            .where(
                Bid.auction_id == auction.id,
                Bid.id != bid.id,
                Bid.status.in_(["PENDING", "WINNING"]),
            )
            .values(status="LOST", is_winner=False, updated_at=now_loss)
        )

        # 2. Calcul financier & Instanciation de l'accord commercial officiel (Order)
        total = float(bid.offered_price * auction.quantity)

        new_order = Order(
            id=uuid.uuid4(),
            buyer_id=auction.buyer_id,
            auction_id=auction.id,
            winning_bid_id=bid.id,
            total_amount=total,
            status="CONFIRMED",
            zone_id=auction.target_zone_id,
            created_at=datetime.now()
        )

        current_session.add(new_order)
        await current_session.flush()

        # Notifie le producteur gagnant via l'outbox (même transaction que la
        # commande : si le commit échoue, la notif n'est pas non plus enfilée).
        # Dispatché en ~30s (cron outbox-dispatch) — pas synchrone mais quasi
        # temps réel. dedupe_key sur new_order.id : jamais deux fois pour le
        # même deal même si select_winning_bid est rejoué.
        notified = False
        if prod_phone:
            from agriconnect.workers.outbox import templates as _outbox_templates
            from agriconnect.workers.repositories import outbox_repo as _outbox_repo

            await _outbox_repo.enqueue(current_session, [{
                "channel": "WHATSAPP",
                "recipient_phone": prod_phone,
                "template_key": _outbox_templates.AUCTION_WON_PRODUCER,
                "payload": {
                    "product": sub_cat.name,
                    "quantity": float(auction.quantity),
                    "unit": auction.unit,
                    "total": total,
                },
                "dedupe_key": f"AUCTION_WON:{new_order.id}",
            }])
            notified = True

        clean_prod_name = prod_name or "Producteur Anonyme"
        producer_status_line = (
            "Le producteur a été informé et prépare la livraison."
            if notified
            else "Pensez à contacter directement le producteur : son numéro n'est pas encore renseigné."
        )
        return {
            "status": "success",
            "order_id": str(new_order.id),
            "summary_buyer": (
                f"🤝 *Félicitations ! Deal conclu.*\n\n"
                f"Vous avez choisi l'offre de *{clean_prod_name}*.\n"
                f"📦 Produit : {sub_cat.name}\n"
                f"💰 Total à payer : *{total} CFA*\n\n"
                f"{producer_status_line}"
            ),
            "summary_producer": (
                f"🎉 *Bonne nouvelle !*\n\n"
                f"Votre offre pour {auction.quantity} {auction.unit} de {sub_cat.name} a été retenue !\n"
                f"💵 Montant du marché : *{total} CFA*\n\n"
                f"Veuillez contacter l'acheteur pour coordonner les détails de la livraison."
            )
        }



    # ==================================================================
    # 5. extra
    # ==================================================================
    async def cancel_auction(self, auction_id: str, phone: str) -> Dict[str, Any]:
        """Annule un appel d'offre ouvert. Sécurisé par le numéro de téléphone de l'acheteur."""
        current_session = self.session
        if not current_session:
            raise BusinessRuleException("Session de base de données indisponible.")

        clean_phone = normalize_phone(phone)
        a_uuid = uuid.UUID(auction_id)

        # Recherche de l'enchère en s'assurant qu'elle appartient bien à l'utilisateur via son profil
        stmt = (
            select(Auction)
            .join(BuyerProfile, Auction.buyer_id == BuyerProfile.id)
            .join(User, BuyerProfile.user_id == User.id)
            .where(Auction.id == a_uuid, User.phone == clean_phone)
        )
        auction = await current_session.scalar(stmt)

        if not auction:
            raise BusinessRuleException("Action non autorisée ou marché introuvable.")

        if auction.status.upper() != "OPEN":
            raise BusinessRuleException(f"Impossible d'annuler un marché avec le statut : {auction.status}.")

        # Passage à l'état annulé
        auction.status = "CANCELLED"
        auction.updated_at = datetime.now()
        await current_session.flush()

        return {
            "status": "success",
            "message": "🗑️ Votre appel d'offre a été retiré du marché avec succès."
        }

    async def update_bid_price(self, bid_id: str, phone: str, new_price: float) -> Dict[str, Any]:
        """Permet à un producteur de corriger le prix d'une offre encore PENDING."""
        current_session = self.session
        if not current_session:
            raise BusinessRuleException("Session de base de données indisponible.")

        clean_phone = normalize_phone(phone)
        b_uuid = uuid.UUID(bid_id)

        stmt = (
            select(Bid)
            .join(Producer, Bid.producer_id == Producer.id)
            .join(User, Producer.user_id == User.id)
            .where(Bid.id == b_uuid, User.phone == clean_phone)
        )
        bid = await current_session.scalar(stmt)

        if not bid:
            raise BusinessRuleException("Offre introuvable ou action non autorisée.")

        if bid.status.upper() != "PENDING":
            raise BusinessRuleException("Impossible de modifier une offre déjà traitée.")

        if float(new_price) <= 0:
            raise BusinessRuleException("Le prix proposé doit être supérieur à 0 CFA.")

        bid.offered_price = float(new_price)
        bid.updated_at = datetime.now()
        await current_session.flush()

        price_txt = _fmt_num(new_price)
        return {
            "status": "success",
            "bid_id": str(bid.id),
            "message": f"✅ Votre offre a été mise à jour à *{price_txt} CFA*.",
        }

    async def withdraw_bid(self, bid_id: str, phone: str) -> Dict[str, Any]:
        """Permet à un producteur de retirer sa proposition de prix (Bid)."""
        current_session = self.session
        if not current_session:
            raise BusinessRuleException("Session de base de données indisponible.")

        clean_phone = normalize_phone(phone)
        b_uuid = uuid.UUID(bid_id)

        # Sécurisation : Le bid doit appartenir au producteur lié au numéro de téléphone
        stmt = (
            select(Bid)
            .join(Producer, Bid.producer_id == Producer.id)
            .join(User, Producer.user_id == User.id)
            .where(Bid.id == b_uuid, User.phone == clean_phone)
        )
        bid = await current_session.scalar(stmt)

        if not bid:
            raise BusinessRuleException("Proposition introuvable ou action non autorisée.")

        if bid.status.upper() != "PENDING":
            raise BusinessRuleException("Impossible de retirer une offre déjà traitée.")

        bid.status = "WITHDRAWN"
        bid.updated_at = datetime.now()
        await current_session.flush()

        return {
            "status": "success",
            "message": "👋 Votre proposition de prix a été retirée avec succès."
        }

    async def check_and_expire_auctions(self) -> Dict[str, Any]:
        """Passe le statut des enchères expirées de 'OPEN' à 'EXPIRED'."""
        current_session = self.session
        if not current_session:
            raise BusinessRuleException("Session indisponible.")

        now = datetime.now()
        stmt = (
            update(Auction)
            .where(Auction.status == "OPEN", Auction.deadline <= now)
            .values(status="EXPIRED", updated_at=now)
        )
        result = await current_session.execute(stmt)
        await current_session.flush()

        return {
            "status": "success",
            "expired_count": result.rowcount,
            "message": f"🤖 Nettoyage effectué : {result.rowcount} marché(s) expiré(s)."
        }

    async def get_my_active_auctions(self, phone: str) -> Dict[str, Any]:
        """Affiche le tableau de bord de l'acheteur avec ses marchés ouverts et le nombre d'offres reçues."""
        current_session = self.session
        if not current_session:
            return {"status": "error", "message": "Session indisponible."}
        try:
            clean_phone = normalize_phone(phone)

            # Requête groupée pour compter le nombre de Bids reçus par appel d'offre
            stmt = (
                select(
                    Auction,
                    SubCategory.name.label("product_name"),
                    func.count(Bid.id).label("bids_count")
                )
                .join(SubCategory, Auction.sub_category_id == SubCategory.id)
                .join(BuyerProfile, Auction.buyer_id == BuyerProfile.id)
                .join(User, BuyerProfile.user_id == User.id)
                .outerjoin(Bid, Bid.auction_id == Auction.id)
                .where(User.phone == clean_phone, Auction.status == "OPEN")
                .group_by(Auction.id, SubCategory.name)
                .order_by(Auction.created_at.desc())
            )

            results = await current_session.execute(stmt)
            rows = results.all()

            if not rows:
                return {"status": "success", "count": 0, "message": "📢 Vous n'avez aucun appel d'offre actif sur le marché actuellement."}

            menu_lines = ["📊 *Vos appels d'offres en cours :*"]
            auctions_list = []
            mapping_cache = {}

            for i, (auction, p_name, bids_count) in enumerate(rows, start=1):
                diff = auction.deadline - datetime.now()
                time_str = f"{diff.days}j {diff.seconds // 3600}h" if diff.days > 0 else f"{diff.seconds // 3600}h"

                auctions_list.append({"auction_id": str(auction.id), "product": p_name})

                line = (
                    f"\n*{i}. Demande de {p_name}* ({auction.quantity} {auction.unit})\n"
                    f"⏱️ Temps restant : {time_str}\n"
                    f"📩 Propositions reçues : *{bids_count}* d'agriculteurs"
                )
                menu_lines.append(line)
                mapping_cache[str(i)] = str(auction.id)

            menu_lines.append("\n_Pour voir ou accepter les offres d'un lot, affichez ses propositions._")

            return {
                "status": "success",
                "count": len(rows),
                "formatted_menu": "\n".join(menu_lines),
                "mapping": mapping_cache,
                "data": auctions_list
            }
        except Exception as e:
            logger.error(f"❌ Erreur get_my_active_auctions: {e}", exc_info=True)
            return {"status": "error", "message": "Impossible de charger votre tableau de bord."}

    async def get_auction_details(self, auction_id: str) -> Dict[str, Any]:
        """Récupère la fiche signalétique complète d'un appel d'offre spécifique."""
        current_session = self.session
        if not current_session:
            return {"status": "error", "message": "Session indisponible."}
        try:
            a_uuid = uuid.UUID(auction_id)
            stmt = (
                select(
                    Auction,
                    SubCategory.name.label("product_name"),
                    Zone.name.label("zone_name"),
                    BuyerProfile.establishment_name.label("buyer_name")
                )
                .join(SubCategory, Auction.sub_category_id == SubCategory.id)
                .outerjoin(Zone, Auction.target_zone_id == Zone.id)
                .outerjoin(BuyerProfile, Auction.buyer_id == BuyerProfile.id)
                .where(Auction.id == a_uuid)
            )
            result = await current_session.execute(stmt)
            record = result.fetchone()

            if not record:
                return {"status": "error", "message": "Cette opportunité de marché est introuvable."}

            auction, p_name, z_name, b_name = record
            diff = auction.deadline - datetime.now()
            time_left = f"{diff.days}j {diff.seconds // 3600}h" if diff.days > 0 else f"{diff.seconds // 3600}h"

            details = (
                f"📦 *FICHE MARCHÉ : {p_name.upper()}*\n\n"
                f"👤 *Acheteur :* {b_name or 'Acheteur AgriConnect'}\n"
                f"⚖️ *Quantité demandée :* {auction.quantity} {auction.unit}\n"
                f"💰 *Prix plafond accepté :* {auction.max_price_per_unit} CFA / {auction.unit}\n"
                f"📍 *Lieu de livraison :* {z_name or 'Non spécifié'}\n"
                f"⏳ *Clôture du marché :* {time_left}\n"
                f"📝 *Spécifications :* {auction.description or 'Standard (Bonne qualité)'}\n"
                f"🛡️ *Garantie :* Paiement sécurisé AgriConnect Escrow"
            )

            return {
                "status": "success",
                "formatted_details": details,
                "data": {
                    "auction_id": str(auction.id),
                    "product": p_name,
                    "max_price": auction.max_price_per_unit
                }
            }
        except Exception as e:
            logger.error(f"❌ Erreur get_auction_details: {e}", exc_info=True)
            return {"status": "error", "message": "Erreur lors de la récupération des détails."}


    async def get_price_recommendation(self, product_query: str, zone_query: str | None = None) -> Dict[str, Any]:
        """Calcule le prix moyen pratiqué sur le marché pour un produit sur la base des deals conclus (Orders)."""
        current_session = self.session
        if not current_session:
            return {"status": "error", "message": "Session indisponible."}
        try:
            # 1. Résolution floue trigram de la sous-catégorie cible.
            product_query_clean = product_query.strip()
            sub_cat = await current_session.scalar(
                select(SubCategory)
                .where(fuzzy_match(SubCategory.name, product_query_clean))
                .order_by(similarity_rank(SubCategory.name, product_query_clean))
                .limit(1)
            )
            if not sub_cat:
                return {"status": "error", "message": f"Produit '{product_query}' inconnu."}

            # 2. Construction de la requête statistique sur les ordres passés (CONFIRMED)
            # Prix unitaire calculé par le montant de l'ordre divisé par la quantité de l'enchère liée
            stmt = (
                select(
                    func.avg(Order.total_amount / Auction.quantity).label("avg_price"),
                    func.min(Order.total_amount / Auction.quantity).label("min_price"),
                    func.max(Order.total_amount / Auction.quantity).label("max_price")
                )
                .join(Auction, Order.auction_id == Auction.id)
                .where(Auction.sub_category_id == sub_cat.id, Order.status == "CONFIRMED")
            )

            # Filtrage géographique optionnel pour affiner la recommandation locale
            # — recherche floue trigram (référentiel logistique).
            if zone_query and zone_query.strip():
                zone_query_clean = zone_query.strip()
                zone_obj = await current_session.scalar(
                    select(Zone)
                    .where(fuzzy_match(Zone.name, zone_query_clean))
                    .order_by(similarity_rank(Zone.name, zone_query_clean))
                    .limit(1)
                )
                if zone_obj:
                    stmt = stmt.where(Auction.target_zone_id == zone_obj.id)

            stats = (await current_session.execute(stmt)).fetchone()

            # Fallback si l'historique des ventes réelles est vide
            if not stats or stats.avg_price is None:
                return {
                    "status": "success",
                    "has_history": False,
                    "message": f"📊 *Indicateur de prix ({sub_cat.name}) :*\n\nAucun deal historique enregistré pour le moment. Nous vous conseillons de vous baser sur les prix du marché local."
                }

            avg_p, min_p, max_p = round(stats.avg_price, 1), round(stats.min_price, 1), round(stats.max_price, 1)

            msg = (
                f"📊 *Indicateur de prix AgriConnect ({sub_cat.name}) :*\n\n"
                f"Prix moyen constaté : *{avg_p} CFA*\n"
                f"📉 Prix minimum bas : {min_p} CFA\n"
                f"📈 Prix maximum haut : {max_p} CFA\n\n"
                f"_💡 Conseil : Proposer un prix proche de la moyenne augmente vos chances de conclure rapidement._"
            )

            return {
                "status": "success",
                "has_history": True,
                "formatted_recommendation": msg,
                "data": {"average": avg_p, "min": min_p, "max": max_p}
            }
        except Exception as e:
            logger.error(f"❌ Erreur get_price_recommendation: {e}", exc_info=True)
            return {"status": "error", "message": "Calcul de la tendance de prix indisponible."}