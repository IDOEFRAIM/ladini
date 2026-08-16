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

from agriconnect.graphs.agents.market_coach.core.base import get_node_logger
from agriconnect.graphs.agents.market_coach.utils import MarketRuntime, llm_deviation_reply

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


async def _get_stored_location(mc_runtime: MarketRuntime, phone: str) -> "tuple[Optional[float], Optional[float]]":
    """Point GPS PAR DÉFAUT du profil (`User.latitude`/`longitude`), ou
    `(None, None)` si absent/introuvable. Jamais levé — best-effort."""
    if not phone:
        return None, None
    try:
        from agriconnect.graphs.agents.market_coach.services.mcp.gateway import ProfileGateway
        result = await ProfileGateway(mc_runtime).get_user_by_phone(phone)
        data = result.get("data") or {}
        lat, lon = data.get("latitude"), data.get("longitude")
        if lat is None or lon is None:
            return None, None
        return float(lat), float(lon)
    except Exception:
        logger.warning("_get_stored_location: échec de lecture du profil pour %s", phone[-4:] if len(phone) >= 4 else phone)
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
    exécuter l'opération), soit un message à afficher en attendant mieux."""
    resolved: bool
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
) -> GpsResolution:
    """Un tour PENDANT l'étape GPS (gps_stage déjà True). Résout le point de
    livraison à partir d'un partage WhatsApp natif, d'un "oui" au point par
    défaut, ou — si l'utilisateur dit autre chose — génère une réponse
    adaptée (LLM) avant de rappeler ce qui est attendu, plutôt que de
    répéter le même texte figé quoi qu'il arrive."""
    if location_shared:
        # Le webhook a déjà persisté ce point (best-effort, tâche de fond,
        # voir api/routes/twilio_webhook.py::_persist_location_background) —
        # on relit la position COURANTE du profil plutôt que de faire
        # transiter lat/lon dans le state du graphe.
        lat, lon = await _get_stored_location(mc_runtime, phone)
        if lat is None or lon is None:
            # Filet de sécurité : improbable (le webhook vient de l'écrire),
            # mais ne doit jamais planter l'opération.
            return GpsResolution(resolved=False, message="Je n'ai pas pu récupérer ce point GPS, merci de le repartager.")
        return GpsResolution(resolved=True, lat=lat, lon=lon)

    if is_yes:
        default = gps_default or {}
        lat, lon = default.get("lat"), default.get("lon")
        if lat is None or lon is None:
            # "oui" sans point par défaut connu (désynchro d'état) → redemander
            # explicitement le partage.
            return GpsResolution(resolved=False, message=_GPS_FIRST_TIME_PROMPT)
        return GpsResolution(resolved=True, lat=lat, lon=lon)

    # Texte libre (ni "oui", ni position partagée) : reconnaître ce qui a été
    # dit avant de rappeler le bouton GPS, au lieu du même rappel figé en
    # boucle — voir [[precommande-architecture-consolidation-2026-08]].
    note = await llm_deviation_reply(mc_runtime, user_text, _GPS_STAGE_CONTEXT)
    message = f"{note}\n\n{_GPS_TEXT_REMINDER}" if note else _GPS_TEXT_REMINDER
    return GpsResolution(resolved=False, message=message)


__all__ = [
    "_GPS_HABITUAL_PROMPT",
    "_GPS_FIRST_TIME_PROMPT",
    "_GPS_TEXT_REMINDER",
    "_get_stored_location",
    "enter_gps_stage",
    "resolve_gps_stage",
    "GpsResolution",
]
