from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta
import logging
import unicodedata
import uuid
from rapidfuzz import fuzz, process
from sqlalchemy import select, and_, func, update
from sqlalchemy.orm import aliased
from .common import normalize_phone

# Importation stricte des modèles requis pour le domaine des enchères
from agriconnect.domain.models import Auction, Bid, SubCategory, Zone, BuyerProfile, User,Producer, Order
from .base import BaseMixin

logger = logging.getLogger("agriconnect.services.database.auction")

_FUZZY_SUBCATEGORY_THRESHOLD = 78


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

        
    async def resolve_sub_category(self, name: str) -> Optional[uuid.UUID]:
        """Recherche floue pour récupérer l'ID d'une sous-catégorie de produit."""
        current_session = self.session
        if not current_session:
            return None
        stmt = select(SubCategory.id).where(SubCategory.name.ilike(f"%{name.strip()}%")).limit(1)
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
            logger.error("❌ Aucune session active trouvée")
            return {"status": "error", "message": "Session indisponible."}

        try:
            # 1. Résolution de l'acheteur
            try:
                user_obj, buyer_profile = await self.get_buyer_profile(phone=str(phone))
            except ValueError as e:
                logger.warning(f"⚠️ Échec résolution acheteur : {str(e)}")
                return {"status": "error", "message": "Profil acheteur manquant."}
            
            buyer_id = buyer_profile.id
            target_zone_id = user_obj.zone_id
            final_zone_name = "Zone non spécifiée"

            product_query_clean = str(product_query or "").strip()

            # 2. Résolution du produit
            sub_cat_stmt = (
                select(SubCategory)
                .where(SubCategory.name.ilike(f"%{product_query_clean}%"))
                .order_by(func.length(SubCategory.name).asc())
                .limit(1)
            )
            sub_cat = await current_session.scalar(sub_cat_stmt)
            if not sub_cat:
                sub_cat = await self._fuzzy_match_sub_category(product_query_clean)
            if not sub_cat:
                return {"status": "error", "message": f"Produit '{product_query}' inconnu."}

            # 3. Résolution zone
            if zone_query and zone_query.strip():
                zone_obj = await current_session.scalar(
                    select(Zone).where(Zone.name.ilike(f"%{zone_query.strip()}%")).limit(1)
                )
                if zone_obj:
                    target_zone_id = zone_obj.id
                    final_zone_name = zone_obj.name

            # 4. Validation dates
            if isinstance(deadline, str):
                deadline = datetime.fromisoformat(deadline.replace(" ", "T"))
            
            now = datetime.now()
            if deadline <= now:
                return {"status": "error", "message": "Date limite invalide."}
                
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

            qty_txt = f"{float(qty):g}"
            unit_txt = unit.upper().strip()
            price_txt = f"{float(max_price):g}"
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

        except Exception as e:
            logger.error(f"Erreur critique create_auction: {str(e)}", exc_info=True)
            return {"status": "error", "message": f"Erreur lors de la création de l'enchère : {str(e)}"}
        



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
            return {"status": "error", "message": "Session de base de données indisponible."}
        try:
            # 1. Résolution via le nouveau BaseMixin (Retourne un Tuple d'objets SQL)
            try:
                _, producer_obj = await self.get_producer_profile(phone=str(phone))
            except ValueError as e:
                logger.warning(f"⚠️ Échec de résolution producteur pour {phone} : {str(e)}")
                return {"status": "error", "message": "Votre compte producteur n'est pas encore identifié."}
            
            producer_id = producer_obj.id

            # 2. Validation de la cible
            a_uuid = uuid.UUID(auction_id)
            auction = await current_session.get(Auction, a_uuid)
            
            if not auction:
                return {"status": "error", "message": "Cette opportunité de marché n'existe plus."}
                
            if auction.status.upper() != "OPEN":
                return {"status": "error", "message": f"Désolé, cette enchère est fermée (Statut : {auction.status})."}

            if float(offered_price) <= 0:
                return {"status": "error", "message": "Le prix proposé doit être supérieur à 0 CFA."}

            # 3. Persistance de l'offre (Bid)
            now = datetime.now()
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
                "message": f"✅ Votre offre de *{offered_price} CFA* a été transmise à l'acheteur avec succès."
            }
        except Exception as e:
            logger.error(f"❌ Erreur critique place_bid: {str(e)}", exc_info=True)
            return {"status": "error", "message": "Impossible d'enregistrer votre offre."}

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

            stmt = (
                select(
                    Auction, 
                    SubCategory.name.label("product_name"),
                    Zone.name.label("zone_name"),
                    BuyerProfile.establishment_name.label("buyer_name")
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
                    stmt = stmt.where(User.phone != clean_phone)

            stmt = stmt.where(Auction.status.ilike(status))

            if product_name:
                stmt = stmt.where(SubCategory.name.ilike(f"%{product_name}%"))
            if zone_name:
                stmt = stmt.where(Zone.name.ilike(f"%{zone_name}%"))

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

            for i, (auction, p_name, z_name, b_name) in enumerate(rows, start=1):
                diff = auction.deadline - datetime.now()
                time_str = f"{diff.days}j {diff.seconds // 3600}h" if diff.days > 0 else f"{diff.seconds // 3600}h"

                auctions_list.append({
                    "auction_id": str(auction.id),
                    "product": p_name,
                    "qty": auction.quantity,
                    "unit": auction.unit,
                    "max_price": auction.max_price_per_unit
                })

                line = (
                    f"\n*{i}. {p_name}* ({auction.quantity} {auction.unit})\n"
                    f"💰 Prix Max : *{auction.max_price_per_unit} CFA/{auction.unit}*\n"
                    f"📍 Zone : {z_name or 'Non spécifiée'}\n"
                    f"⏳ Restant : {time_str}\n"
                    f"👤 Client : {b_name or 'Acheteur AgriConnect'}"
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
        
    async def get_auction_bids(self, auction_id: str) -> Dict[str, Any]:
        """Récupère l'ensemble des propositions de prix soumises sur une offre."""
        current_session = self.session
        if not current_session:
            return {"status": "error", "message": "Session de base de données indisponible."}
        try:
            stmt = (
                select(Bid, User.name)
                .join(Producer, Bid.producer_id == Producer.id)
                .join(User, Producer.user_id == User.id)
                .where(Bid.auction_id == uuid.UUID(auction_id))
                .order_by(Bid.offered_price.asc()) 
            )
            results = await current_session.execute(stmt)
            
            bids_list = []
            for bid, prod_name in results:
                bids_list.append({
                    "bid_id": str(bid.id),
                    "producer": prod_name or "Producteur Anonyme",
                    "price": bid.offered_price,
                    "message": bid.message,
                    "delivery": "Non inclus"
                })

            return {
                "status": "success",
                "count": len(bids_list),
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
                    "producer": clean_prod_name
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
                    Auction.unit
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
                return {"status": "success", "count": 0, "message": "Vous n'avez fait aucune proposition de prix pour le moment."}
                
            menu_lines = ["📋 *Le statut de vos propositions :*"]
            status_map = {
                "PENDING": "⏳ En attente",
                "WINNING": "🤝 Acceptée (Félicitations !)",
                "LOST": "❌ Non retenue"
            }
            
            for i, (bid, product_name, qty, unit) in enumerate(rows, start=1):
                friendly_status = status_map.get(str(bid.status).upper(), bid.status)
                
                line = (
                    f"\n*{i}. Demande de {product_name}* ({qty} {unit})\n"
                    f"💰 Votre prix : *{bid.offered_price} CFA*\n"
                    f"📊 Statut : {friendly_status}"
                )
                menu_lines.append(line)
                
            return {
                "status": "success",
                "count": len(rows),
                "formatted_menu": "\n".join(menu_lines)
            }
            
        except Exception as e:
            logger.error(f"❌ Erreur get_my_active_bids: {e}", exc_info=True)
            return {"status": "error", "message": "Impossible de récupérer vos propositions."}


    # ─── SECTION 5 : CLÔTURE ET CONVERSION EN COMMANDE ───────────────────

    async def select_winning_bid(self, bid_id: str) -> Dict[str, Any]:
        """
        Désigne l'offre gagnante, passe l'appel d'offre à l'état 'CLOSED' 
        et instancie la commande officielle (Order) au sein de la transaction courante.
        """
        current_session = self.session
        if not current_session:
            return {"status": "error", "message": "Erreur interne : session de base de données indisponible."}
        try:
            b_id = uuid.UUID(bid_id)
            
            stmt = (
                select(Bid, Auction, User.name.label("producer_name"), SubCategory)
                .join(Auction, Bid.auction_id == Auction.id)
                .join(Producer, Bid.producer_id == Producer.id)
                .join(User, Producer.user_id == User.id)
                .join(SubCategory, Auction.sub_category_id == SubCategory.id)
                .where(Bid.id == b_id)
            )
            res = await current_session.execute(stmt)
            record = res.fetchone()

            if not record:
                return {"status": "error", "message": "Offre introuvable ou retirée par le producteur."}

            bid, auction, prod_name, sub_cat = record

            if auction.status == "CLOSED":
                return {"status": "error", "message": "Cette enchère a déjà été clôturée avec un autre partenaire."}

            # 1. Mises à jour atomiques des états du Marché
            bid.is_winner = True
            bid.status = "WINNING"
            auction.status = "CLOSED"
            auction.winner_bid_id = bid.id
            
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

            clean_prod_name = prod_name or "Producteur Anonyme"
            return {
                "status": "success",
                "order_id": str(new_order.id),
                "summary_buyer": (
                    f"🤝 *Félicitations ! Deal conclu.*\n\n"
                    f"Vous avez choisi l'offre de *{clean_prod_name}*.\n"
                    f"📦 Produit : {sub_cat.name}\n"
                    f"💰 Total à payer : *{total} CFA*\n\n"
                    f"Le producteur a été informé et prépare la livraison."
                ),
                "summary_producer": (
                    f"🎉 *Bonne nouvelle !*\n\n"
                    f"Votre offre pour {auction.quantity} {auction.unit} de {sub_cat.name} a été retenue !\n"
                    f"💵 Montant du marché : *{total} CFA*\n\n"
                    f"Veuillez contacter l'acheteur pour coordonner les détails de la livraison."
                )
            }

        except Exception as e:
            logger.error(f"❌ Erreur critique select_winning_bid: {e}", exc_info=True)
            return {"status": "error", "message": f"Échec technique lors du traitement de validation : {str(e)}"}



    # ==================================================================
    # 5. extra
    # ==================================================================
    async def cancel_auction(self, auction_id: str, phone: str) -> Dict[str, Any]:
        """Annule un appel d'offre ouvert. Sécurisé par le numéro de téléphone de l'acheteur."""
        current_session = self.session
        if not current_session:
            return {"status": "error", "message": "Session de base de données indisponible."}
        try:
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
                return {"status": "error", "message": "Action non autorisée ou marché introuvable."}

            if auction.status.upper() != "OPEN":
                return {"status": "error", "message": f"Impossible d'annuler un marché avec le statut : {auction.status}."}

            # Passage à l'état annulé
            auction.status = "CANCELLED"
            auction.updated_at = datetime.now()
            await current_session.flush()

            return {
                "status": "success",
                "message": "🗑️ Votre appel d'offre a été retiré du marché avec succès."
            }
        except Exception as e:
            logger.error(f"❌ Erreur cancel_auction: {e}", exc_info=True)
            return {"status": "error", "message": "Échec technique lors de l'annulation."}

    async def withdraw_bid(self, bid_id: str, phone: str) -> Dict[str, Any]:
        """Permet à un producteur de retirer sa proposition de prix (Bid)."""
        current_session = self.session
        if not current_session:
            return {"status": "error", "message": "Session de base de données indisponible."}
        try:
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
                return {"status": "error", "message": "Proposition introuvable ou action non autorisée."}

            if bid.status.upper() != "PENDING":
                return {"status": "error", "message": "Impossible de retirer une offre déjà traitée."}

            bid.status = "WITHDRAWN"
            bid.updated_at = datetime.now()
            await current_session.flush()

            return {
                "status": "success",
                "message": "👋 Votre proposition de prix a été retirée avec succès."
            }
        except Exception as e:
            logger.error(f"❌ Erreur withdraw_bid: {e}", exc_info=True)
            return {"status": "error", "message": "Impossible de retirer votre offre."}

    async def check_and_expire_auctions(self) -> Dict[str, Any]:
        """Passe le statut des enchères expirées de 'OPEN' à 'EXPIRED'."""
        current_session = self.session
        if not current_session:
            return {"status": "error", "message": "Session indisponible."}
        try:
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
        except Exception as e:
            logger.error(f"❌ Erreur check_and_expire_auctions: {e}", exc_info=True)
            return {"status": "error", "message": "Erreur lors de la mise à jour des expirations."}

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
            # 1. Résolution floue de la sous-catégorie cible
            sub_cat = await current_session.scalar(
                select(SubCategory)
                .where(SubCategory.name.ilike(f"%{product_query.strip()}%"))
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
            if zone_query and zone_query.strip():
                zone_obj = await current_session.scalar(
                    select(Zone).where(Zone.name.ilike(f"%{zone_query.strip()}%")).limit(1)
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