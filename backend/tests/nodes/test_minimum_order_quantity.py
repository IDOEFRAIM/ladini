"""Seuil minimum de commande par TYPE de produit (2026-09-02).

Feature full-stack (web + agent), demande explicite utilisateur. La
plateforme (ADMIN) peut imposer, pour un TYPE de produit (`SubCategory`), une
quantité minimale qu'une commande de ce type doit représenter. Politique
PLATEFORME — jamais une propriété du producteur, jamais dérivée de
`pricing_tiers`/du prix. Voir `domain/order_policy.py` (source de vérité
unique) et `domain/governance/models.py::SubCategory` (colonne).

Chaque `ref` (vendor dict) ci-dessous porte `minimum_order_quantity`/
`minimum_order_unit` exactement comme `services/database/buyer.py::search_products`
les fournit désormais (JOIN `SubCategory`) — c'est la frontière testée ici :
le domaine ne fait AUCUNE hypothèse sur COMMENT ces valeurs sont arrivées,
seulement sur ce qu'il en fait.
"""
from __future__ import annotations

from agriconnect.graphs.agents.market_coach.core.pending_interaction import (
    get_pending_interaction,
    to_tunnel_category,
)
from agriconnect.domain.order_policy import (
    validate_minimum_order_quantity,
)
from agriconnect.graphs.agents.market_coach.services.domain.cart_service import (
    CartDomainService,
)
from tests.conftest import StubRuntime, run

_TIERS = [
    {
        "tier_id": "tier-10kg",
        "quantity": 10.0,
        "unit": "KG",
        "price": 4000.0,
        "packaging": "sac",
        "base_unit_quantity": 10.0,
        "min_order_quantity": 1,
    },
]


def _flat_vendor(minimum=None, minimum_unit=None):
    return {
        "product_id": "P-TOM",
        "name": "tomates",
        "price": 500.0,
        "unit": "KG",
        "vendor_name": "jojo",
        "producer_id": "PR-JOJO",
        "source_type": "DIRECT",
        "is_auction": False,
        "pricing_tiers": None,
        "minimum_order_quantity": minimum,
        "minimum_order_unit": minimum_unit,
    }


def _tiered_vendor(minimum=None, minimum_unit=None):
    return {
        "product_id": "P-TOM",
        "name": "tomates",
        "price": 500.0,
        "unit": "KG",
        "vendor_name": "jojo",
        "producer_id": "PR-JOJO",
        "source_type": "DIRECT",
        "is_auction": False,
        "pricing_tiers": _TIERS,
        "minimum_order_quantity": minimum,
        "minimum_order_unit": minimum_unit,
    }


def _rt(available=500.0):
    return StubRuntime(responses={
        "validate_stock_availability_atomic": {
            "status": "success", "available_quantity": available,
            "unit": "KG", "unit_price": 500.0,
        },
    })


# =====================================================================
# Le service métier réutilisable lui-même (règle 43)
# =====================================================================


