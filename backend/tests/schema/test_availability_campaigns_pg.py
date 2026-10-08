"""Campagnes de disponibilités contre un VRAI PostgreSQL migré depuis les fichiers officiels (migration 0016).

Invariants testés : AUCUN envoi dupliqué (exécution répétée, deux workers concurrents, reprise après arrêt, annulation
entre deux lots), la désinscription gagne toujours (y compris en concurrence), statuts de livraison monotones, un
intérêt n'est jamais une commande et son rejeu ne duplique ni l'intérêt ni la notification producteur.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta

import psycopg2
import pytest
from factories import Graph, insert, uniq
from psycopg2.extras import Json
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ladini.services.availability_campaigns import campaign_service as cs
from ladini.services.availability_campaigns import (
    compliance_gate,
    consent_service,
    interest_service,
)
from ladini.workers.outbox import campaign_hooks
from ladini.workers.outbox.channels.base import SendResult

TEMPLATE = cs.TEMPLATE_KEY


# ── Harnais ─────────────────────────────────────────────────────────────────────────────────────────────────────────


def _phone() -> str:
    return "+22671" + f"{uuid.uuid4().int % 10**6:06d}"


class Env:
    """Seed psycopg2 + exécution asynchrone contre le DSN de test (sessions de worker redirigées sur ce DSN)."""

    def __init__(self, dsn: str, monkeypatch) -> None:
        self.dsn = dsn
        self.mp = monkeypatch
        self.conn = psycopg2.connect(dsn)
        self.conn.autocommit = True
        self.cur = self.conn.cursor()
        self.cur.execute(
            "delete from intelligence.availability_interests; delete from intelligence.availability_campaign_recipients; "
            "delete from intelligence.availability_campaigns; delete from intelligence.communication_consents; "
            "delete from intelligence.notification_outbox; delete from agri_workspaces; "
            # la base de test est partagée par toute la session : les produits des autres tests ne doivent pas être « vus »
            "update marketplace.products set is_available = false, quantity_for_sale = 0"
        )
        self.g = Graph(self.cur)
        self.admin = insert(self.cur, "auth.users", phone=uniq("+226"), role="ADMIN")
        self.cur.execute("update marketplace.producers set status='APPROVED' where id=%s", (self.g.producer,))
        self.product(name="Tomate", quantity_for_sale=500, unit="KG", _first=True)

    # -- seed
    def product(self, *, name, quantity_for_sale=100, unit="KG", hours_old=2, _first=False, available=True):
        if _first:
            self.cur.execute(
                "update marketplace.products set name=%s, quantity_for_sale=%s, unit=%s, is_available=true, updated_at=%s where id=%s",
                (name, quantity_for_sale, unit, datetime.utcnow() - timedelta(hours=hours_old), self.g.product),
            )
            return self.g.product
        pid = insert(
            self.cur, "marketplace.products", name=name, category_label="Légumes", price=100, unit=unit,
            quantity_for_sale=quantity_for_sale, is_available=available, producer_id=self.g.producer,
            sub_category_id=self.g.sub_category, updated_at=datetime.utcnow() - timedelta(hours=hours_old),
        )
        return pid

    def buyer(self, *, consent="OPTED_IN", with_user=False, location=None) -> str:
        phone = _phone()
        uid = None
        if with_user:
            uid = insert(self.cur, "auth.users", phone=phone, name="Acheteur Test", declared_location=location)
        if consent:
            self.cur.execute(
                "insert into intelligence.communication_consents (phone, user_id, status, source, proof, consented_at) "
                "values (%s, %s, %s, 'ADMIN_IMPORT', %s, now())", (phone, uid, consent, Json({"text_version": 1})),
            )
        return phone

    def q(self, sql, *args):
        self.cur.execute(sql, args)
        return self.cur.fetchall()

    def one(self, sql, *args):
        return self.q(sql, *args)[0][0]

    # -- async
    def run(self, scenario):
        async def go():
            engine = create_async_engine(self.dsn.replace("postgresql://", "postgresql+asyncpg://", 1), pool_size=10)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            self.mp.setattr("ladini.workers.runtime.get_sessionmaker", lambda: factory)
            self.mp.setattr("ladini.core.database.get_sessionmaker", lambda: factory)
            try:
                return await scenario(factory)
            finally:
                await engine.dispose()

        return asyncio.run(go())

    def campaign(self, *, frequency="ONCE", products=("tomate",), **over) -> str:
        """Crée + valide une campagne (via le service) ; retourne son id."""
        async def sc(factory):
            async with factory() as s:
                data = {"name": "Camp", "frequency": frequency, "actor_id": str(self.admin),
                        "offer_filter": {"products": list(products)}, "send_at": datetime.utcnow() - timedelta(minutes=1)}
                data.update(over)
                c = await cs.create_campaign(s, data, actor_id=self.admin)
                await cs.validate_campaign(s, c["id"], actor_id=self.admin)
                await s.commit()
                return str(c["id"])

        return self.run(sc)

    def close(self):
        self.cur.close()
        self.conn.close()


@pytest.fixture
def env(pg_dsn, monkeypatch):
    e = Env(pg_dsn, monkeypatch)
    try:
        yield e
    finally:
        e.close()


def tick(env, batch=50, now=None):
    return env.run(lambda f: cs.run_due_campaigns(batch_size=batch, now=now))


# ── Schéma ──────────────────────────────────────────────────────────────────────────────────────────────────────────


def test_recipient_uniqueness_is_enforced_by_the_database(env):
    cid = env.campaign()
    env.buyer()
    tick(env)
    row = env.q("select campaign_id, run_key, phone from intelligence.availability_campaign_recipients limit 1")[0]
    with pytest.raises(psycopg2.errors.UniqueViolation):
        env.cur.execute(
            "insert into intelligence.availability_campaign_recipients (campaign_id, run_key, phone) values (%s,%s,%s)", row
        )
    assert cid == str(row[0])


# ── Exécution : aucun doublon ───────────────────────────────────────────────────────────────────────────────────────


def test_a_campaign_reaches_each_consented_buyer_exactly_once_even_if_ticked_again(env):
    ok = [env.buyer() for _ in range(3)]
    env.buyer(consent="OPTED_OUT")
    env.buyer(consent=None)  # a déjà écrit à LADINI mais n'a JAMAIS consenti
    cid = env.campaign()
    r1 = tick(env)
    assert r1["campaigns"][0]["queued"] == 3
    assert env.one("select count(*) from intelligence.notification_outbox where template_key=%s", TEMPLATE) == 3
    assert {p for (p,) in env.q("select recipient_phone from intelligence.notification_outbox")} == set(ok)
    assert env.one("select status from intelligence.availability_campaigns where id=%s", cid) == "COMPLETED"
    for _ in range(3):
        tick(env)  # exécution répétée : rien de plus
    assert env.one("select count(*) from intelligence.notification_outbox where template_key=%s", TEMPLATE) == 3
    assert env.one("select count(*) from intelligence.availability_campaign_recipients") == 3
    body = env.one("select payload->>'body' from intelligence.notification_outbox limit 1")
    assert "Tomate — 500 kg" in body and "STOP" in body


def test_two_workers_running_the_same_campaign_never_duplicate_a_recipient(env):
    phones = [env.buyer() for _ in range(40)]
    env.campaign()

    async def both(factory):
        return await asyncio.gather(
            cs.run_due_campaigns(batch_size=5), cs.run_due_campaigns(batch_size=5), cs.run_due_campaigns(batch_size=5)
        )

    env.run(both)
    tick(env, batch=5)  # un éventuel reliquat est repris au tick suivant
    assert env.one("select count(*) from intelligence.notification_outbox where template_key=%s", TEMPLATE) == len(phones)
    assert env.one("select count(distinct recipient_phone) from intelligence.notification_outbox") == len(phones)
    assert env.one("select count(*) from intelligence.availability_campaign_recipients where status='QUEUED'") == len(phones)
    assert env.one("select status from intelligence.availability_campaigns") == "COMPLETED"


def test_an_interrupted_run_resumes_without_resending_what_was_already_queued(env):
    phones = [env.buyer() for _ in range(7)]
    cid = env.campaign()

    async def crash_after_one_batch(factory):
        now = datetime.utcnow()
        async with factory() as s:
            started = await cs.start_run(s, cid, now)
            await s.commit()
        async with factory() as s:
            await cs.queue_batch(s, cid, started["run_key"], batch_size=3, now=now)
            await s.commit()
        return started["run_key"]  # « le worker s'arrête ici »

    run_key = env.run(crash_after_one_batch)
    assert env.one("select status from intelligence.availability_campaigns") == "RUNNING"
    assert env.one("select count(*) from intelligence.availability_campaign_recipients where status='QUEUED'") == 3
    assert env.one("select count(*) from intelligence.availability_campaign_recipients where status='PREPARED'") == 4
    r = tick(env, batch=3)
    assert r["campaigns"][0]["resumed"] is True and r["campaigns"][0]["queued"] == 4
    assert env.one("select count(*) from intelligence.notification_outbox where template_key=%s", TEMPLATE) == len(phones)
    assert env.one("select last_run_key from intelligence.availability_campaigns") == run_key
    assert env.one("select status from intelligence.availability_campaigns") == "COMPLETED"


def test_a_crash_between_the_outbox_insert_and_the_state_update_cannot_duplicate(env):
    """Ligne d'outbox déjà présente (dedupe_key) mais destinataire encore PREPARED : on rattache, on ne réinsère pas."""
    phone = env.buyer()
    cid = env.campaign()

    async def scenario(factory):
        now = datetime.utcnow()
        async with factory() as s:
            started = await cs.start_run(s, cid, now)
            await s.commit()
        key = f"availability:{cid}:{started['run_key']}:{phone}"
        async with factory() as s:
            await s.execute(
                text("insert into intelligence.notification_outbox (channel, recipient_phone, template_key, payload, dedupe_key) "
                     "values ('WHATSAPP', :p, :t, cast('{\"body\":\"x\"}' as jsonb), :d)"),
                {"p": phone, "t": TEMPLATE, "d": key},
            )
            await s.commit()
        async with factory() as s:
            out = await cs.queue_batch(s, cid, started["run_key"], batch_size=10, now=now)
            await s.commit()
        return out

    out = env.run(scenario)
    assert out["queued"] == 1
    assert env.one("select count(*) from intelligence.notification_outbox where template_key=%s", TEMPLATE) == 1
    assert env.one("select count(*) from intelligence.availability_campaign_recipients where outbox_id is not null") == 1


