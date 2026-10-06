"""UPDATE SALES — chaque champ modifiable produit le MÊME modèle
transactionnel (2026-09-04, migration SALES, mandat §14 : "nous ne voulons
plus une architecture qui ne fonctionne proprement que pour la quantité").

Teste `quantity`/`unit`/`price`/`description`/`category_label` — PAS
`deadline` (n'existe pas pour SALES, contrairement à PROCUREMENT) —
chacun individuellement, vérifiant : v1 → v2, target v1 invalidé, contenu
correct après update."""
from __future__ import annotations

import pytest

from tests.architecture.test_sales_publish_draft_persistence import _draft

from ladini.graphs.agents.market_coach.core.confirmation_target import ConfirmationTarget
from ladini.graphs.agents.market_coach.domain.sales_publish_draft import (
    ConfirmSalesPublishDraft,
    SalesPublishOutcomeKind,
    UpdateSalesPublishDraft,
    apply_domain_action,
)

_ALWAYS_CLAIM = lambda key: True  # noqa: E731


@pytest.mark.parametrize(
    "field,initial_value,new_value",
    [
        ("quantity", 50.0, 120.0),
        ("unit", "KG", "TONNE"),
        ("price", 250.0, 400.0),
        ("description", "Récolte fraîche", "Récolte fraîche, bio, sans traitement"),
        ("category_label", "Céréales", "Légumineuses"),
    ],
)
def test_any_single_field_update_produces_the_same_transactional_model(
    field: str, initial_value, new_value
):
    v1 = _draft(draft_id=f"mf-{field}", **{field: initial_value})
    v2 = v1.with_updates(**{field: new_value})

    # v1 → v2 : exactement 1 bump de version, peu importe LE champ modifié.
    assert v2.version == v1.version + 1
    assert getattr(v2, field) == new_value

    # La cible de confirmation v1 est invalidée — STALE_TARGET, peu importe
    # le champ qui a changé.
    stale_target = ConfirmationTarget(draft_id=v1.draft_id, draft_version=v1.version)
    outcome = apply_domain_action(v2, ConfirmSalesPublishDraft(target=stale_target), claim=_ALWAYS_CLAIM)
    assert outcome.kind == SalesPublishOutcomeKind.STALE_TARGET

    # La NOUVELLE cible (v2) exécute avec la valeur mise à jour.
    fresh_target = ConfirmationTarget(draft_id=v2.draft_id, draft_version=v2.version)
    confirmed = apply_domain_action(v2, ConfirmSalesPublishDraft(target=fresh_target), claim=_ALWAYS_CLAIM)
    assert confirmed.kind == SalesPublishOutcomeKind.CONFIRMED_READY_FOR_EXECUTION
    assert getattr(confirmed.draft, field) == new_value


def test_pricing_tiers_update_also_produces_exactly_one_version_bump():
    """`pricing_tiers` (liste de dicts) suit la MÊME règle — pas de
    traitement spécial parce que c'est une liste plutôt qu'un scalaire."""
    v1 = _draft(draft_id="mf-tiers", pricing_tiers=[{"quantity": 1, "unit": "L", "price": 500}])
    v2 = v1.with_updates(pricing_tiers=[{"quantity": 1, "unit": "L", "price": 550}])
    assert v2.version == v1.version + 1
    assert v2.pricing_tiers[0]["price"] == 550


def test_two_consecutive_updates_on_different_fields_each_bump_once():
    """Une correction de quantité PUIS une correction de prix (2 champs
    différents, 2 tours) produit v1 → v2 → v3 — jamais une fusion silencieuse
    qui masquerait l'une des deux corrections dans le récap."""
    v1 = _draft(draft_id="mf-chain", quantity=50.0, price=250.0)
    v2 = v1.with_updates(quantity=120.0)
    v3 = v2.with_updates(price=400.0)

    assert (v1.version, v2.version, v3.version) == (1, 2, 3)
    assert v3.quantity == 120.0, "la correction de quantité (v1->v2) doit survivre à celle du prix (v2->v3)"
    assert v3.price == 400.0
