"""Cycle de vie et exécution des campagnes de disponibilités (base de données).

Machine d'états (CAS sur `status` et `content_version`, jamais de lecture-puis-écriture non gardée) ::

    DRAFT --validate--> SCHEDULED --tick--> RUNNING --(plus de PREPARED)--> COMPLETED           (ONCE)
                                            RUNNING --(plus de PREPARED)--> SCHEDULED + période (WEEKLY / CUSTOM_DAYS)
    DRAFT | SCHEDULED | RUNNING --cancel--> CANCELLED

Anti-doublon, de bout en bout :
* `UNIQUE (campaign_id, run_key, phone)` : un destinataire = UNE ligne par exécution (INSERT ... ON CONFLICT DO NOTHING) ;
* `PREPARED -> QUEUED` ET insertion outbox (`dedupe_key` unique) dans la MÊME transaction ;
* lot réclamé `FOR UPDATE SKIP LOCKED` : deux workers ne prennent jamais le même destinataire ;
* un `RUNNING` interrompu reprend là où il en était (les lignes déjà QUEUED ne sont jamais re-sélectionnées).
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

import ladini.domain.models  # noqa: F401  (registre de mappers complet : Product/Producer ont des relations croisées)
from ladini.core.settings import settings
from ladini.domain.catalog.models import Product
from ladini.domain.commercial_pricing_snapshot import (
    PricingReliability,
    product_pricing_view,
)
from ladini.domain.identity.models import Producer
from ladini.services.availability_campaigns import (
    consent_service,
    eligibility,
    message,
    targeting,
)
from ladini.services.availability_campaigns.eligibility import (
    MAX_OFFERS_PER_MESSAGE,
    EligibilityRules,
    OfferCandidate,
)
from ladini.services.database.common import escape_like, normalize_phone

logger = logging.getLogger("Ladini.Campaigns.Service")

TEMPLATE_KEY = "AVAILABILITY_CAMPAIGN_BUYER"
FREQUENCIES = ("ONCE", "WEEKLY", "CUSTOM_DAYS")
CANCELLABLE = ("DRAFT", "SCHEDULED", "RUNNING")
SERVICE_WINDOW_HOURS = 24
#: Un message resté SENDING au-delà est d'issue INCONNUE : jamais renvoyé automatiquement (pas de doublon).
STUCK_SENDING_MINUTES = 15
_CANDIDATE_SCAN_LIMIT = 500


class CampaignError(Exception):
    """Erreur métier présentable à l'opérateur (message en français)."""

    def __init__(self, message_: str, *, status: int = 400) -> None:
        super().__init__(message_)
        self.status = status


# ── Définition & validation (pures) ──────────────────────────────────────────────────────────────────────────────


def normalize_definition(data: Dict[str, Any]) -> Dict[str, Any]:
    name = str(data.get("name") or "").strip()
    if not name:
        raise CampaignError("Le nom de la campagne est obligatoire.")
    frequency = str(data.get("frequency") or "ONCE").upper()
    if frequency not in FREQUENCIES:
        raise CampaignError("Fréquence inconnue (ONCE, WEEKLY ou CUSTOM_DAYS).")
    interval_days = data.get("interval_days")
    if frequency == "CUSTOM_DAYS":
        if not isinstance(interval_days, int) or isinstance(interval_days, bool) or interval_days < 1:
            raise CampaignError("CUSTOM_DAYS exige interval_days entier >= 1.")
    elif frequency == "WEEKLY":
        interval_days = 7
    else:
        interval_days = None
    window = data.get("response_window_hours", 48)
    if not isinstance(window, int) or isinstance(window, bool) or window < 1 or window > 24 * 30:
        raise CampaignError("response_window_hours doit être un entier entre 1 et 720.")
    offer_filter = dict(data.get("offer_filter") or {})
    max_offers = offer_filter.get("max_offers", MAX_OFFERS_PER_MESSAGE)
    if not isinstance(max_offers, int) or isinstance(max_offers, bool) or not 1 <= max_offers <= MAX_OFFERS_PER_MESSAGE:
        raise CampaignError(f"max_offers doit être un entier entre 1 et {MAX_OFFERS_PER_MESSAGE}.")
    offer_filter["max_offers"] = max_offers
    for key in ("products", "categories", "regions"):
        vals = offer_filter.get(key)
        if vals is not None and (not isinstance(vals, list) or not all(isinstance(v, str) for v in vals)):
            raise CampaignError(f"offer_filter.{key} doit être une liste de textes.")
    audience = dict(data.get("audience") or {})
    if str(audience.get("audience_kind") or "ALL").upper() not in ("ALL", "KNOWN", "NEW"):
        raise CampaignError("audience.audience_kind : ALL, KNOWN ou NEW.")
    delivery_date = data.get("delivery_date")
    if isinstance(delivery_date, str):
        try:
            delivery_date = date.fromisoformat(delivery_date)
        except ValueError as exc:
            raise CampaignError("delivery_date : format AAAA-MM-JJ attendu.") from exc
    send_at = data.get("send_at")
    if isinstance(send_at, str):
        try:
            send_at = datetime.fromisoformat(send_at).replace(tzinfo=None)
        except ValueError as exc:
            raise CampaignError("send_at : date/heure ISO attendue.") from exc
    return {
        "name": name[:200], "audience": audience, "offer_filter": offer_filter, "send_at": send_at,
        "frequency": frequency, "interval_days": interval_days, "response_window_hours": window,
        "delivery_date": delivery_date,
    }


