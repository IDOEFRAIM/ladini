"""Exigences de profil PROGRESSIVES — « la friction est proportionnelle au risque métier ».

Ladini ne fait plus remplir un formulaire d'inscription avant de comprendre l'utilisateur : on COMMENCE par
l'intention, puis on ne demande une information que si l'ACTION métier suivante la requiert. Ce module est PUR
(aucune DB, aucun LLM) : il dit, pour une classe d'action et des faits de profil, CE QUI MANQUE.

    DISCOVERY      explorer : saluer, s'informer, chercher, voir disponibilité/prix        -> rien
    BUY_REQUEST    exprimer un besoin d'achat livrable (demande directe, appel d'offres,
                   besoin récurrent)                                                        -> région
    BUY_COMMIT     engager l'acheteur (précommande, confirmation, accepter une offre)       -> nom + région
    SELL           publier / vendre / fournir (le producteur est identifié et suivi)        -> nom + région
    FINANCING      scoring / financement                                                    -> nom + région + identité vérifiée

« nom » = nom d'affichage (personne OU établissement : « Restaurant Wend Konta ») ; la base ne porte qu'un champ
`name`, utilisé comme `display_name` — pas de prénom/nom obligatoires. « région » = l'une des 17 régions
(`domain/burkina_regions.py`) ; aucune sous-zone n'est jamais requise.

Les CAPACITÉS (acheter / vendre) restent cumulatives et indépendantes : un même utilisateur peut avoir les deux.
La vérification producteur (`Producer.status`, admin, coopérative) n'est JAMAIS contournée ici : un profil complet
n'est pas un profil vérifié.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional, Tuple


class ProfileAction(str, Enum):
    DISCOVERY = "DISCOVERY"
    BUY_REQUEST = "BUY_REQUEST"
    BUY_COMMIT = "BUY_COMMIT"
    SELL = "SELL"
    FINANCING = "FINANCING"


class ProfileField(str, Enum):
    NAME = "name"
    REGION = "region"
    VERIFIED_IDENTITY = "verified_identity"


#: Contrat unique des exigences, dans l'ORDRE où on les demande (une question à la fois).
REQUIREMENTS: Mapping[ProfileAction, Tuple[ProfileField, ...]] = {
    ProfileAction.DISCOVERY: (),
    ProfileAction.BUY_REQUEST: (ProfileField.REGION,),
    ProfileAction.BUY_COMMIT: (ProfileField.NAME, ProfileField.REGION),
    ProfileAction.SELL: (ProfileField.NAME, ProfileField.REGION),
    ProfileAction.FINANCING: (ProfileField.NAME, ProfileField.REGION, ProfileField.VERIFIED_IDENTITY),
}

#: Noms de repli posés par le système (jamais dits par l'utilisateur) : ils ne comptent PAS comme un nom connu.
PLACEHOLDER_NAMES = frozenset({"", "utilisateur", "client", "n/a", "na", "none", "null", "user", "inconnu", "unknown"})


def is_real_name(value: Any) -> bool:
    """`True` si `value` est un nom dit par l'utilisateur (pas un repli système comme « Utilisateur » ou « User_1234 »)."""
    text = str(value or "").strip()
    if text.lower() in PLACEHOLDER_NAMES:
        return False
    return not text.lower().startswith("user_")


class CapabilityState(str, Enum):
    CONTACT = "CONTACT"  # on ne connaît que l'identité WhatsApp
    BUYER_DISCOVERY = "BUYER_DISCOVERY"  # peut explorer/chercher
    BUYER_READY = "BUYER_READY"  # minimum pour engager un achat
    PRODUCER_PENDING_PROFILE = "PRODUCER_PENDING_PROFILE"  # veut/peut vendre mais profil insuffisant
    PRODUCER_READY = "PRODUCER_READY"  # profil minimum producteur valide
    PRODUCER_VERIFIED = "PRODUCER_VERIFIED"  # vérifié (admin/coopérative) — jamais déduit d'un profil complet


