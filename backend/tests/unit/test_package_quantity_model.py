"""Modèle canonique package_count/package_size/available_quantity (2026-09-30).

Incident réel : « j'ai 50 pot de 4 litre » devenait `quantity=4 L` (perte du
« 50 ») puis « Quel est votre prix par litre ? » — le modèle métier ne
distinguait pas `available_quantity` (quantité totale vendable),
`package.count` (combien de conditionnements), `package.size` (contenance
d'UN conditionnement) et la taille d'un palier tarifaire
(`domain/pricing_tiers.py::PricingTier.quantity`).

Ce fichier verrouille UNIQUEMENT la REPRÉSENTATION et son invariant central
(`count × size` ne se calcule qu'à conditions explicites, un seul endroit) —
PAS le parser linguistique ("pot" reste absent de tout vocabulaire ici,
volontairement : voir `domain/commercial_offer.py::PackageDefinition`,
mandat §6 "aucune whitelist").

Module pur testé : `domain/commercial_offer.py`.
"""
from __future__ import annotations

import pytest

from ladini.domain.commercial_offer import (
    CommercialOffer,
    InventoryQuantity,
    PackageDefinition,
    PackageStatus,
    Provenance,
    derive_available_quantity_from_package,
)
from ladini.domain.pricing_tiers import PricingTier


def _known_package(
    *, package_type: str, content_amount: float, content_unit: str, count=None
) -> PackageDefinition:
    return PackageDefinition(
        package_type=package_type,
        content_amount=content_amount,
        content_unit=content_unit,
        count=count,
        status=PackageStatus.KNOWN,
        source=Provenance.USER_EXPLICIT,
    )


# =====================================================================
# §12 du mandat — cas canoniques 1-7
# =====================================================================


class TestCanonicalDerivation:
    def test_1_bare_availability_60L_no_package(self):
        """Cas A : « 60 L », pas de conditionnement."""
        availability = InventoryQuantity(amount=60.0, unit="LITRE")
        assert availability.amount == 60.0
        assert availability.unit == "LITRE"
        assert derive_available_quantity_from_package(None) is None

    def test_2_50_pots_de_4L_derives_200L(self):
        """Cas B : « 50 pots de 4 L » -> count=50, size=4, unit=LITRE, total=200 L.

        `50 × 4 = 200` est autorisé PARCE QUE count + size + unit sont TOUS
        explicitement présents — exactement l'invariant central du mandat."""
        package = _known_package(
            package_type="POT", content_amount=4.0, content_unit="LITRE", count=50
        )
        assert package.count == 50
        assert package.content_amount == 4.0
        assert package.is_count_known is True
        assert package.is_content_known is True
        assert package.can_derive_available_quantity is True

        derived = derive_available_quantity_from_package(package)
        assert derived is not None
        assert derived.amount == 200.0
        assert derived.unit == "LITRE"
        assert derived.source == Provenance.DOMAIN_DERIVED

    def test_3_50_pots_size_unknown_total_unavailable(self):
        """Cas C : « 50 pots » -> count=50, size=None -> INTERDIT de produire
        `available_quantity = 50 L` (ou quoi que ce soit)."""
        package = PackageDefinition(
            package_type="POT", count=50, status=PackageStatus.UNKNOWN
        )
        assert package.is_count_known is True
        assert package.is_content_known is False
        assert package.can_derive_available_quantity is False
        assert derive_available_quantity_from_package(package) is None

    def test_4_pot_de_4L_count_unknown_total_unavailable(self):
        """Cas D : « pot de 4 L » -> count=None -> INTERDIT de produire
        `available_quantity = 4 L`."""
        package = _known_package(
            package_type="POT", content_amount=4.0, content_unit="LITRE", count=None
        )
        assert package.is_content_known is True
        assert package.is_count_known is False
        assert package.can_derive_available_quantity is False
        assert derive_available_quantity_from_package(package) is None

    def test_5_20_sacs_de_50kg_derives_1000kg(self):
        package = _known_package(
            package_type="SAC", content_amount=50.0, content_unit="KG", count=20
        )
        derived = derive_available_quantity_from_package(package)
        assert derived is not None
        assert derived.amount == 1000.0
        assert derived.unit == "KG"

    def test_6_15_bidons_de_20L_derives_300L(self):
        package = _known_package(
            package_type="BIDON", content_amount=20.0, content_unit="LITRE", count=15
        )
        derived = derive_available_quantity_from_package(package)
        assert derived is not None
        assert derived.amount == 300.0
        assert derived.unit == "LITRE"

    def test_7_30_caisses_de_12kg_derives_360kg(self):
        """« caisse » n'est dans AUCUN vocabulaire/whitelist du dépôt — le
        modèle n'en a pas besoin, voir test 8 ci-dessous."""
        package = _known_package(
            package_type="CAISSE", content_amount=12.0, content_unit="KG", count=30
        )
        derived = derive_available_quantity_from_package(package)
        assert derived is not None
        assert derived.amount == 360.0
        assert derived.unit == "KG"


