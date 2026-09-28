"""`domain/commercial_offer.py::validate_commercial_offer` — Phase 13/25 du
mandat "Commercial Quantity & Pricing Domain Hardening" : matrice de test
couvrant les scénarios canoniques de la mission (Phase 25, scénarios 1-14).

Ce validateur n'est PAS encore câblé dans le pipeline conversationnel live
(`confirmation_gate.py`/`SalesPublishDraft`) — voir
`docs/domain/COMMERCIAL_QUANTITY_PRICING_MODEL.md` §Phase B pour le plan.
Ce fichier prouve que son CONTRAT est correct et stable, en isolation,
avant tout câblage — condition nécessaire avant de le brancher sur un
chemin qui affecte chaque conversation."""
from __future__ import annotations

from ladini.domain.commercial_offer import (
    InventoryQuantity,
    PackageDefinition,
    PackageStatus,
    PriceBasis,
    Pricing,
    Provenance,
    validate_commercial_offer,
)

_EXPLICIT = Provenance.USER_EXPLICIT


class TestScenario1SimplePerUnitPriceIsValid:
    """1. 50 L lait, 500/L -> valid."""

    def test_valid(self):
        result = validate_commercial_offer(
            inventory_quantity=InventoryQuantity(50, "LITRE", source=_EXPLICIT),
            pricing=Pricing(
                500, PriceBasis.PER_BASE_UNIT, source=_EXPLICIT, basis_source=_EXPLICIT
            ),
        )
        assert result.status == "VALID"
        assert result.is_valid is True


class TestScenario2PackagePriceWithUnknownContentRequiresClarification:
    """2. 50 L lait, 500/sachet, package unknown -> clarification."""

    def test_incomplete_with_package_clarification_question(self):
        result = validate_commercial_offer(
            inventory_quantity=InventoryQuantity(50, "LITRE", source=_EXPLICIT),
            pricing=Pricing(
                500, PriceBasis.PER_PACKAGE, source=_EXPLICIT, basis_source=_EXPLICIT
            ),
            package=PackageDefinition(
                package_type="SACHET", status=PackageStatus.UNKNOWN, source=_EXPLICIT
            ),
        )
        assert result.status == "INCOMPLETE"
        assert "package_content_amount" in result.missing_fields
        assert result.clarification_question is not None
        assert "conditionnement" in result.clarification_question.lower()

    def test_no_package_object_at_all_is_also_incomplete(self):
        """Même conclusion si `package` n'est même pas fourni (None) — le
        validateur ne doit jamais supposer NOT_REQUIRED par défaut quand
        `basis == PER_PACKAGE`."""
        result = validate_commercial_offer(
            inventory_quantity=InventoryQuantity(50, "LITRE", source=_EXPLICIT),
            pricing=Pricing(
                500, PriceBasis.PER_PACKAGE, source=_EXPLICIT, basis_source=_EXPLICIT
            ),
            package=None,
        )
        assert result.status == "INCOMPLETE"
        assert "package_content_amount" in result.missing_fields


class TestScenario3PackagePriceWithKnownContentIsValid:
    """3. 50 L lait, 500/sachet, 0.5L/package -> valid."""

    def test_valid(self):
        result = validate_commercial_offer(
            inventory_quantity=InventoryQuantity(50, "LITRE", source=_EXPLICIT),
            pricing=Pricing(
                500, PriceBasis.PER_PACKAGE, source=_EXPLICIT, basis_source=_EXPLICIT
            ),
            package=PackageDefinition(
                package_type="SACHET",
                content_amount=0.5,
                content_unit="LITRE",
                status=PackageStatus.KNOWN,
                source=_EXPLICIT,
            ),
        )
        assert result.status == "VALID"


class TestScenario4TonnePriceIsValidAndNormalizable:
    """4. 200 TONNE maïs, 500000/TONNE -> valid, normalisé 500/KG (voir
    domain/commercial_offer.py::convert_commercial_quantity_to_base_unit,
    testé séparément dans test_commercial_offer_price_basis_hardening.py —
    ce test-ci vérifie seulement que la VALIDATION elle-même passe)."""

    def test_valid(self):
        result = validate_commercial_offer(
            inventory_quantity=InventoryQuantity(200, "TONNE", source=_EXPLICIT),
            pricing=Pricing(
                500000, PriceBasis.PER_BASE_UNIT, source=_EXPLICIT, basis_source=_EXPLICIT
            ),
        )
        assert result.status == "VALID"


class TestScenario5PriceWithoutAnyBasisRequiresClarification:
    """5. 200 TONNE maïs, 500000 sans base -> clarification (sauf contexte de
    question qui définit déjà la base — voir Phase 26, testé séparément)."""

    def test_incomplete_when_basis_is_entirely_absent(self):
        result = validate_commercial_offer(
            inventory_quantity=InventoryQuantity(200, "TONNE", source=_EXPLICIT),
            pricing=Pricing(500000, basis=None),
        )
        assert result.status == "INCOMPLETE"
        assert "price_basis" in result.missing_fields
        assert result.clarification_question is not None

    def test_incomplete_when_basis_provenance_is_llm_inferred_not_confirmed(self):
        """Règle centrale du mandat : LLM_INFERRED sur une donnée financière
        critique -> NO BUSINESS WRITE, même si `basis` a une VALEUR."""
        result = validate_commercial_offer(
            inventory_quantity=InventoryQuantity(200, "TONNE", source=_EXPLICIT),
            pricing=Pricing(
                500000,
                PriceBasis.PER_BASE_UNIT,
                source=_EXPLICIT,
                basis_source=Provenance.LLM_INFERRED,
            ),
        )
        assert result.status == "INCOMPLETE"
        assert "price_basis" in result.missing_fields