def test_cancelling_between_batches_stops_the_run_and_blocks_what_is_already_queued(env):
    [env.buyer() for _ in range(6)]
    cid = env.campaign()

    async def scenario(factory):
        now = datetime.utcnow()
        async with factory() as s:
            started = await cs.start_run(s, cid, now)
            await s.commit()
        async with factory() as s:
            await cs.queue_batch(s, cid, started["run_key"], batch_size=2, now=now)
            await s.commit()
        async with factory() as s:
            await cs.cancel_campaign(s, cid)
            await s.commit()
        async with factory() as s:
            after = await cs.queue_batch(s, cid, started["run_key"], batch_size=10, now=now)
            await s.commit()
        return after

    after = env.run(scenario)
    assert after["stop"] == 1 and after["queued"] == 0
    assert env.one("select count(*) from intelligence.notification_outbox where template_key=%s", TEMPLATE) == 2
    assert env.one("select count(*) from intelligence.availability_campaign_recipients where skip_reason='campaign_cancelled'") == 4
    # les 2 déjà en file sont refusés au moment d'envoyer
    job = _job(env)
    assert env.run(lambda f: campaign_hooks.before_send(job)) == "campaign_cancelled"
    assert env.one("select status from intelligence.availability_campaigns") == "CANCELLED"
    assert env.run(lambda f: _cancel_again(f, cid))["status"] == "CANCELLED"  # idempotent


