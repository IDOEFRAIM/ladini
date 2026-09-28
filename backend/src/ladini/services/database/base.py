from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Tuple

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from ladini.domain.models import (
    BuyerProfile,
    DeliveryAgent,
    Farm,
    Producer,
    SubCategory,
    User,
    Zone,
)

from .common import normalize_phone
from .errors import BusinessRuleException
from .search import fuzzy_match, similarity_rank

logger = logging.getLogger("Ladini.BaseMixin")


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
    # 0. GARDE DE PROPRIÉTÉ (IDOR) — audit sécurité agent 2026-09-10
    # ==================================================================
    async def _assert_farm_owned_by(self, farm_id: Any, phone: str | None) -> Farm:
        """Refuse un ``farm_id`` qui n'appartient pas à ``phone``. Fail-closed.

        ⚠️ POURQUOI CE GARDE EXISTE — un `farm_id` n'est PAS une preuve
        d'autorisation, c'est juste un UUID qui circule. Plusieurs outils
        d'écriture (`add_stock`, `remove_stock`, `add_expense`, `update_farm`)
        le recevaient comme SEUL critère de ciblage, sans jamais vérifier à qui
        la ferme appartient : `select(Farm).where(Farm.id == farm_id)` trouve
        n'importe quelle ferme de la plateforme.

        Or `farm_id` est un slot déclaré (`core/slots.py`), donc une valeur qui
        peut arriver dans `transaction_payload` par extraction LLM ou par
        `form_data` — c'est-à-dire, in fine, depuis le texte de l'utilisateur.
        Combiné à `ensure_farm_node` qui court-circuitait dès qu'un `farm_id`
        était déjà présent (sans le valider), un producteur pouvait viser
        l'exploitation d'un CONCURRENT : lui ajouter du stock fantôme, lui en
        RETIRER (destruction d'inventaire vendable), ou lui imputer des
        dépenses.

        La leçon générale, pour tout nouvel outil : dans un agent, tout
        identifiant de ressource venu du payload est une entrée utilisateur.
        La seule identité digne de confiance est celle liée au tour
        (`state["user_phone"]`, issue du webhook signé).

        Retourne l'objet ``Farm`` vérifié (évite au site d'appel une seconde
        requête pour la même ligne).
        """
        current_session = self.session
        if current_session is None:
            raise RuntimeError("Database session is missing or uninitialized.")

        # `required=False` + try/except : `normalize_phone(..., required=True)`
        # lève un `ValueError` brut sur une valeur vide/blanche. Une garde de
        # sécurité doit produire UN SEUL type de refus explicite, jamais
        # laisser fuiter une exception d'une autre couche (que l'appelant
        # pourrait traiter comme une panne technique — donc réessayer — au
        # lieu d'un refus d'autorisation).
        try:
            clean_phone = normalize_phone(phone, required=False) if phone else ""
        except (ValueError, TypeError):
            clean_phone = ""
        if not clean_phone:
            # Pas d'identité = pas d'autorisation possible. Refuser, jamais
            # « laisser passer parce qu'on ne sait pas ».
            raise BusinessRuleException(
                "Opération refusée : identité de l'appelant manquante.",
                reason="missing_caller_identity",
            )

        try:
            farm_uuid = uuid.UUID(str(farm_id))
        except (TypeError, ValueError) as exc:
            raise BusinessRuleException(
                "Identifiant d'exploitation invalide.", reason="invalid_farm_id"
            ) from exc

        stmt = (
            select(Farm)
            .join(Producer, Producer.id == Farm.producer_id)
            .join(User, User.id == Producer.user_id)
            .where(Farm.id == farm_uuid, User.phone == clean_phone)
            .limit(1)
        )
        farm = (await current_session.execute(stmt)).scalars().first()
        if farm is None:
            # Message volontairement identique pour « n'existe pas » et
            # « existe mais appartient à un autre » : ne pas transformer ce
            # refus en oracle permettant d'énumérer les fermes de la plateforme.
            logger.warning(
                "FARM_OWNERSHIP_DENIED | farm_id=%s | caller=%s", farm_id, clean_phone
            )
            raise BusinessRuleException(
                "Exploitation introuvable ou non autorisée.",
                reason="farm_not_owned",
            )
        return farm

    async def _assert_stock_owned_by(self, stock_id: Any, phone: str | None) -> None:
        """Refuse un ``stock_id`` dont l'exploitation n'appartient pas à ``phone``.

        Même classe de faille que ``_assert_farm_owned_by``, côté LECTURE :
        ``get_stock_movements(stock_id)`` renvoyait l'historique complet des
        mouvements de n'importe quelle ligne de stock de la plateforme. Sur une
        place de marché où les producteurs s'affrontent en enchères inversées,
        la rotation d'inventaire d'un concurrent est une information
        directement monétisable.
        """
        from ladini.domain.models import Stock  # import local : évite un cycle

        current_session = self.session
        if current_session is None:
            raise RuntimeError("Database session is missing or uninitialized.")

        try:
            clean_phone = normalize_phone(phone, required=False) if phone else ""
        except (ValueError, TypeError):
            clean_phone = ""
        if not clean_phone:
            raise BusinessRuleException(
                "Opération refusée : identité de l'appelant manquante.",
                reason="missing_caller_identity",
            )

        try:
            stock_uuid = uuid.UUID(str(stock_id))
        except (TypeError, ValueError) as exc:
            raise BusinessRuleException(
                "Identifiant de stock invalide.", reason="invalid_stock_id"
            ) from exc

        stmt = (
            select(Stock.id)
            .join(Farm, Farm.id == Stock.farm_id)
            .join(Producer, Producer.id == Farm.producer_id)
            .join(User, User.id == Producer.user_id)
            .where(Stock.id == stock_uuid, User.phone == clean_phone)
            .limit(1)
        )
        if (await current_session.execute(stmt)).scalars().first() is None:
            logger.warning(
                "STOCK_OWNERSHIP_DENIED | stock_id=%s | caller=%s",
                stock_id,
                clean_phone,
            )
            raise BusinessRuleException(
                "Lot introuvable ou non autorisé.", reason="stock_not_owned"
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
        # (mandat onboarding 2026-09-26, migration 0005) : `coverage_status` a un défaut DB
        # ('COVERED') pour les appelants qui n'en fournissent pas (parité avant/après ce
        # correctif) — mais l'onboarding fournit toujours une valeur explicite dès que la zone
        # a été traitée (voir agents/onboarding.py::_step_create_profile).
        coverage_status = data.get("coverage_status")
        declared_location = data.get("declared_location")

        try:
            new_user = User(
                phone=clean_phone,
                name=data.get("name", "Utilisateur"),
                role=data.get("role", "BUYER").upper(),
                zone_id=zone_uuid,
                declared_location=declared_location,
                **({"coverage_status": coverage_status} if coverage_status else {}),
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
                    f"Aucun utilisateur Ladini enregistré avec le numéro : {phone}"
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

        (2026-09-18, retour produit onboarding) — restreint aux zones RACINES
        (`parent_id IS NULL`, la "région" au sens de `buyer.py`, ex: Hauts-
        Bassins) plutôt que sur toute la table (qui mélange région ET ville/
        village, cette dernière étant le niveau habituellement assigné aux
        users/producers/farms — voir `Zone.parent_id`). Une ville précise a
        beaucoup moins de chances d'être déjà en base qu'une région large ;
        forcer une résolution au niveau ville faisait donc souvent échouer
        l'onboarding sur une localité non répertoriée, alors que la région
        englobante existe presque toujours. Compromis assumé (choix produit) :
        la logique "circuit ultra-court même ville" de `buyer.py` (comparaison
        `producer_zone_id == buyer_zone_id`) devient de facto "même région"
        pour tout profil créé via l'onboarding — voir aussi
        docs/ONBOARDING_ZONE_REGION_LEVEL_2026-09-18.md.
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
            # 1. Recherche par similarité trigram, RÉGIONS UNIQUEMENT
            # (parent_id IS NULL). Le threshold par défaut est 0.3. On utilise
            # l'opérateur '%' pour comparer la similarité entre la colonne et
            # l'input.
            stmt = (
                select(Zone)
                .where(Zone.name.op("%")(name))
                .where(Zone.parent_id.is_(None))
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

    async def get_zone_hierarchy_by_name(self, name: str) -> Dict[str, Any]:
        """Résout *name* à N'IMPORTE QUEL niveau (région OU ville/village — sans la
        restriction `parent_id IS NULL` de `get_zone_by_name`), puis remonte la chaîne
        `parent_id` jusqu'à la région racine correspondante.

        Mandat onboarding 2026-09-26 : "Somgandé" (jamais seedée en base comme région
        racine) doit pouvoir se rattacher à sa région englobante ("Ouagadougou") SI ET
        SEULEMENT SI cette hiérarchie existe déjà dans `governance.zones` — ne DEVINE
        jamais un rattachement absent du référentiel (aucun nom de localité/région en dur
        ici, contrairement à l'ancien comportement documenté dans
        docs/ONBOARDING_ZONE_REGION_LEVEL_2026-09-18.md). `status: "error"` si rien ne
        matche à aucun niveau — l'appelant traite alors la localité comme hors couverture
        plutôt que d'inventer un rattachement.

        Borne anti-cycle (`_MAX_HOPS`) : `parent_id` est une FK auto-référencée sans
        contrainte d'acyclicité côté DB — une chaîne corrompue ne doit jamais boucler
        indéfiniment ici."""
        current_session = self.session
        if not current_session:
            raise RuntimeError("Database session missing.")

        _MAX_HOPS = 5
        try:
            stmt = (
                select(Zone)
                .where(Zone.name.op("%")(name))
                .order_by(func.similarity(Zone.name, name).desc())
                .limit(1)
            )
            result = await current_session.execute(stmt)
            matched = result.scalar_one_or_none()
            if matched is None:
                return {"status": "error", "message": f"Zone '{name}' introuvable."}

            root = matched
            hops = 0
            while root.parent_id is not None and hops < _MAX_HOPS:
                parent = await current_session.get(Zone, root.parent_id)
                if parent is None:
                    break
                root = parent
                hops += 1

            return {
                "status": "success",
                "data": {
                    "matched": {"id": str(matched.id), "name": matched.name},
                    "root": (
                        {"id": str(root.id), "name": root.name}
                        if root.parent_id is None
                        else None
                    ),
                },
            }
        except Exception as e:
            logger.error(
                f"[BaseMixin] Erreur lors de la résolution hiérarchique de zone '{name}': {e}"
            )
            return {"status": "error", "message": str(e)}

    async def get_available_zones(self) -> list[dict[str, Any]]:
        """
        Récupère la liste des zones RACINES disponibles ("région" au sens de
        `buyer.py` — `parent_id IS NULL`), pas les villes/villages enfants.

        (2026-09-18, retour produit onboarding ; comportement de rejet retiré le
        2026-09-26 — voir `agents/onboarding.py::_resolve_zone`, qui ne bloque plus
        jamais l'onboarding sur une zone non couverte) — catalogue public générique
        des zones de service, exposé en ressource MCP en lecture seule (voir
        `infrastructure/mcp/exposure.py`). Proposer des villes précises listait souvent
        des localités que
        l'utilisateur ne reconnaît pas forcément lui-même ; les régions,
        moins nombreuses et plus larges, sont plus facilement reconnaissables
        et évitent une deuxième non-correspondance. Voir `get_zone_by_name`
        (même fichier) pour le même raisonnement côté résolution.
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
            stmt = select(Zone).where(Zone.parent_id.is_(None))
            result = await current_session.execute(stmt)
            zones = result.scalars().all()

            return {
                "status": "success",
                "data": [{"id": str(z.id), "label": z.name} for z in zones],
            }

        except Exception as e:
            logger.error(f"[BaseMixin] Erreur lors de la récupération des zones: {e}")
            return {"status": "error", "message": str(e)}

    async def get_product_category_unit_config(self, product_name: str) -> Dict[str, Any]:
        """Config d'unités (ensemble autorisé + unité prioritaire) pour la
        sous-catégorie dont le nom matche `product_name`.

        (2026-09-19, retour produit) — remplace le pari fait sur le texte
        libre par la config ADMIN de la sous-catégorie, sur le même patron que
        `SubCategory.minimum_order_quantity`/`minimum_order_unit`
        (`domain/governance/models.py`, `domain/order_policy.py`). Consommée
        par `resolve_product_unit(..., category_config=...)`
        (`domain/quantity_unit.py`).

        (2026-09-28, correction : `priority_unit`/`allowed_units` EXISTENT
        bien dans `governance.sub_categories` depuis la migration Drizzle de
        référence — `schema_contract/migrations/0000_baseline.sql` les
        déclare déjà. Cette docstring affirmait auparavant l'inverse ; c'était
        faux depuis le début (vérifié par audit du mandat "Commercial
        Quantity & Pricing Domain Hardening"). Le SELECT brut (hors ORM) dans
        un SAVEPOINT dédié (`begin_nested`) est conservé par prudence — il ne
        coûte rien et protège contre un futur schéma qui les retirerait —
        mais ce N'EST PLUS pourquoi cette méthode renvoie souvent
        `{"status": "error", "message": "Aucune config d'unité..."}` : la
        cause réelle, aujourd'hui, est qu'AUCUNE sous-catégorie n'a encore
        ces colonnes RENSEIGNÉES (un problème de DONNÉE administrative, pas de
        schéma ni de code) — voir
        `docs/domain/COMMERCIAL_QUANTITY_PRICING_MODEL.md` pour le détail et
        la recommandation (peupler `allowed_units`/`priority_unit` pour les
        sous-catégories à risque de confusion, ex. "Lait" -> `[LITRE, ML]`,
        "Bovins vivants" -> `[TETE]`). Consommée par
        `resolve_product_unit(..., category_config=...)`
        (`domain/quantity_unit.py`).
        """
        current_session = self.session
        if not current_session:
            raise RuntimeError("Database session missing.")

        name_clean = (product_name or "").strip()
        if not name_clean:
            return {"status": "error", "message": "product_name vide."}

        try:
            # 1. Résolution du produit -> sous-catégorie, même patron
            # trigram que `producer.py::create_product`.
            sub_cat_stmt = (
                select(SubCategory.id)
                .where(fuzzy_match(SubCategory.name, name_clean))
                .order_by(similarity_rank(SubCategory.name, name_clean))
                .limit(1)
            )
            sub_category_id = await current_session.scalar(sub_cat_stmt)
            if not sub_category_id:
                return {
                    "status": "error",
                    "message": f"Sous-categorie introuvable pour '{product_name}'.",
                }

            # 2. Lecture défensive des colonnes futures. SAVEPOINT propre :
            # si `priority_unit`/`allowed_units` n'existent pas encore, seul
            # ce point de reprise est annulé (jamais toute la transaction).
            row = None
            try:
                async with current_session.begin_nested():
                    raw = await current_session.execute(
                        text(
                            "SELECT priority_unit, allowed_units "
                            "FROM governance.sub_categories WHERE id = :id"
                        ),
                        {"id": sub_category_id},
                    )
                    row = raw.mappings().first()
            except (ProgrammingError, OperationalError) as exc:
                logger.info(
                    "[BaseMixin] get_product_category_unit_config: colonnes "
                    "unite pas encore en base (%s) — comportement historique.",
                    exc,
                )
                row = None

            allowed_units = list(row["allowed_units"]) if row and row.get("allowed_units") else []
            if not allowed_units:
                return {
                    "status": "error",
                    "message": "Aucune config d'unite pour cette sous-categorie.",
                }

            return {
                "status": "success",
                "data": {
                    "priority_unit": row.get("priority_unit"),
                    "allowed_units": allowed_units,
                },
            }

        except Exception as e:
            logger.error(
                f"[BaseMixin] Erreur get_product_category_unit_config('{product_name}'): {e}"
            )
            return {"status": "error", "message": str(e)}
