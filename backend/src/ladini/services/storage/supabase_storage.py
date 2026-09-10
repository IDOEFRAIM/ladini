"""Upload vers Supabase Storage (photos produits reçues par WhatsApp).

Client HTTP direct (API REST Storage) — même style de client que
``services/whatsapp/cloud_api_client.py`` (httpx.AsyncClient, timeout
explicite, exception métier dédiée). Réservé aux uploads serveur à serveur
avec la clé de SERVICE ROLE : jamais exposé côté client.
"""

from __future__ import annotations

import logging
import uuid
from typing import Dict

import httpx

from ladini.core.settings import settings

logger = logging.getLogger("ladini.services.storage.supabase")

_TIMEOUT_S = 20.0  # upload binaire — un peu plus large que les 15s des appels JSON
_MAX_BYTES = 8 * 1024 * 1024  # 8 Mo — largement suffisant pour une photo WhatsApp

_EXTENSION_BY_CONTENT_TYPE: Dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}


class SupabaseStorageError(Exception):
    """Erreur de communication ou de réponse de Supabase Storage."""


def is_configured() -> bool:
    return bool(
        str(settings.SUPABASE_URL or "").strip()
        and str(settings.SUPABASE_SERVICE_ROLE_KEY or "").strip()
    )


def _object_path(phone: str, content_type: str) -> str:
    ext = _EXTENSION_BY_CONTENT_TYPE.get(content_type, "bin")
    safe_phone = "".join(ch for ch in str(phone or "") if ch.isalnum()) or "unknown"
    return f"{safe_phone}/{uuid.uuid4().hex}.{ext}"


async def upload_product_photo(binary: bytes, content_type: str, phone: str) -> str:
    """Upload une image produit et renvoie son URL publique Supabase.

    Lève ``SupabaseStorageError`` (message déjà sûr pour l'utilisateur final)
    sur toute défaillance — configuration manquante, type non supporté,
    fichier trop volumineux, ou échec réseau/HTTP.
    """
    if not is_configured():
        raise SupabaseStorageError(
            "L'envoi de photos n'est pas encore disponible, réessayez plus tard."
        )
    if content_type not in _EXTENSION_BY_CONTENT_TYPE:
        raise SupabaseStorageError(
            "Format de photo non supporté (JPEG, PNG ou WebP uniquement)."
        )
    if not binary:
        raise SupabaseStorageError("La photo reçue est vide.")
    if len(binary) > _MAX_BYTES:
        raise SupabaseStorageError("Photo trop volumineuse (8 Mo maximum).")

    bucket = str(settings.SUPABASE_PRODUCT_BUCKET or "product-photos").strip()
    base_url = str(settings.SUPABASE_URL or "").rstrip("/")
    service_key = str(settings.SUPABASE_SERVICE_ROLE_KEY or "").strip()
    path = _object_path(phone, content_type)
    upload_url = f"{base_url}/storage/v1/object/{bucket}/{path}"
    headers = {
        "Authorization": f"Bearer {service_key}",
        "apikey": service_key,
        "Content-Type": content_type,
        "x-upsert": "false",
    }

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            resp = await client.post(upload_url, content=binary, headers=headers)
    except httpx.HTTPError as exc:
        # L'URL elle-même ne contient aucun secret (bucket + chemin de
        # fichier) — sûre à journaliser pour diagnostiquer un SUPABASE_URL
        # mal formé (ex: getaddrinfo failed = hôte introuvable).
        logger.error("SUPABASE_UPLOAD_HTTP_ERROR | url=%s | %s", upload_url, exc)
        raise SupabaseStorageError(
            "Impossible de contacter le stockage de photos pour le moment."
        ) from exc

    if resp.status_code not in (200, 201):
        logger.error(
            "SUPABASE_UPLOAD_REJECTED | status=%s | body=%r",
            resp.status_code,
            resp.text[:300],
        )
        raise SupabaseStorageError("Échec de l'envoi de la photo, réessayez.")

    public_url = f"{base_url}/storage/v1/object/public/{bucket}/{path}"
    logger.info("SUPABASE_UPLOAD_OK | bucket=%s | path=%s", bucket, path)
    return public_url