async def _cancel_again(factory, cid):
    async with factory() as s:
        r = await cs.cancel_campaign(s, cid)
        await s.commit()
        return r


def _job(env, idx=0):
    row = env.q("select o.id, o.payload, r.id from intelligence.notification_outbox o "
                "join intelligence.availability_campaign_recipients r on r.outbox_id = o.id order by o.recipient_phone offset %s limit 1", idx)[0]
    env.cur.execute("update intelligence.notification_outbox set status='SENDING' where id=%s", (row[0],))
    return {"id": row[0], "payload": dict(row[1]), "channel": "WHATSAPP"}


def test_a_campaign_cannot_be_cancelled_once_completed(env):
    env.buyer()
    cid = env.campaign()
    tick(env)
    with pytest.raises(cs.CampaignError):
        env.run(lambda f: _cancel_again(f, cid))


# ── Désinscription ──────────────────────────────────────────────────────────────────────────────────────────────────


def test_opt_out_before_the_batch_skips_the_recipient(env):
    stay, leave = env.buyer(), env.buyer()
    env.campaign()

    async def scenario(factory):
        now = datetime.utcnow()
        async with factory() as s:
            started = await cs.start_run(s, env_cid[0], now)
            await s.commit()
        async with factory() as s:  # désinscription APRÈS la préparation, AVANT l'envoi
            await consent_service.opt_out(s, leave, source="CONVERSATION_STOP", proof={"text_version": 1})
            await s.commit()
        async with factory() as s:
            await cs.queue_batch(s, env_cid[0], started["run_key"], batch_size=10, now=now)
            await s.commit()

    env_cid = [env.one("select id from intelligence.availability_campaigns")]
    env.run(scenario)
    assert env.one("select skip_reason from intelligence.availability_campaign_recipients where phone=%s", leave) == "opted_out"
    assert env.one("select status from intelligence.availability_campaign_recipients where phone=%s", stay) == "QUEUED"
    assert {p for (p,) in env.q("select recipient_phone from intelligence.notification_outbox")} == {stay}


