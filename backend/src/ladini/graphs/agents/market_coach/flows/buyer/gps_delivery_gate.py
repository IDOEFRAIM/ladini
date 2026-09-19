"""Shared GPS delivery gate — single source of truth for the 2-stage
delivery-location state machine used by BOTH `order_tracking.py::finalize_winner`
(auction winner selection) and `preorder.py::create_preorder` (cart checkout).

Before this module existed, each flow carried its OWN copy of this state
machine (prompts, "did they share a location vs answer yes vs say something
else" branching, stored-default lookup). That duplication is exactly why
précommande kept breaking every time the location feature needed a change —
a fix applied to one copy silently didn't apply to the other. See
[[precommande-architecture-consolidation-2026-08]] and
[[gps-delivery-burkina-faso-2026-08]] for the original feature history.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from ladini.core.location import (
    LocationOutcome,
    parse_google_maps_coordinates,
    persist_shared_location,
)
from ladini.graphs.agents.market_coach.core.base import get_node_logger
from ladini.graphs.agents.market_coach.utils import (
    MarketRuntime,
    llm_deviation_reply,
)

logger = get_node_logger("GpsDeliveryGate")

_GPS_HABITUAL_PROMPT = (
    "📍 Livraison à ton point GPS habituel ? Réponds *OUI* pour valider, "
    "ou clique sur le trombone 📎 puis *Localisation* pour m'envoyer un nouveau point."
)
_GPS_FIRST_TIME_PROMPT = (
    "📍 Envoie-moi ton point GPS exact en cliquant sur le trombone 📎 puis "
    "*Localisation* sur WhatsApp pour finaliser la livraison."
)
_GPS_TEXT_REMINDER = (
    "Pour garantir la livraison, j'ai besoin de ta position GPS exacte. "
    "Clique sur le bouton 📎 de WhatsApp puis sur *Localisation* pour me la partager."
)

_GPS_STAGE_CONTEXT = (
    "l'agent attend un point GPS de livraison (bouton 📎 puis Localisation sur "
    "WhatsApp) — c'est la SEULE chose attendue à cette étape, réponds oui/non "
    "sans reformuler"
)


async def _get_stored_location(
    mc_runtime: MarketRuntime, phone: str
) -> "tuple[Optional[float], Optional[float]]":
    """Point GPS PAR DÉFAUT du profil (`User.latitude`/`longitude`), ou
    `(None, None)` si absent/introuvable. Jamais levé — best-effort."""
    if not phone:
        return None, None
    try:
        from ladini.graphs.agents.market_coach.services.mcp.gateway import (
            ProfileGateway,
        )

        result = await ProfileGateway(mc_runtime).get_user_by_phone(phone)
        data = result.get("data") or {}
        lat, lon = data.get("latitude"), data.get("longitude")
        if lat is None or lon is None:
            return None, None
        return float(lat), float(lon)
    except Exception:
        logger.warning(
            "_get_stored_location: échec de lecture du profil pour %s",
            phone[-4:] if len(phone) >= 4 else phone,
        )
        return None, None


async def enter_gps_stage(mc_runtime: MarketRuntime, phone: str) -> Dict[str, Any]:
    """Appelé une fois quand l'opération parente (gagnant d'enchère /
    précommande) vient d'être confirmée — bascule vers l'étape GPS et
    renvoie le prompt adapté (point par défaut connu ou premier partage)."""
    stored_lat, stored_lon = await _get_stored_location(mc_runtime, phone)
    if stored_lat is not None and stored_lon is not None:
        return {
            "gps_stage": True,
            "gps_default": {"lat": stored_lat, "lon": stored_lon},
            "prompt": _GPS_HABITUAL_PROMPT,
        }
    return {"gps_stage": True, "gps_default": None, "prompt": _GPS_FIRST_TIME_PROMPT}


@dataclass
class GpsResolution:
    """Résultat d'un tour pendant l'étape GPS : soit un point résolu (prêt à
    exécuter l'opération), soit un message à afficher en attendant mieux.

    `outcome` (2026-09-02, refonte GPS) est le contrat canonique — voir
    `core/location.py::LocationOutcome`. `resolved` reste un booléen dérivé
    (`outcome == NEW_LOCATION_ACCEPTED`) pour compatibilité descendante avec
    les 2 appelants existants (`preorder.py`, `order_tracking.py`), qui ne
    lisent que ce champ aujourd'hui — jamais un repli implicite : si un
    appelant a besoin de distinguer un rejet géographique d'une erreur
    technique, il lit `outcome` directement."""

    resolved: bool
    outcome: LocationOutcome = LocationOutcome.NO_LOCATION
    lat: Optional[float] = None
    lon: Optional[float] = None
    message: Optional[str] = None


async def resolve_gps_stage(
    mc_runtime: MarketRuntime,
    phone: str,
    *,
    location_shared: bool,
    is_yes: bool,
    gps_default: Optional[Dict[str, Any]],
    user_text: str = "",
    location_outcome: Optional[str] = None,
    location_lat: Optional[float] = None,
    location_lon: Optional[float] = None,
) -> GpsResolution:
    """Un tour PENDANT l'étape GPS (gps_stage déjà True). Résout le point de
    livraison à partir d'un partage WhatsApp natif, d'un "oui" au point par
    défaut, ou — si l'utilisateur dit autre chose — génère une réponse
    adaptée (LLM) avant de rappeler ce qui est attendu, plutôt que de
    répéter le même texte figé quoi qu'il arrive.

    `location_outcome`/`location_lat`/`location_lon` (2026-09-02) : l'issue
    EXACTE déjà résolue côté webhook, SYNCHRONE avant l'enqueue Celery — voir
    core/location.py. Remplace l'ancienne relecture DB
    (`_get_stored_location`), qui pouvait courir avant l'écriture réelle
    (BackgroundTasks vs tâche Celery déjà enqueuée) et produisait un message
    ambigu ("je n'ai pas pu récupérer ce point") aussi bien pour une vraie
    erreur technique que pour un rejet géographique — jamais de repli
    silencieux sur une ancienne position stockée dans les deux cas."""
    if location_shared:
        try:
            outcome = LocationOutcome(location_outcome or "")
        except ValueError:
            outcome = LocationOutcome.LOCATION_PERSISTENCE_ERROR

        if outcome == LocationOutcome.NEW_LOCATION_ACCEPTED and (
            location_lat is not None and location_lon is not None
        ):
            return GpsResolution(
                resolved=True,
                outcome=outcome,
                lat=location_lat,
                lon=location_lon,
            )

        if outcome == LocationOutcome.LOCATION_OUT_OF_ZONE:
            # Jamais de repli silencieux sur une ancienne position stockée —
            # le rejet est explicite, l'utilisateur DOIT en être informé
            # (le webhook a déjà envoyé OUT_OF_COUNTRY_MESSAGE en parallèle,
            # ce message-ci ré-ancre la conversation sur l'étape GPS).
            return GpsResolution(
                resolved=False,
                outcome=outcome,
                message=(
                    "Ce point est hors de notre zone de livraison — "
                    "partage une position à l'intérieur du Burkina Faso."
                ),
            )

        # Erreur de persistance (infra) — distincte d'un rejet géographique :
        # jamais présentée comme "je n'ai pas compris ta position" (message
        # trompeur), ni comblée silencieusement par une ancienne position.
        return GpsResolution(
            resolved=False,
            outcome=LocationOutcome.LOCATION_PERSISTENCE_ERROR,
            message=(
                "Un souci technique a empêché l'enregistrement de ce point "
                "GPS. Peux-tu le repartager ?"
            ),
        )

    # Lien Google Maps collé en TEXTE (client sans message `location` natif) :
    # avant `is_yes`, sinon l'interpréteur le lit comme un « oui » et, sans
    # point par défaut, on redemande le GPS en boucle. Même persistance +
    # geofencing que le partage natif → mêmes issues, jamais de repli silencieux.
    text_point = parse_google_maps_coordinates(user_text)
    if text_point is not None:
        lat, lon = text_point
        outcome, _msg = await persist_shared_location(phone, lat, lon)
        if outcome == LocationOutcome.NEW_LOCATION_ACCEPTED:
            return GpsResolution(resolved=True, outcome=outcome, lat=lat, lon=lon)
        return await resolve_gps_stage(
            mc_runtime,
            phone,
            location_shared=True,
            is_yes=False,
            gps_default=gps_default,
            location_outcome=outcome.value,
        )

    if is_yes:
        default = gps_default or {}
        lat, lon = default.get("lat"), default.get("lon")
        if lat is None or lon is None:
            # "oui" sans point par défaut connu (désynchro d'état) → redemander
            # explicitement le partage.
            return GpsResolution(
                resolved=False,
                outcome=LocationOutcome.NO_LOCATION,
                message=_GPS_FIRST_TIME_PROMPT,
            )
        return GpsResolution(
            resolved=True,
            outcome=LocationOutcome.NEW_LOCATION_ACCEPTED,
            lat=lat,
            lon=lon,
        )

    # Texte libre (ni "oui", ni position partagée) : reconnaître ce qui a été
    # dit avant de rappeler le bouton GPS, au lieu du même rappel figé en
    # boucle — voir [[precommande-architecture-consolidation-2026-08]].
    # Seuls les liens Google Maps à coordonnées explicites sont parsés (plus
    # haut) ; adresses/plus codes/liens courts restent du texte libre, jamais
    # faussement acceptés.
    note = await llm_deviation_reply(mc_runtime, user_text, _GPS_STAGE_CONTEXT)
    message = f"{note}\n\n{_GPS_TEXT_REMINDER}" if note else _GPS_TEXT_REMINDER
    return GpsResolution(
        resolved=False, outcome=LocationOutcome.LOCATION_PARSE_ERROR, message=message
    )


__all__ = [
    "_GPS_HABITUAL_PROMPT",
    "_GPS_FIRST_TIME_PROMPT",
    "_GPS_TEXT_REMINDER",
    "_get_stored_location",
    "enter_gps_stage",
    "resolve_gps_stage",
    "GpsResolution",
]
