"""`EscrowMixin.initiate_escrow_payment` — geofencing Burkina Faso sur le
point de livraison optionnel (``delivery_lat``/``delivery_lon``), même
garde-fou que `AuctionMixin.select_winning_bid`
(`test_select_winning_bid_geofencing.py`).

Gap réel comblé le 2026-08-18 : ce paramètre n'existait pas du tout avant
(voir [[precommande-architecture-consolidation-2026-08]]) — resté sans
conséquence tant qu'`ESCROW_PAYMENT_ENABLED` était False (le paiement à la
livraison, seul chemin actif, appliquait déjà le point GPS via
`confirm_preorder_draft`), mais serait devenu une régression silencieuse
(précommandes payées via escrow livrées sans point GPS) une fois le
paiement Paydunya réactivé."""
from __future__ import annotations

import pytest

from ladini.services.database.errors import BusinessRuleException
from tests.conftest import run


def _service():
    from ladini.services.database.escrow import EscrowMixin

    class _Svc(EscrowMixin):
        @property
        def session(self):
            return object()  # jamais atteint : le rejet geofencing est avant toute requête

    return _Svc()


class TestInitiateEscrowPaymentGeofencing:
    def test_a_delivery_point_outside_burkina_faso_is_rejected_before_any_query(self):
        svc = _service()
        with pytest.raises(BusinessRuleException) as exc_info:
            run(svc.initiate_escrow_payment(
                buyer_phone="+22670000001", preorder_id="p1",
                delivery_lat=48.85, delivery_lon=2.35,
            ))
        assert exc_info.value.reason == "out_of_country"

    def test_no_delivery_point_at_all_skips_the_geofencing_check(self):
        """Rétro-compatibilité : un appel sans delivery_lat/lon (ancien
        comportement, avant l'ajout de ces paramètres) ne doit jamais être
        bloqué par le geofencing — il continue simplement vers la
        résolution de la précommande (qui échouera ici sur un id invalide,
        preuve que le geofencing n'a pas interrompu avant)."""
        svc = _service()
        with pytest.raises(Exception) as exc_info:
            run(svc.initiate_escrow_payment(buyer_phone="+22670000001", preorder_id="not-a-uuid"))
        assert not isinstance(exc_info.value, BusinessRuleException) or exc_info.value.reason != "out_of_country"
