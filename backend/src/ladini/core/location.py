"""Location — taxonomie canonique + persistance synchrone partagée
(refonte architecturale 2026-09-02, GPS).

## Ce que l'audit a confirmé (à ne pas re-découvrir)

Aucun parseur de texte/URL n'existe dans ce repo — seul le partage natif
WhatsApp (`Latitude`/`Longitude` de Twilio, `location.latitude/longitude` de
la Cloud API) est réellement implémenté. `LocationSourceKind` déclare
explicitement les autres formats plutôt que de prétendre les supporter
(mandat §29) : les ajouter reste une décision de périmètre séparée, hors de
cette refonte.

Deuxième problème confirmé : la persistance GPS tournait en `BackgroundTasks`
FastAPI, exécutée APRÈS la réponse HTTP à Twilio — alors que la tâche
Celery de traitement de l'agent était déjà enqueuée AVANT cette réponse.
Aucune garantie d'ordre entre les deux : le graphe pouvait relire un profil
DB pas encore à jour, produisant le message ambigu "Je n'ai pas pu récupérer
ce point GPS" pour un point pourtant valide (juste pas encore écrit) — ou,
plus grave, retomber SILENCIEUSEMENT sur une ancienne position stockée.

Fix (mandat §30, Option A) : `persist_shared_location` est maintenant
appelée SYNCHRONE (`await`), AVANT l'enqueue Celery — voir
`api/routes/twilio_webhook.py`/`whatsapp_webhook.py`. L'issue exacte
(accepté/rejeté/erreur) est transmise EXPLICITEMENT au tour de l'agent
(`location_outcome` dans le state du graphe) au lieu d'être redécouverte une
relecture DB plus tard — élimine à la fois la course ET l'ambiguïté du
message d'erreur (mandat §31 : jamais de repli silencieux)."""

from __future__ import annotations

import logging
import re
from enum import Enum
from typing import Optional, Tuple

from ladini.core.geofencing import OUT_OF_COUNTRY_MESSAGE, is_within_burkina_faso

logger = logging.getLogger("ladini.core.location")


class LocationSourceKind(str, Enum):
    """Formats d'entrée GPS — un SEUL est réellement implémenté aujourd'hui.
    Les autres sont déclarés pour rendre le périmètre actuel explicite
    (mandat §29 : "ne pas prétendre supporter des formats non implémentés"),
    pas pour être traités silencieusement comme équivalents."""

    NATIVE_WHATSAPP_LOCATION = "NATIVE_WHATSAPP_LOCATION"
    GOOGLE_MAPS_URL = "GOOGLE_MAPS_URL"  # implémenté : parse_google_maps_coordinates
    COORDINATES = "COORDINATES"  # déclaré, PAS implémenté
    TEXT_ADDRESS = "TEXT_ADDRESS"  # déclaré, PAS implémenté
    PLUS_CODE = "PLUS_CODE"  # déclaré, PAS implémenté


_COORD = r"(-?\d{1,3}\.\d+)"
# Formes Google Maps qui portent les coordonnées DANS l'URL (aucun appel
# réseau) : ?q=lat,lon / ?ll=lat,lon / ?query=lat,lon, /@lat,lon, !3dlat!4dlon.
# Les liens courts (maps.app.goo.gl) exigent une résolution HTTP — non gérés.
_MAPS_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:google\.[a-z.]+/maps|maps\.google\.[a-z.]+|goo\.gl/maps)\S*",
    re.IGNORECASE,
)
_MAPS_COORD_PATTERNS = (
    re.compile(rf"[?&](?:q|ll|query|destination)={_COORD}(?:,|%2C)\s*{_COORD}", re.I),
    re.compile(rf"/@{_COORD},{_COORD}"),
    re.compile(rf"!3d{_COORD}!4d{_COORD}"),
)


# Paire « lat,lon » collée seule (ex. copiée depuis Google Maps). ≥3 décimales
# des deux côtés : un prix ou une quantité (« 12.5, 3.2 ») n'est jamais un GPS.
_BARE_COORDS_RE = re.compile(
    r"(?<![\d.])(-?\d{1,3}\.\d{3,})\s*[,;]\s*(-?\d{1,3}\.\d{3,})(?![\d.])"
)