class TestFreePackagingLabel:
    def test_8_unknown_free_label_cageot_is_valid(self):
        """§6 du mandat : le modèle ne doit PAS imposer de whitelist. « cageot »
        n'existe dans aucun vocabulaire du dépôt (vérifié : absent de
        `domain/quantity_unit.py::_TIER_PACKAGING_WORDS`/`UNIT_SYNONYMS`) et
        pourtant se représente et se calcule sans difficulté."""
        package = _known_package(
            package_type="CAGEOT", content_amount=8.0, content_unit="KG", count=25
        )
        assert package.package_type == "CAGEOT"
        derived = derive_available_quantity_from_package(package)
        assert derived is not None
        assert derived.amount == 200.0
        assert derived.unit == "KG"

    @pytest.mark.parametrize(
        "label", ["BARQUETTE", "CALEBASSE", "FUT", "REGIME", "TONNEAU"]
    )
    def test_any_free_label_is_representable(self, label):
        """Aucun label n'est rejeté par construction — le parser pourra rester
        limité, le modèle jamais (mandat §6)."""
        package = _known_package(
            package_type=label, content_amount=1.0, content_unit="KG", count=3
        )
        assert derive_available_quantity_from_package(package).amount == 3.0


# =====================================================================
# Cas E du mandat — availability et pricing tiers coexistent SANS jamais
# se confondre (I5, I8, I10) — réutilise le moteur PricingTier existant,
# n'invente rien.
# =====================================================================


class TestAvailabilityIndependentFromPricingTiers:
    def test_9_availability_stays_60L_alongside_two_tiers(self):
        availability = InventoryQuantity(amount=60.0, unit="LITRE", source=Provenance.USER_EXPLICIT)
        tiers = [
            PricingTier(
                tier_id="t1", quantity=5.0, unit="L", price=700.0,
                packaging="bidon", base_unit_quantity=5.0,
            ),
            PricingTier(
                tier_id="t2", quantity=9.0, unit="L", price=1000.0,
                packaging="bidon", base_unit_quantity=9.0,
            ),
        ]
        offer = CommercialOffer(product="miel", inventory_quantity=availability)
        # Rien dans le modèle canonique ne lit/somme les tiers pour produire
        # ou modifier `inventory_quantity` — la valeur reste EXACTEMENT celle
        # posée, quel que soit le nombre/la taille des tiers à côté.
        assert offer.inventory_quantity is not None
        assert offer.inventory_quantity.amount == 60.0
        assert len(tiers) == 2  # les tiers existent, indépendamment

    def test_10_tier_sizes_are_never_summed_into_stock(self):
        """I10 : aucun tier ne définit implicitement le stock total — somme
        des tiers (5+9=14) INTERDITE comme substitut de `available_quantity`."""
        tiers_total = 5.0 + 9.0  # ce que produirait, à tort, une sommation naïve
        availability = InventoryQuantity(amount=60.0, unit="LITRE")
        assert availability.amount != tiers_total
        assert availability.amount == 60.0

    def test_11_package_size_alone_never_becomes_availability(self):
        """I4 : `package_size != available_quantity` par défaut — une taille de
        conditionnement seule (pas de count) ne devient JAMAIS une
        disponibilité, quelle que soit sa valeur."""
        package = _known_package(
            package_type="BIDON", content_amount=4.0, content_unit="LITRE", count=None
        )
        result = derive_available_quantity_from_package(package)
        assert result is None
        # Interdiction explicite : la disponibilité ne doit jamais "devenir"
        # silencieusement la taille du conditionnement (4 L).
        assert result != InventoryQuantity(amount=4.0, unit="LITRE")

    def test_12_package_count_alone_never_becomes_availability(self):
        package = PackageDefinition(
            package_type="BIDON", count=50, status=PackageStatus.UNKNOWN
        )
        result = derive_available_quantity_from_package(package)
        assert result is None
        assert result != InventoryQuantity(amount=50.0, unit="LITRE")


# =====================================================================
# §8 du mandat — invariants structurels I1-I10
# =====================================================================


