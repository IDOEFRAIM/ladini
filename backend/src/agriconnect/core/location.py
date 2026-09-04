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
from enum import Enum
from typing import Optional, Tuple

from agriconnect.core.geofencing import OUT_OF_COUNTRY_MESSAGE, is_within_burkina_faso

logger = logging.getLogger("agriconnect.core.location")


class LocationSourceKind(str, Enum):
    """Formats d'entrée GPS — un SEUL est réellement implémenté aujourd'hui.
    Les autres sont déclarés pour rendre le périmètre actuel explicite
    (mandat §29 : "ne pas prétendre supporter des formats non implémentés"),
    pas pour être traités silencieusement comme équivalents."""

    NATIVE_WHATSAPP_LOCATION = "NATIVE_WHATSAPP_LOCATION"
    GOOGLE_MAPS_URL = "GOOGLE_MAPS_URL"  # déclaré, PAS implémenté
    COORDINATES = "COORDINATES"  # déclaré, PAS implémenté
    TEXT_ADDRESS = "TEXT_ADDRESS"  # déclaré, PAS implémenté
    PLUS_CODE = "PLUS_CODE"  # déclaré, PAS implémenté


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
        from agriconnect.core.database import get_sessionmaker
        from agriconnect.services.database.auth import AuthMixin

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
    "is_within_burkina_faso",
    "OUT_OF_COUNTRY_MESSAGE",
]
