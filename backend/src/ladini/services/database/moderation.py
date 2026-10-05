"""Moderation & account-abuse service mixin.

Regroupe la persistance transactionnelle liée à la protection de la
marketplace :

* état d'un compte (``ACTIVE`` / ``BLOCKED`` / ``BANNED``) ;
* liste noire des produits interdits (table admin + base par défaut fail-safe) ;
* journalisation des strikes de modération et bannissement au-delà du seuil ;
* captation de la demande non satisfaite (produits cherchés en vain).

Toutes les méthodes s'appuient sur ``self.session`` (ContextVar unifié) —
aucune ouverture de session ici, le wrapper d'``AgriDatabaseService`` s'en charge.
"""

from __future__ import annotations

import logging
import time
import unicodedata
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from sqlalchemy import func, select, text

from ladini.domain.models import (
    DemandSignal,
    ModerationEvent,
    ProhibitedTerm,
    User,
)

from .common import normalize_phone

logger = logging.getLogger("ladini.services.database.moderation")


# ── Seuils (interprétation « strictement plus de 3 fois » → au 4e) ──────────
MAX_CANCELLATIONS = 3  # blocage si annulations > 3
MAX_MODERATION_STRIKES = 3  # bannissement si mentions interdites > 3


# ── Base par défaut de produits interdits (fail-safe si la table est vide) ──
# Stockée en formes repliées ASCII (sans accents) ; le matching replie aussi
# l'entrée utilisateur, donc « cocaïne » == « cocaine ».
DEFAULT_PROHIBITED_TERMS: Set[str] = {
    # drogues
    "drogue",
    "drogues",
    "cocaine",
    "coke",
    "heroine",
    "cannabis",
    "weed",
    "marijuana",
    "ganja",
    "haschich",
    "hashish",
    "shit",
    "meth",
    "methamphetamine",
    "amphetamine",
    "ecstasy",
    "mdma",
    "lsd",
    "crack",
    "opium",
    "kush",
    "chanvre indien",
    "stupefiant",
    "stupefiants",
    # armes & explosifs
    "arme a feu",
    "armes a feu",
    "pistolet",
    "revolver",
    "kalachnikov",
    "kalash",
    "fusil",
    "munition",
    "munitions",
    "grenade",
    "explosif",
    "explosifs",
    "tnt",
    "dynamite",
    "lance roquette",
    # illicite divers
    "faux billet",
    "faux billets",
    "contrefacon",
    "ivoire",
    "pangolin",
    "corne de rhinoceros",
    "organe humain",
    "organes humains",
    "espece protegee",
}


def _utcnow_naive() -> datetime:
    """UTC sans tzinfo — les colonnes DB sont TIMESTAMP WITHOUT TIME ZONE."""
    return datetime.utcnow()


def _fold(text: str) -> str:
    """Replie une chaîne en minuscule ASCII sans accents pour un matching robuste."""
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", str(text))
    ascii_only = "".join(c for c in nfkd if not unicodedata.combining(c))
    return ascii_only.lower().strip()


# Cache process-wide de la liste des termes interdits (TTL court).
_TERMS_CACHE: Dict[str, Any] = {"terms": None, "ts": 0.0}
_TERMS_TTL_SECONDS = 300.0


def _to_uuid(value: Any) -> Optional[uuid.UUID]:
    if not value:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value).strip())
    except (ValueError, AttributeError, TypeError):
        return None