def test_opt_out_after_queueing_still_wins_at_send_time(env):
    phone = env.buyer()
    env.campaign()
    tick(env)
    job = _job(env)
    assert env.run(lambda f: campaign_hooks.before_send(job)) is None  # inscrit : on enverrait

    async def stop(factory):
        async with factory() as s:
            await consent_service.opt_out(s, phone, source="CONVERSATION_STOP", proof={"text_version": 1})
            await s.commit()

    env.run(stop)
    assert env.run(lambda f: campaign_hooks.before_send(job)) == "opted_out"
    env.run(lambda f: campaign_hooks.skip(job, "opted_out"))
    assert env.one("select status from intelligence.availability_campaign_recipients") == "SKIPPED"
    assert env.one("select status from intelligence.notification_outbox") == "SKIPPED"


def test_concurrent_opt_out_waits_for_the_batch_transaction_then_wins(env):
    """`FOR SHARE` : la désinscription concurrente est sérialisée avec la transaction qui met le message en file."""
    phone = env.buyer()

    async def scenario(factory):
        s1, s2 = factory(), factory()
        try:
            assert await consent_service.lock_status(s1, phone) == "OPTED_IN"  # lot en cours (transaction ouverte)
            stop = asyncio.ensure_future(
                consent_service.opt_out(s2, phone, source="CONVERSATION_STOP", proof={"text_version": 1})
            )
            await asyncio.sleep(0.6)
            assert not stop.done(), "la désinscription aurait dû attendre la transaction du lot"
            await s1.commit()  # le lot finit (message mis en file)
            assert await asyncio.wait_for(stop, 5) is True
            await s2.commit()
        finally:
            await s1.close()
            await s2.close()

    env.run(scenario)
    assert env.one("select status from intelligence.communication_consents where phone=%s", phone) == "OPTED_OUT"


def test_consent_rules(env):
    phone = _phone()

    async def scenario(factory):
        async with factory() as s:
            proof = consent_service.build_proof(source="ADMIN_IMPORT", evidence="formulaire papier")
            assert await consent_service.opt_in(s, phone, source="ADMIN_IMPORT", proof=proof) is True
            assert await consent_service.opt_out(s, phone, source="CONVERSATION_STOP", proof={"text_version": 1}) is True
            assert await consent_service.opt_out(s, phone, source="CONVERSATION_STOP", proof={"text_version": 1}) is False  # idempotent
            # un import administrateur ne renverse JAMAIS une désinscription ...
            assert await consent_service.opt_in(s, phone, source="ADMIN_IMPORT", proof=proof) is False
            assert await consent_service.get_status(s, phone) == "OPTED_OUT"
            # ... seule la personne elle-même peut redemander
            assert await consent_service.opt_in(s, phone, source="CONVERSATION_EXPLICIT", proof=proof, override_opt_out=True)
            assert await consent_service.get_status(s, phone) == "OPTED_IN"
            with pytest.raises(ValueError):
                await consent_service.opt_in(s, phone, source="ADMIN_IMPORT", proof={})
            await s.commit()

    env.run(scenario)
    proof = env.one("select proof from intelligence.communication_consents where phone=%s", phone)
    assert proof["text_version"] == 1 and "message_ref_hash" not in proof or len(proof["message_ref_hash"]) == 16


# ── Garde de conformité (entrée) ────────────────────────────────────────────────────────────────────────────────────


def test_inbound_stop_opts_out_without_the_graph_and_is_replay_safe(env):
    phone = env.buyer()

    async def scenario(factory):
        a = await compliance_gate.handle_inbound(phone, "STOP", message_sid="wamid.1")
        b = await compliance_gate.handle_inbound(phone, "STOP", message_sid="wamid.1")  # retry Celery du même message
        c = await compliance_gate.handle_inbound(phone, "arrête la commande", message_sid="wamid.2")
        return a, b, c

    a, b, c = env.run(scenario)
    assert a["compliance"] == "OPT_OUT" and b["compliance"] == "OPT_OUT" and c is None
    assert env.one("select status from intelligence.communication_consents where phone=%s", phone) == "OPTED_OUT"
    assert env.one("select version from intelligence.communication_consents where phone=%s", phone) == 2  # le rejeu n'a rien changé