@dataclass(frozen=True)
class ProfileFacts:
    """Ce que l'on SAIT du profil — dérivé des champs/capacités existants, jamais déclaré par le modèle."""

    name: Optional[str] = None
    has_region: bool = False
    can_buy: bool = False  # ligne BuyerProfile (ou ADMIN)
    can_sell: bool = False  # ligne Producer (ou ADMIN)
    producer_status: Optional[str] = None  # PENDING | APPROVED | ... (jamais modifié ici)
    identity_verified: bool = False

    @property
    def has_name(self) -> bool:
        return is_real_name(self.name)

    @classmethod
    def from_state(cls, state: Mapping[str, Any]) -> "ProfileFacts":
        """Faits lus dans l'état de session (alimenté par `load_user_profile`)."""
        raw_perms = state.get("user_permissions")
        perms: Mapping[str, Any] = raw_perms if isinstance(raw_perms, Mapping) else {}
        return cls(
            name=state.get("user_name"),
            has_region=bool(state.get("zone_id")) or bool(str(state.get("declared_location") or "").strip()),
            can_buy=bool(perms.get("can_buy")),
            can_sell=bool(perms.get("can_sell")),
            producer_status=state.get("producer_status"),
            identity_verified=bool(state.get("identity_verified")),
        )


def has_field(facts: ProfileFacts, field: ProfileField) -> bool:
    if field is ProfileField.NAME:
        return facts.has_name
    if field is ProfileField.REGION:
        return facts.has_region
    return facts.identity_verified


def get_missing_requirements(action: ProfileAction, facts: ProfileFacts) -> Tuple[ProfileField, ...]:
    """Champs manquants pour `action`, dans l'ordre où on les demande. Vide = on peut agir."""
    return tuple(f for f in REQUIREMENTS[action] if not has_field(facts, f))


def capability_state(facts: ProfileFacts) -> CapabilityState:
    """État conceptuel DÉRIVÉ (aucun enum persisté) : où en est cet utilisateur ?"""
    if facts.can_sell:
        if not (facts.has_name and facts.has_region):
            return CapabilityState.PRODUCER_PENDING_PROFILE
        if facts.identity_verified or str(facts.producer_status or "").upper() in {"APPROVED", "VERIFIED", "ACTIVE"}:
            return CapabilityState.PRODUCER_VERIFIED
        return CapabilityState.PRODUCER_READY
    if facts.can_buy:
        if facts.has_name and facts.has_region:
            return CapabilityState.BUYER_READY
        return CapabilityState.BUYER_DISCOVERY
    return CapabilityState.CONTACT


# Quels objectifs (goals) relèvent de quelle action -----------------------------------------------------------------
# Tout ce qui n'est pas listé ici est de la DÉCOUVERTE (recherche, aide, prix, suivi de ses propres objets…).

SELL_GOALS = frozenset(
    {
        "SALES_PUBLISH_PRODUCT",
        "SALES_RECORD_DIRECT",
        "STOCK_REGISTER_HARVEST",
        "PRODUCTION_DECLARE_FUTURE",
        "SALES_PLACE_BID",
        "FARM_CREATE",
    }
)
BUY_REQUEST_GOALS = frozenset({"BUYER_REQUEST", "PROCUREMENT_CREATE_REQUEST", "CREATE_RECURRING_NEED"})
BUY_COMMIT_GOALS = frozenset({"BUYER_PREORDER_INIT", "BUYER_PREORDER_CONFIRM", "BUYER_CREATE_PREORDER"})


def action_for_goal(goal: Optional[str]) -> ProfileAction:
    g = str(goal or "").strip().upper()
    if g in SELL_GOALS:
        return ProfileAction.SELL
    if g in BUY_COMMIT_GOALS:
        return ProfileAction.BUY_COMMIT
    if g in BUY_REQUEST_GOALS:
        return ProfileAction.BUY_REQUEST
    return ProfileAction.DISCOVERY


__all__ = [
    "BUY_COMMIT_GOALS",
    "BUY_REQUEST_GOALS",
    "CapabilityState",
    "PLACEHOLDER_NAMES",
    "ProfileAction",
    "ProfileFacts",
    "ProfileField",
    "REQUIREMENTS",
    "SELL_GOALS",
    "action_for_goal",
    "capability_state",
    "get_missing_requirements",
    "has_field",
    "is_real_name",
]
