"""Commercial Quantity & Pricing Domain Hardening (2026-09-28) — Phase A.

## Le bug P0 confirmé par audit, rejoué ici

`actions/common.py::normalize_quantity_to_kg` convertit `quantity`/`unit`
vers KG pour un but comme SALES_PUBLISH_PRODUCT ("200 tonnes"), mais 3
sites (`domain/sales.py::publish_product`, `domain/agro.py::declare_crop_cycle`,
`domain/procurement.py::create_request`) passaient le `price`/`max_price`
associé INCHANGÉ — "200 tonnes à 500 000 F la tonne" devenait exécuté comme
500 000 F/KG (facteur 1000x), sans jamais lever d'erreur ni d'avertissement.

Deuxième bug P0 confirmé, même fichier : la table `_UNIT_TO_KG` assimilait
des CONDITIONNEMENTS (SAC, PANIER, CHARRETTE) à des unités de masse, avec un
poids FIXE deviné (SAC=100kg, PANIER=25kg, CHARRETTE=250kg) — "3 sacs"
devenait silencieusement "300 KG" quel que soit le poids réel du sac du
producteur. `domain/quantity_unit.py::UNIT_SYNONYMS` mappe même
"sachet"/"sachets" vers ce même code "SAC".

## Le correctif

1. `normalize_quantity_to_kg` ne convertit plus JAMAIS un conditionnement ou
   une unité non reconnue — il les laisse tels quels (même traitement que
   TETE/UNITE/LITRE, qui bypassaient déjà la conversion). Aucun poids n'est
   plus jamais deviné.
2. Les 3 sites qui pairent un prix PAR-UNITÉ avec une quantité convertie
   re-basent maintenant le prix par le MÊME facteur que la quantité, via
   `domain/commercial_offer.py::convert_commercial_quantity_to_base_unit`
   (règle déterministe partagée, familles MASS/VOLUME uniquement — jamais
   une conversion devinée).
"""
from __future__ import annotations

from ladini.domain.commercial_offer import (
    CommercialQuantity,
    PackageDefinition,
    PackageStatus,
    PriceBasis,
    Pricing,
    Provenance,
    convert_commercial_quantity_to_base_unit,
    convertible_measurement_family,
)
from ladini.graphs.agents.market_coach.actions.common import normalize_quantity_to_kg
from ladini.graphs.agents.market_coach.domain.agro import AgronomyService
from ladini.graphs.agents.market_coach.domain.model import DomainContext
from ladini.graphs.agents.market_coach.domain.procurement import (
    ProcurementCreateRequestCommand,
    ProcurementService,
)
from ladini.graphs.agents.market_coach.domain.sales import (
    SalesPublishProductCommand,
    SalesService,
)

_CTX = DomainContext(
    user_id=None,
    phone="+22670000001",
    role="PRODUCER",
    language="fr",
    region=None,
    organization=None,
    permissions=frozenset(),
    tenant=None,
    timezone=None,
)


class TestCommercialOfferDomainPrimitives:
    def test_convertible_measurement_family_recognizes_mass_and_volume(self):
        assert convertible_measurement_family("TONNE") == "MASS"
        assert convertible_measurement_family("KG") == "MASS"
        assert convertible_measurement_family("LITRE") == "VOLUME"

    def test_convertible_measurement_family_refuses_packages_and_counts(self):
        # Un conditionnement (SAC/SACHET/PANIER) ou un dénombrement (TETE)
        # n'est PAS une famille convertible — famille singleton, voir
        # domain/pricing_tiers.py::unit_family.
        assert convertible_measurement_family("SAC") is None
        assert convertible_measurement_family("TETE") is None
        assert convertible_measurement_family(None) is None

    def test_convert_commercial_quantity_applies_the_deterministic_factor(self):
        result = convert_commercial_quantity_to_base_unit(
            CommercialQuantity(200, "TONNE"), "KG"
        )
        assert result == (200000.0, 1000.0)

    def test_convert_commercial_quantity_refuses_package_to_mass_conversion(self):
        # C'est exactement le garde-fou qui manquait : SAC n'a pas de
        # facteur de conversion déterministe vers KG.
        assert convert_commercial_quantity_to_base_unit(
            CommercialQuantity(3, "SAC"), "KG"
        ) is None

    def test_pricing_without_basis_is_never_execution_safe(self):
        price = Pricing(amount=500, basis=None, source=Provenance.USER_EXPLICIT)
        assert price.is_basis_known is False

    def test_pricing_with_llm_inferred_basis_is_not_execution_safe(self):
        price = Pricing(
            amount=500,
            basis=PriceBasis.PER_PACKAGE,
            basis_source=Provenance.LLM_INFERRED,
        )
        assert price.is_basis_known is False

    def test_pricing_with_user_explicit_basis_is_execution_safe(self):
        price = Pricing(
            amount=500,
            basis=PriceBasis.PER_PACKAGE,
            basis_source=Provenance.USER_EXPLICIT,
        )
        assert price.is_basis_known is True

    def test_package_definition_unknown_content_is_not_known(self):
        # Scénario exact "500 F le sachet" : conditionnement identifié, taille
        # inconnue.
        pkg = PackageDefinition(
            package_type="SACHET", status=PackageStatus.UNKNOWN, source=Provenance.USER_EXPLICIT
        )
        assert pkg.is_content_known is False

    def test_package_definition_with_known_content(self):
        pkg = PackageDefinition(
            package_type="SACHET",
            content_amount=0.5,
            content_unit="LITRE",
            status=PackageStatus.KNOWN,
            source=Provenance.USER_EXPLICIT,
        )
        assert pkg.is_content_known is True