class TestScenario6LivestockPerHeadIsValid:
    """6. 20 TETE bovins, 450000/TETE -> valid."""

    def test_valid(self):
        result = validate_commercial_offer(
            inventory_quantity=InventoryQuantity(20, "TETE", source=_EXPLICIT),
            pricing=Pricing(
                450000, PriceBasis.PER_BASE_UNIT, source=_EXPLICIT, basis_source=_EXPLICIT
            ),
        )
        assert result.status == "VALID"


class TestScenario9And10PackagePricingWithKnownCaseSize:
    """9. 100 KG tomates, 12000/CAISSE 25KG -> valid (contenu connu)."""

    def test_valid_when_case_content_is_known(self):
        result = validate_commercial_offer(
            inventory_quantity=InventoryQuantity(100, "KG", source=_EXPLICIT),
            pricing=Pricing(
                12000, PriceBasis.PER_PACKAGE, source=_EXPLICIT, basis_source=_EXPLICIT
            ),
            package=PackageDefinition(
                package_type="CAISSE",
                content_amount=25,
                content_unit="KG",
                status=PackageStatus.KNOWN,
                source=_EXPLICIT,
            ),
        )
        assert result.status == "VALID"

    def test_incomplete_when_case_content_is_unknown(self):
        """8. 100 KG tomates, 12000/CAISSE, package unknown -> clarification."""
        result = validate_commercial_offer(
            inventory_quantity=InventoryQuantity(100, "KG", source=_EXPLICIT),
            pricing=Pricing(
                12000, PriceBasis.PER_PACKAGE, source=_EXPLICIT, basis_source=_EXPLICIT
            ),
            package=PackageDefinition(
                package_type="CAISSE", status=PackageStatus.UNKNOWN, source=_EXPLICIT
            ),
        )
        assert result.status == "INCOMPLETE"
        assert "package_content_amount" in result.missing_fields


class TestScenario11TotalLotPriceIsValidWhenExplicitlySelected:
    """11. price total lot -> valid quand TOTAL_LOT explicitement sélectionné."""

    def test_valid(self):
        result = validate_commercial_offer(
            inventory_quantity=InventoryQuantity(200, "TONNE", source=_EXPLICIT),
            pricing=Pricing(
                50000000, PriceBasis.TOTAL_LOT, source=_EXPLICIT, basis_source=_EXPLICIT
            ),
        )
        assert result.status == "VALID"


class TestScenario12NegativeQuantityIsInvalid:
    """12. negative quantity -> invalid."""

    def test_invalid(self):
        result = validate_commercial_offer(
            inventory_quantity=InventoryQuantity(-5, "KG", source=_EXPLICIT),
            pricing=Pricing(
                500, PriceBasis.PER_BASE_UNIT, source=_EXPLICIT, basis_source=_EXPLICIT
            ),
        )
        assert result.status == "INVALID"
        assert "quantity_not_positive" in result.conflicts


class TestScenario13ZeroPriceIsInvalid:
    """13. zero price -> invalid."""

    def test_invalid(self):
        result = validate_commercial_offer(
            inventory_quantity=InventoryQuantity(50, "KG", source=_EXPLICIT),
            pricing=Pricing(
                0, PriceBasis.PER_BASE_UNIT, source=_EXPLICIT, basis_source=_EXPLICIT
            ),
        )
        assert result.status == "INVALID"
        assert "price_amount_not_positive" in result.conflicts


class TestMissingQuantityOrPriceEntirely:
    def test_missing_quantity_is_incomplete(self):
        result = validate_commercial_offer(
            inventory_quantity=None,
            pricing=Pricing(
                500, PriceBasis.PER_BASE_UNIT, source=_EXPLICIT, basis_source=_EXPLICIT
            ),
        )
        assert result.status == "INCOMPLETE"
        assert "quantity" in result.missing_fields

    def test_missing_price_entirely_is_incomplete(self):
        result = validate_commercial_offer(
            inventory_quantity=InventoryQuantity(50, "KG", source=_EXPLICIT),
            pricing=None,
        )
        assert result.status == "INCOMPLETE"
        assert "price" in result.missing_fields


class TestInvalidTakesPrecedenceOverIncomplete:
    def test_a_conflict_is_reported_even_if_other_fields_are_also_missing(self):
        """Un prix à 0 ET une base absente : le conflit (donnée présente
        mais invalide) prime sur le simple manque — INVALID, pas INCOMPLETE."""
        result = validate_commercial_offer(
            inventory_quantity=InventoryQuantity(50, "KG", source=_EXPLICIT),
            pricing=Pricing(0, basis=None),
        )
        assert result.status == "INVALID"
        assert "price_amount_not_positive" in result.conflicts