def test_yes_is_a_consent_only_after_the_consent_question(env):
    phone = _phone()

    async def scenario(factory):
        before = await compliance_gate.handle_inbound(phone, "oui", message_sid="wamid.a")
        env.cur.execute(
            "insert into intelligence.notification_outbox (channel, recipient_phone, template_key, payload, dedupe_key, status, sent_at) "
            "values ('WHATSAPP', %s, 'CONSENT_REQUEST_BUYER', %s, %s, 'SENT', now() at time zone 'UTC')",
            (phone, Json({"body": "q"}), uniq("consent")),
        )
        after = await compliance_gate.handle_inbound(phone, "oui", message_sid="wamid.b")
        return before, after

    before, after = env.run(scenario)
    assert before is None and after["compliance"] == "OPT_IN"
    row = env.q("select status, source from intelligence.communication_consents where phone=%s", phone)[0]
    assert row == ("OPTED_IN", "CONSENT_QUESTION_YES")


def test_having_written_to_us_is_not_a_subscription(env):
    phone = _phone()
    env.run(lambda f: compliance_gate.handle_inbound(phone, "100 kg de tomate", message_sid="wamid.z"))
    assert env.q("select 1 from intelligence.communication_consents where phone=%s", phone) == []


# ── Éligibilité à l'exécution ───────────────────────────────────────────────────────────────────────────────────────


def test_offers_are_revalidated_when_the_run_starts(env):
    env.buyer()
    env.campaign()
    env.cur.execute("update marketplace.products set updated_at = now() - interval '300 hours' where id=%s", (env.g.product,))
    r = tick(env)
    assert r["campaigns"] == []  # rien n'est parti
    assert env.one("select status from intelligence.availability_campaigns") == "FAILED"
    assert env.one("select last_error from intelligence.availability_campaigns") == "no_eligible_offer"
    assert env.one("select count(*) from intelligence.notification_outbox") == 0


def test_a_once_campaign_only_sends_offers_the_operator_validated(env):
    env.buyer()
    env.campaign(products=("tomate", "oignon"))
    env.product(name="Oignon", quantity_for_sale=80)  # apparu APRÈS la validation
    tick(env)
    body = env.one("select payload->>'body' from intelligence.notification_outbox")
    assert "Tomate" in body and "Oignon" not in body


def test_validation_refuses_a_campaign_without_eligible_offer_and_a_stale_preview(env):
    async def scenario(factory):
        async with factory() as s:
            c = await cs.create_campaign(s, {"name": "Vide", "offer_filter": {"products": ["inexistant"]}}, actor_id=env.admin)
            with pytest.raises(cs.CampaignError):
                await cs.validate_campaign(s, c["id"], actor_id=env.admin)
            c2 = await cs.create_campaign(s, {"name": "Ok", "offer_filter": {"products": ["tomate"]}}, actor_id=env.admin)
            with pytest.raises(cs.CampaignError) as e:
                await cs.validate_campaign(s, c2["id"], actor_id=env.admin, expected_hash="pas-le-bon")
            assert e.value.status == 409
            pv = await cs.preview_campaign(s, c2["id"])
            assert pv["offers"] and pv["audience"]["recipients"] == 0
            ok = await cs.validate_campaign(s, c2["id"], actor_id=env.admin, expected_hash=pv["content_hash"])
            assert ok["status"] == "SCHEDULED" and ok["preview"]["offers"]
            with pytest.raises(cs.CampaignError):  # plus modifiable ni re-validable
                await cs.update_draft(s, c2["id"], {"name": "x"}, expected_version=1)
            await s.commit()

    env.run(scenario)


def test_draft_update_uses_optimistic_versioning(env):
    async def scenario(factory):
        async with factory() as s:
            c = await cs.create_campaign(s, {"name": "A"}, actor_id=env.admin)
            u = await cs.update_draft(s, c["id"], {"name": "B"}, expected_version=1)
            assert u["name"] == "B" and u["content_version"] == 2
            with pytest.raises(cs.CampaignError):
                await cs.update_draft(s, c["id"], {"name": "C"}, expected_version=1)  # version périmée
            await s.commit()

    env.run(scenario)


def test_weekly_campaign_reschedules_and_is_not_resent_before_its_next_date(env):
    env.buyer()
    cid = env.campaign(frequency="WEEKLY")
    tick(env)
    row = env.q("select status, next_run_at from intelligence.availability_campaigns where id=%s", cid)[0]
    assert row[0] == "SCHEDULED" and row[1] > datetime.utcnow() + timedelta(days=6)
    tick(env)
    tick(env)
    assert env.one("select count(*) from intelligence.notification_outbox where template_key=%s", TEMPLATE) == 1
    # la semaine suivante : nouvelle exécution = nouveau run_key = nouveau message (et pas de doublon dans l'ancien)
    env.cur.execute("update intelligence.availability_campaigns set next_run_at = now() at time zone 'utc' - interval '2 hours'")
    tick(env)
    assert env.one("select count(*) from intelligence.notification_outbox where template_key=%s", TEMPLATE) == 2
    assert env.one("select count(distinct run_key) from intelligence.availability_campaign_recipients") == 2