def _parse_bare_coordinates(text: str) -> Optional[Tuple[float, float]]:
    found = _BARE_COORDS_RE.search(text)
    if not found:
        return None
    lat, lon = float(found.group(1)), float(found.group(2))
    if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
        return lat, lon
    return None


def parse_google_maps_coordinates(text: str) -> Optional[Tuple[float, float]]:
    """Extrait `(lat, lon)` d'un lien Google Maps collé en TEXTE, ou None.

    Incident : un client (webchat / partage « position actuelle ») envoie
    `📍 Ma position actuelle : https://www.google.com/maps?q=..,..` comme
    message texte, pas comme message `location` natif. Sans ce parseur le
    point était ignoré et l'agent redemandait le GPS en boucle. Déterministe
    (jamais de LLM) ; le geofencing reste appliqué par `persist_shared_location`."""
    match = _MAPS_URL_RE.search(text or "")
    if not match:
        return _parse_bare_coordinates(text or "")
    url = match.group(0)
    for pattern in _MAPS_COORD_PATTERNS:
        found = pattern.search(url)
        if found:
            lat, lon = float(found.group(1)), float(found.group(2))
            if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
                return lat, lon
    return None


class LocationOutcome(str, Enum):
    NO_LOCATION = "NO_LOCATION"
    NEW_LOCATION_ACCEPTED = "NEW_LOCATION_ACCEPTED"
    NEW_LOCATION_REJECTED = "NEW_LOCATION_REJECTED"
    LOCATION_PARSE_ERROR = "LOCATION_PARSE_ERROR"
    LOCATION_PERSISTENCE_ERROR = "LOCATION_PERSISTENCE_ERROR"
    LOCATION_OUT_OF_ZONE = "LOCATION_OUT_OF_ZONE"


async def persist_shared_location(
    phone: str, lat: float, lon: float
) -> Tuple[LocationOutcome, Optional[str]]:
    """Écrit `User.latitude/longitude` — SYNCHRONE (à `await`), plus jamais
    en `BackgroundTasks`. Retourne `(outcome, user_message_or_None)` :
    l'appelant (webhook) transmet CET outcome explicitement au tour de
    l'agent au lieu de le laisser redécouvrir l'état par une relecture DB.

    Ne lève JAMAIS — un souci d'infra pendant la persistance GPS ne doit
    jamais faire échouer le webhook Twilio/WhatsApp lui-même."""
    try:
        from ladini.core.database import get_sessionmaker
        from ladini.services.database.auth import AuthMixin

        class _GeoOnlyService(AuthMixin):
            def __init__(self, session):
                self._session = session

            @property
            def session(self):
                return self._session

        sessionmaker = get_sessionmaker()
        if sessionmaker is None:
            logger.warning("LOCATION_PERSIST_SKIPPED | sessionmaker indisponible")
            return LocationOutcome.LOCATION_PERSISTENCE_ERROR, None

        async with sessionmaker() as session:
            service = _GeoOnlyService(session)
            result = await service.update_geo_location(phone=phone, lat=lat, lon=lon)
            status = str(result.get("status") or "")
            if status == "success":
                await session.commit()
                logger.info(
                    "LOCATION_PERSIST_SAVED | phone=***%s",
                    phone[-4:] if len(phone) >= 4 else phone,
                )
                return LocationOutcome.NEW_LOCATION_ACCEPTED, None

            await session.rollback()
            reason = str(result.get("reason") or "")
            logger.warning(
                "LOCATION_PERSIST_REJECTED | phone=***%s | reason=%s",
                phone[-4:] if len(phone) >= 4 else phone,
                result.get("message"),
            )
            if reason == "out_of_country":
                return LocationOutcome.LOCATION_OUT_OF_ZONE, OUT_OF_COUNTRY_MESSAGE
            return LocationOutcome.LOCATION_PERSISTENCE_ERROR, result.get("message")
    except Exception:
        logger.exception(
            "LOCATION_PERSIST_ERROR | phone=***%s",
            phone[-4:] if len(phone) >= 4 else phone,
        )
        return LocationOutcome.LOCATION_PERSISTENCE_ERROR, None


__all__ = [
    "LocationSourceKind",
    "LocationOutcome",
    "persist_shared_location",
    "parse_google_maps_coordinates",
    "is_within_burkina_faso",
    "OUT_OF_COUNTRY_MESSAGE",
]
