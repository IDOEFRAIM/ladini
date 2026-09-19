"""Téléchargement d'un média entrant WhatsApp Cloud API (Meta Graph API).

Miroir de ``services/whatsapp/twilio_media.py`` (même contrat : renvoie
``(binaire, content_type)``, même discipline anti-SSRF), mais le protocole
Meta diffère structurellement de celui de Twilio :

- Twilio donne directement une ``MediaUrl0`` fetchable (Basic Auth).
- Meta ne donne qu'un ``id`` opaque dans le webhook (``message["image"]["id"]``)
  — il faut d'abord résoudre cet id en une URL temporaire via un GET
  authentifié sur le Graph API (``GET /{version}/{media_id}``), PUIS
  télécharger cette URL avec le MÊME jeton porteur (contrairement à Twilio,
  Meta exige le Bearer token sur les DEUX requêtes, y compris la seconde).

Voir ``api/routes/whatsapp_webhook.py`` pour le point d'entrée qui extrait
cet ``id`` du payload webhook.
"""

from __future__ import annotations

import logging
from typing import Tuple
from urllib.parse import urlsplit

import httpx

from ladini.core.settings import settings

logger = logging.getLogger("ladini.services.whatsapp.cloud_api_media")

_TIMEOUT_S = 20.0
_MAX_BYTES = 8 * 1024 * 1024

# Domaines autorisés pour l'URL TEMPORAIRE renvoyée par le Graph API (étape 2).
#
# Moins exposé au SSRF que Twilio (l'URL n'est pas fournie telle quelle par
# l'appelant du webhook : elle vient de la RÉPONSE de Meta à notre propre
# requête authentifiée sur un `media_id`), mais on applique la même défense
# en profondeur que `twilio_media.py` plutôt que de faire confiance à la
# réponse d'un service tiers sans vérification — un compte Meta compromis ou
# un bug de parsing ne doit pas se traduire par un SSRF interne.
_ALLOWED_MEDIA_HOST_SUFFIXES = (
    ".fbcdn.net",
    ".fbsbx.com",
    ".facebook.com",
)


class CloudAPIMediaError(Exception):
    """Erreur de téléchargement d'un média WhatsApp Cloud API."""


def _assert_allowed_media_url(media_url: str) -> None:
    parts = urlsplit(media_url)
    if parts.scheme != "https":
        raise CloudAPIMediaError("URL de média refusée (schéma non https).")
    host = (parts.hostname or "").lower()
    if not host:
        raise CloudAPIMediaError("URL de média refusée (hôte absent).")
    if not any(
        host == suffix.lstrip(".") or host.endswith(suffix)
        for suffix in _ALLOWED_MEDIA_HOST_SUFFIXES
    ):
        logger.error("CLOUD_API_MEDIA_HOST_REJECTED | host=%s", host)
        raise CloudAPIMediaError("URL de média refusée (domaine non autorisé).")


def _graph_base_url() -> str:
    version = str(settings.WHATSAPP_GRAPH_API_VERSION or "v21.0").strip()
    return f"https://graph.facebook.com/{version}"


def _auth_headers() -> dict:
    token = str(settings.WHATSAPP_CLOUD_API_TOKEN or "").strip()
    return {"Authorization": f"Bearer {token}"}


async def download_cloud_api_media(media_id: str) -> Tuple[bytes, str]:
    """Télécharge un média WhatsApp Cloud API et renvoie ``(binaire, content_type)``.

    Deux requêtes Graph API, TOUTES DEUX avec le Bearer token :
      1. ``GET /{version}/{media_id}`` → JSON ``{"url": ..., "mime_type": ...}``.
      2. ``GET <url>`` → octets réels.
    """
    token = str(settings.WHATSAPP_CLOUD_API_TOKEN or "").strip()
    if not token:
        raise CloudAPIMediaError("Configuration WhatsApp Cloud API incomplète (token).")
    if not media_id:
        raise CloudAPIMediaError("Identifiant de média manquant.")

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            resolve_resp = await client.get(
                f"{_graph_base_url()}/{media_id}", headers=_auth_headers()
            )
    except httpx.HTTPError as exc:
        logger.error("CLOUD_API_MEDIA_RESOLVE_HTTP_ERROR | %s", exc)
        raise CloudAPIMediaError(
            "Impossible de résoudre la photo depuis WhatsApp."
        ) from exc

    if resolve_resp.status_code != 200:
        logger.error(
            "CLOUD_API_MEDIA_RESOLVE_REJECTED | status=%s", resolve_resp.status_code
        )
        raise CloudAPIMediaError("Échec de résolution de la photo depuis WhatsApp.")

    try:
        resolved = resolve_resp.json()
    except ValueError as exc:
        raise CloudAPIMediaError("Réponse de résolution média illisible.") from exc

    media_url = str(resolved.get("url") or "").strip()
    mime_type_hint = str(resolved.get("mime_type") or "").strip().lower()
    if not media_url:
        raise CloudAPIMediaError("URL de média absente de la réponse Meta.")
    _assert_allowed_media_url(media_url)

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            resp = await client.get(media_url, headers=_auth_headers())
    except httpx.HTTPError as exc:
        logger.error("CLOUD_API_MEDIA_DOWNLOAD_HTTP_ERROR | %s", exc)
        raise CloudAPIMediaError(
            "Impossible de récupérer la photo depuis WhatsApp."
        ) from exc

    if resp.status_code != 200:
        logger.error("CLOUD_API_MEDIA_DOWNLOAD_REJECTED | status=%s", resp.status_code)
        raise CloudAPIMediaError("Échec de récupération de la photo depuis WhatsApp.")

    binary = resp.content
    if len(binary) > _MAX_BYTES:
        raise CloudAPIMediaError("Photo trop volumineuse (8 Mo maximum).")

    content_type = (
        (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        or mime_type_hint
    )
    return binary, content_type
