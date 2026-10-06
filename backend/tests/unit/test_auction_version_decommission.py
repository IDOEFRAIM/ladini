"""`Auction.version` — décision finale (2026-09-04, audit Auction/Bid,
volet winner-selection/post-order lifecycle).

## Fait établi (pas supposé) avant ce correctif

`update_negotiation_offer` (`services/database/buyer.py`) était le SEUL
écrivain de `Auction.version` dans tout le dépôt (recherche exhaustive) —
et rien nulle part ne la lisait (pas de CAS optimiste : la fonction protège
déjà sa cohérence via `.with_for_update()` pessimiste). Un compteur
write-only sans consommateur.

## Décision

Retirer l'incrément (code mort supprimé, aucun risque : rien ne le lisait).
Conserver la COLONNE en base (aucune suppression de schéma — ce dépôt n'a
pas de mécanisme de migration destructive, et un lecteur externe au dépôt
ne peut être exclu depuis ici) — voir `domain/orders/models.py::Auction.version`
pour la justification complète, marquée DEPRECATED/UNUSED."""
from __future__ import annotations

import types
import uuid

from ladini.services.database.buyer import BuyerMixin
from tests.conftest import run


def _buyer_profile():
    user = types.SimpleNamespace(id=uuid.uuid4())
    profile = types.SimpleNamespace(id=uuid.uuid4())
    return user, profile


async def _async_return(value):
    return value


class _FakeSession:
    def __init__(self, auction):
        self._auction = auction

    async def scalar(self, _stmt):
        return self._auction

    async def flush(self):
        pass


def _service(session):
    class _Svc(BuyerMixin):
        @property
        def session(self):
            return session

    svc = _Svc()
    svc.get_buyer_profile = lambda phone: _async_return(_buyer_profile())
    return svc


class TestUpdateNegotiationOfferNoLongerTouchesVersion:
    def test_price_update_succeeds_without_incrementing_the_deprecated_version_column(self):
        auction = types.SimpleNamespace(
            id=uuid.uuid4(),
            status="OPEN",
            max_price_per_unit=250.0,
            unit="KG",
            version=0,
        )
        session = _FakeSession(auction)
        svc = _service(session)

        result = run(
            svc.update_negotiation_offer(
                buyer_phone="+22670000001", negotiation_id=str(auction.id), new_price=300.0
            )
        )

        assert result["status"] == "success"
        assert auction.max_price_per_unit == 300.0
        # Le compteur reste figé — plus aucun code n'y écrit.
        assert auction.version == 0

    def test_repeated_updates_never_move_the_deprecated_counter(self):
        auction = types.SimpleNamespace(
            id=uuid.uuid4(), status="OPEN", max_price_per_unit=250.0, unit="KG", version=5,
        )
        session = _FakeSession(auction)
        svc = _service(session)

        run(svc.update_negotiation_offer(buyer_phone="+22670000001", negotiation_id=str(auction.id), new_price=275.0))
        run(svc.update_negotiation_offer(buyer_phone="+22670000001", negotiation_id=str(auction.id), new_price=300.0))

        assert auction.max_price_per_unit == 300.0
        assert auction.version == 5  # jamais retouché, valeur historique préservée telle quelle