class TestValidateMinimumOrderQuantityDomainFunction:
    def test_null_minimum_is_no_rule_historical_behaviour(self):
        result = validate_minimum_order_quantity(
            minimum_order_quantity=None, minimum_order_unit=None,
            total_quantity=1.0, total_unit="KG",
        )
        assert result.passed
        assert result.reason == "NO_RULE"

    def test_below_minimum_is_rejected(self):
        result = validate_minimum_order_quantity(
            minimum_order_quantity=50.0, minimum_order_unit="KG",
            total_quantity=30.0, total_unit="KG",
        )
        assert not result.passed
        assert result.reason == "BELOW_MINIMUM"
        assert result.minimum_in_total_unit == 50.0

    def test_exactly_at_minimum_is_accepted(self):
        result = validate_minimum_order_quantity(
            minimum_order_quantity=50.0, minimum_order_unit="KG",
            total_quantity=50.0, total_unit="KG",
        )
        assert result.passed
        assert result.reason == "OK"

    def test_above_minimum_is_accepted(self):
        result = validate_minimum_order_quantity(
            minimum_order_quantity=50.0, minimum_order_unit="KG",
            total_quantity=60.0, total_unit="KG",
        )
        assert result.passed

    def test_unit_conversion_uses_the_shared_engine(self):
        # 50 KG minimum, commande en TONNE (0.05 T < 50 KG => refus).
        result = validate_minimum_order_quantity(
            minimum_order_quantity=50.0, minimum_order_unit="KG",
            total_quantity=0.03, total_unit="TONNE",
        )
        assert not result.passed
        assert result.minimum_in_total_unit == 0.05

    def test_incompatible_unit_families_never_guess(self):
        # SAC n'a pas d'équivalence universelle avec KG — jamais de devinette.
        result = validate_minimum_order_quantity(
            minimum_order_quantity=50.0, minimum_order_unit="KG",
            total_quantity=3.0, total_unit="SAC",
        )
        assert not result.passed
        assert result.reason == "UNIT_INCOMPATIBLE"
        assert result.minimum_in_total_unit is None

    def test_zero_or_negative_minimum_is_treated_as_no_rule(self):
        """Garde défensive — la validation d'écriture ADMIN interdit déjà
        0/négatif (règle 13), mais le domaine ne doit jamais silencieusement
        bloquer TOUTE commande sur une donnée corrompue."""
        assert validate_minimum_order_quantity(
            minimum_order_quantity=0, minimum_order_unit="KG",
            total_quantity=1.0, total_unit="KG",
        ).passed
        assert validate_minimum_order_quantity(
            minimum_order_quantity=-5, minimum_order_unit="KG",
            total_quantity=1.0, total_unit="KG",
        ).passed


# =====================================================================
# Tests A-F du cahier des charges (§35), via le VRAI add_to_cart_with_ref
# =====================================================================


class TestFlatPricingMinimum:
    """Test A/B : produit à tarif unique."""

    def test_a_below_minimum_is_rejected(self):
        svc = CartDomainService(_rt())
        ref = _flat_vendor(minimum=50.0, minimum_unit="KG")
        result = run(svc.add_to_cart_with_ref(
            "+22601479800", "tomates", 20, ref, [], {}, buyer_unit="KG",
        ))
        assert result["status"] == "WAITING_INPUT"
        assert to_tunnel_category(get_pending_interaction(result)) == "QUANTITY"
        assert "active_cart" not in result
        body = result["final_response"]
        assert "50" in body and "20" in body
        assert "minimale" in body.lower()

    def test_b_exactly_at_minimum_is_accepted(self):
        svc = CartDomainService(_rt())
        ref = _flat_vendor(minimum=50.0, minimum_unit="KG")
        result = run(svc.add_to_cart_with_ref(
            "+22601479800", "tomates", 50, ref, [], {}, buyer_unit="KG",
        ))
        assert result["status"] == "COMPLETED"
        assert result["active_cart"][-1]["quantity"] == 50.0


