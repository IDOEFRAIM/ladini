"""Client HTTP WhatsApp Cloud API (Meta Graph API) — envoi de messages.

Remplace le wrapper ``twilio.rest.Client`` : appels HTTP directs vers l'API
Graph de Meta, sans intermédiaire facturé au message ni au segment. Voir
``api/routes/whatsapp_webhook.py`` pour la réception (webhook + vérification
de signature HMAC) et ``core/settings.py`` pour la configuration.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

from agriconnect.core.settings import settings

logger = logging.getLogger("agriconnect.services.whatsapp.cloud_api")

# Marge de sécurité pour la lisibilité — la limite réelle Meta est 4096
# caractères par message texte (bien plus haute que Twilio) ; on garde le
# même seuil de découpage que l'ancien code pour ne rien changer côté UX.
_SOFT_CHUNK_LIMIT = 1500
_TIMEOUT_S = 15.0


class WhatsAppCloudAPIError(Exception):
    """Erreur de communication ou de réponse de l'API Cloud WhatsApp."""

    def __init__(self, message: str, *, error_code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code


def _base_url() -> str:
    version = str(settings.WHATSAPP_GRAPH_API_VERSION or "v21.0").strip()
    phone_id = str(settings.WHATSAPP_PHONE_NUMBER_ID or "").strip()
    return f"https://graph.facebook.com/{version}/{phone_id}/messages"


def _headers() -> Dict[str, str]:
    token = str(settings.WHATSAPP_CLOUD_API_TOKEN or "").strip()
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def chunk_body(body: str, limit: int = _SOFT_CHUNK_LIMIT) -> List[str]:
    """Découpe un texte long en segments lisibles (coupe de préférence sur un
    saut de ligne) — même logique que l'ancien découpage Twilio."""
    body = (body or "").strip()
    if not body:
        return [""]
    chunks: List[str] = []
    remaining = body
    while len(remaining) > limit:
        split_idx = remaining.rfind("\n", 0, limit)
        if split_idx == -1 or split_idx < limit // 2:
            split_idx = limit
        chunks.append(remaining[:split_idx].rstrip())
        remaining = remaining[split_idx:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks[:4]


def is_configured() -> bool:
    return bool(
        str(settings.WHATSAPP_CLOUD_API_TOKEN or "").strip()
        and str(settings.WHATSAPP_PHONE_NUMBER_ID or "").strip()
    )


async def _post(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not is_configured():
        raise WhatsAppCloudAPIError("Configuration WhatsApp Cloud API incomplète (token/phone_number_id).")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            resp = await client.post(_base_url(), json=payload, headers=_headers())
            data = resp.json() if resp.content else {}
    except httpx.HTTPError as exc:
        logger.error("WHATSAPP_SEND_HTTP_ERROR | %s", exc)
        raise WhatsAppCloudAPIError("Impossible de contacter WhatsApp Cloud API pour le moment.") from exc

    if resp.status_code >= 400:
        err = data.get("error") or {}
        logger.error("WHATSAPP_SEND_REJECTED | status=%s | error=%s", resp.status_code, err)
        raise WhatsAppCloudAPIError(
            err.get("message") or f"Échec d'envoi WhatsApp (HTTP {resp.status_code}).",
            error_code=str(err.get("code") or ""),
        )
    return data


async def send_text(to_phone: str, body: str) -> List[str]:
    """Envoie un message texte, découpé en plusieurs messages si trop long.

    Retourne la liste des ``message_id`` (wamid) envoyés, dans l'ordre.
    """
    clean_to = to_phone.replace("whatsapp:", "").strip().lstrip("+")
    message_ids: List[str] = []
    for chunk in chunk_body(body):
        if not chunk:
            continue
        data = await _post({
            "messaging_product": "whatsapp",
            "to": clean_to,
            "type": "text",
            "text": {"body": chunk, "preview_url": False},
        })
        msgs = data.get("messages") or []
        if msgs:
            message_ids.append(str(msgs[0].get("id") or ""))
    return message_ids


async def send_interactive_buttons(
    to_phone: str,
    body: str,
    buttons: List[Dict[str, str]],
) -> Optional[str]:
    """Boutons de réponse rapide (max 3, titre 20 caractères max).

    ``buttons`` : liste de ``{"id": "CONFIRM", "title": "✅ Confirmer"}``.
    Nativement supporté par l'API Cloud sans template pré-approuvé — c'est
    l'un des principaux avantages face à la Content API Twilio (qui exigeait
    un ContentSid enregistré à l'avance).
    """
    clean_to = to_phone.replace("whatsapp:", "").strip().lstrip("+")
    data = await _post({
        "messaging_product": "whatsapp",
        "to": clean_to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body[:1024]},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": str(b["id"])[:256], "title": str(b["title"])[:20]}}
                    for b in buttons[:3]
                ]
            },
        },
    })
    msgs = data.get("messages") or []
    return str(msgs[0].get("id")) if msgs else None


__all__ = [
    "WhatsAppCloudAPIError",
    "is_configured",
    "send_text",
    "send_interactive_buttons",
    "chunk_body",
]
