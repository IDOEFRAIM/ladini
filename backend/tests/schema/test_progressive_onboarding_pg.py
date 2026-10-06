"""Onboarding PROGRESSIF — contact minimal, enrichissement incrémental et capacités cumulatives, sur PostgreSQL réel.

`identify_or_create_user` (contact : téléphone, rôle neutre, aucun nom) puis `complete_user_profile` (nom, région,
capacité vendeur/acheteur) — le même utilisateur, jamais un second compte, sans toucher à la vérification producteur.
"""
from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from ladini.domain.identity.models import BuyerProfile, Producer, User


def _run(dsn, fn):
    async def go():
        engine = create_async_engine(dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                out = await fn(session)
                await session.commit()
                return out
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _svc(session):
    from ladini.services.database.auth import AuthMixin
    from ladini.services.database.base import BaseMixin

    class _Svc(AuthMixin, BaseMixin):
        @property
        def session(self):
            return session

    return _Svc()


def _phone() -> str:
    return "+226" + str(uuid.uuid4().int)[:8]


def _row(dsn, phone):
    async def fn(session):
        user = (await session.execute(select(User).where(User.phone == phone))).scalar_one()
        producers = (await session.execute(select(Producer).where(Producer.user_id == user.id))).scalars().all()
        buyers = (await session.execute(select(BuyerProfile).where(BuyerProfile.user_id == user.id))).scalars().all()
        return user, producers, buyers

    return _run(dsn, fn)


def test_a_first_contact_is_a_bare_row_with_no_invented_identity(pg_dsn):
    phone = _phone()
    _run(pg_dsn, lambda s: _svc(s).identify_or_create_user(phone))
    user, producers, buyers = _row(pg_dsn, phone)
    assert user.name is None and user.role == "USER" and user.zone_id is None and user.declared_location is None
    assert user.onboarding_completed is False and user.identity_verified is False
    assert producers == [] and buyers == []


def test_identifying_the_same_phone_twice_never_creates_a_second_user(pg_dsn):
    phone = _phone()
    _run(pg_dsn, lambda s: _svc(s).identify_or_create_user(phone))
    _run(pg_dsn, lambda s: _svc(s).identify_or_create_user(phone))

    async def count(session):
        return len((await session.execute(select(User).where(User.phone == phone))).scalars().all())

    assert _run(pg_dsn, count) == 1


def test_the_profile_is_enriched_incrementally_and_each_field_is_persisted(pg_dsn):
    phone = _phone()
    _run(pg_dsn, lambda s: _svc(s).identify_or_create_user(phone))

    _run(pg_dsn, lambda s: _svc(s).complete_user_profile(phone, name="Moussa"))
    user, _, _ = _row(pg_dsn, phone)
    assert user.name == "Moussa" and user.declared_location is None  # la région n'est pas inventée

    _run(pg_dsn, lambda s: _svc(s).complete_user_profile(phone, declared_location="Guiriko", coverage="COVERED"))
    user, _, _ = _row(pg_dsn, phone)
    assert user.name == "Moussa", "un champ déjà connu n'est jamais écrasé par une mise à jour d'un autre champ"
    assert user.declared_location == "Guiriko" and user.coverage_status == "COVERED"


def test_selling_adds_a_pending_producer_row_once_and_never_a_verified_one(pg_dsn):
    phone = _phone()
    _run(pg_dsn, lambda s: _svc(s).identify_or_create_user(phone))
    for _ in range(2):  # idempotent
        _run(pg_dsn, lambda s: _svc(s).complete_user_profile(phone, name="Moussa", capability="SELL"))
    user, producers, _ = _row(pg_dsn, phone)
    assert len(producers) == 1 and producers[0].status == "PENDING"
    assert user.identity_verified is False  # un profil complet n'est PAS un profil vérifié


def test_a_buyer_becomes_a_producer_on_the_same_identity_keeping_the_buyer_profile(pg_dsn):
    phone = _phone()
    _run(pg_dsn, lambda s: _svc(s).identify_or_create_user(phone))
    _run(pg_dsn, lambda s: _svc(s).complete_user_profile(phone, name="Awa", capability="BUY"))
    user_before, _, buyers_before = _row(pg_dsn, phone)
    assert len(buyers_before) == 1

    _run(pg_dsn, lambda s: _svc(s).complete_user_profile(phone, capability="SELL"))
    user, producers, buyers = _row(pg_dsn, phone)
    assert user.id == user_before.id, "même identité"
    assert len(producers) == 1 and len(buyers) == 1  # les deux capacités coexistent


def test_an_existing_producer_verification_is_preserved(pg_dsn):
    phone = _phone()
    _run(pg_dsn, lambda s: _svc(s).identify_or_create_user(phone))
    _run(pg_dsn, lambda s: _svc(s).complete_user_profile(phone, name="Moussa", capability="SELL"))

    async def approve(session):
        user = (await session.execute(select(User).where(User.phone == phone))).scalar_one()
        user.identity_verified = True
        producer = (await session.execute(select(Producer).where(Producer.user_id == user.id))).scalar_one()
        producer.status = "APPROVED"

    _run(pg_dsn, approve)
    _run(pg_dsn, lambda s: _svc(s).complete_user_profile(phone, name="Moussa Traoré", declared_location="Kadiogo", capability="SELL"))
    user, producers, _ = _row(pg_dsn, phone)
    assert user.identity_verified is True and producers[0].status == "APPROVED"
    assert user.name == "Moussa Traoré"


def test_the_profile_read_back_exposes_region_and_capabilities_for_the_gate(pg_dsn):
    phone = _phone()
    _run(pg_dsn, lambda s: _svc(s).identify_or_create_user(phone))
    _run(pg_dsn, lambda s: _svc(s).complete_user_profile(phone, name="Moussa", declared_location="Guiriko", capability="SELL"))

    async def read(session):
        return await _svc(session).get_user_by_phone(phone)

    profile = _run(pg_dsn, read)["data"]
    assert profile["declared_location"] == "Guiriko"
    assert profile["permissions"]["can_sell"] is True and profile["status"]["producer"] == "PENDING"
    assert profile["status"]["identity_verified"] is False


def test_unknown_phone_and_missing_phone_are_clean_errors(pg_dsn):
    out = _run(pg_dsn, lambda s: _svc(s).complete_user_profile(_phone(), name="X"))
    assert out["status"] == "error"
    assert _run(pg_dsn, lambda s: _svc(s).complete_user_profile("", name="X"))["status"] == "error"
