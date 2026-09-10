"""ResponseDispatcher — SEUL point d'autorité pour l'envoi d'une réponse
RÉACTIVE (déclenchée par un inbound event WhatsApp/Twilio) vers l'utilisateur
(2026-09-02, consolidation architecturale finale).

Avant ce module, DEUX chokepoints d'envoi coexistaient (`process_agent_task`
et `send_confirmation_text`, tous deux dans `api/tasks.py`), chacun avec sa
propre logique de branchement provider — et l'envoi MÉDIA
(`services/twilio_sender.py::send_whatsapp_media`, utilisé par
`workers/media/product_photo_task.py`) n'avait AUCUNE protection contre un
retry Celery. Ce module remplace les deux par UN SEUL, avec TEXTE et MÉDIA
soumis à la même garantie :

    ResponsePlan (event_id + items[])
        → ResponseDispatcher.dispatch()
        → claim(event_id, item_index) — atomique, Redis SETNX
        → Transport Adapter (send_whatsapp_message / send_whatsapp_media)
        → Twilio / WhatsApp Cloud API

Contrat : 1 inbound event → 1 ResponsePlan → dispatch de CHAQUE item déclaré
au plus une fois — jamais deux fois pour le MÊME item sur un retry/duplicate,
mais un plan multipart explicite (texte + image) envoie bien SES DEUX items,
volontairement — ce n'est pas la même chose qu'un doublon accidentel de DEUX
ResponsePlans concurrents pour le même event (voir docstring de
`ResponseDispatcher.dispatch`).

Sémantique : "at-most-once dispatch LOGIQUE" (jamais un second essai côté
process), PAS "exactly-once delivery" — Twilio/WhatsApp restent des systèmes
externes dont la livraison finale n'est pas garantie par ce contrat (un envoi
réussi côté Twilio mais dont l'accusé de réception se perd reste possible et
hors du périmètre de ce module).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union

from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

from ladini.core.idempotency import claim_once
from ladini.core.settings import settings

logger = logging.getLogger("Ladini.ResponseDispatcher")

# =====================================================================
# RESPONSE PLAN / RESPONSE ITEM — le contrat de sortie du domaine
# =====================================================================


@dataclass(frozen=True)
class TextResponse:
    text: str
    # Forme historique de `result.get("interactive")` (voir
    # orchestrator.py::_interactive_hint) — boutons quick-reply/confirm.
    interactive: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class ImageResponse:
    url: str
    caption: str = ""


ResponseItem = Union[TextResponse, ImageResponse]


@dataclass(frozen=True)
class ResponsePlan:
    """``event_id`` : identité stable de l'événement entrant (MessageSid
    Twilio / message.id WhatsApp Cloud) — clé de corrélation ET
    d'idempotence, inchangée du webhook jusqu'au dispatch (mandat §5/§8).
    ``items`` : 1 ou plusieurs `ResponseItem` — EXPLICITEMENT déclarés par le
    domaine (jamais 2 ResponsePlans concurrents pour le même event, voir
    ResponseDispatcher.dispatch)."""

    event_id: Optional[str]
    items: Tuple[ResponseItem, ...] = field(default_factory=tuple)

    @classmethod
    def text(
        cls,
        event_id: Optional[str],
        text: str,
        *,
        interactive: Optional[Dict[str, Any]] = None,
    ) -> "ResponsePlan":
        return cls(event_id=event_id, items=(TextResponse(text=text, interactive=interactive),))


# =====================================================================
# IDEMPOTENCE — claim atomique par ITEM (pas seulement par event)
#
# (2026-09-03, consolidation architecturale) : cette garde et celle de
# `domain/procurement_draft.py::apply_domain_action` (claim d'une
# confirmation) implémentaient CHACUNE leur propre client Redis + SET-NX-EX
# — la même primitive, dupliquée. Les deux délèguent maintenant à
# `core/idempotency.py::claim_once`, SEULE implémentation du mécanisme —
# celle-ci garde son nom et son préfixe de clé (`resp:`) propres à son
# usage (idempotence d'un ITEM de réponse sortante), distincts de
# `procurement_confirm:` (idempotence d'une DÉCISION métier) : mêmes
# PRIMITIVE, espaces de clés différents, pas deux mécanismes.
# =====================================================================


def claim_response_item(item_key: Optional[str]) -> bool:
    """Retourne True si CET item a le droit d'être envoyé maintenant
    (première et seule fois pour cette clé), False s'il l'a déjà été — un
    retry Celery/une redelivery tombera alors sur False et n'enverra rien de
    plus pour CET item précis. Fail-open (True) si Redis est indisponible ou
    si `item_key` est absent : mieux vaut un doublon rarissime qu'une
    réponse jamais envoyée. Voir `core/idempotency.py::claim_once` pour le
    contrat exact (SET-NX-EX, sûr sous accès concurrents réels)."""
    if not item_key:
        return True
    return claim_once(f"resp:{item_key}", ttl_seconds=3600)


# =====================================================================
# TRANSPORT ADAPTERS — HTTP/API call pur, aucune décision de dispatch
# (déplacés depuis api/tasks.py — inchangés, "conservés intacts" au sens du
# mandat §12 : ce ne sont QUE des primitives d'appel provider).
# =====================================================================

# Marge sous la limite dure Twilio (1600c, erreur 21617 au-delà).
_TWILIO_SOFT_LIMIT = 1400
_TWILIO_DISCLAIMER = " (Détails complets disponibles sur votre dashboard)"


def sanitize_content_variables(raw_vars: Dict[str, Any]) -> Dict[str, str]:
    """Force chaque clé/valeur de `content_variables` en string non nulle
    (incident Twilio 21656, 2026-08-27)."""
    clean_vars: Dict[str, str] = {}
    for key, val in raw_vars.items():
        str_key = str(key)
        if val is None:
            clean_vars[str_key] = ""
        elif isinstance(val, (int, float)):
            clean_vars[str_key] = str(val)
        else:
            clean_vars[str_key] = str(val).strip()
    return clean_vars


def send_whatsapp_message(
    client: Client,
    from_number: str,
    to_phone: str,
    body: Optional[str] = None,
    content_sid: Optional[str] = None,
    content_vars: Optional[Dict[str, Any]] = None,
) -> Optional[Any]:
    """Envoie un message WhatsApp via Twilio (texte brut ou Content Template)."""
    clean_from = from_number.replace("whatsapp:", "").strip()
    clean_to = to_phone.replace("whatsapp:", "").strip()

    if clean_from == clean_to:
        logger.warning(
            "Tentative d'envoi WhatsApp vers le même numéro (%s). Ignoré.", clean_to
        )
        return None

    from_formatted = f"whatsapp:{clean_from}"
    to_formatted = f"whatsapp:{clean_to}"

    kwargs: Dict[str, Any] = {"from_": from_formatted, "to": to_formatted}

    if content_sid:
        kwargs["content_sid"] = content_sid
        if content_vars:
            clean_vars = sanitize_content_variables(content_vars)
            kwargs["content_variables"] = json.dumps(clean_vars)
            logger.debug(
                "TWILIO_CONTENT_VARS | content_sid=%s | vars=%s", content_sid, clean_vars
            )
        if body:
            kwargs["body"] = body
    elif body:
        kwargs["body"] = body
    else:
        logger.warning("Ni body ni content_sid fourni à send_whatsapp_message.")
        return None

    return client.messages.create(**kwargs)


def _chunk_by_char_limit(body: str, limit: int) -> List[str]:
    remaining = body.strip()
    chunks: List[str] = []
    while len(remaining) > limit:
        split_idx = remaining.rfind("\n", 0, limit)
        if split_idx == -1 or split_idx < limit // 2:
            split_idx = limit
        chunk = remaining[:split_idx].rstrip()
        chunks.append(chunk)
        remaining = remaining[split_idx:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _chunk_whatsapp_body(body: str, limit: int = _TWILIO_SOFT_LIMIT) -> List[str]:
    if not body:
        return [""]

    # Importé depuis SON module de définition (`text_pagination`, qui
    # l'exporte explicitement via `__all__`), pas à travers `rendering.success`
    # qui ne faisait que le ré-exporter par accident : ce ré-export implicite
    # a été supprimé par un `ruff --fix` (import non utilisé DANS ce
    # module-là) le 2026-09-10, cassant l'import d'ici — la pagination
    # retombait alors silencieusement sur "une seule page" pour tout message.
    from ladini.graphs.agents.market_coach.services.text_pagination import (
        PAGE_BREAK,
    )

    pages = body.split(PAGE_BREAK) if PAGE_BREAK in body else [body]
    chunks: List[str] = []
    for page in pages:
        chunks.extend(_chunk_by_char_limit(page, limit))

    if len(chunks) > 4:
        kept = chunks[:3]
        kept.append(
            f"{chunks[3][: limit - len(_TWILIO_DISCLAIMER) - 5]} {_TWILIO_DISCLAIMER}"
        )
        logger.warning("Response exceeded chunk limit; truncated with disclaimer")
        return kept

    return chunks


def _send_via_twilio(
    phone_number: str, final_text: str, result: Dict[str, Any]
) -> Dict[str, Any]:
    """Repli Twilio — conservé intact (nom ET signature d'origine, seule
    l'adresse du module change) pour un rollback instantané
    (``MESSAGING_PROVIDER=twilio``) ET pour ne pas casser la suite de tests
    existante qui verrouille ce comportement en détail
    (tests/unit/test_tasks_twilio_interactive.py)."""
    account_sid = str(settings.TWILIO_ACCOUNT_SID or "").strip()
    auth_token = str(settings.TWILIO_AUTH_TOKEN or "").strip()
    from_number = str(settings.TWILIO_WHATSAPP_NUMBER or "").strip()

    if not account_sid or not auth_token or not from_number:
        logger.error("Twilio configuration incomplete; cannot send WhatsApp response")
        raise RuntimeError("Twilio configuration incomplete")

    client = Client(account_sid, auth_token)
    to_addr = f"whatsapp:{phone_number}"

    logger.info(
        "TWILIO_SEND | account=%s | from=%s | to=%s | body_len=%d | body_preview=%r",
        account_sid[:10], from_number, to_addr, len(str(final_text)), str(final_text)[:120],
    )

    try:
        interactive = result.get("interactive") or {}
        kind = interactive.get("kind")
        interactive_enabled = bool(getattr(settings, "TWILIO_INTERACTIVE_ENABLED", False))

        if kind in ("confirm", "quick_reply") and interactive_enabled:
            confirm_sid = str(
                getattr(settings, "TWILIO_CONFIRM_CONTENT_SID", "") or ""
            ).strip()
            if confirm_sid:
                try:
                    message = send_whatsapp_message(
                        client=client,
                        from_number=from_number,
                        to_phone=to_addr,
                        body=str(final_text)[:_TWILIO_SOFT_LIMIT],
                        content_sid=confirm_sid,
                        content_vars={"1": str(final_text)[:_TWILIO_SOFT_LIMIT]},
                    )
                    if message is None:
                        return {"status": "message_skipped", "reason": "same_from_to"}
                    return {"status": "message_sent", "sid": message.sid, "interactive": kind}
                except TwilioRestException as exc:
                    logger.warning(
                        "TWILIO_CONTENT_SEND_FAILED | kind=%s | code=%s | status=%s | msg=%s — repli texte brut",
                        kind, exc.code, exc.status, exc.msg,
                    )

        chunks = _chunk_whatsapp_body(str(final_text))
        last_sid = None
        for chunk in chunks:
            message = send_whatsapp_message(
                client=client, from_number=from_number, to_phone=to_addr, body=chunk
            )
            if message is None:
                return {"status": "message_skipped", "reason": "same_from_to"}
            last_sid = message.sid

        return {"status": "message_sent", "sid": last_sid, "chunks": len(chunks)}
    except Exception as e:
        logger.error("Erreur Twilio : %s", e)
        raise


async def _send_via_whatsapp_cloud(
    phone_number: str, final_text: str, result: Dict[str, Any]
) -> Dict[str, Any]:
    """Envoi via l'API Cloud WhatsApp (Meta directe) — provider par défaut.
    Nom ET signature d'origine conservés, voir `_send_via_twilio`."""
    from ladini.services.whatsapp import cloud_api_client as wa

    if not wa.is_configured():
        logger.error("WhatsApp Cloud API configuration incomplete; cannot send response")
        raise RuntimeError("WhatsApp Cloud API configuration incomplete")

    logger.info(
        "WHATSAPP_CLOUD_SEND | to=%s | body_len=%d | body_preview=%r",
        phone_number, len(str(final_text)), str(final_text)[:120],
    )

    interactive = result.get("interactive") or {}
    native_enabled = getattr(settings, "WHATSAPP_NATIVE_INTERACTIVE_ENABLED", True)
    kind = interactive.get("kind")
    if kind in ("confirm", "quick_reply") and native_enabled:
        buttons = interactive.get("buttons") or [
            {"id": "CONFIRM", "title": "✅ Confirmer"},
            {"id": "REJECT", "title": "❌ Annuler"},
        ]
        message_id = await wa.send_interactive_buttons(
            phone_number, str(final_text), buttons=buttons
        )
        if message_id is None:
            return {"status": "message_skipped", "reason": "send_failed"}
        return {"status": "message_sent", "sid": message_id, "interactive": kind}

    message_ids = await wa.send_text(phone_number, str(final_text))
    if not message_ids:
        return {"status": "message_skipped", "reason": "send_failed"}
    return {"status": "message_sent", "sid": message_ids[-1], "chunks": len(message_ids)}


def _send_image_via_twilio(phone_number: str, item: ImageResponse) -> Dict[str, Any]:
    """Média — Twilio uniquement aujourd'hui (limitation PRÉEXISTANTE,
    inchangée par cette consolidation : `services/twilio_sender.py::
    send_whatsapp_media` n'a jamais eu de repli WhatsApp Cloud — voir
    rapport final, section G)."""
    from ladini.services.twilio_sender import send_whatsapp_media

    sid = send_whatsapp_media(phone_number, item.url, item.caption)
    if sid is None:
        return {"status": "message_skipped", "reason": "send_failed"}
    return {"status": "message_sent", "sid": sid}


# =====================================================================
# RESPONSE DISPATCHER
# =====================================================================


class ResponseDispatcher:
    """SEUL point d'autorité pour l'envoi d'une réponse réactive. Aucun
    node métier, renderer, ou webhook ne doit appeler un transport adapter
    directement — voir `tests/architecture/test_response_dispatcher_is_the_only_sender.py`."""

    async def dispatch(
        self, phone_number: str, plan: ResponsePlan
    ) -> List[Dict[str, Any]]:
        """Envoie CHAQUE item de `plan.items`, au plus une fois par item —
        la clé d'idempotence est `{event_id}:{index}`, pas seulement
        `event_id` : un plan multipart (texte + image) a DEUX clés
        distinctes, donc un retry qui aurait réussi à envoyer le texte mais
        échoué avant l'image ne renvoie PAS le texte, mais envoie bien
        l'image manquante (chaque item a son propre état d'idempotence,
        mandat §9/§19)."""
        provider = (
            str(getattr(settings, "MESSAGING_PROVIDER", "") or "whatsapp_cloud")
            .strip()
            .lower()
        )
        results: List[Dict[str, Any]] = []
        for index, item in enumerate(plan.items):
            item_key = f"{plan.event_id}:{index}" if plan.event_id else None
            if not claim_response_item(item_key):
                logger.warning(
                    "RESPONSE_DISPATCH_DUPLICATE_SUPPRESSED | item_key=%s | "
                    "déjà envoyé — probable retry Celery/redelivery post-envoi",
                    item_key,
                )
                results.append({"status": "duplicate_suppressed", "item_key": item_key})
                continue

            if isinstance(item, TextResponse):
                legacy_result = {"interactive": item.interactive} if item.interactive else {}
                if provider == "twilio":
                    try:
                        results.append(
                            _send_via_twilio(phone_number, item.text, legacy_result)
                        )
                    except TwilioRestException as exc:
                        # Erreur de VALIDATION CLIENT (ex: 21617) : jamais
                        # résolue par un retry identique — abandon sans
                        # relancer, comme avant cette consolidation.
                        if exc.status is not None and 400 <= exc.status < 500:
                            logger.error(
                                "TWILIO_PERMANENT_FAILURE | code=%s | status=%s | msg=%s — abandon sans retry",
                                exc.code, exc.status, exc.msg,
                            )
                            results.append(
                                {
                                    "status": "message_failed",
                                    "reason": "twilio_client_error",
                                    "code": exc.code,
                                }
                            )
                        else:
                            raise
                else:
                    results.append(
                        await _send_via_whatsapp_cloud(phone_number, item.text, legacy_result)
                    )
            elif isinstance(item, ImageResponse):
                # `_send_image_via_twilio` est SYNC (SDK Twilio, appel réseau
                # bloquant) — délestée sur un thread pour ne jamais bloquer
                # la boucle asyncio partagée du worker (même discipline que
                # l'ancien `product_photo_task.py`, voir aussi
                # `workers/outbox/channels/whatsapp.py::_send_sync_twilio`).
                results.append(
                    await asyncio.to_thread(_send_image_via_twilio, phone_number, item)
                )
            else:  # pragma: no cover - garde défensive, pas un item connu
                logger.error("RESPONSE_DISPATCH_UNKNOWN_ITEM_TYPE | type=%s", type(item))
        return results


_dispatcher = ResponseDispatcher()


def get_dispatcher() -> ResponseDispatcher:
    return _dispatcher


__all__ = [
    "TextResponse",
    "ImageResponse",
    "ResponseItem",
    "ResponsePlan",
    "ResponseDispatcher",
    "get_dispatcher",
    "claim_response_item",
    "sanitize_content_variables",
    "send_whatsapp_message",
]
