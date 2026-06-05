from __future__ import annotations

import logging
import uuid

from sqlalchemy.orm import joinedload
from typing import Any, Dict, Tuple
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from agriconnect.domain.models import User, Producer, BuyerProfile, DeliveryAgent, Farm,Zone
from .common import normalize_phone

logger = logging.getLogger("AgriConnect.BaseMixin")

class BaseMixin:
    """
    Mixin de Base - Moteur d'Infrastructure et de Résolution d'Identité.
    Centralise toutes les lectures polymorphes (Téléphone -> Entités/Dict).
    
    CONVENTION DE ROUTAGE UNIFIÉE :
    - Les méthodes ne s'échangent JAMAIS la session explicitement via les arguments.
    - Elles lisent dynamiquement la session via la propriété `self.session`, alimentée
      en arrière-plan par le conteneur de contexte (ContextVar) du service central.
    """

    @property
    def session(self) -> AsyncSession | None:
        """Lit dynamiquement la session depuis l'AgriDatabaseService parent."""
        raise NotImplementedError(
            "La propriété 'session' doit être fournie par la classe de service principale."
        )

    # ==================================================================
    # 1. COEUR INFRASTRUCTURE (Objets SQLAlchemy Bruts & Sérialisation)
    # ==================================================================
    async def create_user_profile(self, data: Dict[str, Any]) -> str:
            """
            CRÉATION POLYMORPHE ATOMIQUE :
            Persiste un User et son profil lié en une seule transaction asynchrone.
            Respecte la signature et les patterns de performance des fonctions de lecture.
            """
            current_session = self.session
            if current_session is None:
                logger.error("Erreur d'infrastructure : Aucune session active pour la création.")
                raise RuntimeError("Database session is missing or uninitialized.")

            try:
                # Transaction asynchrone atomique pour garantir l'intégrité des relations
                async with current_session.begin_nested():
                    # 1. Création de l'entité User (Racine)
                    new_user = User(
                        phone=normalize_phone(data["phone"]),
                        name=data.get("name", "Producteur"),
                        role=data.get("role", "PRODUCER"),
                        zone_id=data.get("zone_id"),
                        onboarding_completed=True
                    )
                    current_session.add(new_user)
                    await current_session.flush() # Récupération de l'ID généré

                    # 2. Création du profil métier (Polymorphisme)
                    role = data.get("role", "PRODUCER")
                    if role == "PRODUCER":
                        profile = Producer(user_id=new_user.id, status="ACTIVE")
                    elif role == "BUYER":
                        profile = BuyerProfile(user_id=new_user.id)
                    elif role == "DELIVERY":
                        profile = DeliveryAgent(user_id=new_user.id)
                    else:
                        profile = None
                    
                    if profile:
                        current_session.add(profile)
                    
                    # Commit explicite de la transaction
                    await current_session.commit()
                    return str(new_user.id)

            except Exception as exc:
                # Rollback automatique en cas d'erreur pour éviter les données orphelines
                await current_session.rollback()
                logger.error(f"Erreur critique lors de la création de profil pour {data.get('phone')}: {exc}", exc_info=True)
                raise RuntimeError(f"Échec de création utilisateur : {str(exc)}") from exc
        

    async def _fetch_user_entities(
        self, phone: str
    ) -> Tuple[User, Producer | None, BuyerProfile | None, DeliveryAgent | None] | None:
        """
        LA REQUÊTE UNIQUE : Jointure polymorphe massive à haute performance.
        Exécutée en une seule passe via le contexte actif pour minimiser la latence réseau.
        """
        if not phone:
            logger.warning("Tentative de fetch utilisateur avec un numéro de téléphone vide ou None.")
            return None

        current_session = self.session
        if current_session is None:
            logger.error("Erreur d'infrastructure : Aucune session active trouvée dans le ContextVar.")
            raise RuntimeError("Database session is missing or uninitialized on the current context.")

        try:
            clean_phone = normalize_phone(phone)
            
            stmt = (
                select(User, Producer, BuyerProfile, DeliveryAgent)
                .outerjoin(Producer, Producer.user_id == User.id)
                .outerjoin(BuyerProfile, BuyerProfile.user_id == User.id)
                .outerjoin(DeliveryAgent, DeliveryAgent.user_id == User.id)
                .outerjoin(Zone, Zone.id == User.zone_id)
                .where(User.phone == clean_phone)
            )
            
            result = await current_session.execute(stmt)
            row = result.first()
            return row if row else None
            
        except Exception as exc:
            logger.error("Erreur critique lors de l'exécution de la requête polymorphe pour %s: %s", phone, exc, exc_info=True)
            raise RuntimeError(f"Impossible de résoudre l'identité pour le numéro {phone} suite à une panne de base de données.") from exc

    def _serialize_user_entities(
        self, row: Tuple[User, Producer | None, BuyerProfile | None, DeliveryAgent | None]
    ) -> Dict[str, Any]:
        """
        TRANSFORMATION EN O(1) : Convertit des entités SQL déjà chargées en mémoire
        en un dictionnaire plat standardisé et blindé pour l'Agent IA.
        Évite les allers-retours redondants avec la base de données.
        """
        user_obj, producer_obj, buyer_obj, delivery_obj,zone_obj = row
        
        # Protection Robustesse : Génération d'un nom de secours en cas de champ vide
        phone_str = user_obj.phone or ""
        name_fallback = user_obj.name or (f"User_{phone_str[-4:]}" if len(phone_str) >= 4 else "User_Unknown")
        
        return {
            "id": str(user_obj.id) if user_obj.id else "",
            "name": name_fallback,
            "phone": phone_str,
            "role": user_obj.role or "USER",
            "zone": { # Retourner un objet zone est plus pratique pour l'agent
            "id": str(zone_obj.id) if zone_obj else None,
            "name": zone_obj.name if zone_obj else "Zone inconnue"
            },
            "longitude": user_obj.longitude,
            "latitude": user_obj.latitude,
            "profile_ids": {
                "producer": str(producer_obj.id) if producer_obj else None,
                "buyer": str(buyer_obj.id) if buyer_obj else None,
                "delivery": str(delivery_obj.id) if delivery_obj else None,
            },
            "status": {
                "producer": producer_obj.status if producer_obj else None,
                "identity_verified": bool(user_obj.identity_verified),
                "whatsapp_enabled": bool(user_obj.whatsapp_enabled),
                "onboarding_completed": bool(getattr(user_obj, "onboarding_completed", False)),
            },
            "permissions": {
                "can_sell": producer_obj is not None or user_obj.role == "ADMIN",
                "can_buy": buyer_obj is not None or user_obj.role == "ADMIN",
                "can_deliver": delivery_obj is not None or user_obj.role == "ADMIN",
                "is_admin": user_obj.role == "ADMIN"
            }
        }

    # ==================================================================
    # 2. INTERFACE PIVOT PUBLIC (Pour l'Agent IA et l'AG-UI)
    # ==================================================================

    async def get_user_by_phone(self, phone: str) -> Dict[str, Any] | None:
        """
        Résout l'identité complète à partir d'un téléphone et retourne le dictionnaire standardisé.
        Garantit qu'aucun bug de mapping interne n'est masqué silencieusement.
        """
        clean_phone = normalize_phone(phone)
        try:
            row = await self._fetch_user_entities(phone=clean_phone)
            if not row:
                return {
                    "status": "NEW_USER",
                    "phone": clean_phone,
                }
            
            return {"status": "SUCCESS", "data": self._serialize_user_entities(row)}

        except (AttributeError, TypeError, KeyError) as bug:
            logger.critical("Bug de logique interne détecté lors du mapping utilisateur pour %s: %s", phone, bug, exc_info=True)
            raise RuntimeError("Erreur interne critique lors de la conversion des profils utilisateurs.") from bug
        except Exception as exc:
            logger.warning("Résolution d'identité impossible ou abandonnée pour %s: %s", phone, exc)
            return None

    async def get_producer_profile(self, phone: str) -> Tuple[User, Producer]:
        """
        Garantit la résolution du couple User/Producer pour les mixins métiers.
        Lève une exception métier explicite si les critères d'accès ne sont pas remplis.
        """
        # 1. Récupération de la ligne brute en BDD via le helper de base
        row = await self._fetch_user_entities(phone=phone)
        
        if not row or not row[0]:
            raise ValueError(f"Aucun compte utilisateur trouvé pour le numéro : {phone}")
        
        # 2. Extraction des objets SQLAlchemy mappés
        user_obj, producer_obj, _, _ = row
        
        # 3. Validation de l'existence du profil producteur rattaché
        if not producer_obj:
            raise ValueError(f"Opération refusée : Le numéro {phone} ne possède pas de profil Producteur.")
            
        return user_obj, producer_obj



    async def get_buyer_profile(self, phone: str) -> Tuple[Any, Any]:
        """Récupère l'objet utilisateur et son profil Acheteur associé à partir du numéro de téléphone.

        Utilise de manière transparente la session de contexte via self.session.
        """
        if not phone:
            raise ValueError("Le numéro de téléphone est requis pour résoudre le profil.")

        # Utilisation de la propriété dynamique interceptée par le wrapper ContextVar
        current_session = self.session
        if not current_session:
            raise RuntimeError("Session de base de données introuvable dans le contexte asynchrone actuel.")

        try:
            self._logger.info("[Profile Resolver] Tentative de récupération du profil acheteur pour %s", phone)
            
            # 1. Sélection de l'utilisateur principal (User Account) via SQLAlchemy pur
            user_stmt = select(User).where(User.phone == str(phone))
            user_result = await current_session.execute(user_stmt)
            user_obj = user_result.scalar_one_or_none()
            
            if not user_obj:
                self._logger.error("[Profile Resolver] Aucun compte utilisateur trouvé pour le téléphone: %s", phone)
                raise ValueError(f"Aucun utilisateur AgriConnect enregistré avec le numéro : {phone}")

            # 2. Récupération du profil Acheteur lié à cet utilisateur
            buyer_stmt = select(BuyerProfile).where(BuyerProfile.user_id == user_obj.id)
            buyer_result = await current_session.execute(buyer_stmt)
            buyer_profile = buyer_result.scalar_one_or_none()
            
            if not buyer_profile:
                self._logger.warning(
                    "[Profile Resolver] Utilisateur trouvé (%s) mais le profil BUYER est inexistant. "
                    "Création automatique d'un profil acheteur invité.", 
                    user_obj.id
                )
                # Initialisation à la volée du profil
                buyer_profile = BuyerProfile(
                    user_id=user_obj.id,
                    establishment_name=f"Acheteur Indépendant ({phone})",
                )
                current_session.add(buyer_profile)
                # flush() génère l'ID en base sans commiter la transaction globale (gérée par le wrapper parent)
                await current_session.flush()

            self._logger.info(
                "[Profile Resolver] Profil résolu avec succès. UserID=%s | BuyerProfileID=%s", 
                user_obj.id, buyer_profile.id
            )
            return user_obj, buyer_profile

        except ValueError as ve:
            raise ve
        except Exception as e:
            self._logger.error(
                "[Profile Resolver] Erreur critique lors de la récupération du profil de %s: %s", 
                phone, str(e), exc_info=True
            )
            raise RuntimeError(f"Échec de la résolution du profil acheteur en base de données : {str(e)}")

    async def get_producer_farm(self, phone: str, *args, **kwargs) -> Dict[str, Any]:
        """Récupère la liste de toutes les exploitations d'un producteur avec leurs stocks 
        pour permettre des mises à jour fluides en langage naturel.
        """
        try:
            clean_phone = str(phone).strip()
            logger.debug(f"[get_producer_farm] Recherche des fermes et stocks pour : {clean_phone}")

            # 🚀 ON AJOUTE LE PRÉCHARGEMENT DES STOCKS (Farm.stocks)
            stmt = (
                select(Farm)
                .join(Farm.producer)
                .join(Producer.user)
                .options(
                    joinedload(Farm.producer),
                    joinedload(Farm.stocks)  # 🔥 Permet à l'agent de voir les poussins/produits de la ferme instantanément
                )
                .where(User.phone == clean_phone)
            )

            result = await self.session.execute(stmt)
            farms = result.scalars().unique().all()

            if not farms:
                return {
                    "status": "success",
                    "message": f"Aucune exploitation trouvée pour le numéro {clean_phone}.",
                    "data": []
                }

            farms_list = []
            for farm in farms:
                # 🐥 On extrait la liste des stocks de cette ferme spécifique
                stocks_list = []
                if hasattr(farm, 'stocks') and farm.stocks:
                    for stock in farm.stocks:
                        stocks_list.append({
                            "stock_id": str(stock.id),
                            "item_name": str(stock.item_name),
                            "quantity": float(stock.quantity) if stock.quantity else 0.0,
                            "unit": str(stock.unit) if stock.unit else "KG",
                            "updated_at": stock.updated_at.isoformat() if stock.updated_at else None
                        })

                farms_list.append({
                    "farm_id": str(farm.id),  
                    "name": str(farm.name),
                    "location": str(farm.location) if farm.location else "Non spécifiée",
                    "size": float(farm.size) if farm.size else 0.0,
                    "soil_type": str(farm.soil_type) if farm.soil_type else "Non spécifié",
                    "water_source": str(farm.water_source) if farm.water_source else "Non spécifiée",
                    "zone_id": str(farm.zone_id) if farm.zone_id else None,
                    "created_at": farm.created_at.isoformat() if farm.created_at else None,
                    "stocks": stocks_list  # 🔥 Injecté dans le payload pour le LLM !
                })

            return {
                "status": "success",
                "message": f"{len(farms_list)} exploitation(s) récupérée(s) avec succès.",
                "data": farms_list
            }

        except Exception as e:
            logger.error(f"❌ Erreur critique dans get_producer_farm : {str(e)}", exc_info=True)
            raise e

    async def get_zone_by_name(self, name: str) -> Dict[str, Any]:
            """
            Résout le nom d'une zone (ex: 'Berrechid') en UUID via la base de données.
            """
            current_session = self.session
            if not current_session:
                raise RuntimeError("Database session missing.")

            try:
                # Recherche insensible à la casse avec 'ilike'
                stmt = select(Zone).where(Zone.name.ilike(name))
                result = await current_session.execute(stmt)
                zone_obj = result.scalar_one_or_none()
                
                if zone_obj:
                    return {
                        "status": "SUCCESS",
                        "data": {"id": str(zone_obj.id), "name": zone_obj.name}
                    }
                
                return {"status": "NOT_FOUND", "message": f"Zone '{name}' introuvable."}
                
            except Exception as e:
                logger.error(f"[BaseMixin] Erreur lors de la résolution de zone '{name}': {e}")
                return {"status": "ERROR", "message": str(e)}