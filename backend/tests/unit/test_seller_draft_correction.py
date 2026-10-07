"""Correction du PRODUIT d'un brouillon de vente : champs compatibles conservés, champs liés au produit revalidés, confirmation précédente périmée."""
from __future__ import annotations

from ladini.graphs.agents.market_coach.domain.sales_publish_draft import (
    SalesPublishDraft,
    SalesPublishOutcomeKind,
    UpdateSalesPublishDraft,
    apply_domain_action,
    carry_compatible_fields,
    resolve_domain_action,
)

TIERS = [{"tier_id": "t1", "quantity": 0.5, "unit": "L", "price": 100.0, "packaging": "sachet"}]


def _draft(**kw):
    base = {"product": "tomate", "quantity": 300.0, "unit": "KG", "price": 250.0}
    base.update(kw)
    return SalesPublishDraft.new("d1", **base)


def test_a_product_correction_keeps_the_physical_values_and_revalidates_the_product_bound_ones():
    draft = _draft(pricing_tiers=TIERS)
    out = apply_domain_action(draft, UpdateSalesPublishDraft(fields={"product": "oignon"}, correction=True))
    new = out.draft
    assert new.product == "oignon" and new.quantity == 300.0 and new.unit == "KG", "300 kg reste valide : c'est la même marchandise"
    assert new.price is None and not new.pricing_tiers and new.commercial_offer is None, "le prix et le palier « sachet 500 ml » ne suivent PAS le nouveau produit"
    assert new.version == draft.version + 1, "nouvelle version : l'ancienne confirmation est périmée"
    assert out.kind == SalesPublishOutcomeKind.NEEDS_MORE_INFO, "il reste à redonner le prix"


def test_without_the_correction_flag_a_product_change_still_purges_everything_like_before():
    out = apply_domain_action(_draft(), UpdateSalesPublishDraft(fields={"product": "miel"}))
    assert out.draft.product == "miel" and out.draft.quantity is None and out.draft.price is None


def test_values_restated_in_the_correction_are_never_overwritten():
    out = apply_domain_action(_draft(), UpdateSalesPublishDraft(fields={"product": "oignon", "quantity": 280.0}, correction=True))
    assert out.draft.quantity == 280.0 and out.draft.product == "oignon"


def test_the_same_product_is_not_a_correction_of_anything():
    draft = _draft()
    assert carry_compatible_fields(draft, {"product": "Tomate"}) == {"product": "Tomate"}


def test_quantity_and_price_corrections_change_only_that_field():
    draft = _draft()
    q = apply_domain_action(draft, UpdateSalesPublishDraft(fields={"quantity": 250.0}, correction=True)).draft
    assert q.quantity == 250.0 and q.price == 250.0 and q.product == "tomate" and q.version == draft.version + 1
    p = apply_domain_action(draft, UpdateSalesPublishDraft(fields={"price": 300.0}, correction=True)).draft
    assert p.price == 300.0 and p.quantity == 300.0


def test_the_correction_flag_reaches_the_domain_action_but_is_not_a_draft_field():
    action = resolve_domain_action(
        interpreted_event="NEW_TASK", extracted_entities={"product": "oignon", "is_correction": True}, pending_target=None
    )
    assert isinstance(action, UpdateSalesPublishDraft) and action.correction is True and "is_correction" not in action.fields