def next_run_after(frequency: str, interval_days: Optional[int], scheduled: datetime, now: datetime) -> Optional[datetime]:
    """Prochaine exécution : jamais de rattrapage des occurrences manquées (on saute à la prochaine date FUTURE)."""
    if frequency == "ONCE":
        return None
    step = timedelta(days=7 if frequency == "WEEKLY" else int(interval_days or 7))
    nxt = scheduled + step
    while nxt <= now:
        nxt += step
    return nxt


def make_run_key(scheduled: datetime) -> str:
    return scheduled.strftime("%Y%m%dT%H%M%S")


def _j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


# ── Offres ───────────────────────────────────────────────────────────────────────────────────────────────────────


async def load_candidates(session: AsyncSession, offer_filter: Dict[str, Any]) -> List[OfferCandidate]:
    stmt = select(Product, Producer).join(Producer, Producer.id == Product.producer_id)
    names = [n.strip() for n in offer_filter.get("products") or [] if n.strip()]
    cats = [n.strip() for n in offer_filter.get("categories") or [] if n.strip()]
    if names or cats:
        conds = [Product.name.ilike(f"%{escape_like(n)}%", escape="\\") for n in names]
        conds += [Product.category_label.ilike(f"%{escape_like(n)}%", escape="\\") for n in cats]
        stmt = stmt.where(or_(*conds))
    regions = [r for r in offer_filter.get("regions") or [] if r.strip()]
    if regions:
        stmt = stmt.where(or_(*[Producer.region.ilike(f"%{escape_like(r)}%", escape="\\") for r in regions]))
    stmt = stmt.order_by(Product.updated_at.desc(), Product.id).limit(_CANDIDATE_SCAN_LIMIT)
    out: List[OfferCandidate] = []
    for product, producer in (await session.execute(stmt)).all():
        view = product_pricing_view(product)
        # Prix affiché seulement s'il est fiable : certifié, ou libellé de paliers. Sinon « prix à confirmer ».
        reliable = view.reliability == PricingReliability.CERTIFIED or bool(view.tiers_label)
        out.append(
            OfferCandidate(
                product_id=str(product.id), producer_id=str(product.producer_id), name=str(product.name or ""),
                unit=str(product.unit or ""), quantity=float(product.quantity_for_sale or 0),
                updated_at=product.updated_at, is_available=bool(product.is_available),
                producer_status=producer.status, category_label=product.category_label, region=producer.region,
                zone_id=str(producer.zone_id) if producer.zone_id else None, harvest_date=product.harvest_date,
                quality_class=product.quality_class,
                price_label=view.pricing_label if reliable else None, price_status=view.status,
            )
        )
    return out


def rules_for(definition: Dict[str, Any]) -> EligibilityRules:
    flt = definition.get("offer_filter") or {}
    return EligibilityRules(
        max_age_hours=int(flt.get("max_age_hours") or settings.AVAILABILITY_OFFER_MAX_AGE_HOURS),
        require_price=bool(flt.get("require_price")),
        delivery_date=definition.get("delivery_date"),
    )


