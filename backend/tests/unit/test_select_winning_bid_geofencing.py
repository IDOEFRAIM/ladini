"""`AuctionMixin.select_winning_bid` — geofencing Burkina Faso sur le point
de livraison optionnel (``delivery_lat``/``delivery_lon``), en défense en
profondeur avant même de toucher la base (l'outil est aussi appelable
directement, hors du tunnel de confirmation WhatsApp qui valide déjà)."""
from __future__ import annotations

import pytest

from agriconnect.services.database.errors import BusinessRuleException
from tests.conftest import run


def _service():
    from agriconnect.services.database.auction import AuctionMixin

    class _Svc(AuctionMixin):
        @property
        def session(self):
            return object()  # jamais atteint : le rejet geofencing est avant toute requête

    return _Svc()


class TestSelectWinningBidGeofencing:
    def test_a_delivery_point_outside_burkina_faso_is_rejected_before_any_query(self):
        svc = _service()
        with pytest.raises(BusinessRuleException) as exc_info:
            run(svc.select_winning_bid(bid_id="b1", phone="+22670000001", delivery_lat=48.85, delivery_lon=2.35))
        assert exc_info.value.reason == "out_of_country"

    def test_no_delivery_point_at_all_skips_the_geofencing_check(self):
        """Rétro-compatibilité : un appel sans delivery_lat/lon (ancien
        comportement) ne doit jamais être bloqué par le geofencing — il
        continue simplement vers la résolution du bid (qui échouera ici sur
        un id invalide, preuve que le geofencing n'a pas interrompu avant)."""
        svc = _service()
        with pytest.raises(Exception) as exc_info:
            run(svc.select_winning_bid(bid_id="not-a-uuid", phone="+22670000001"))
        assert not isinstance(exc_info.value, BusinessRuleException) or exc_info.value.reason != "out_of_country"
