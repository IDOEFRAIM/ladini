"""Téléchargement d'un média entrant Twilio (``MediaUrl0``).

Les URLs de média Twilio exigent une authentification HTTP Basic avec les
identifiants du compte (même ``TWILIO_ACCOUNT_SID``/``TWILIO_AUTH_TOKEN`` que
l'envoi sortant, voir ``services/twilio_sender.py``) — sans elle, Twilio
répond 401.
"""

from __future__ import annotations

import logging
from typing import Tuple
from urllib.parse import urlsplit

import httpx

from ladini.core.settings import settings

logger = logging.getLogger("ladini.services.whatsapp.twilio_media")

_TIMEOUT_S = 20.0
_MAX_BYTES = 8 * 1024 * 1024

# Domaines autorisés pour l'URL INITIALE d'un média entrant.
#
# ⚠️ SÉCURITÉ (audit 2026-09-10) — `media_url` vient TEL QUEL du champ
# `MediaUrl0` du formulaire webhook, donc du réseau. Sans cette liste blanche,
# cette fonction est une primitive SSRF authentifiée : le corps de la réponse
# d'une URL choisie par l'appelant (métadonnées cloud sur 169.254.169.254,
# service interne sur localhost, port en écriture d'un Redis…) est téléchargé
# par le worker, puis publié dans un bucket Supabase et renvoyé à
# l'utilisateur. La signature Twilio (`api/security.py`) est la première
# barrière ; celle-ci est la seconde, pour que la compromission du secret
# Twilio ne donne pas en prime un accès en lecture au réseau interne.
#
# Seul l'hôte INITIAL est vérifié : Twilio répond systématiquement 307 vers
# son CDN/S3 (voir `follow_redirects=True` plus bas), et httpx retire
# l'en-tête `Authorization` au changement d'origine.
_ALLOWED_MEDIA_HOST_SUFFIXES = (
    ".twilio.com",
    ".twiliocdn.com",
)


class TwilioMediaError(Exception):
    """Erreur de téléchargement d'un média Twilio."""


def _assert_allowed_media_url(media_url: str) -> None:
    """Refuse toute URL de média hors des domaines Twilio (anti-SSRF)."""
    parts = urlsplit(media_url)
    if parts.scheme != "https":
        raise TwilioMediaError("URL de média refusée (schéma non https).")
    host = (parts.hostname or "").lower()
    if not host:
        raise TwilioMediaError("URL de média refusée (hôte absent).")
    if not any(
        host == suffix.lstrip(".") or host.endswith(suffix)
        for suffix in _ALLOWED_MEDIA_HOST_SUFFIXES
    ):
        logger.error("TWILIO_MEDIA_HOST_REJECTED | host=%s", host)
        raise TwilioMediaError("URL de média refusée (domaine non autorisé).")


async def download_twilio_media(media_url: str) -> Tuple[bytes, str]:
    """Télécharge un média Twilio et renvoie ``(binaire, content_type)``.

    ``content_type`` provient de la réponse HTTP (source la plus fiable) —
    le ``MediaContentType0`` du formulaire webhook reste la valeur de repli
    côté appelant si ce champ manque.
    """
    account_sid = str(settings.TWILIO_ACCOUNT_SID or "").strip()
    auth_token = str(settings.TWILIO_AUTH_TOKEN or "").strip()
    if not account_sid or not auth_token:
        raise TwilioMediaError("Configuration Twilio incomplète.")
    if not media_url:
        raise TwilioMediaError("URL de média manquante.")
    _assert_allowed_media_url(media_url)

    try:
        # `follow_redirects=True` est nécessaire : l'URL de média Twilio
        # répond systématiquement par un 307 vers l'emplacement réel du
        # fichier (constaté en prod — httpx ne suit PAS les redirections par
        # défaut). httpx retire automatiquement l'en-tête Authorization s'il
        # y a un changement d'origine — pas de fuite des identifiants Twilio
        # vers un hôte tiers.
        async with httpx.AsyncClient(
            timeout=_TIMEOUT_S,
            auth=(account_sid, auth_token),
            follow_redirects=True,
        ) as client:
            resp = await client.get(media_url)
    except httpx.HTTPError as exc:
        logger.error("TWILIO_MEDIA_DOWNLOAD_HTTP_ERROR | %s", exc)
        raise TwilioMediaError(
            "Impossible de récupérer la photo depuis WhatsApp."
        ) from exc

    if resp.status_code != 200:
        logger.error("TWILIO_MEDIA_DOWNLOAD_REJECTED | status=%s", resp.status_code)
        raise TwilioMediaError("Échec de récupération de la photo depuis WhatsApp.")

    binary = resp.content
    if len(binary) > _MAX_BYTES:
        raise TwilioMediaError("Photo trop volumineuse (8 Mo maximum).")

    content_type = (
        (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    )
    return binary, content_type
