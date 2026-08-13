import logging
import uuid
from typing import Dict, Any, Optional

from sqlalchemy import select, update, func
from sqlalchemy.exc import IntegrityError
from agriconnect.domain.identity.models import User, Producer
from agriconnect.core.geofencing import is_within_burkina_faso, OUT_OF_COUNTRY_MESSAGE
from .common import clean_text, normalize_phone

logger = logging.getLogger("AgriConnect.AuthMixin")

class AuthMixin:
    """
    AuthMixin - Couche Spécialisée d'Écriture et Mutations d'Identité Noyau.
    Hérite des capacités de lecture de BaseMixin pour valider les états.
    
    CONVENTION DE ROUTAGE UNIFIÉE :
    - Les méthodes ne reçoivent plus 'session' en argument.
    - Elles lisent 'self.session' issue du conteneur de contexte (ContextVar).
    - Pas de commits sauvages ici (laissé au dispatcher principal de l'AgriDatabaseService).
    """

    # ==================================================================
    # 1. MUTATIONS ET ONBOARDING D'IDENTITÉ NOYAU
    # ==================================================================

    async def identify_or_create_user(
        self, 
        phone: str, 
        name: Optional[str] = None, 
        zone_id: Optional[str] = None,
        initial_role: str = "USER"
    ) -> Optional[Dict[str, Any]]:
        """
        Onboarding intelligent du compte noyau de l'utilisateur.
        Résilient aux écritures concurrentes grâce à une capture d'IntegrityError.
        """
        clean_phone = normalize_phone(phone)
        if not clean_phone:
            logger.warning("Tentative d'onboarding avec un numéro de téléphone invalide.")
            return {"status": "error", "message": "Numéro de téléphone invalide."}

        current_session = self.session
        if current_session is None:
            raise RuntimeError("Database session is missing on the current context.")

        # 1. Idempotence : Vérification immédiate de l'existence via le BaseMixin
        existing_row = await self._fetch_user_entities(phone=clean_phone)
        if existing_row:
            return self._serialize_user_entities(existing_row)

        try:
            # 2. Préparation atomique de l'identité noyau
            user_uuid = uuid.uuid4()
            new_user = User(
                id=user_uuid,
                phone=clean_phone,
                name=clean_text(name, "name") if name else "Utilisateur",
                role=initial_role.upper(),
                zone_id=uuid.UUID(zone_id) if zone_id else None,
                whatsapp_enabled=True,
                identity_verified=False,
                onboarding_completed=False,
            )
            current_session.add(new_user)
            
            # 3. Allocation minimale du profil satellite si requis d'entrée de jeu
            if initial_role.upper() == "PRODUCER":
                producer = Producer(
                    id=uuid.uuid4(),
                    user_id=user_uuid,
                    status="PENDING",
                    zone_id=new_user.zone_id
                )
                current_session.add(producer)

            # Émission forcée vers le moteur SQL pour valider les contraintes de clés uniques
            await current_session.flush()
            
            return await self.get_user_by_phone(phone=clean_phone)

        except IntegrityError as race_condition:
            # Gestion d'une concurrence d'écriture : un autre thread/agent a créé le user au même millième de seconde
            await current_session.rollback()
            logger.warning("Collision d'écriture détectée (Race Condition) pour le numéro %s. Redirection.", clean_phone)
            return await self.get_user_by_phone(phone=clean_phone)
            
        except Exception as e:
            logger.error("❌ Erreur critique lors de l'onboarding de %s : %s", clean_phone, e, exc_info=True)
            return {"status": "error", "message": "Échec technique de la création du compte."}

    # ==================================================================
    # 2. VALIDATIONS ET SÉCURISATION DES PROFILS CORES
    # ==================================================================

    async def update_geo_location(
        self,
        user_id: Optional[str] = None,
        lat: Optional[float] = None,
        lon: Optional[float] = None,
        *,
        phone: Optional[str] = None,
    ) -> Dict[str, str]:
        """Met à jour les coordonnées géographiques (météo, proximité, logistique).

        Accepte soit `user_id` (UUID déjà résolu), soit `phone` (résolu en
        interne) — l'ingestion webhook natif WhatsApp ne connaît que le
        numéro de téléphone, jamais l'UUID interne. Met aussi à jour
        `location_updated_at`, jamais bloquant en cas d'absence de coordonnées
        (l'appelant ne devrait simplement pas invoquer cette méthode dans ce cas).
        """
        current_session = self.session
        if current_session is None:
            raise RuntimeError("Database session is missing on the current context.")

        if lat is None or lon is None:
            return {"status": "error", "message": "Latitude et longitude sont requises."}

        # Validation rapide des plages géographiques (Anti-corruption de données)
        if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
            return {"status": "error", "message": "Coordonnées géographiques hors limites mathématiques."}

        # Geofencing Burkina Faso — défense en profondeur : cette méthode est
        # aussi un outil MCP appelable hors du webhook Twilio (qui fait déjà
        # sa propre vérification côté `_persist_location_background`), donc
        # ne JAMAIS supposer que l'appelant a déjà validé le pays.
        # `reason` distingue ce cas précis pour l'appelant (message dédié
        # côté webhook) plutôt qu'un texte générique.
        if not is_within_burkina_faso(lat, lon):
            return {
                "status": "error",
                "message": OUT_OF_COUNTRY_MESSAGE,
                "reason": "out_of_country",
            }

        resolved_id: Optional[uuid.UUID] = None
        if user_id:
            try:
                resolved_id = uuid.UUID(str(user_id))
            except (TypeError, ValueError):
                resolved_id = None

        if resolved_id is None and phone:
            clean_phone = normalize_phone(phone)
            row = await current_session.execute(
                select(User.id).where(User.phone == clean_phone)
            )
            resolved_id = row.scalar_one_or_none()

        if resolved_id is None:
            return {"status": "error", "message": "Utilisateur introuvable pour la mise à jour de la position."}

        try:
            stmt = (
                update(User)
                .where(User.id == resolved_id)
                .values(latitude=lat, longitude=lon, location_updated_at=func.now(), updated_at=func.now())
            )
            await current_session.execute(stmt)
            return {"status": "success", "message": "Géolocalisation mise à jour avec succès."}
        except Exception as e:
            logger.error("Erreur update_geo_location pour l'utilisateur %s: %s", resolved_id, e)
            return {"status": "error", "message": "Erreur d'infrastructure lors de la mise à jour géographique."}

    async def verify_user_identity(self, user_id: str, cnib_number: str) -> bool:
        """Enregistre et valide la pièce d'identité nationale (CNIB - Burkina Faso) au niveau Core."""
        current_session = self.session
        if current_session is None:
            raise RuntimeError("Database session is missing on the current context.")

        clean_cnib = clean_text(cnib_number, "generic").strip()
        if not clean_cnib:
            return False

        try:
            stmt = (
                update(User)
                .where(User.id == uuid.UUID(user_id))
                .values(cnib_number=clean_cnib, identity_verified=True, updated_at=func.now())
            )
            await current_session.execute(stmt)
            return True
        except Exception as e:
            logger.error("❌ Erreur lors de l'enregistrement CNIB pour l'user %s: %s", user_id, e)
            return False

    async def update_communication_prefs(self, user_id: str, advice_time: str, enabled: bool = True) -> Dict[str, str]:
        """Règle l'heure de réception WhatsApp et l'état des notifications matinales de l'Agent."""
        current_session = self.session
        if current_session is None:
            raise RuntimeError("Database session is missing on the current context.")

        try:
            stmt = (
                update(User)
                .where(User.id == uuid.UUID(user_id))
                .values(
                    whatsapp_enabled=enabled,
                    updated_at=func.now()
                )
            )
            await current_session.execute(stmt)
            return {"status": "success", "message": "Préférences de notification enregistrées."}

        except ValueError:
            return {"status": "error", "message": "Le format de l'heure doit être une chaîne valide 'HH:MM'."}
        except Exception as e:
            logger.error("Erreur update_communication_prefs pour l'user %s: %s", user_id, e)
            return {"status": "error", "message": "Erreur de traitement des préférences de routage."}



    async def mark_onboarding_completed(self, user_id: str) -> Dict[str, str]:
        """Marque l'utilisateur comme ayant terminé l'onboarding initial."""
        current_session = self.session
        if current_session is None:
            raise RuntimeError("Database session is missing on the current context.")

        try:
            stmt = (
                update(User)
                .where(User.id == uuid.UUID(user_id))
                .values(onboarding_completed=True, updated_at=func.now())
            )
            await current_session.execute(stmt)
            return {"status": "success", "message": "Onboarding marqué comme terminé."}
        except Exception as e:
            logger.error("Erreur lors du marquage onboarding pour %s: %s", user_id, e)
            return {"status": "error", "message": "Impossible de mettre à jour l'état d'onboarding."}