# ── Envoi : hooks du dispatcher ─────────────────────────────────────────────────────────────────────────────────────


def test_send_hooks_record_the_provider_reference_and_refuse_a_second_send(env):
    env.buyer()
    env.campaign()
    tick(env)
    job = _job(env)

    async def scenario(factory):
        assert await campaign_hooks.before_send(job) is None
        async with factory() as s:
            await campaign_hooks.after_send(s, job, SendResult.success(provider_ref="wamid.OK1"))
            await s.commit()
        return await campaign_hooks.before_send(job)

    assert env.run(scenario) == "already_processed"  # même si l'outbox était rejouée, le destinataire n'est plus QUEUED
    row = env.q("select status, provider_ref, sent_at from intelligence.availability_campaign_recipients")[0]
    assert row[0] == "SENT" and row[1] == "wamid.OK1" and row[2] is not None


def test_a_temporary_failure_keeps_the_recipient_queued_until_the_outbox_gives_up(env):
    env.buyer()
    env.campaign()
    tick(env)
    job = _job(env)

    async def scenario(factory):
        env.cur.execute("update intelligence.notification_outbox set status='PENDING'")
        async with factory() as s:
            await campaign_hooks.after_send(s, job, SendResult.failure("timeout"))
            await s.commit()
        mid = env.one("select status from intelligence.availability_campaign_recipients")
        env.cur.execute("update intelligence.notification_outbox set status='DEAD'")
        async with factory() as s:
            await campaign_hooks.after_send(s, job, SendResult.failure("timeout"))
            await s.commit()
        return mid

    assert env.run(scenario) == "QUEUED"
    assert env.one("select status from intelligence.availability_campaign_recipients") == "FAILED"


def test_a_send_with_unknown_outcome_is_never_retried_automatically(env):
    env.buyer()
    env.campaign()
    tick(env)
    env.cur.execute("update intelligence.notification_outbox set status='SENDING', updated_at = now() - interval '2 hours'")
    n = env.run(lambda f: _reap(f))
    assert n == 1
    assert env.one("select status from intelligence.notification_outbox") == "DEAD"
    row = env.q("select status, last_error from intelligence.availability_campaign_recipients")[0]
    assert row == ("FAILED", "send_outcome_unknown")


async def _reap(factory):
    async with factory() as s:
        n = await cs.fail_stuck_sending(s, datetime.utcnow())
        await s.commit()
        return n


def test_service_window_is_computed_from_the_last_processed_turn(env):
    p_open, p_closed, p_never = _phone(), _phone(), _phone()
    now = datetime.utcnow()
    env.cur.execute("insert into agri_workspaces (workspace_id, updated_at) values (%s, %s)", (p_open, (now - timedelta(hours=2)).timestamp()))
    env.cur.execute("insert into agri_workspaces (workspace_id, updated_at) values (%s, %s)", (p_closed, (now - timedelta(hours=30)).timestamp()))

    async def scenario(factory):
        async with factory() as s:
            return [await cs._window_open(s, p, now) for p in (p_open, p_closed, p_never)]

    assert env.run(scenario) == [True, False, False]


# ── Statuts de livraison ────────────────────────────────────────────────────────────────────────────────────────────


def _sent_recipient(env, ref="wamid.S1"):
    env.buyer()
    env.campaign()
    tick(env)
    env.cur.execute("update intelligence.availability_campaign_recipients set status='SENT', provider_ref=%s, sent_at=now()", (ref,))


def _apply(env, ref, status, error=None):
    async def sc(factory):
        async with factory() as s:
            r = await cs.record_delivery_status(s, ref, status, error=error)
            await s.commit()
            return r

    return env.run(sc)


def test_delivery_statuses_are_monotonic_and_order_independent(env):
    _sent_recipient(env)
    assert _apply(env, "wamid.S1", "delivered")
    assert env.one("select status from intelligence.availability_campaign_recipients") == "DELIVERED"
    assert _apply(env, "wamid.S1", "read")
    assert _apply(env, "wamid.S1", "delivered")  # arrive en retard : aucun recul
    assert _apply(env, "wamid.S1", "failed", "131047")  # un échec tardif n'écrase pas « lu »
    row = env.q("select status, delivered_at is not null, read_at is not null, last_error from intelligence.availability_campaign_recipients")[0]
    assert row == ("READ", True, True, None)
    assert _apply(env, "inconnu", "delivered") is False