class TestTierPricingMinimum:
    """Test C/D/E : produit multi-tier — la règle porte sur `total_quantity`
    (package_count * tier.quantity), JAMAIS sur `package_count` seul."""

    def test_c_below_minimum_is_rejected(self):
        svc = CartDomainService(_rt())
        ref = _tiered_vendor(minimum=50.0, minimum_unit="KG")
        result = run(svc.add_to_cart_with_ref(
            "+22601479800", "tomates", 3, ref, [], {}, tier_id="tier-10kg",
        ))
        assert result["status"] == "WAITING_INPUT"
        assert "active_cart" not in result
        body = result["final_response"]
        assert "30" in body  # 3 x 10kg = 30kg, quantité actuelle affichée
        assert "50" in body
        assert "sac" in body.lower()  # reste scopé "combien de paquets", jamais KG

    def test_d_exactly_at_minimum_is_accepted(self):
        svc = CartDomainService(_rt())
        ref = _tiered_vendor(minimum=50.0, minimum_unit="KG")
        result = run(svc.add_to_cart_with_ref(
            "+22601479800", "tomates", 5, ref, [], {}, tier_id="tier-10kg",
        ))
        assert result["status"] == "COMPLETED"
        line = result["active_cart"][-1]
        assert line["quantity"] == 5  # package_count, jamais comparé au seuil directement
        assert line["base_unit_quantity"] == 50.0

    def test_e_above_minimum_is_accepted(self):
        svc = CartDomainService(_rt())
        ref = _tiered_vendor(minimum=50.0, minimum_unit="KG")
        result = run(svc.add_to_cart_with_ref(
            "+22601479800", "tomates", 6, ref, [], {}, tier_id="tier-10kg",
        ))
        assert result["status"] == "COMPLETED"
        assert result["active_cart"][-1]["base_unit_quantity"] == 60.0

    def test_package_count_is_never_compared_directly_to_the_threshold(self):
        """Règle 9 explicite : `package_count=3` seul ne doit JAMAIS être lu
        comme "3 < 50 KG" par erreur d'unité — c'est bien 30 KG < 50 KG qui
        motive le refus, vérifié via le message (30, pas 3)."""
        svc = CartDomainService(_rt())
        ref = _tiered_vendor(minimum=50.0, minimum_unit="KG")
        result = run(svc.add_to_cart_with_ref(
            "+22601479800", "tomates", 3, ref, [], {}, tier_id="tier-10kg",
        ))
        assert "30" in result["final_response"]
        assert "au moins 5" in result["final_response"]  # ceil(50/10) = 5 sacs


class TestNullMinimumIsHistoricalBehaviour:
    """Test F : `SubCategory.minimum_order_quantity IS NULL` — comportement
    strictement identique à avant cette feature."""

    def test_flat_product_with_no_threshold_configured(self):
        svc = CartDomainService(_rt())
        ref = _flat_vendor(minimum=None, minimum_unit=None)
        result = run(svc.add_to_cart_with_ref(
            "+22601479800", "tomates", 1, ref, [], {}, buyer_unit="KG",
        ))
        assert result["status"] == "COMPLETED"

    def test_tiered_product_with_no_threshold_configured(self):
        svc = CartDomainService(_rt())
        ref = _tiered_vendor(minimum=None, minimum_unit=None)
        result = run(svc.add_to_cart_with_ref(
            "+22601479800", "tomates", 1, ref, [], {}, tier_id="tier-10kg",
        ))
        assert result["status"] == "COMPLETED"


class TestPreorderFutureOffersAlsoRespectTheThreshold:
    """Le seuil s'applique quel que soit le canal — y compris les
    réservations de production future (MarketOffer), pas seulement l'achat
    catalogue direct."""

    def test_future_offer_reservation_below_minimum_is_rejected(self, monkeypatch):
        import agriconnect.graphs.agents.market_coach.services.domain.cart_service as m

        async def _fake_reserve(self, **kwargs):
            raise AssertionError(
                "reserve_future_offer ne doit jamais être appelé sous le seuil"
            )

        monkeypatch.setattr(m.PreorderGateway, "reserve_future_offer", _fake_reserve)

        svc = CartDomainService(_rt())
        ref = {
            "product_id": "OFFER-1", "name": "tomates", "price": 500.0,
            "unit": "KG", "vendor_name": "jojo", "producer_id": "PR-JOJO",
            "source_type": "FUTURE", "market_offer_id": "OFFER-1",
            "is_auction": False, "pricing_tiers": None,
            "minimum_order_quantity": 50.0, "minimum_order_unit": "KG",
        }
        result = run(svc.add_to_cart_with_ref(
            "+22601479800", "tomates", 20, ref, [], {}, buyer_unit="KG",
        ))
        assert result["status"] == "WAITING_INPUT"
        assert "50" in result["final_response"]
