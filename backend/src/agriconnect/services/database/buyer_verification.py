import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Literal
from sqlalchemy import select, update, and_
from sqlalchemy.orm import selectinload, joinedload
from sqlalchemy.ext.asyncio import AsyncSession

# Alignement sur les modèles du schéma v3
from agriconnect.domain.models import (
    BuyerProfile, User, BuyerType, TrustScore, AuditLog
)

logger = logging.getLogger("agriconnect.services.database.verification")

# Badges alignés sur la table buyer_profiles (trust_badge)
TrustBadge = Literal['VERIFIED_INSTITUTION', 'VERIFIED_COMMERCE', 'VERIFIED_ID']

class BuyerVerificationMixin:
    """
    Mixin de vérification haute sécurité.
    Gère la transition entre l'identité physique (CNIB) et la confiance commerciale.
    """

    async def verify_buyer_profile(
        self, 
        session: AsyncSession, 
        buyer_profile_id: str, 
        admin_user_id: str, 
        verification_type: Literal['CNIB', 'COMMERCE_REGISTER']
    ) -> Dict[str, Any]:
        """
        Valide un profil acheteur et impacte le TrustScore global.
        """
        bp_id = uuid.UUID(buyer_profile_id)
        a_id = uuid.UUID(admin_user_id)

        # 1. Chargement optimisé (Joinedload pour le type et l'user)
        stmt = (
            select(BuyerProfile)
            .options(
                joinedload(BuyerProfile.user).joinedload(User.trust_score),
                joinedload(BuyerProfile.buyer_type)
            )
            .where(BuyerProfile.id == bp_id)
        )
        result = await session.execute(stmt)
        profile = result.scalar_one_or_none()

        if not profile:
            raise ValueError("Profil acheteur introuvable")

        # 2. Validation documentaire stricte (KYC)
        if verification_type == 'CNIB':
            if not profile.user or not profile.user.cnib_number:
                raise ValueError("Vérification CNIB impossible : numéro manquant")

        # 3. Logique de Badge B2B
        badge: TrustBadge = 'VERIFIED_ID'
        type_name = (profile.buyer_type.name or "").upper() if profile.buyer_type else ""

        if verification_type == 'COMMERCE_REGISTER':
            # Distinction Institution (Hôtels/Restos) vs Commerce (Grossistes/Revendeurs)
            if any(k in type_name for k in ['HOTEL', 'HÔTEL', 'RESTAURANT', 'INSTITUTION']):
                badge = 'VERIFIED_INSTITUTION'
            else:
                badge = 'VERIFIED_COMMERCE'
        
        # 4. Mise à jour atomique
        profile.is_verified = True
        profile.trust_badge = badge
        # Correction : Ces champs doivent exister dans ta classe SQLAlchemy BuyerProfile
        # Si absent, l'IA utilisera l'AuditLog pour le tracking
        
        if verification_type == 'CNIB' and profile.user:
            profile.user.identity_verified = True
            # Boost immédiat du TrustScore (Compliance Index)
            if profile.user.trust_score:
                profile.user.trust_score.compliance_index = 1.0
                profile.user.trust_score.global_score += 0.1 # Bonus de vérification

        # 5. Création de l'entrée d'Audit (Table audit_logs du schéma)
        audit_entry = AuditLog(
            actor_id=a_id,
            action="VERIFY_BUYER",
            entity_id=str(bp_id),
            entity_type="BUYER_PROFILE",
            new_value={"badge": badge, "type": verification_type}
        )
        session.add(audit_entry)
        
        await session.commit()
        logger.info(f"VERIFY_BUYER | Admin: {a_id} | Profile: {bp_id} | Badge: {badge}")

        return profile.to_dict()

    async def get_pending_buyer_verifications(self, session: AsyncSession) -> List[Dict[str, Any]]:
        """Liste les profils en attente avec infos User pour le dashboard Admin."""
        stmt = (
            select(BuyerProfile)
            .options(joinedload(BuyerProfile.user))
            .where(BuyerProfile.is_verified == False)
            .order_by(BuyerProfile.created_at.desc())
        )
        result = await session.execute(stmt)
        return [p.to_dict() for p in result.scalars().all()]

    async def revoke_buyer_trust_badge(
        self, 
        session: AsyncSession, 
        buyer_profile_id: str, 
        admin_user_id: str,
        reason: str
    ) -> Dict[str, Any]:
        """
        Révoque le badge et dégrade le TrustScore (Sanction).
        """
        bp_id = uuid.UUID(buyer_profile_id)
        
        stmt = (
            select(BuyerProfile)
            .options(joinedload(BuyerProfile.user).joinedload(User.trust_score))
            .where(BuyerProfile.id == bp_id)
        )
        result = await session.execute(stmt)
        profile = result.scalar_one_or_none()

        if not profile:
            raise ValueError("Profil introuvable")

        # Reset des flags de confiance
        profile.is_verified = False
        profile.trust_badge = None
        
        if profile.user and profile.user.trust_score:
            profile.user.trust_score.compliance_index = 0.0
            profile.user.trust_score.global_score -= 0.3 # Malus sévère

        # Audit de la révocation
        audit = AuditLog(
            actor_id=uuid.UUID(admin_user_id),
            action="REVOKE_BUYER_BADGE",
            entity_id=str(bp_id),
            entity_type="BUYER_PROFILE",
            new_value={"reason": reason}
        )
        session.add(audit)
        
        await session.commit()
        return profile.to_dict()