class TestStructuralInvariants:
    def test_i1_package_count_not_positive_is_never_known(self):
        for bad_count in (0, -1, -50):
            package = _known_package(
                package_type="POT", content_amount=4.0, content_unit="LITRE", count=bad_count
            )
            assert package.is_count_known is False, f"count={bad_count} ne doit jamais être 'connu'"
            assert derive_available_quantity_from_package(package) is None

    def test_i2_package_size_not_positive_is_never_known(self):
        for bad_amount in (0.0, -4.0):
            package = PackageDefinition(
                package_type="POT",
                content_amount=bad_amount,
                content_unit="LITRE",
                count=50,
                status=PackageStatus.KNOWN,
                source=Provenance.USER_EXPLICIT,
            )
            assert package.is_content_known is False, f"content_amount={bad_amount} ne doit jamais être 'connu'"
            assert derive_available_quantity_from_package(package) is None

    def test_i3_available_quantity_positive_when_present(self):
        # `InventoryQuantity` reste un dataclass permissif par construction
        # (comme avant cette étape — pas de validation ajoutée en dehors de
        # `PackageDefinition`, voir le rapport final) ; la garantie "> 0" est
        # portée par les APPELANTS existants (`validate_commercial_offer`,
        # qui rejette déjà `amount <= 0` en `INVALID`, inchangé ici).
        from ladini.domain.commercial_offer import validate_commercial_offer

        verdict = validate_commercial_offer(
            inventory_quantity=InventoryQuantity(amount=0.0, unit="LITRE"),
            pricing=None,
        )
        assert verdict.status == "INVALID"
        assert "quantity_not_positive" in verdict.conflicts

    def test_i6_partial_package_spec_is_valid_count_without_size(self):
        package = PackageDefinition(package_type="POT", count=50)
        assert package.count == 50
        assert package.content_amount is None

    def test_i6_partial_package_spec_is_valid_size_without_count(self):
        package = _known_package(package_type="POT", content_amount=4.0, content_unit="LITRE")
        assert package.content_amount == 4.0
        assert package.count is None

    def test_i7_unknown_label_still_representable(self):
        package = PackageDefinition(package_type="UNTRUCMACHIN", count=1)
        assert package.package_type == "UNTRUCMACHIN"

    def test_i9_derivation_requires_all_three_explicit(self):
        """count seul, size seul, ou unité manquante -> jamais de calcul."""
        assert derive_available_quantity_from_package(
            PackageDefinition(count=50, status=PackageStatus.UNKNOWN)
        ) is None
        assert derive_available_quantity_from_package(
            _known_package(package_type="POT", content_amount=4.0, content_unit="LITRE")
        ) is None
        # content_unit manquant malgré amount+count présents : jamais KNOWN.
        incomplete = PackageDefinition(
            package_type="POT",
            content_amount=4.0,
            content_unit=None,
            count=50,
            status=PackageStatus.KNOWN,
        )
        assert incomplete.is_content_known is False
        assert derive_available_quantity_from_package(incomplete) is None


# =====================================================================
# §13 du mandat — sérialisation / état
# =====================================================================


class TestSerializationRoundTrip:
    def test_serialize_deserialize_with_count(self):
        offer = CommercialOffer(
            product="miel",
            inventory_quantity=InventoryQuantity(amount=200.0, unit="LITRE", source=Provenance.DOMAIN_DERIVED),
            package=_known_package(
                package_type="POT", content_amount=4.0, content_unit="LITRE", count=50
            ),
        )
        data = offer.to_dict()
        assert data["package"]["count"] == 50
        restored = CommercialOffer.from_dict(data)
        assert restored is not None
        assert restored.package is not None
        assert restored.package.count == 50
        assert restored.package.content_amount == 4.0
        assert restored.package.can_derive_available_quantity is True

    def test_deserialize_partial_none_count(self):
        offer = CommercialOffer(
            product="miel",
            package=_known_package(package_type="POT", content_amount=4.0, content_unit="LITRE"),
        )
        data = offer.to_dict()
        assert data["package"]["count"] is None
        restored = CommercialOffer.from_dict(data)
        assert restored is not None
        assert restored.package is not None
        assert restored.package.count is None

    def test_copy_update_preserves_count(self):
        from dataclasses import replace

        package = _known_package(
            package_type="POT", content_amount=4.0, content_unit="LITRE", count=50
        )
        updated = replace(package, content_amount=5.0)
        assert updated.count == 50
        assert updated.content_amount == 5.0

    def test_backward_compatibility_old_payload_without_count_key(self):
        """Un `commercial_offer` sérialisé AVANT cette étape (pas de clé
        "count" du tout dans le dict) doit continuer à se désérialiser
        proprement, avec `count=None` (jamais une KeyError)."""
        legacy_payload = {
            "schema": 1,
            "product": "miel",
            "commercial_quantity": None,
            "inventory_quantity": None,
            "pricing": None,
            "package": {
                "package_type": "POT",
                "content_amount": 4.0,
                "content_unit": "LITRE",
                "status": "KNOWN",
                "source": "USER_EXPLICIT",
                # pas de clé "count" — payload historique
            },
            "normalized": None,
        }
        restored = CommercialOffer.from_dict(legacy_payload)
        assert restored is not None
        assert restored.package is not None
        assert restored.package.count is None
        assert restored.package.content_amount == 4.0

    def test_deserialize_garbage_count_is_none_not_a_crash(self):
        payload = {
            "schema": 1,
            "product": "miel",
            "commercial_quantity": None,
            "inventory_quantity": None,
            "pricing": None,
            "package": {
                "package_type": "POT",
                "content_amount": 4.0,
                "content_unit": "LITRE",
                "count": "beaucoup",
                "status": "KNOWN",
                "source": "USER_EXPLICIT",
            },
            "normalized": None,
        }
        restored = CommercialOffer.from_dict(payload)
        assert restored is not None
        assert restored.package is not None
        assert restored.package.count is None
