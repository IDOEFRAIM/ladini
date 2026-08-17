import logging
import uuid
from typing import Any, Dict, List, Literal

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from agriconnect.domain.models import AuditLog, BuyerProfile, User

logger = logging.getLogger("agriconnect.services.database.verification")

TrustBadge = Literal["VERIFIED_INSTITUTION", "VERIFIED_COMMERCE", "VERIFIED_ID"]


class BuyerVerificationMixin:
    """
    Mixin de vérification haute sécurité.
    Gère la transition entre l'identité physique (CNIB) et la confiance commerciale.
    """

    async def verify_buyer_profile(
        self,
        buyer_profile_id: str,
        admin_user_id: str,
        verification_type: Literal["CNIB", "COMMERCE_REGISTER"],
    ) -> Dict[str, Any]:
        current_session = self.session
        bp_id = uuid.UUID(buyer_profile_id)
        a_id = uuid.UUID(admin_user_id)

        stmt = (
            select(BuyerProfile)
            .options(
                joinedload(BuyerProfile.user).joinedload(User.trust_score),
                joinedload(BuyerProfile.buyer_type),
            )
            .where(BuyerProfile.id == bp_id)
        )
        result = await current_session.execute(stmt)
        profile = result.scalar_one_or_none()

        if not profile:
            raise ValueError("Profil acheteur introuvable")

        if verification_type == "CNIB":
            if not profile.user or not profile.user.cnib_number:
                raise ValueError("Vérification CNIB impossible : numéro manquant")

        badge: TrustBadge = "VERIFIED_ID"
        type_name = (
            (profile.buyer_type.name or "").upper() if profile.buyer_type else ""
        )

        if verification_type == "COMMERCE_REGISTER":
            if any(
                k in type_name for k in ["HOTEL", "HÔTEL", "RESTAURANT", "INSTITUTION"]
            ):
                badge = "VERIFIED_INSTITUTION"
            else:
                badge = "VERIFIED_COMMERCE"

        profile.is_verified = True
        profile.trust_badge = badge

        if verification_type == "CNIB" and profile.user:
            profile.user.identity_verified = True
            if profile.user.trust_score:
                profile.user.trust_score.compliance_index = 1.0
                profile.user.trust_score.global_score += 0.1

        audit_entry = AuditLog(
            actor_id=a_id,
            action="VERIFY_BUYER",
            entity_id=str(bp_id),
            entity_type="BUYER_PROFILE",
            new_value={"badge": badge, "type": verification_type},
        )
        current_session.add(audit_entry)
        await current_session.flush()

        logger.info(f"VERIFY_BUYER | Admin: {a_id} | Profile: {bp_id} | Badge: {badge}")
        return profile.to_dict()

    async def get_pending_buyer_verifications(self) -> List[Dict[str, Any]]:
        current_session = self.session
        stmt = (
            select(BuyerProfile)
            .options(joinedload(BuyerProfile.user))
            .where(not BuyerProfile.is_verified)
            .order_by(BuyerProfile.created_at.desc())
        )
        result = await current_session.execute(stmt)
        return [p.to_dict() for p in result.scalars().all()]

    async def revoke_buyer_trust_badge(
        self, buyer_profile_id: str, admin_user_id: str, reason: str
    ) -> Dict[str, Any]:
        current_session = self.session
        bp_id = uuid.UUID(buyer_profile_id)

        stmt = (
            select(BuyerProfile)
            .options(joinedload(BuyerProfile.user).joinedload(User.trust_score))
            .where(BuyerProfile.id == bp_id)
        )
        result = await current_session.execute(stmt)
        profile = result.scalar_one_or_none()

        if not profile:
            raise ValueError("Profil introuvable")

        profile.is_verified = False
        profile.trust_badge = None

        if profile.user and profile.user.trust_score:
            profile.user.trust_score.compliance_index = 0.0
            profile.user.trust_score.global_score -= 0.3

        audit = AuditLog(
            actor_id=uuid.UUID(admin_user_id),
            action="REVOKE_BUYER_BADGE",
            entity_id=str(bp_id),
            entity_type="BUYER_PROFILE",
            new_value={"reason": reason},
        )
        current_session.add(audit)
        await current_session.flush()

        return profile.to_dict()