def test_read_received_before_delivered_fills_both_timestamps(env):
    _sent_recipient(env)
    _apply(env, "wamid.S1", "read")
    row = env.q("select status, delivered_at is not null, read_at is not null from intelligence.availability_campaign_recipients")[0]
    assert row == ("READ", True, True)


def test_provider_failure_marks_a_sent_message_failed_with_the_reason(env):
    _sent_recipient(env)
    _apply(env, "wamid.S1", "failed", "131047")
    row = env.q("select status, last_error from intelligence.availability_campaign_recipients")[0]
    assert row == ("FAILED", "131047")


# ── Réponses, intérêts, producteur ──────────────────────────────────────────────────────────────────────────────────


def _delivered_campaign(env, *, buyer_location="Bobo-Dioulasso"):
    phone = env.buyer(with_user=True, location=buyer_location)
    env.campaign(products=("tomate", "oignon")) if False else env.campaign()
    tick(env)
    env.cur.execute("update intelligence.availability_campaign_recipients set status='DELIVERED', provider_ref='wamid.C1', "
                    "sent_at=now() at time zone 'utc' - interval '3 hours', delivered_at=now() at time zone 'utc'")
    return phone


def _interest(env, phone, product="tomate", qty=100, ref="wamid.in1"):
    async def sc(factory):
        async with factory() as s:
            r = await interest_service.record_interest(s, phone, product_label=product, quantity=qty, unit="kg", message_ref=ref)
            await s.commit()
            return r

    return env.run(sc)


def _orders(env):
    return env.one("select count(*) from marketplace.orders")


def test_a_natural_reply_is_attributed_and_never_creates_an_order(env):
    phone = _delivered_campaign(env)
    orders_before, items_before = _orders(env), env.one("select count(*) from marketplace.order_items")
    r = _interest(env, phone, "des tomates", 100)
    assert r["attributed"] and r["created"] and r["changes"] == {} and r["note"] is None
    assert env.one("select status from intelligence.availability_campaign_recipients") == "REPLIED"
    assert env.one("select count(*) from intelligence.availability_interests where kind='INTEREST'") == 1
    assert _orders(env) == orders_before and env.one("select count(*) from marketplace.order_items") == items_before
    note = env.one("select payload->>'product' from intelligence.notification_outbox where template_key='AVAILABILITY_INTEREST_PRODUCER'")
    assert note.lower() == "des tomates"
    assert env.one("select status from intelligence.availability_interests") == "PRODUCER_NOTIFIED"


def test_replaying_the_same_inbound_message_duplicates_nothing(env):
    phone = _delivered_campaign(env)
    first = _interest(env, phone, "tomate", 100, ref="wamid.same")
    again = _interest(env, phone, "tomate", 100, ref="wamid.same")
    assert first["created"] and not again["created"] and first["interest_id"] == again["interest_id"]
    assert env.one("select count(*) from intelligence.availability_interests") == 1
    assert env.one("select count(*) from intelligence.notification_outbox where template_key='AVAILABILITY_INTEREST_PRODUCER'") == 1
    other = _interest(env, phone, "tomate", 100, ref="wamid.other")  # un AUTRE message = un autre intérêt, mais pas de 2e notification
    assert other["created"]


def test_a_late_reply_is_revalidated_against_the_live_offer(env):
    phone = _delivered_campaign(env)
    env.cur.execute("update marketplace.products set quantity_for_sale = 120 where id=%s", (env.g.product,))
    r = _interest(env, phone, "tomate", 200)
    assert r["changes"]["quantity"] == {"presented": 500.0, "live": 120.0}
    assert r["changes"]["requested_exceeds_live"] == {"requested": 200.0, "live": 120.0}
    assert "il reste 120 kg (au lieu de 500 kg)" in r["note"]
    stored = env.one("select changes from intelligence.availability_interests")
    assert stored["quantity"]["live"] == 120.0


def test_a_sold_out_offer_is_reported_and_the_producer_is_not_bothered(env):
    phone = _delivered_campaign(env)
    env.cur.execute("update marketplace.products set is_available=false, quantity_for_sale=0 where id=%s", (env.g.product,))
    r = _interest(env, phone, "tomate", 50)
    assert r["changes"] == {"unavailable": True} and "n'est plus disponible" in r["note"]
    assert env.one("select count(*) from intelligence.notification_outbox where template_key='AVAILABILITY_INTEREST_PRODUCER'") == 0
    assert env.one("select status from intelligence.availability_interests") == "OPEN"