async def compute_preview(session: AsyncSession, definition: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    """Offres retenues + exclues (avec raisons) + message rendu + empreinte. Lecture seule."""
    candidates = await load_candidates(session, definition.get("offer_filter") or {})
    chosen, excluded = eligibility.select_offers(
        candidates, now, rules_for(definition), max_offers=int((definition.get("offer_filter") or {}).get("max_offers") or 5)
    )
    offers = [message.offer_snapshot(c) for c in chosen]
    body = (
        message.render_campaign_message(offers, delivery_date=definition.get("delivery_date"), seen_on=now.date())
        if offers else None
    )
    return {
        "offers": offers, "excluded": excluded, "message": body,
        "content_hash": message.content_hash(body, offers) if body else None,
        "computed_at": now.isoformat(),
    }


# ── Cycle de vie opérateur ───────────────────────────────────────────────────────────────────────────────────────


_COLS = (
    "id, name, status, audience, offer_filter, send_at, frequency, interval_days, response_window_hours, delivery_date, "
    "next_run_at, last_run_key, content_version, content_hash, preview, validated_at, validated_by_id, created_by_id, "
    "cancelled_at, last_error, created_at, updated_at"
)


async def get_campaign(session: AsyncSession, campaign_id: Any, *, lock: bool = False) -> Dict[str, Any]:
    row = (
        await session.execute(
            text(f"select {_COLS} from intelligence.availability_campaigns where id = :id {'for update' if lock else ''}"),
            {"id": str(campaign_id)},
        )
    ).mappings().first()
    if row is None:
        raise CampaignError("Campagne introuvable.", status=404)
    return dict(row)


async def create_campaign(session: AsyncSession, data: Dict[str, Any], *, actor_id: Any) -> Dict[str, Any]:
    d = normalize_definition(data)
    row = (
        await session.execute(
            text(
                "insert into intelligence.availability_campaigns "
                "(name, status, audience, offer_filter, send_at, frequency, interval_days, response_window_hours, "
                "delivery_date, created_by_id) values (:name, 'DRAFT', cast(:aud as jsonb), cast(:flt as jsonb), "
                ":send_at, :freq, :interval, :win, :dd, :actor) returning id"
            ),
            {"name": d["name"], "aud": _j(d["audience"]), "flt": _j(d["offer_filter"]), "send_at": d["send_at"],
             "freq": d["frequency"], "interval": d["interval_days"], "win": d["response_window_hours"],
             "dd": d["delivery_date"], "actor": str(actor_id) if actor_id else None},
        )
    ).first()
    assert row is not None
    return await get_campaign(session, row[0])


async def update_draft(session: AsyncSession, campaign_id: Any, data: Dict[str, Any], *, expected_version: int) -> Dict[str, Any]:
    cur = await get_campaign(session, campaign_id, lock=True)
    if cur["status"] != "DRAFT":
        raise CampaignError("Seule une campagne en brouillon peut être modifiée.", status=409)
    merged = {k: cur[k] for k in ("name", "audience", "offer_filter", "send_at", "frequency", "interval_days",
                                  "response_window_hours", "delivery_date")}
    merged.update({k: v for k, v in data.items() if k in merged})
    d = normalize_definition(merged)
    res = await session.execute(
        text(
            "update intelligence.availability_campaigns set name=:name, audience=cast(:aud as jsonb), "
            "offer_filter=cast(:flt as jsonb), send_at=:send_at, frequency=:freq, interval_days=:interval, "
            "response_window_hours=:win, delivery_date=:dd, content_version=content_version+1, updated_at=now() "
            "where id=:id and status='DRAFT' and content_version=:v returning id"
        ),
        {"name": d["name"], "aud": _j(d["audience"]), "flt": _j(d["offer_filter"]), "send_at": d["send_at"],
         "freq": d["frequency"], "interval": d["interval_days"], "win": d["response_window_hours"],
         "dd": d["delivery_date"], "id": str(campaign_id), "v": int(expected_version)},
    )
    if res.first() is None:
        raise CampaignError("La campagne a été modifiée entre-temps (version périmée).", status=409)
    return await get_campaign(session, campaign_id)


async def preview_campaign(session: AsyncSession, campaign_id: Any, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.utcnow()
    c = await get_campaign(session, campaign_id)
    live = await compute_preview(session, c, now)
    audience_count = await count_audience(session, c)
    return {"campaign_id": str(c["id"]), "status": c["status"], "content_version": c["content_version"],
            "validated_preview": c["preview"], **live, "audience": audience_count}


async def validate_campaign(
    session: AsyncSession, campaign_id: Any, *, actor_id: Any, expected_hash: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Fige offres + message + empreinte, puis programme. `expected_hash` = empreinte vue dans l'aperçu : si le
    contenu vivant a changé depuis, la validation est refusée (l'opérateur ne valide jamais autre chose que ce qu'il a vu)."""
    now = now or datetime.utcnow()
    c = await get_campaign(session, campaign_id, lock=True)
    if c["status"] != "DRAFT":
        raise CampaignError("Seule une campagne en brouillon peut être validée.", status=409)
    live = await compute_preview(session, c, now)
    if not live["offers"]:
        raise CampaignError("Aucune offre éligible : la campagne ne peut pas être validée.", status=422)
    if expected_hash is not None and expected_hash != live["content_hash"]:
        raise CampaignError("Le contenu a changé depuis l'aperçu : relisez-le puis validez à nouveau.", status=409)
    res = await session.execute(
        text(
            "update intelligence.availability_campaigns set status='SCHEDULED', preview=cast(:pv as jsonb), "
            "content_hash=:h, validated_at=:now, validated_by_id=:actor, next_run_at=coalesce(send_at, :now), "
            "updated_at=now() where id=:id and status='DRAFT' and content_version=:v returning id"
        ),
        {"pv": _j(live), "h": live["content_hash"], "now": now, "actor": str(actor_id) if actor_id else None,
         "id": str(campaign_id), "v": c["content_version"]},
    )
    if res.first() is None:
        raise CampaignError("La campagne a été modifiée entre-temps.", status=409)
    return await get_campaign(session, campaign_id)


async def cancel_campaign(session: AsyncSession, campaign_id: Any, *, actor_id: Any = None) -> Dict[str, Any]:
    """Annule : plus aucun lot ne démarre (le lot en cours finit sa transaction), les destinataires PREPARED sont
    écartés, et le dispatcher refuse d'envoyer les messages déjà en file (`campaign_cancelled`)."""
    res = await session.execute(
        text(
            "update intelligence.availability_campaigns set status='CANCELLED', cancelled_at=now(), updated_at=now(), "
            "next_run_at=null where id=:id and status = any(:ok) returning id"
        ),
        {"id": str(campaign_id), "ok": list(CANCELLABLE)},
    )
    if res.first() is None:
        c = await get_campaign(session, campaign_id)
        if c["status"] == "CANCELLED":
            return c  # idempotent
        raise CampaignError("Une campagne terminée ne peut plus être annulée.", status=409)
    await session.execute(
        text(
            "update intelligence.availability_campaign_recipients set status='SKIPPED', skip_reason='campaign_cancelled', "
            "updated_at=now() where campaign_id=:id and status='PREPARED'"
        ),
        {"id": str(campaign_id)},
    )
    return await get_campaign(session, campaign_id)


# ── Audience ─────────────────────────────────────────────────────────────────────────────────────────────────────


async def _audience_rows(session: AsyncSession) -> List[Dict[str, Any]]:
    rows = (
        await session.execute(
            text(
                "select c.phone, c.status as consent_status, u.id as user_id, u.account_status, u.whatsapp_enabled, "
                "u.deleted_at, u.declared_location from intelligence.communication_consents c "
                "left join auth.users u on u.phone = c.phone where c.topic = :t order by c.phone"
            ),
            {"t": consent_service.TOPIC},
        )
    ).mappings().all()
    return [dict(r) for r in rows]


async def count_audience(session: AsyncSession, definition: Dict[str, Any]) -> Dict[str, Any]:
    kept, skipped = targeting.select_recipients(await _audience_rows(session), definition.get("audience") or {})
    by_reason: Dict[str, int] = {}
    for s in skipped:
        by_reason[s["reason"]] = by_reason.get(s["reason"], 0) + 1
    return {"recipients": len(kept), "skipped": by_reason}


# ── Exécution ────────────────────────────────────────────────────────────────────────────────────────────────────


async def _window_open(session: AsyncSession, phone: str, now: datetime) -> bool:
    """Fenêtre de service WhatsApp (24 h) : proxy = dernier tour traité (`agri_workspaces.updated_at`, epoch)."""
    plain = phone.lstrip("+")
    row = (
        await session.execute(
            text("select max(updated_at) from agri_workspaces where workspace_id = any(:ids)"),
            {"ids": [phone, plain, "+" + plain]},
        )
    ).first()
    last = row[0] if row else None
    if last is None:
        return False
    return float(last) >= (now - timedelta(hours=SERVICE_WINDOW_HOURS)).timestamp()


async def start_run(session: AsyncSession, campaign_id: Any, now: datetime) -> Optional[Dict[str, Any]]:
    """Réclame la campagne (FOR UPDATE SKIP LOCKED). SCHEDULED dû -> prépare l'exécution ; RUNNING -> reprise.
    Retourne {run_key, resumed, prepared} ou None (rien à faire / déjà pris par un autre worker)."""
    row = (
        await session.execute(
            text(f"select {_COLS} from intelligence.availability_campaigns where id=:id for update skip locked"),
            {"id": str(campaign_id)},
        )
    ).mappings().first()
    if row is None:
        return None
    c = dict(row)
    if c["status"] == "RUNNING":
        return {"run_key": c["last_run_key"], "resumed": True, "prepared": 0, "campaign": c}
    if c["status"] != "SCHEDULED" or c["next_run_at"] is None or c["next_run_at"] > now:
        return None
    run_key = make_run_key(c["next_run_at"])
    live = await compute_preview(session, c, now)
    offers = live["offers"]
    if c["frequency"] == "ONCE" and c["preview"]:
        # UNE exécution = ce que l'opérateur a validé : jamais d'offre qu'il n'a pas vue, quantités/prix revérifiés.
        approved = {o["product_id"] for o in (c["preview"] or {}).get("offers", [])}
        offers = [o for o in offers if o["product_id"] in approved]
    if not offers:
        res = await session.execute(
            text(
                "update intelligence.availability_campaigns set status=case when frequency='ONCE' then 'FAILED' else 'SCHEDULED' end, "
                "last_run_key=:rk, last_error='no_eligible_offer', next_run_at=:nxt, updated_at=now() "
                "where id=:id and status='SCHEDULED' returning id"
            ),
            {"id": str(campaign_id), "rk": run_key,
             "nxt": next_run_after(c["frequency"], c["interval_days"], c["next_run_at"], now), },
        )
        res.first()
        return None
    kept, _ = targeting.select_recipients(await _audience_rows(session), c["audience"] or {})
    if kept:
        await session.execute(
            text(
                "insert into intelligence.availability_campaign_recipients "
                "(campaign_id, run_key, user_id, phone, status, offers, content_version) "
                "values (:c, :rk, :u, :p, 'PREPARED', cast(:o as jsonb), :v) "
                "on conflict (campaign_id, run_key, phone) do nothing"
            ),
            [{"c": str(campaign_id), "rk": run_key, "u": r.user_id, "p": r.phone, "o": _j(offers),
              "v": c["content_version"]} for r in kept],
        )
    await session.execute(
        text(
            "update intelligence.availability_campaigns set status='RUNNING', last_run_key=:rk, last_error=null, "
            "updated_at=now() where id=:id and status='SCHEDULED'"
        ),
        {"id": str(campaign_id), "rk": run_key},
    )
    return {"run_key": run_key, "resumed": False, "prepared": len(kept), "campaign": c}


async def queue_batch(session: AsyncSession, campaign_id: Any, run_key: str, *, batch_size: int, now: datetime) -> Dict[str, int]:
    """Un lot : PREPARED -> QUEUED (+ ligne d'outbox) ou SKIPPED, dans UNE transaction. `stop=1` si la campagne n'est plus RUNNING."""
    out = {"queued": 0, "skipped": 0, "remaining": 0, "stop": 0}
    status = (
        await session.execute(
            text("select status, delivery_date from intelligence.availability_campaigns where id=:id for share"),
            {"id": str(campaign_id)},
        )
    ).first()
    if status is None or status[0] != "RUNNING":
        out["stop"] = 1
        return out
    batch = (
        await session.execute(
            text(
                "select id, phone, user_id, offers from intelligence.availability_campaign_recipients "
                "where campaign_id=:c and run_key=:rk and status='PREPARED' order by phone limit :n "
                "for update skip locked"
            ),
            {"c": str(campaign_id), "rk": run_key, "n": int(batch_size)},
        )
    ).mappings().all()
    for r in batch:
        phone = r["phone"]
        reason = None
        consent = await consent_service.lock_status(session, phone)
        if consent != consent_service.OPTED_IN:
            reason = "opted_out" if consent == consent_service.OPTED_OUT else "no_consent"
        elif r["user_id"] is not None:
            ok = (
                await session.execute(
                    text(
                        "select 1 from auth.users where id=:u and coalesce(account_status,'ACTIVE')='ACTIVE' "
                        "and whatsapp_enabled is not false and deleted_at is null"
                    ),
                    {"u": str(r["user_id"])},
                )
            ).first()
            if ok is None:
                reason = "account_not_active"
        if reason:
            await session.execute(
                text("update intelligence.availability_campaign_recipients set status='SKIPPED', skip_reason=:s, "
                     "updated_at=now() where id=:id and status='PREPARED'"),
                {"s": reason, "id": str(r["id"])},
            )
            out["skipped"] += 1
            continue
        body = message.render_campaign_message(r["offers"], delivery_date=status[1], seen_on=now.date())
        dedupe = f"availability:{campaign_id}:{run_key}:{phone}"
        payload = {
            "body": body, "proactive": True, "campaign_id": str(campaign_id),
            "campaign_recipient_id": str(r["id"]), "run_key": run_key,
            "service_window_open": await _window_open(session, phone, now),
        }
        ins = (
            await session.execute(
                text(
                    "insert into intelligence.notification_outbox "
                    "(channel, recipient_user_id, recipient_phone, template_key, payload, dedupe_key) "
                    "values ('WHATSAPP', :u, :p, :t, cast(:pl as jsonb), :d) "
                    "on conflict (dedupe_key) do nothing returning id"
                ),
                {"u": str(r["user_id"]) if r["user_id"] else None, "p": phone, "t": TEMPLATE_KEY,
                 "pl": _j(payload), "d": dedupe},
            )
        ).first()
        if ins is None:  # déjà en file (reprise après incident) : on rattache, on ne réinsère pas
            ins = (
                await session.execute(
                    text("select id from intelligence.notification_outbox where dedupe_key=:d"), {"d": dedupe}
                )
            ).first()
        assert ins is not None
        await session.execute(
            text("update intelligence.availability_campaign_recipients set status='QUEUED', outbox_id=:o, queued_at=:now, "
                 "updated_at=now() where id=:id and status='PREPARED'"),
            {"o": str(ins[0]), "now": now, "id": str(r["id"])},
        )
        out["queued"] += 1
    out["remaining"] = int(
        (
            await session.execute(
                text("select count(*) from intelligence.availability_campaign_recipients "
                     "where campaign_id=:c and run_key=:rk and status='PREPARED'"),
                {"c": str(campaign_id), "rk": run_key},
            )
        ).scalar()
        or 0
    )
    return out


async def finish_run(session: AsyncSession, campaign_id: Any, run_key: str, now: datetime) -> str:
    """Clôt l'exécution quand plus rien n'est PREPARED. CAS sur (RUNNING, last_run_key) : un seul worker avance la date."""
    c = await get_campaign(session, campaign_id)
    scheduled = datetime.strptime(run_key, "%Y%m%dT%H%M%S")
    nxt = next_run_after(c["frequency"], c["interval_days"], scheduled, now)
    new_status = "COMPLETED" if nxt is None else "SCHEDULED"
    res = await session.execute(
        text(
            "update intelligence.availability_campaigns set status=:s, next_run_at=:nxt, updated_at=now() "
            "where id=:id and status='RUNNING' and last_run_key=:rk "
            "and not exists (select 1 from intelligence.availability_campaign_recipients r where r.campaign_id=:id "
            "and r.run_key=:rk and r.status='PREPARED') returning id"
        ),
        {"s": new_status, "nxt": nxt, "id": str(campaign_id), "rk": run_key},
    )
    return new_status if res.first() is not None else "UNCHANGED"


async def fail_stuck_sending(session: AsyncSession, now: datetime) -> int:
    """Message de campagne resté SENDING trop longtemps : l'issue de l'envoi est INCONNUE (le worker est mort entre
    l'appel fournisseur et l'écriture d'état). On NE renvoie JAMAIS : destinataire FAILED(send_outcome_unknown)."""
    cutoff = now - timedelta(minutes=STUCK_SENDING_MINUTES)
    rows = (
        await session.execute(
            text(
                "update intelligence.notification_outbox o set status='DEAD', last_error='send_outcome_unknown', updated_at=now() "
                "where o.status='SENDING' and o.template_key=:t and o.updated_at < :cut returning o.id"
            ),
            {"t": TEMPLATE_KEY, "cut": cutoff},
        )
    ).all()
    ids = [str(r[0]) for r in rows]
    if ids:
        await session.execute(
            text(
                "update intelligence.availability_campaign_recipients set status='FAILED', last_error='send_outcome_unknown', "
                "updated_at=now() where outbox_id = any(cast(:ids as uuid[])) and status='QUEUED'"
            ),
            {"ids": ids},
        )
    return len(ids)


# ── Statuts de livraison (monotones) ─────────────────────────────────────────────────────────────────────────────


async def record_delivery_status(
    session: AsyncSession, provider_ref: str, status: str, *, at: Optional[datetime] = None, error: Optional[str] = None
) -> bool:
    """Applique un statut fournisseur (`sent`/`delivered`/`read`/`failed`) à la ligne portant ce `provider_ref`.
    Transitions MONOTONES : jamais de recul, jamais d'écrasement d'un statut plus avancé ; un `read` sans `delivered`
    reçu avant renseigne aussi `delivered_at`. Retourne True si une ligne de campagne existe pour cette référence."""
    at = at or datetime.utcnow()
    st = str(status).lower()
    if st == "delivered":
        sql = ("update intelligence.availability_campaign_recipients set delivered_at=coalesce(delivered_at,:at), "
               "status=case when status in ('QUEUED','SENT') then 'DELIVERED' else status end, updated_at=now() "
               "where provider_ref=:r returning id")
    elif st == "read":
        sql = ("update intelligence.availability_campaign_recipients set read_at=coalesce(read_at,:at), "
               "delivered_at=coalesce(delivered_at,:at), "
               "status=case when status in ('QUEUED','SENT','DELIVERED') then 'READ' else status end, updated_at=now() "
               "where provider_ref=:r returning id")
    elif st == "failed":
        sql = ("update intelligence.availability_campaign_recipients set "
               "last_error=case when status in ('QUEUED','SENT') then :err else last_error end, "
               "status=case when status in ('QUEUED','SENT') then 'FAILED' else status end, updated_at=now() "
               "where provider_ref=:r returning id")
    else:
        return False
    res = await session.execute(text(sql), {"r": provider_ref, "at": at, "err": (error or "provider_failed")[:300]})
    return res.first() is not None


async def mark_replied(session: AsyncSession, phone: str, *, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """Rattache une réponse entrante à la DERNIÈRE campagne reçue par ce numéro, dans sa fenêtre de réponse.
    Idempotent. Retourne le contexte de campagne (ou None si aucune campagne récente)."""
    now = now or datetime.utcnow()
    norm = normalize_phone(phone, required=False)
    if not norm:
        return None
    row = (
        await session.execute(
            text(
                "select r.id, r.campaign_id, r.run_key, r.offers, r.sent_at, r.status, r.provider_ref "
                "from intelligence.availability_campaign_recipients r "
                "join intelligence.availability_campaigns c on c.id = r.campaign_id "
                "where r.phone=:p and r.sent_at is not null and r.status in ('SENT','DELIVERED','READ','REPLIED') "
                "and r.sent_at > cast(:now as timestamp) - make_interval(hours => c.response_window_hours) "
                "order by r.sent_at desc limit 1"
            ),
            {"p": norm, "now": now},
        )
    ).mappings().first()
    if row is None:
        return None
    await session.execute(
        text(
            "update intelligence.availability_campaign_recipients set replied_at=coalesce(replied_at,:now), "
            "status='REPLIED', updated_at=now() where id=:id and status in ('SENT','DELIVERED','READ','REPLIED')"
        ),
        {"id": str(row["id"]), "now": now},
    )
    return {"recipient_id": str(row["id"]), "campaign_id": str(row["campaign_id"]), "run_key": row["run_key"],
            "offers": row["offers"], "sent_at": row["sent_at"], "provider_ref": row["provider_ref"]}


# ── Tick Celery ──────────────────────────────────────────────────────────────────────────────────────────────────


async def run_due_campaigns(*, now: Optional[datetime] = None, batch_size: Optional[int] = None) -> Dict[str, Any]:
    """Un tick : récupère les campagnes dues (SCHEDULED échues ou RUNNING à reprendre) et les exécute lot par lot.
    Chaque transaction est courte ; une exception sur une campagne n'interrompt pas les autres."""
    from ladini.workers.runtime import worker_session

    now = now or datetime.utcnow()
    n = int(batch_size or settings.AVAILABILITY_CAMPAIGN_BATCH_SIZE)
    report: Dict[str, Any] = {"campaigns": [], "reaped": 0}
    async with worker_session() as s:
        report["reaped"] = await fail_stuck_sending(s, now)
        ids = [
            str(r[0])
            for r in (
                await s.execute(
                    text(
                        "select id from intelligence.availability_campaigns "
                        "where status='RUNNING' or (status='SCHEDULED' and next_run_at <= :now) order by next_run_at nulls first"
                    ),
                    {"now": now},
                )
            ).all()
        ]
    for cid in ids:
        item: Dict[str, Any] = {"campaign_id": cid, "queued": 0, "skipped": 0}
        try:
            async with worker_session() as s:
                started = await start_run(s, cid, now)
            if started is None:
                continue
            rk = started["run_key"]
            item.update(run_key=rk, resumed=started["resumed"], prepared=started["prepared"])
            while True:
                async with worker_session() as s:
                    b = await queue_batch(s, cid, rk, batch_size=n, now=now)
                item["queued"] += b["queued"]
                item["skipped"] += b["skipped"]
                if b["stop"] or b["remaining"] == 0:
                    break
                if b["queued"] + b["skipped"] == 0:
                    break  # le reste est verrouillé par un autre worker : il le terminera, sinon le prochain tick reprend
            if not b["stop"]:
                async with worker_session() as s:
                    item["final"] = await finish_run(s, cid, rk, now)
        except Exception as exc:  # noqa: BLE001 - isolation par campagne
            logger.exception("Campagne %s en échec", cid)
            item["error"] = type(exc).__name__
        report["campaigns"].append(item)
    try:  # reprise des intérêts dont la notification producteur n'a pas pu être mise en file
        from ladini.services.availability_campaigns import interest_service

        async with worker_session() as s:
            report["interests_notified"] = await interest_service.notify_open_interests(s)
    except Exception:  # noqa: BLE001
        logger.exception("Reprise des notifications d'intérêt en échec")
    return report


# ── Résultats & métriques ────────────────────────────────────────────────────────────────────────────────────────


async def campaign_metrics(session: AsyncSession, campaign_id: Any) -> Dict[str, Any]:
    """Compteurs et taux AVEC DÉNOMINATEURS EXPLICITES. Un intérêt n'est jamais une vente."""
    row = (
        await session.execute(
            text(
                "select count(*) as prepared, "
                "count(*) filter (where status='SKIPPED') as skipped, "
                "count(*) filter (where status='FAILED') as failed, "
                "count(*) filter (where sent_at is not null) as accepted, "
                "count(*) filter (where delivered_at is not null) as delivered, "
                "count(*) filter (where read_at is not null) as read, "
                "count(*) filter (where replied_at is not null) as replied "
                "from intelligence.availability_campaign_recipients where campaign_id=:c"
            ),
            {"c": str(campaign_id)},
        )
    ).mappings().one()
    m = {k: int(v or 0) for k, v in dict(row).items()}
    interests = (
        await session.execute(
            text("select kind, count(*) from intelligence.availability_interests where campaign_id=:c group by kind"),
            {"c": str(campaign_id)},
        )
    ).all()
    optouts = (
        await session.execute(
            text(
                "select count(distinct r.phone) from intelligence.availability_campaign_recipients r "
                "join intelligence.communication_consents k on k.phone=r.phone and k.topic=:t "
                "where r.campaign_id=:c and k.status='OPTED_OUT' and r.sent_at is not null and k.revoked_at >= r.sent_at"
            ),
            {"c": str(campaign_id), "t": consent_service.TOPIC},
        )
    ).scalar()

    def rate(num: int, den: int) -> Optional[float]:
        return round(num / den, 4) if den else None

    delivery_measurable = m["delivered"] > 0
    reply_den = m["delivered"] if delivery_measurable else m["accepted"]
    return {
        **m,
        "interests": {k: int(v) for k, v in interests},
        "opt_outs_after_send": int(optouts or 0),
        "rates": {
            "delivery_rate": {"value": rate(m["delivered"], m["accepted"]), "numerator": "delivered", "denominator": "accepted"},
            "reply_rate": {
                "value": rate(m["replied"], reply_den), "numerator": "replied",
                "denominator": "delivered" if delivery_measurable else "accepted (livraison non mesurable)",
            },
        },
        "notes": "Un intérêt n'est pas une vente. Statuts livré/lu = ceux que le fournisseur remonte.",
    }
