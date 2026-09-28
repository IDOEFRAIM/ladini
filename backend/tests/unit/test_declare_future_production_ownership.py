"""`ProducerMgmtMixin.declare_future_production` — ownership au niveau DB-layer.

## Pourquoi ce fichier existe

La Phase 2 du chantier reliability gèle le payload que l'utilisateur confirme
(`confirmation_gate`) pour les 10 goals génériques, dont
`PRODUCTION_DECLARE_FUTURE`. Ce gel garantit « ce que l'utilisateur confirme ==
ce que le métier exécute » mais NE remplace PAS le contrôle d'appartenance :
un `farm_id` fantaisiste (hallucination LLM) ou la ferme d'un AUTRE producteur
doit être rejeté par la couche service. Ce contrôle existait (lu ligne par
ligne en Phase 2) mais aucun test ne le verrouillait — invariant I4 :
« une entité modifiée doit appartenir à l'acteur autorisé ».

## Portée honnête

Même doublure de session que `test_select_winning_bid_state_guards.py` : pas de
Postgres réel. On simule `session.get(Farm, ...)` et la résolution du profil
producteur ; on vérifie (a) les 3 rejets, sans AUCUNE écriture, et (b) que le
chemin légitime ATTEINT l'écriture, avec le `producer_id` de l'appelant
(jamais celui du payload)."""
from __future__ import annotations

import types
import uuid

import pytest

from ladini.services.database.producer import ProducerMgmtMixin
from tests.conftest import run


class _ReachedWrite(Exception):
    """Levée par `flush()` : prouve que le chemin d'autorisation est passé."""


class _FakeSession:
    def __init__(self, farm):
        self._farm = farm
        self.added: list = []

    async def get(self, model, pk):
        return self._farm

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        raise _ReachedWrite()


def _service(session, *, user_row):
    class _Svc(ProducerMgmtMixin):
        @property
        def session(self):
            return session

        async def _resolve_producer_phone(self, *, phone=None, producer_id=None):
            return phone or "+22670000001"

        async def _fetch_user_entities(self, phone):
            return user_row

    return _Svc()


def _payload(farm_id):
    return {
        "farm_id": str(farm_id),
        "product_label": "Maïs",
        "quantity": 100,
        "unit": "KG",
        "price_per_unit": 250,
        "expected_harvest_date": "2030-01-15",
    }


class TestDeclareFutureProductionOwnership:
    def test_unknown_farm_is_rejected_without_any_write(self):
        session = _FakeSession(farm=None)
        me = types.SimpleNamespace(id=uuid.uuid4())
        svc = _service(session, user_row=(object(), me))

        with pytest.raises(ValueError, match="Ferme introuvable"):
            run(svc.declare_future_production(_payload(uuid.uuid4()), phone="+22670000001"))
        assert not session.added

    def test_farm_of_another_producer_is_rejected_without_any_write(self):
        me = types.SimpleNamespace(id=uuid.uuid4())
        someone_else = uuid.uuid4()
        farm = types.SimpleNamespace(id=uuid.uuid4(), producer_id=someone_else)
        session = _FakeSession(farm=farm)
        svc = _service(session, user_row=(object(), me))

        with pytest.raises(ValueError, match="n'appartient pas"):
            run(svc.declare_future_production(_payload(farm.id), phone="+22670000001"))
        assert not session.added

    @pytest.mark.parametrize("user_row", [None, (object(), None)])
    def test_caller_without_producer_profile_is_rejected_without_any_write(self, user_row):
        farm = types.SimpleNamespace(id=uuid.uuid4(), producer_id=uuid.uuid4())
        session = _FakeSession(farm=farm)
        svc = _service(session, user_row=user_row)

        with pytest.raises(ValueError, match="Profil producteur introuvable"):
            run(svc.declare_future_production(_payload(farm.id), phone="+22670000001"))
        assert not session.added

    def test_own_farm_reaches_the_write_with_the_callers_producer_id(self):
        me = types.SimpleNamespace(id=uuid.uuid4())
        farm = types.SimpleNamespace(id=uuid.uuid4(), producer_id=me.id)
        session = _FakeSession(farm=farm)
        svc = _service(session, user_row=(object(), me))

        with pytest.raises(_ReachedWrite):
            run(svc.declare_future_production(_payload(farm.id), phone="+22670000001"))

        assert len(session.added) == 1
        offer = session.added[0]
        assert offer.producer_id == me.id
        assert offer.farm_id == farm.id
