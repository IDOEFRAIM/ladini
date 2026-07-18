"""DTOs du contexte Identity — frontière API + Tools injectés dans l'Agent IA.

Chaque champ porte une `description` : ces schémas sont utilisés tels quels
comme signature de fonction/tool par l'agent WhatsApp (function-calling), la
description EST le prompt qui guide le LLM sur quoi renseigner.
"""

from __future__ import annotations

from typing import Optional

from pydantic import Field

from agriconnect.domain.base_model import BaseMarketplaceModel


class UserDTO(BaseMarketplaceModel):
    """↔ auth.users — identité racine d'un utilisateur WhatsApp."""

    id: Optional[str] = Field(default=None, description="Identifiant unique de l'utilisateur (UUID).")
    name: Optional[str] = Field(default=None, description="Nom complet déclaré par l'utilisateur.")
    email: Optional[str] = Field(default=None, description="Adresse email (optionnelle, rarement fournie via WhatsApp).")
    phone: Optional[str] = Field(default=None, description="Numéro de téléphone WhatsApp au format E.164 (ex: +22670000000).")
    role: str = Field(default="USER", description="Rôle principal déclaré : PRODUCER, BUYER ou USER (avant onboarding).")
    zone_id: Optional[str] = Field(default=None, description="Zone géographique de rattachement (référence governance.zones).")
    latitude: Optional[float] = Field(default=None, description="Latitude GPS si partagée par l'utilisateur.")
    longitude: Optional[float] = Field(default=None, description="Longitude GPS si partagée par l'utilisateur.")
    identity_verified: bool = Field(default=False, description="True si l'identité a été vérifiée (KYC léger).")
    whatsapp_enabled: bool = Field(default=True, description="True si l'utilisateur accepte les notifications WhatsApp.")
    onboarding_completed: bool = Field(default=False, description="True si le parcours d'inscription initial est terminé.")


class ProducerDTO(BaseMarketplaceModel):
    """↔ marketplace.producers"""

    id: Optional[str] = Field(default=None, description="Identifiant du profil producteur.")
    user_id: str = Field(description="Identifiant de l'utilisateur propriétaire de ce profil producteur.")
    business_name: Optional[str] = Field(default=None, description="Nom commercial affiché aux acheteurs.")
    status: str = Field(default="PENDING", description="Statut du profil : PENDING, ACTIVE ou SUSPENDED.")
    is_certified: bool = Field(default=False, description="True si le producteur détient une certification qualité.")
    zone_id: Optional[str] = Field(default=None, description="Zone géographique d'exploitation principale.")
    region: Optional[str] = Field(default=None, description="Région administrative.")
    province: Optional[str] = Field(default=None, description="Province administrative.")
    commune: Optional[str] = Field(default=None, description="Commune administrative.")
    phone_number: Optional[str] = Field(default=None, description="Numéro de contact professionnel, si distinct du compte.")
    rating: Optional[int] = Field(default=None, description="Note moyenne (1-5) attribuée par les acheteurs.")
    reviews_count: int = Field(default=0, description="Nombre total d'avis reçus.")


class BuyerProfileDTO(BaseMarketplaceModel):
    """↔ marketplace.buyer_profiles"""

    id: Optional[str] = Field(default=None, description="Identifiant du profil acheteur.")
    user_id: str = Field(description="Identifiant de l'utilisateur propriétaire de ce profil acheteur.")
    buyer_type_id: Optional[str] = Field(default=None, description="Type d'acheteur (particulier, restaurant, grossiste...).")
    establishment_name: Optional[str] = Field(default=None, description="Nom de l'établissement, si applicable.")
    default_delivery_address: Optional[str] = Field(default=None, description="Adresse de livraison utilisée par défaut.")
    is_verified: bool = Field(default=False, description="True si le profil acheteur a été vérifié.")
    trust_badge: Optional[str] = Field(default=None, description="Badge de confiance affiché (ex: 'Acheteur vérifié').")
    rating: Optional[float] = Field(default=None, description="Note moyenne attribuée par les producteurs.")
    reviews_count: int = Field(default=0, description="Nombre total d'avis reçus.")


class TrustScoreDTO(BaseMarketplaceModel):
    """↔ intelligence.trust_scores"""

    id: Optional[str] = Field(default=None, description="Identifiant de l'enregistrement de score.")
    user_id: str = Field(description="Identifiant de l'utilisateur noté.")
    global_score: float = Field(default=0.0, description="Score de confiance global agrégé (0.0 à 1.0).")
    reliability_index: float = Field(default=0.0, description="Indice de fiabilité (respect des engagements).")
    quality_index: float = Field(default=0.0, description="Indice de qualité perçue des produits/services.")
    compliance_index: float = Field(default=0.0, description="Indice de conformité (respect des règles de la plateforme).")
    resilience_bonus: float = Field(default=0.0, description="Bonus attribué pour la résilience face aux aléas (météo, retards...).")


class UserContextDTO(BaseMarketplaceModel):
    """Vue agrégée résolue par UserContextService — identité + profils + droits.

    C'est le DTO le plus consommé par l'Agent : il répond en un seul appel à
    « qui est cet utilisateur et que peut-il faire ? ».
    """

    id: str = Field(description="Identifiant unique de l'utilisateur.")
    name: str = Field(description="Nom affiché de l'utilisateur.")
    phone: str = Field(description="Numéro de téléphone WhatsApp de l'utilisateur.")
    role: str = Field(default="USER", description="Rôle actif pour la conversation en cours : PRODUCER ou BUYER.")
    zone_id: Optional[str] = Field(default=None, description="Identifiant de la zone géographique de rattachement.")
    zone_name: Optional[str] = Field(default=None, description="Nom lisible de la zone géographique.")
    latitude: Optional[float] = Field(default=None, description="Latitude GPS connue.")
    longitude: Optional[float] = Field(default=None, description="Longitude GPS connue.")
    producer_id: Optional[str] = Field(default=None, description="Identifiant du profil producteur, si l'utilisateur en a un.")
    buyer_id: Optional[str] = Field(default=None, description="Identifiant du profil acheteur, si l'utilisateur en a un.")
    delivery_agent_id: Optional[str] = Field(default=None, description="Identifiant du profil livreur, si applicable.")
    can_sell: bool = Field(default=False, description="True si l'utilisateur peut publier des offres de vente.")
    can_buy: bool = Field(default=False, description="True si l'utilisateur peut passer des commandes.")
    can_deliver: bool = Field(default=False, description="True si l'utilisateur peut assurer des livraisons.")
    is_admin: bool = Field(default=False, description="True si l'utilisateur a des droits d'administration.")
    identity_verified: bool = Field(default=False, description="True si l'identité de l'utilisateur a été vérifiée.")
    onboarding_completed: bool = Field(default=False, description="True si l'utilisateur a terminé son inscription.")


__all__ = [
    "UserDTO",
    "ProducerDTO",
    "BuyerProfileDTO",
    "TrustScoreDTO",
    "UserContextDTO",
]
