import hashlib
import hmac
import secrets
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import select, update, and_, func, text
from sqlalchemy.ext.asyncio import AsyncSession
from agriconnect.domain.models import (
    SeedAllocation, SeedDistribution, SeedDistributionAttempt,
    Producer, User, UserOrganization, _uuid4
)

logger = logging.getLogger("agriconnect.services.seeds")

class SeedDistributionMixin:
    """
    Mixin pour la distribution sécurisée de semences.
    Gère l'initialisation OTP, la vérification, la sécurité et l'impact sur le Trust Score.
    """

    # --- Configuration ---
    OTP_TTL_MINUTES = 10
    MAX_ATTEMPTS = 3

    # --- Sécurité & Crypto ---

    def _hash_otp(self, code: str, salt: str) -> str:
        """Format v2: sha256(code + salt) pour la vérification."""
        return hashlib.sha256((code + salt).encode()).hexdigest()

    def _verify_otp_securely(self, input_code: str, salt: str, stored_hash: str) -> bool:
        """Vérification immunisée contre les attaques temporelles (timing attacks)."""
        expected_v2 = self._hash_otp(input_code, salt)
        expected_v1 = hashlib.sha256((salt + input_code).encode()).hexdigest()
        
        return (hmac.compare_digest(expected_v2, stored_hash) or 
                hmac.compare_digest(expected_v1, stored_hash))

    # --- Actions Principales ---

    async def initialize_seed_distribution(
        self, 
        session: AsyncSession, 
        agent_id: str, 
        producer_id: str, 
        allocation_id: str, 
        quantity: float,
        cnib: Optional[str] = None
    ) -> Dict[str, Any]:
        """Initialise une distribution et génère le code de vérification à destination du producteur."""
        
        # 1. Vérification de l'allocation et du stock disponible
        stmt = select(SeedAllocation).where(SeedAllocation.id == allocation_id)
        alloc = (await session.execute(stmt)).scalar_one_or_none()
        
        if not alloc or (alloc.remaining_quantity or 0) < quantity:
            raise ValueError("Allocation introuvable ou stock disponible insuffisant")

        # 2. Vérification stricte des droits et de la zone de l'agent
        agent_stmt = select(User).where(User.id == agent_id)
        agent_user = (await session.execute(agent_stmt)).scalar_one_or_none()
        if not agent_user:
            raise ValueError("Agent introuvable")
        
        if agent_user.role not in ['ADMIN', 'SUPERADMIN']:
            org_stmt = select(UserOrganization).where(
                and_(
                    UserOrganization.user_id == agent_id,
                    UserOrganization.organization_id == alloc.organization_id
                )
            )
            membership = (await session.execute(org_stmt)).scalar_one_or_none()
            if not membership:
                raise PermissionError("L'agent ne possède pas les droits pour cette organisation")
            
            # Vérification géographique locale
            if membership.role in ['FIELD_AGENT', 'ZONE_AGENT'] and str(membership.managed_zone_id) != str(alloc.zone_id):
                raise PermissionError("L'agent opère en dehors de sa zone de couverture assignée")

        # 3. Génération sécurisée de l'OTP
        code = str(secrets.randbelow(900000) + 100000)
        salt = secrets.token_hex(12)
        hash_val = self._hash_otp(code, salt)
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=self.OTP_TTL_MINUTES)

        # 4. Enregistrement de la distribution à l'état en attente ('PENDING')
        dist = SeedDistribution(
            id=str(_uuid4()),
            allocation_id=allocation_id,
            producer_id=producer_id,
            agent_id=agent_id,
            organization_id=alloc.organization_id,
            zone_id=alloc.zone_id,
            quantity=quantity,
            cnib_provided=cnib,
            verification_code_hash=hash_val,
            verification_code_expires_at=expires_at,
            status='PENDING',
            attempts_count=0,
            metadata={'salt': salt}
        )
        session.add(dist)
        await session.flush()

        logger.info("SEED_DISTRIBUTION_INITIALIZED: Dist %s initiée par l'agent %s pour le producteur %s", dist.id, agent_id, producer_id)
        
        # En production, 'otp_debug' doit être retiré pour transiter uniquement via passerelle SMS / USSD
        return {"distribution_id": dist.id, "otp_debug": code}

    async def verify_seed_distribution(
        self, 
        session: AsyncSession, 
        agent_id: str, 
        distribution_id: str, 
        input_code: str
    ) -> bool:
        """Valide le code OTP, décrémente de façon atomique le stock et met à jour les scores de confiance."""
        
        # 1. Charger et verrouiller la distribution pour parer aux écritures concurrentes
        stmt = select(SeedDistribution).where(SeedDistribution.id == distribution_id).with_for_update()
        dist = (await session.execute(stmt)).scalar_one_or_none()

        if not dist or dist.status != 'PENDING':
            raise ValueError("Distribution introuvable, annulée ou déjà validée")

        # 2. Validation temporelle de l'OTP (Aware datetime)
        if dist.verification_code_expires_at.tzinfo is None:
            # Sécurité si les dates de la DB arrivent naïves
            dist.verification_code_expires_at = dist.verification_code_expires_at.replace(tzinfo=timezone.utc)

        if dist.verification_code_expires_at < datetime.now(timezone.utc):
            dist.status = 'FAILED'
            await session.flush()
            raise ValueError("Le code de validation a expiré")

        # 3. Évaluation cryptographique de l'OTP fourni
        salt = dist.metadata.get('salt', '') if dist.metadata else ''
        is_valid = self._verify_otp_securely(input_code, salt, dist.verification_code_hash)

        # Historisation et audit de la tentative
        attempt = SeedDistributionAttempt(
            id=str(_uuid4()),
            distribution_id=distribution_id,
            actor_id=agent_id,
            success=is_valid,
            attempted_at=datetime.now(timezone.utc),
            # Optionnel: On stocke le hash de l'input erroné pour l'analyse de force-brute
            metadata={"input_hash": hashlib.sha256(input_code.encode()).hexdigest()} if not is_valid else None
        )
        session.add(attempt)

        if not is_valid:
            dist.attempts_count = (dist.attempts_count or 0) + 1
            if dist.attempts_count >= self.MAX_ATTEMPTS:
                dist.status = 'FAILED'
                logger.warning("SEED_DISTRIBUTION_LOCKED: Trop de tentatives sur la distribution %s", distribution_id)
            await session.flush()
            raise ValueError("Code de validation incorrect")

        # 4. Décrémentation atomique de l'allocation d'origine
        update_alloc = (
            update(SeedAllocation)
            .where(and_(
                SeedAllocation.id == dist.allocation_id,
                SeedAllocation.remaining_quantity >= dist.quantity
            ))
            .values(remaining_quantity=SeedAllocation.remaining_quantity - dist.quantity)
        )
        res = await session.execute(update_alloc)
        
        if res.rowcount == 0:
            dist.status = 'FAILED'
            await session.flush()
            raise ValueError("Erreur critique d'allocation : rupture de stock ou ajustement simultané")

        # 5. Clôture transactionnelle positive
        dist.status = 'COMPLETED'
        dist.receipt_at = datetime.now(timezone.utc)

        # 6. Systèmes de réputation (Trust Score) : Incrémentations directes et atomiques
        await session.execute(
            text("UPDATE intelligence.trust_scores "
                 "SET reliability_index = reliability_index + 1.0, updated_at = :now "
                 "WHERE user_id IN (:p_id, :a_id)"),
            {
                "p_id": dist.producer_id, 
                "a_id": agent_id, 
                "now": datetime.now(timezone.utc)
            }
        )

        await session.flush()
        logger.info("SEED_DISTRIBUTION_SUCCESS: Validée avec succès pour la distribution %s", distribution_id)
        return True