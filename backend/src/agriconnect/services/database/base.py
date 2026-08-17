from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Tuple

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from agriconnect.domain.models import (
    BuyerProfile,
    DeliveryAgent,
    Farm,
    Producer,
    User,
    Zone,
)

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
    async def create_user_profile(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Persiste un User et son profil satellite (idempotent).

        Gère la race condition (IntegrityError sur phone unique) en
        retournant le profil existant au lieu de crasher.
        """
        current_session = self.session
        if current_session is None:
            raise RuntimeError("Database session is missing or uninitialized.")

        clean_phone = normalize_phone(data.get("phone", ""))
        if not clean_phone:
            return {"status": "error", "message": "Numero de telephone invalide."}

        existing = await self._fetch_user_entities(phone=clean_phone)
        if existing:
            serialized = self._serialize_user_entities(existing)
            return {
                "status": "success",
                "data": serialized,
                "message": "already exists",
            }

        raw_zone = data.get("zone_id")
        zone_uuid = uuid.UUID(str(raw_zone)) if raw_zone else None

        try:
            new_user = User(
                phone=clean_phone,
                name=data.get("name", "Utilisateur"),
                role=data.get("role", "BUYER").upper(),
                zone_id=zone_uuid,
                onboarding_completed=True,
            )
            current_session.add(new_user)
            await current_session.flush()

            role = (data.get("role") or "BUYER").upper()
            if role == "PRODUCER":
                current_session.add(
                    Producer(
                        user_id=new_user.id,
                        zone_id=zone_uuid,
                        status="PENDING",
                    )
                )
            elif role == "BUYER":
                current_session.add(BuyerProfile(user_id=new_user.id))
            elif role == "DELIVERY":
                current_session.add(DeliveryAgent(user_id=new_user.id))

            await current_session.flush()
            return {
                "status": "success",
                "data": {"id": str(new_user.id), "phone": clean_phone},
            }

        except IntegrityError:
            await current_session.rollback()
            logger.warning(
                "Race condition sur create_user_profile pour %s — retour profil existant.",
                clean_phone,
            )
            row = await self._fetch_user_entities(phone=clean_phone)
            if row:
                return {
                    "status": "success",
                    "data": self._serialize_user_entities(row),
                    "message": "already exists",
                }
            return {"status": "error", "message": "Creation echouee apres collision."}

    async def _fetch_user_entities(
        self, phone: str
    ) -> (
        Tuple[
            User,
            Producer | None,
            BuyerProfile | None,
            DeliveryAgent | None,
            Zone | None,
        ]
        | None
    ):
        """
        LA REQUÊTE UNIQUE : Jointure polymorphe massive à haute performance.
        Exécutée en une seule passe via le contexte actif pour minimiser la latence réseau.
        """
        if not phone:
            logger.warning(
                "Tentative de fetch utilisateur avec un numéro de téléphone vide ou None."
            )
            return None

        current_session = self.session
        if current_session is None:
            logger.error(
                "Erreur d'infrastructure : Aucune session active trouvée dans le ContextVar."
            )
            raise RuntimeError(
                "Database session is missing or uninitialized on the current context."
            )

        try:
            clean_phone = normalize_phone(phone)

            stmt = (
                select(User, Producer, BuyerProfile, DeliveryAgent, Zone)
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
            logger.error(
                "Erreur critique lors de l'exécution de la requête polymorphe pour %s: %s",
                phone,
                exc,
                exc_info=True,
            )
            raise RuntimeError(
                f"Impossible de résoudre l'identité pour le numéro {phone} suite à une panne de base de données."
            ) from exc

    async def _resolve_producer_phone(
        self,
        *,
        producer_id: uuid.UUID | str | None = None,
        phone: str | None = None,
    ) -> str:
        """Retourne un numéro de téléphone normalisé à partir d'une identité producteur."""

        identity_hint = {
            "producer_id": str(producer_id) if producer_id else None,
            "phone": phone,
        }
        logger.debug("resolve_producer_phone:start", extra={"identity": identity_hint})

        if phone:
            cleaned = normalize_phone(phone)
            if cleaned:
                logger.debug(
                    "resolve_producer_phone:phone_normalized",
                    extra={"identity": identity_hint, "resolved_phone": cleaned},
                )
                return cleaned

        if producer_id is None:
            raise ValueError(
                "Identité producteur manquante : phone ou producer_id requis."
            )

        try:
            producer_uuid = uuid.UUID(str(producer_id))
        except (TypeError, ValueError) as exc:
            cleaned = normalize_phone(str(producer_id))
            if cleaned:
                return cleaned
            raise ValueError(
                "Identifiant producteur invalide : impossible de déterminer le téléphone."
            ) from exc

        current_session = self.session
        if current_session is None:
            raise RuntimeError(
                "Session de base de données introuvable pour la résolution d'identité producteur."
            )

        stmt = (
            select(User.phone)
            .join(Producer, Producer.user_id == User.id)
            .where(Producer.id == producer_uuid)
            .limit(1)
        )
        result = await current_session.execute(stmt)
        phone_value = result.scalar_one_or_none()
        cleaned_phone = normalize_phone(phone_value) if phone_value else None
        if not cleaned_phone:
            raise ValueError(
                "Numéro de téléphone introuvable pour le producteur fourni."
            )
        logger.debug(
            "resolve_producer_phone:resolved_from_uuid",
            extra={"identity": identity_hint, "resolved_phone": cleaned_phone},
        )
        return cleaned_phone

    def _serialize_user_entities(
        self,
        row: Tuple[
            User,
            Producer | None,
            BuyerProfile | None,
            DeliveryAgent | None,
            Zone | None,
        ],
    ) -> Dict[str, Any]:
        """
        TRANSFORMATION EN O(1) : Convertit des entités SQL déjà chargées en mémoire
        en un dictionnaire plat standardisé et blindé pour l'Agent IA.
        Évite les allers-retours redondants avec la base de données.
        """
        user_obj, producer_obj, buyer_obj, delivery_obj, zone_obj = row

        # Protection Robustesse : Génération d'un nom de secours en cas de champ vide
        phone_str = user_obj.phone or ""
        name_fallback = user_obj.name or (
            f"User_{phone_str[-4:]}" if len(phone_str) >= 4 else "User_Unknown"
        )

        return {
            "id": str(user_obj.id) if user_obj.id else "",
            "name": name_fallback,
            "phone": phone_str,
            "role": user_obj.role or "USER",
            "zone": {  # Retourner un objet zone est plus pratique pour l'agent
                "id": str(zone_obj.id) if zone_obj else None,
                "name": zone_obj.name if zone_obj else "Zone inconnue",
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
                "onboarding_completed": bool(
                    getattr(user_obj, "onboarding_completed", False)
                ),
            },
            "permissions": {
                "can_sell": producer_obj is not None or user_obj.role == "ADMIN",
                "can_buy": buyer_obj is not None or user_obj.role == "ADMIN",
                "can_deliver": delivery_obj is not None or user_obj.role == "ADMIN",
                "is_admin": user_obj.role == "ADMIN",
            },
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

            return {"status": "success", "data": self._serialize_user_entities(row)}

        except (AttributeError, TypeError, KeyError) as bug:
            logger.critical(
                "Bug de logique interne détecté lors du mapping utilisateur pour %s: %s",
                phone,
                bug,
                exc_info=True,
            )
            raise RuntimeError(
                "Erreur interne critique lors de la conversion des profils utilisateurs."
            ) from bug
        except Exception as exc:
            logger.warning(
                "Résolution d'identité impossible ou abandonnée pour %s: %s", phone, exc
            )
            return None

    async def get_producer_profile(self, phone: str) -> Tuple[User, Producer]:
        """
        Garantit la résolution du couple User/Producer pour les mixins métiers.
        Lève une exception métier explicite si les critères d'accès ne sont pas remplis.
        """
        # 1. Récupération de la ligne brute en BDD via le helper de base
        row = await self._fetch_user_entities(phone=phone)

        if not row or not row[0]:
            raise ValueError(
                f"Aucun compte utilisateur trouvé pour le numéro : {phone}"
            )

        # 2. Extraction des objets SQLAlchemy mappés
        user_obj, producer_obj, _, _, _ = row

        # 3. Validation de l'existence du profil producteur rattaché
        if not producer_obj:
            raise ValueError(
                f"Opération refusée : Le numéro {phone} ne possède pas de profil Producteur."
            )

        return user_obj, producer_obj

    async def get_buyer_profile(self, phone: str) -> Tuple[Any, Any]:
        """Récupère l'objet utilisateur et son profil Acheteur associé à partir du numéro de téléphone.

        Utilise de manière transparente la session de contexte via self.session.
        """
        if not phone:
            raise ValueError(
                "Le numéro de téléphone est requis pour résoudre le profil."
            )

        # Utilisation de la propriété dynamique interceptée par le wrapper ContextVar
        current_session = self.session
        if not current_session:
            raise RuntimeError(
                "Session de base de données introuvable dans le contexte asynchrone actuel."
            )

        try:
            self._logger.info(
                "[Profile Resolver] Tentative de récupération du profil acheteur pour %s",
                phone,
            )

            # 1. Sélection de l'utilisateur principal (User Account) via SQLAlchemy pur
            user_stmt = select(User).where(User.phone == str(phone))
            user_result = await current_session.execute(user_stmt)
            user_obj = user_result.scalar_one_or_none()

            if not user_obj:
                self._logger.error(
                    "[Profile Resolver] Aucun compte utilisateur trouvé pour le téléphone: %s",
                    phone,
                )
                raise ValueError(
                    f"Aucun utilisateur AgriConnect enregistré avec le numéro : {phone}"
                )

            # 2. Récupération du profil Acheteur lié à cet utilisateur
            buyer_stmt = select(BuyerProfile).where(BuyerProfile.user_id == user_obj.id)
            buyer_result = await current_session.execute(buyer_stmt)
            buyer_profile = buyer_result.scalar_one_or_none()

            if not buyer_profile:
                self._logger.warning(
                    "[Profile Resolver] Utilisateur trouvé (%s) mais le profil BUYER est inexistant. "
                    "Création automatique d'un profil acheteur invité.",
                    user_obj.id,
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
                user_obj.id,
                buyer_profile.id,
            )
            return user_obj, buyer_profile

        except ValueError as ve:
            raise ve
        except Exception as e:
            self._logger.error(
                "[Profile Resolver] Erreur critique lors de la récupération du profil de %s: %s",
                phone,
                str(e),
                exc_info=True,
            )
            raise RuntimeError(
                f"Échec de la résolution du profil acheteur en base de données : {str(e)}"
            ) from e

    async def get_producer_farm(self, phone: str, *args, **kwargs) -> Dict[str, Any]:
        """Récupère la liste de toutes les exploitations d'un producteur avec leurs stocks
        pour permettre des mises à jour fluides en langage naturel.
        """
        try:
            clean_phone = normalize_phone(phone) or str(phone).strip()
            logger.debug(
                f"[get_producer_farm] Recherche des fermes et stocks pour : {clean_phone}"
            )

            # 🚀 ON AJOUTE LE PRÉCHARGEMENT DES STOCKS (Farm.stocks)
            stmt = (
                select(Farm)
                .join(Farm.producer)
                .join(Producer.user)
                .options(
                    joinedload(Farm.producer),
                    joinedload(
                        Farm.stocks
                    ),  # 🔥 Permet à l'agent de voir les poussins/produits de la ferme instantanément
                )
                .where(User.phone == clean_phone)
            )

            result = await self.session.execute(stmt)
            farms = result.scalars().unique().all()

            if not farms:
                return {
                    "status": "success",
                    "message": f"Aucune exploitation trouvée pour le numéro {clean_phone}.",
                    "data": [],
                }

            farms_list = []
            for farm in farms:
                # 🐥 On extrait la liste des stocks de cette ferme spécifique
                stocks_list = []
                if hasattr(farm, "stocks") and farm.stocks:
                    for stock in farm.stocks:
                        stocks_list.append(
                            {
                                "stock_id": str(stock.id),
                                "item_name": str(stock.item_name),
                                "quantity": float(stock.quantity)
                                if stock.quantity
                                else 0.0,
                                "unit": str(stock.unit) if stock.unit else "KG",
                                "updated_at": stock.updated_at.isoformat()
                                if stock.updated_at
                                else None,
                            }
                        )

                farms_list.append(
                    {
                        "farm_id": str(farm.id),
                        "name": str(farm.name),
                        "location": str(farm.location)
                        if farm.location
                        else "Non spécifiée",
                        "size": float(farm.size) if farm.size else 0.0,
                        "zone_id": str(farm.zone_id) if farm.zone_id else None,
                        "created_at": farm.created_at.isoformat()
                        if farm.created_at
                        else None,
                        "stocks": stocks_list,
                    }
                )

            return {
                "status": "success",
                "message": f"{len(farms_list)} exploitation(s) récupérée(s) avec succès.",
                "data": farms_list,
            }

        except Exception as e:
            logger.error(
                f"❌ Erreur critique dans get_producer_farm : {str(e)}", exc_info=True
            )
            raise e

    async def get_zone_by_name(self, name: str) -> Dict[str, Any]:
        """
        Résout le nom d'une zone via une recherche par similarité (trigram).
        """
        current_session = self.session
        if not current_session:
            raise RuntimeError("Database session missing.")

        try:
            logger.info(
                "[BaseMixin] get_zone_by_name session=%s (id=%s) name=%s",
                current_session,
                hex(id(current_session)),
                name,
            )
            # 1. Recherche par similarité trigram
            # Le threshold par défaut est 0.3. On utilise l'opérateur '%'
            # pour comparer la similarité entre la colonne et l'input.
            stmt = (
                select(Zone)
                .where(Zone.name.op("%")(name))
                .order_by(func.similarity(Zone.name, name).desc())
                .limit(1)
            )

            result = await current_session.execute(stmt)
            zone_obj = result.scalar_one_or_none()

            if zone_obj:
                return {
                    "status": "success",
                    "data": {"id": str(zone_obj.id), "name": zone_obj.name},
                }

            return {"status": "error", "message": f"Zone '{name}' introuvable."}

        except Exception as e:
            logger.error(
                f"[BaseMixin] Erreur lors de la résolution de zone '{name}': {e}"
            )
            return {"status": "error", "message": str(e)}

    async def get_available_zones(self) -> list[dict[str, Any]]:
        """
        Récupère la liste de toutes les zones disponibles.
        """
        current_session = self.session
        if not current_session:
            raise RuntimeError("Database session missing.")

        try:
            logger.info(
                "[BaseMixin] get_available_zones session=%s (id=%s)",
                current_session,
                hex(id(current_session)),
            )
            stmt = select(Zone)
            result = await current_session.execute(stmt)
            zones = result.scalars().all()

            return {
                "status": "success",
                "data": [{"id": str(z.id), "label": z.name} for z in zones],
            }

        except Exception as e:
            logger.error(f"[BaseMixin] Erreur lors de la récupération des zones: {e}")
            return {"status": "error", "message": str(e)}