def test_two_matching_offers_are_an_ambiguity_not_a_silent_choice(env):
    env.product(name="Tomate cerise", quantity_for_sale=30)
    phone = env.buyer(with_user=True)
    env.campaign()
    tick(env)
    env.cur.execute("update intelligence.availability_campaign_recipients set status='SENT', sent_at=now() at time zone 'utc', provider_ref='w'")
    r = _interest(env, phone, "tomate", 10)
    assert r["ambiguous"] is True and r["note"] is None
    assert env.one("select product_id from intelligence.availability_interests") is None
    assert env.one("select count(*) from intelligence.notification_outbox where template_key='AVAILABILITY_INTEREST_PRODUCER'") == 0


def test_a_reply_about_another_product_or_outside_the_window_is_not_attributed(env):
    phone = _delivered_campaign(env)
    assert _interest(env, phone, "mangue")["reason"] == "no_offer_match"
    env.cur.execute("update intelligence.availability_campaign_recipients set sent_at = now() at time zone 'utc' - interval '100 hours'")
    assert _interest(env, phone, "tomate", ref="wamid.late")["reason"] == "no_recent_campaign"
    assert _interest(env, _phone(), "tomate")["reason"] == "no_recent_campaign"
    assert env.one("select count(*) from intelligence.availability_interests") == 0


def test_interest_recorded_but_producer_notification_failed_is_retried_exactly_once(env, monkeypatch):
    phone = _delivered_campaign(env)
    real = interest_service.notify_producer
    calls = {"n": 0}

    async def flaky(session, interest_id, *, now=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("file indisponible")
        return await real(session, interest_id, now=now)

    monkeypatch.setattr(interest_service, "notify_producer", flaky)
    r = _interest(env, phone, "tomate", 60)
    assert r["created"] and r["producer_notified"] is False
    assert env.one("select status from intelligence.availability_interests") == "OPEN"  # l'intérêt, lui, est bien enregistré
    assert env.one("select count(*) from intelligence.notification_outbox where template_key='AVAILABILITY_INTEREST_PRODUCER'") == 0

    async def sweep(factory):
        async with factory() as s:
            n = await interest_service.notify_open_interests(s)
            await s.commit()
            return n

    assert env.run(sweep) == 1
    assert env.run(sweep) == 0  # rien à refaire
    assert env.one("select count(*) from intelligence.notification_outbox where template_key='AVAILABILITY_INTEREST_PRODUCER'") == 1
    assert env.one("select status from intelligence.availability_interests") == "PRODUCER_NOTIFIED"


def test_producer_notification_carries_only_what_is_useful(env):
    phone = _delivered_campaign(env, buyer_location="Bobo-Dioulasso")
    _interest(env, phone, "tomate", 80)
    payload = env.one("select payload from intelligence.notification_outbox where template_key='AVAILABILITY_INTEREST_PRODUCER'")
    assert payload["quantity"] == 80 and payload["zone"] == "Guiriko" and payload["buyer_label"] == "Acheteur Test"
    assert phone not in str(payload)  # pas le téléphone de l'acheteur
    target = env.one("select recipient_phone from intelligence.notification_outbox where template_key='AVAILABILITY_INTEREST_PRODUCER'")
    assert target == env.one("select phone from auth.users where id=%s", env.g.producer_user)


# ── Métriques ───────────────────────────────────────────────────────────────────────────────────────────────────────


def test_metrics_state_their_denominators(env):
    phones = [env.buyer() for _ in range(4)]
    cid = env.campaign()
    tick(env)
    env.cur.execute("update intelligence.availability_campaign_recipients set status='SENT', sent_at=now() at time zone 'utc', provider_ref = 'w' || phone")
    for p in phones[:3]:
        _apply(env, "w" + p, "delivered")
    env.cur.execute("update intelligence.availability_campaign_recipients set replied_at = now(), status='REPLIED' where provider_ref = %s", ("w" + phones[0],))
    _apply(env, "w" + phones[3], "failed", "x")

    async def sc(factory):
        async with factory() as s:
            return await cs.campaign_metrics(s, cid)

    m = env.run(sc)
    assert (m["prepared"], m["accepted"], m["delivered"], m["replied"], m["failed"]) == (4, 4, 3, 1, 1)
    assert m["rates"]["delivery_rate"] == {"value": 0.75, "numerator": "delivered", "denominator": "accepted"}
    assert m["rates"]["reply_rate"]["value"] == pytest.approx(1 / 3, abs=1e-4)
    assert m["rates"]["reply_rate"]["denominator"] == "delivered"
    assert "pas une vente" in m["notes"]