class ModerationMixin:
    """Persistance de la modération et de l'anti-abus (voir docstring module)."""

    session: Any  # fourni par AgriDatabaseService

    # ── Lecture : état du compte ────────────────────────────────────────
    async def get_account_status(self, phone: str) -> Dict[str, Any]:
        """Retourne l'état de modération d'un compte (ACTIVE / BLOCKED / BANNED)."""
        session = self.session
        if session is None:
            return {"status": "error", "message": "Session indisponible."}

        norm = normalize_phone(phone, required=False)
        if not norm:
            return {
                "status": "success",
                "account_status": "ACTIVE",
                "is_blocked": False,
                "is_banned": False,
            }

        user = await session.scalar(select(User).where(User.phone == norm))
        if user is None:
            # Compte inconnu : neutre — l'onboarding créera le profil.
            return {
                "status": "success",
                "account_status": "ACTIVE",
                "is_blocked": False,
                "is_banned": False,
            }

        acc = str(user.account_status or "ACTIVE").upper()
        return {
            "status": "success",
            "account_status": acc,
            "is_blocked": acc == "BLOCKED",
            "is_banned": acc == "BANNED",
            "blocked_reason": user.blocked_reason,
        }

    # ── Lecture : dernier message sortant INTERACTIF (attend une réponse nue) ───────────────────────────────
    async def get_last_interactive_outbound(self, phone: str) -> Dict[str, Any]:
        """B20 — le dernier message PROACTIF envoyé à `phone` qui attend une réponse (digest récurrent, réception de
        livraison, confirmation producteur : `templates.INTERACTIVE_TEMPLATE_OWNERS`). Les notifications purement
        informatives ne sont jamais retournées (elles ne supplantent aucun menu). Fenêtre = TTL du digest
        (`RECURRING_SUPPLY_DIGEST_PENDING_TTL_SECONDS`, déjà la plus longue durée d'attente d'une réponse à un
        message proactif). Lecture seule ; aucun contenu libre n'est retourné (identifiants et actions fermées)."""
        from ladini.core.settings import settings
        from ladini.workers.outbox.templates import INTERACTIVE_TEMPLATE_OWNERS

        session = self.session
        if session is None:
            return {"status": "error", "message": "Session indisponible."}
        norm = normalize_phone(phone, required=False)
        if not norm:
            return {"status": "success", "interactive": None, "recovery": None}
        recovery = await self._last_recovery_context(session, norm)
        row = (
            await session.execute(
                text(
                    "select template_key, payload, sent_at, extract(epoch from sent_at at time zone 'UTC') as sent_epoch "
                    "from intelligence.notification_outbox "
                    "where recipient_phone = :phone and status = 'SENT' and sent_at is not null "
                    "and template_key = any(:keys) "
                    "and sent_at > (now() at time zone 'UTC') - make_interval(secs => :ttl) "
                    "order by sent_at desc limit 1"
                ),
                {"phone": norm, "keys": list(INTERACTIVE_TEMPLATE_OWNERS),
                 "ttl": float(settings.RECURRING_SUPPLY_DIGEST_PENDING_TTL_SECONDS)},
            )
        ).first()
        if row is None:
            return {"status": "success", "interactive": None, "recovery": recovery}
        template_key, payload, _sent_at, sent_epoch = row
        body: Dict[str, Any] = payload if isinstance(payload, dict) else {}
        raw_meta = body.get("interactive")
        meta: Dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
        raw_actions = meta.get("actions")
        actions: Optional[Dict[str, Any]] = raw_actions if isinstance(raw_actions, dict) else None
        raw_occ = body.get("occurrences")
        occurrences: List[Any] = raw_occ if isinstance(raw_occ, list) else []
        return {
            "status": "success",
            "recovery": recovery,
            "interactive": {
                "template_key": template_key,
                "owner_type": INTERACTIVE_TEMPLATE_OWNERS[template_key],
                "sent_at": float(sent_epoch),
                "menu_id": meta.get("menu_id"),
                "actions": actions,
                "recurring_need_ids": [str(o["recurring_need_id"]) for o in occurrences
                                       if isinstance(o, dict) and o.get("recurring_need_id")],
                "occurrence_ids": [str(o["occurrence_id"]) for o in occurrences
                                   if isinstance(o, dict) and o.get("occurrence_id")],
            },
        }

    @staticmethod
    async def _last_recovery_context(session: Any, norm_phone: str) -> Optional[Dict[str, Any]]:
        """B27 — la dernière notification de RÉCUPÉRATION envoyée (livraison récurrente en échec : producteur sans réponse) dans la
        fenêtre du digest. Identifiants uniquement (besoin, occurrence, date) : c'est ce qui permet à « trouve-moi quelqu'un
        d'autre » de viser la bonne livraison. Le DOMAINE décide ensuite si une relance est encore possible. Lecture seule."""
        from ladini.core.settings import settings

        row = (
            await session.execute(
                text(
                    "select payload, extract(epoch from sent_at at time zone 'UTC') as sent_epoch "
                    "from intelligence.notification_outbox "
                    "where recipient_phone = :phone and status = 'SENT' and sent_at is not null "
                    "and (payload ->> 'recovery_candidate') = 'true' "
                    "and sent_at > (now() at time zone 'UTC') - make_interval(secs => :ttl) "
                    "order by sent_at desc limit 1"
                ),
                {"phone": norm_phone, "ttl": float(settings.RECURRING_SUPPLY_DIGEST_PENDING_TTL_SECONDS)},
            )
        ).first()
        if row is None:
            return None
        body: Dict[str, Any] = row[0] if isinstance(row[0], dict) else {}
        occurrences = [o for o in (body.get("occurrences") or []) if isinstance(o, dict) and o.get("recurring_need_id")]
        if not occurrences:
            return None
        return {
            "sent_at": float(row[1]),
            "recurring_need_ids": [str(o["recurring_need_id"]) for o in occurrences],
            "occurrence_ids": [str(o["occurrence_id"]) for o in occurrences if o.get("occurrence_id")],
            "dates": [str(o["date"]) for o in occurrences if o.get("date")],
        }

    # ── Lecture : liste des produits interdits (table + défauts) ────────
    async def get_prohibited_terms(self) -> Dict[str, Any]:
        """Liste des termes interdits actifs (table admin ∪ base par défaut), cachée."""
        now = time.monotonic()
        cached = _TERMS_CACHE.get("terms")
        if (
            cached is not None
            and (now - _TERMS_CACHE.get("ts", 0.0)) < _TERMS_TTL_SECONDS
        ):
            return {"status": "success", "terms": cached, "cached": True}

        db_terms: List[str] = []
        session = self.session
        if session is not None:
            try:
                rows = (
                    (
                        await session.execute(
                            select(ProhibitedTerm.term).where(
                                ProhibitedTerm.is_active.is_(True)
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                db_terms = [_fold(t) for t in rows if t]
            except Exception:
                logger.debug(
                    "get_prohibited_terms: lecture table échouée, défauts seuls",
                    exc_info=True,
                )

        merged = sorted({t for t in db_terms if t} | DEFAULT_PROHIBITED_TERMS)
        _TERMS_CACHE["terms"] = merged
        _TERMS_CACHE["ts"] = now
        return {"status": "success", "terms": merged, "cached": False}

    # ── Écriture : enregistrer un strike + bannir au-delà du seuil ──────
    async def record_moderation_strike(
        self,
        phone: str,
        matched_term: str = "",
        excerpt: str = "",
        kind: str = "PROHIBITED_PRODUCT",
    ) -> Dict[str, Any]:
        """Journalise une infraction (produit interdit) et bannit si strikes > seuil."""
        session = self.session
        if session is None:
            return {"status": "error", "message": "Session indisponible."}

        norm = normalize_phone(phone, required=False) or str(phone or "").strip()
        kind_up = str(kind or "PROHIBITED_PRODUCT").upper()

        user = None
        if norm:
            user = await session.scalar(
                select(User).where(User.phone == norm).with_for_update()
            )

        event = ModerationEvent(
            user_id=user.id if user is not None else None,
            phone=norm or "unknown",
            kind=kind_up,
            matched_term=(str(matched_term)[:120] or None) if matched_term else None,
            excerpt=(str(excerpt)[:280] or None) if excerpt else None,
            action_taken="WARNED",
        )
        session.add(event)
        await session.flush()

        strikes = int(
            await session.scalar(
                select(func.count())
                .select_from(ModerationEvent)
                .where(
                    ModerationEvent.phone == (norm or "unknown"),
                    ModerationEvent.kind == kind_up,
                )
            )
            or 0
        )

        banned = strikes > MAX_MODERATION_STRIKES
        if (
            banned
            and user is not None
            and str(user.account_status or "").upper() != "BANNED"
        ):
            user.account_status = "BANNED"
            user.blocked_reason = (
                "Mentions répétées de produits interdits sur la marketplace."
            )
            user.blocked_at = _utcnow_naive()
            event.action_taken = "BANNED"
            await session.flush()

        return {
            "status": "success",
            "strikes": strikes,
            "limit": MAX_MODERATION_STRIKES,
            "banned": bool(banned),
        }

    # ── Écriture : capter une demande non satisfaite ───────────────────
    async def record_demand_signal(
        self,
        phone: str = "",
        raw_query: str = "",
        normalized_term: str = "",
        zone_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Agrège une demande produit non satisfaite (upsert + incrément occurrences)."""
        session = self.session
        if session is None:
            return {"status": "error", "message": "Session indisponible."}

        term = _fold(normalized_term or raw_query)
        if not term:
            return {"status": "error", "message": "Terme de demande vide."}
        term = term[:120]

        norm_phone = normalize_phone(phone, required=False) or None
        z_uuid = _to_uuid(zone_id)

        existing = await session.scalar(
            select(DemandSignal)
            .where(DemandSignal.normalized_term == term)
            .with_for_update()
        )
        if existing is not None:
            existing.occurrences = int(existing.occurrences or 0) + 1
            if raw_query:
                existing.raw_query = str(raw_query)[:280]
            if norm_phone:
                existing.phone = norm_phone
            if z_uuid is not None:
                existing.zone_id = z_uuid
        else:
            session.add(
                DemandSignal(
                    normalized_term=term,
                    raw_query=(str(raw_query)[:280] or term),
                    phone=norm_phone,
                    user_id=None,
                    zone_id=z_uuid,
                    occurrences=1,
                )
            )
        await session.flush()
        return {"status": "success", "term": term}
