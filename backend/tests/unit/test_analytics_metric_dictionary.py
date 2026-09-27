"""domain/analytics/metric_dictionary.py — schema validation (mission 19.G/H)
and the "no hardcoded product" guarantee (19.F).
"""
from __future__ import annotations

import dataclasses

import pytest

from ladini.domain.analytics.metric_dictionary import (
    METRICS,
    AggregationType,
    Journey,
    Reconstructibility,
    get_metric,
    list_metrics_for_journey,
)

_RATIO_TYPES = {AggregationType.WEIGHTED_RATIO, AggregationType.DURATION_AVG}


class TestRegistryIntegrity:
    def test_registry_is_not_empty(self):
        assert len(METRICS) >= 30

    def test_every_key_matches_its_own_name(self):
        for key, metric in METRICS.items():
            assert key == metric.name

    def test_get_metric_raises_on_unknown_name(self):
        with pytest.raises(KeyError):
            get_metric("does_not_exist")

    def test_list_metrics_for_journey_only_returns_that_journey(self):
        for journey in Journey:
            for metric in list_metrics_for_journey(journey):
                assert metric.journey == journey


class TestEverySingleMetricIsFullySpecified:
    """Mission 19.H: each canonical metric has description, numerator/
    denominator if it's a ratio, dimensions, journey, source entities."""

    @pytest.mark.parametrize("name", sorted(METRICS.keys()))
    def test_metric_is_fully_specified(self, name):
        metric = METRICS[name]
        assert metric.description, f"{name}: missing description"
        assert metric.business_definition, f"{name}: missing business_definition"
        assert isinstance(metric.journey, Journey)
        assert isinstance(metric.aggregation_type, AggregationType)
        assert metric.numerator, f"{name}: missing numerator"
        if metric.aggregation_type in _RATIO_TYPES:
            assert metric.denominator, f"{name}: ratio metric missing denominator"
        assert metric.unit_behavior, f"{name}: missing unit_behavior"
        assert len(metric.supported_dimensions) > 0, f"{name}: missing supported_dimensions"
        assert len(metric.supported_time_windows) > 0, f"{name}: missing supported_time_windows"
        assert len(metric.source_entities) > 0, f"{name}: missing source_entities"
        assert isinstance(metric.reconstructible_historically, Reconstructibility)
        assert metric.reconstructible_note, f"{name}: missing reconstructible_note"
        if metric.alias_of is not None:
            assert metric.alias_of in METRICS, f"{name}: alias_of points to an unregistered metric"


class TestNorthStarIsRigorouslyDefined:
    def test_north_star_is_a_weighted_ratio_not_an_average(self):
        metric = get_metric("successful_procurement_rate")
        assert metric.aggregation_type == AggregationType.WEIGHTED_RATIO
        assert "delivered" in metric.business_definition.lower()

    def test_north_star_denominator_is_needs_created(self):
        metric = get_metric("successful_procurement_rate")
        assert "needs_created" in metric.denominator


class TestGmvIsNeverACatchAllName:
    def test_potential_confirmed_delivered_are_distinct_metrics(self):
        potential = get_metric("potential_gmv")
        confirmed = get_metric("confirmed_gmv")
        delivered = get_metric("delivered_gmv")
        assert potential.alias_of is None
        assert confirmed.alias_of is None
        assert delivered.alias_of is None
        assert potential.business_definition != confirmed.business_definition
        assert confirmed.business_definition != delivered.business_definition

    def test_journey_scoped_gmv_names_are_aliases_not_new_definitions(self):
        for name in ("direct_gmv", "tender_gmv", "recurring_gmv"):
            metric = get_metric(name)
            assert metric.alias_of == "confirmed_gmv"


class TestNoHardcodedProduct:
    """Mission 19.F: the engine must work off category_id/sub_category_id/
    admin config, never a literal product name."""

    _FORBIDDEN_PRODUCT_WORDS = (
        "tomate", "oignon", "lait", "chevre", "chèvre", "boeuf", "bœuf",
        "poulet", "mangue", "riz", "poisson",
    )

    def test_no_metric_definition_mentions_a_literal_product(self):
        for name, metric in METRICS.items():
            haystack = " ".join(
                [metric.description, metric.business_definition, metric.unit_behavior]
            ).lower()
            for word in self._FORBIDDEN_PRODUCT_WORDS:
                assert word not in haystack, f"{name}: hardcodes product name {word!r}"

    def test_dimensions_are_taxonomy_keyed_never_product_keyed(self):
        allowed_dimension_roots = {"date", "zone", "category", "sub_category", "journey", "buyer"}
        for name, metric in METRICS.items():
            for dim in metric.supported_dimensions:
                assert dim in allowed_dimension_roots, f"{name}: unexpected dimension {dim!r}"

    def test_metric_definition_has_no_product_field(self):
        field_names = {f.name for f in dataclasses.fields(get_metric("needs_created"))}
        assert "product" not in field_names
        assert "product_name" not in field_names


class TestReconstructibilityIsHonest:
    def test_direct_search_metrics_are_not_fabricated_as_reconstructible(self):
        # Mission: "ne fabrique aucun historique manquant" — no search log
        # exists anywhere in the codebase (Phase A finding), so these must
        # never claim YES.
        for name in ("direct_searches", "direct_search_success_rate", "direct_search_to_order_rate"):
            assert get_metric(name).reconstructible_historically == Reconstructibility.NO

    def test_recurring_quantity_metrics_are_reconstructible(self):
        for name in (
            "recurring_requested_quantity",
            "recurring_matched_quantity",
            "recurring_confirmed_quantity",
            "recurring_coverage_rate",
        ):
            assert get_metric(name).reconstructible_historically == Reconstructibility.YES

    def test_recurring_delivered_quantity_is_honestly_not_reconstructible(self):
        # Phase C finding: RecurringNeedOccurrence.quantity_delivered has
        # zero writers anywhere in the codebase — a dead column. Phase B
        # wrongly assumed YES; this must never silently regress back to a
        # false claim.
        metric = get_metric("recurring_delivered_quantity")
        assert metric.reconstructible_historically == Reconstructibility.NO
        assert "zero writers" in metric.business_definition or "dead column" in metric.reconstructible_note


class TestDeliveredVsFulfilledContract:
    """Phase C contract test (mission 1.A): DELIVERED and FULFILLED are not
    synonyms — locks in the order_type-conditional rule so it can't quietly
    drift back to a flat 'IN (DELIVERED, FULFILLED)' check that ignores
    RECURRING_SUPPLY's RECEIVED requirement."""

    def test_north_star_flags_the_recurring_supply_split_not_a_flat_synonym_rule(self):
        metric = get_metric("successful_procurement_rate")
        assert "order_group_id" in metric.reconstructible_note
        assert metric.reconstructible_historically == Reconstructibility.PARTIAL

    def test_delivered_gmv_documents_received_not_delivered_for_recurring(self):
        metric = get_metric("delivered_gmv")
        assert "RECEIVED" in metric.business_definition
        assert "RECURRING_SUPPLY" in metric.business_definition

    def test_delivered_gmv_does_not_claim_full_yes_because_of_recurring(self):
        assert get_metric("delivered_gmv").reconstructible_historically == Reconstructibility.PARTIAL