class TestNormalizeQuantityToKgNeverGuessesAPackageWeight:
    def test_a_genuine_mass_unit_still_converts(self):
        assert normalize_quantity_to_kg(200, "TONNE") == (200000.0, "KG")

    def test_quintal_still_converts(self):
        assert normalize_quantity_to_kg(2, "QUINTAL") == (200.0, "KG")

    def test_a_sac_is_never_converted_to_a_guessed_kg_weight(self):
        """Avant ce correctif : `normalize_quantity_to_kg(3, "SAC")` renvoyait
        `(300.0, "KG")` — un poids de 100kg/sac deviné, jamais confirmé par
        le producteur. Après : le sac reste un sac, aucun poids n'est inventé."""
        qty, unit = normalize_quantity_to_kg(3, "SAC")
        assert (qty, unit) == (3.0, "SAC")

    def test_a_panier_is_never_converted_to_a_guessed_kg_weight(self):
        qty, unit = normalize_quantity_to_kg(5, "PANIER")
        assert (qty, unit) == (5.0, "PANIER")

    def test_a_charrette_is_never_converted_to_a_guessed_kg_weight(self):
        qty, unit = normalize_quantity_to_kg(1, "CHARRETTE")
        assert (qty, unit) == (1.0, "CHARRETTE")

    def test_an_unrecognized_unit_is_passed_through_not_silently_relabeled_kg(self):
        """Avant ce correctif : une unité inconnue devenait silencieusement
        "KG" (le texte original n'était même pas gardé). Après : elle
        traverse inchangée."""
        qty, unit = normalize_quantity_to_kg(2, "BIDON")
        assert (qty, unit) == (2.0, "BIDON")


class TestPublishProductRebasesPriceWithQuantity:
    """Le scénario EXACT de la mission : "200 tonnes de maïs à 500 000 F la
    tonne" ne doit JAMAIS devenir exécuté comme 500 000 F/KG."""

    def test_a_tonne_priced_offer_rebases_correctly_to_kg(self):
        service = SalesService(context=_CTX)
        command = SalesPublishProductCommand(
            producer_id="+22670000001",
            product="maïs",
            quantity=200,
            unit="TONNE",
            price=500000,
        )
        result = service.publish_product(command)

        assert result.tool_args["quantity_for_sale"] == 200000.0
        assert result.tool_args["unit"] == "KG"
        assert result.tool_args["price"] == 500.0, (
            f"200 tonnes à 500 000 F/tonne doit devenir 500 F/KG, pas rester "
            f"500 000 (F/KG à tort) : {result.tool_args['price']!r}"
        )

    def test_a_kg_priced_offer_is_unaffected_non_regression(self):
        service = SalesService(context=_CTX)
        command = SalesPublishProductCommand(
            producer_id="+22670000001",
            product="maïs",
            quantity=50,
            unit="KG",
            price=250,
        )
        result = service.publish_product(command)
        assert result.tool_args["quantity_for_sale"] == 50.0
        assert result.tool_args["price"] == 250.0

    def test_a_sac_priced_offer_keeps_its_own_price_and_unit_non_regression(self):
        """Non-régression : un prix par SAC (conditionnement, jamais converti
        maintenant) doit rester tel quel — ni le prix ni la quantité ne
        doivent être altérés par une conversion inventée."""
        service = SalesService(context=_CTX)
        command = SalesPublishProductCommand(
            producer_id="+22670000001",
            product="maïs",
            quantity=3,
            unit="SAC",
            price=15000,
        )
        result = service.publish_product(command)
        assert result.tool_args["quantity_for_sale"] == 3.0
        assert result.tool_args["unit"] == "SAC"
        assert result.tool_args["price"] == 15000.0


class TestDeclareCropCycleRebasesPriceWithQuantity:
    def test_a_tonne_priced_future_production_rebases_correctly(self):
        service = AgronomyService(context=_CTX)
        state = {"user_phone": "+22670000001"}
        payload = {
            "farm_id": "farm-1",
            "product": "maïs",
            "production_type": "CROP",
            "quantity": 200,
            "unit": "TONNE",
            "estimated_available_at": "2026-12-01",
            "price": 500000,
        }
        result = service.declare_crop_cycle(state, payload)
        assert result.tool_args["payload"]["quantity"] == 200000.0
        assert result.tool_args["payload"]["unit"] == "KG"
        assert result.tool_args["payload"]["price_per_unit"] == 500.0

    def test_livestock_quantity_is_never_touched_non_regression(self):
        """Le bétail (TETE) ne passe jamais par la conversion de masse — le
        prix ne doit donc jamais être re-basé non plus."""
        service = AgronomyService(context=_CTX)
        state = {"user_phone": "+22670000001"}
        payload = {
            "farm_id": "farm-1",
            "product": "boeufs",
            "production_type": "LIVESTOCK",
            "quantity": 20,
            "unit": "TETE",
            "estimated_available_at": "2026-12-01",
            "price": 450000,
        }
        result = service.declare_crop_cycle(state, payload)
        assert result.tool_args["payload"]["quantity"] == 20.0
        assert result.tool_args["payload"]["price_per_unit"] == 450000.0


class TestProcurementCreateRequestRebasesMaxPriceWithQuantity:
    def test_a_tonne_priced_rfq_ceiling_rebases_correctly(self):
        service = ProcurementService(context=_CTX)
        command = ProcurementCreateRequestCommand(
            phone="+22670000002",
            product="maïs",
            quantity=200,
            unit="TONNE",
            max_price=500000,
        )
        result = service.create_request(command)
        assert result.tool_args["qty"] == 200000.0
        assert result.tool_args["unit"] == "KG"
        assert result.tool_args["max_price"] == 500.0, (
            "un plafond de 500 000 F/tonne doit devenir 500 F/KG, pas rester "
            "500 000 F/KG par erreur"
        )
