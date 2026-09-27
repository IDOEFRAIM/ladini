"""domain/analytics/business_events.py — contract validation only. No table
exists yet (see module docstring) — these tests lock in the shape Phase C
must match, and the data-quality guards the mission's Phase P asks for."""
from __future__ import annotations

import pytest

from ladini.domain.analytics.business_events import BusinessEvent, BusinessEventName
from ladini.domain.analytics.metric_dictionary import Journey


def _base_kwargs(**overrides):
    kwargs = dict(
        event_name=BusinessEventName.RECURRING_OCCURRENCE_DELIVERED,
        journey=Journey.RECURRING,
        actor_type="SYSTEM",
        actor_id=None,
        buyer_id="buyer-1",
        producer_id=None,
        entity_type="RECURRING_NEED_OCCURRENCE",
        entity_id="occ-1",
        category_id=None,
        sub_category_id="sub-1",
        zone_id="zone-1",
        quantity=10.0,
        unit="KG",
        canonical_quantity=10.0,
        canonical_unit="KG",
        measurement_family="MASS",
        amount=None,
        currency="XOF",
        idempotency_key="RECURRING_OCCURRENCE_DELIVERED:occ-1",
    )
    kwargs.update(overrides)
    return kwargs


class TestBusinessEventContract:
    def test_a_well_formed_event_constructs_cleanly(self):
        event = BusinessEvent(**_base_kwargs())
        assert event.entity_id == "occ-1"

    def test_entity_id_is_required(self):
        with pytest.raises(ValueError):
            BusinessEvent(**_base_kwargs(entity_id=""))

    def test_idempotency_key_is_required(self):
        with pytest.raises(ValueError):
            BusinessEvent(**_base_kwargs(idempotency_key=""))

    def test_journey_must_match_the_event_names_declared_journey(self):
        with pytest.raises(ValueError):
            BusinessEvent(**_base_kwargs(journey=Journey.DIRECT))

    def test_quantity_required_events_reject_a_missing_quantity(self):
        with pytest.raises(ValueError):
            BusinessEvent(**_base_kwargs(quantity=None))

    def test_amount_required_events_reject_a_missing_amount(self):
        kwargs = _base_kwargs(
            event_name=BusinessEventName.TENDER_WINNER_SELECTED,
            journey=Journey.TENDER,
            entity_type="AUCTION",
            entity_id="auction-1",
            idempotency_key="TENDER_WINNER_SELECTED:auction-1",
            amount=None,
        )
        with pytest.raises(ValueError):
            BusinessEvent(**kwargs)

    def test_event_names_are_past_facts_not_imperative_verbs(self):
        # Mission: "un événement doit décrire un FAIT passé... jamais
        # PROCESS_DIGEST." Cheap structural proxy: no name starts with a
        # bare imperative verb stem used elsewhere in this codebase for
        # in-progress actions.
        forbidden_prefixes = ("PROCESS_", "HANDLE_", "RUN_", "DO_")
        for name in BusinessEventName:
            assert not name.value.startswith(forbidden_prefixes), name
