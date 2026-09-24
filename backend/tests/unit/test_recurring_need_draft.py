"""`RecurringNeedDraft` — machine à état CAS (même contrat que `ProcurementDraft`, voir son propre
fichier de tests d'architecture pour la preuve de concurrence réelle sur `compare_and_swap`).
Ici : logique Python pure du domaine (aucune I/O), y compris le rejet d'une confirmation périmée."""
from __future__ import annotations

from ladini.graphs.agents.market_coach.core.confirmation_target import (
    ConfirmationTarget,
)
from ladini.graphs.agents.market_coach.domain.recurring_need_draft import (
    CancelRecurringNeedDraft,
    ConfirmRecurringNeedDraft,
    IllegalDraftTransition,
    NoRecurringNeedAction,
    RecurringNeedDraft,
    RecurringNeedDraftStatus,
    RecurringNeedOutcomeKind,
    UpdateRecurringNeedDraft,
    apply_domain_action,
    resolve_domain_action,
)


def _always_claim(_key: str) -> bool:
    return True


def _never_claim(_key: str) -> bool:
    return False


# ── construction / complétude ────────────────────────────────────────────

def test_new_draft_is_incomplete_without_required_fields():
    d = RecurringNeedDraft.new("d1", product="tomate")
    assert not d.is_complete()
    assert "quantity" in d.missing_fields()


def test_a_daily_need_with_all_required_fields_is_complete():
    d = RecurringNeedDraft.new("d1", product="tomate", quantity=40, unit="KG", recurrence_type="DAILY")
    assert d.is_complete()
    assert d.missing_fields() == []


def test_starts_at_is_never_required():
    d = RecurringNeedDraft.new("d1", product="tomate", quantity=40, unit="KG", recurrence_type="DAILY")
    assert "starts_at" not in d.missing_fields()


def test_weekly_days_without_any_day_is_incomplete():
    d = RecurringNeedDraft.new("d1", product="oignon", quantity=20, unit="KG", recurrence_type="WEEKLY_DAYS")
    assert not d.is_complete()
    assert "weekly_days" in d.missing_fields()


def test_weekly_days_with_days_listed_is_complete():
    d = RecurringNeedDraft.new(
        "d1", product="oignon", quantity=20, unit="KG", recurrence_type="WEEKLY_DAYS", weekly_days=[1, 3, 5]
    )
    assert d.is_complete()


# ── chantier multi-produits (2026-09-23) ─────────────────────────────────

def test_a_complete_additional_item_does_not_block_completion():
    d = RecurringNeedDraft.new(
        "d1", product="tomate", quantity=10, unit="KG", recurrence_type="DAILY",
        additional_items=[{"product": "oignon", "quantity": 20, "unit": "KG"}],
    )
    assert d.is_complete()
    assert d.missing_fields() == []


def test_an_incomplete_additional_item_blocks_completion():
    """Filet de sécurité défensif — `_clean_additional_items` (flow) est censé filtrer un item
    partiel avant qu'il n'atteigne le draft, mais le domaine ne fait jamais confiance à cet
    invariant sans le vérifier lui-même."""
    d = RecurringNeedDraft.new(
        "d1", product="tomate", quantity=10, unit="KG", recurrence_type="DAILY",
        additional_items=[{"product": "oignon", "quantity": None, "unit": "KG"}],
    )
    assert not d.is_complete()
    assert "quantity" in d.missing_fields()


def test_render_summary_lists_every_product_not_only_the_first():
    d = RecurringNeedDraft.new(
        "d1", product="tomate", quantity=10, unit="KG", recurrence_type="DAILY",
        additional_items=[{"product": "oignon", "quantity": 20, "unit": "KG"}],
    )
    summary = d.render_summary()
    assert "Tomate : 10 KG" in summary
    assert "Oignon : 20 KG" in summary


def test_with_updates_replaces_the_whole_additional_items_list():
    d = RecurringNeedDraft.new(
        "d1", product="tomate", quantity=10, unit="KG", recurrence_type="DAILY",
        additional_items=[{"product": "oignon", "quantity": 20, "unit": "KG"}],
    )
    d2 = d.with_updates(additional_items=[{"product": "laitue", "quantity": 5, "unit": "KG"}])
    assert d2.version == 2
    assert d2.additional_items == [{"product": "laitue", "quantity": 5, "unit": "KG"}]


def test_execution_payload_includes_additional_items_when_present():
    d = RecurringNeedDraft.new(
        "d1", product="tomate", quantity=10, unit="KG", recurrence_type="DAILY",
        additional_items=[{"product": "oignon", "quantity": 20, "unit": "KG"}],
    )
    payload = d.execution_payload()
    assert payload["additional_items"] == [{"product": "oignon", "quantity": 20, "unit": "KG"}]


def test_execution_payload_omits_additional_items_when_absent():
    d = RecurringNeedDraft.new("d1", product="tomate", quantity=10, unit="KG", recurrence_type="DAILY")
    assert "additional_items" not in d.execution_payload()


def test_to_dict_and_from_dict_round_trip_additional_items():
    d = RecurringNeedDraft.new(
        "d1", product="tomate", quantity=10, unit="KG", recurrence_type="DAILY",
        additional_items=[{"product": "oignon", "quantity": 20, "unit": "KG"}],
    )
    restored = RecurringNeedDraft.from_dict(d.to_dict())
    assert restored.additional_items == [{"product": "oignon", "quantity": 20, "unit": "KG"}]


# ── with_updates : version bump seulement si un champ change ────────────

def test_with_updates_bumps_version_when_a_field_actually_changes():
    d = RecurringNeedDraft.new("d1", product="tomate", quantity=40, unit="KG", recurrence_type="DAILY")
    d2 = d.with_updates(quantity=25)
    assert d2.version == 2 and d2.quantity == 25


def test_with_updates_is_a_noop_when_the_value_is_unchanged():
    d = RecurringNeedDraft.new("d1", product="tomate", quantity=40, unit="KG", recurrence_type="DAILY")
    d2 = d.with_updates(quantity=40)
    assert d2 is d


def test_with_updates_on_a_finalized_draft_raises():
    d = RecurringNeedDraft.new("d1", product="tomate", quantity=40, unit="KG", recurrence_type="DAILY")
    cancelled = d.with_status(RecurringNeedDraftStatus.CANCELLED)
    try:
        cancelled.with_updates(quantity=25)
        raise AssertionError("devait lever IllegalDraftTransition")
    except IllegalDraftTransition:
        pass


# ── resolve_domain_action ────────────────────────────────────────────────

def test_resolve_domain_action_maps_update_event_to_structured_fields():
    action = resolve_domain_action(
        interpreted_event="UPDATE", extracted_entities={"quantity": 40, "unit": "KG", "noise": "x"}, pending_target=None
    )
    assert isinstance(action, UpdateRecurringNeedDraft)
    assert action.fields == {"quantity": 40, "unit": "KG"}


def test_resolve_domain_action_maps_confirm_event():
    action = resolve_domain_action(
        interpreted_event="CONFIRM", extracted_entities={}, pending_target={"draft_id": "d1", "draft_version": 1}
    )
    assert isinstance(action, ConfirmRecurringNeedDraft)
    assert action.target == ConfirmationTarget(draft_id="d1", draft_version=1)


def test_resolve_domain_action_unknown_event_is_a_no_op():
    action = resolve_domain_action(interpreted_event="SOMETHING_ELSE", extracted_entities={}, pending_target=None)
    assert isinstance(action, NoRecurringNeedAction)


# ── apply_domain_action : création progressive ───────────────────────────

def test_first_update_with_no_prior_draft_creates_one():
    outcome = apply_domain_action(None, UpdateRecurringNeedDraft(fields={"product": "tomate", "quantity": 40}))
    assert outcome.kind == RecurringNeedOutcomeKind.NEEDS_MORE_INFO
    assert outcome.draft.product == "tomate"


def test_completing_the_last_required_field_reaches_draft_updated():
    d = RecurringNeedDraft.new("d1", product="tomate", quantity=40, unit="KG")
    outcome = apply_domain_action(d, UpdateRecurringNeedDraft(fields={"recurrence_type": "DAILY"}))
    assert outcome.kind == RecurringNeedOutcomeKind.DRAFT_UPDATED
    assert outcome.draft.is_complete()


# ── confirmation : chemin heureux, cible périmée, claim perdu ───────────

def test_confirm_with_a_matching_target_moves_to_executing():
    d = RecurringNeedDraft.new("d1", product="tomate", quantity=40, unit="KG", recurrence_type="DAILY")
    target = ConfirmationTarget(draft_id=d.draft_id, draft_version=d.version)
    outcome = apply_domain_action(d, ConfirmRecurringNeedDraft(target=target), claim=_always_claim)
    assert outcome.kind == RecurringNeedOutcomeKind.CONFIRMED_READY_FOR_EXECUTION
    assert outcome.draft.status == RecurringNeedDraftStatus.EXECUTING
    assert outcome.draft.version == d.version + 1


def test_a_stale_confirmation_target_is_rejected():
    """Le coeur du mandat §9/§16 : une confirmation qui vise une version périmée (l'utilisateur a
    corrigé un champ après avoir reçu le récapitulatif) est refusée, jamais silencieusement acceptée."""
    d = RecurringNeedDraft.new("d1", product="tomate", quantity=40, unit="KG", recurrence_type="DAILY")
    stale_target = ConfirmationTarget(draft_id=d.draft_id, draft_version=d.version)  # v1
    updated = d.with_updates(quantity=25)  # -> v2

    outcome = apply_domain_action(updated, ConfirmRecurringNeedDraft(target=stale_target), claim=_always_claim)

    assert outcome.kind == RecurringNeedOutcomeKind.STALE_TARGET
    assert outcome.draft.version == 2
    assert outcome.draft.status == RecurringNeedDraftStatus.DRAFT  # jamais passé à EXECUTING


def test_confirming_an_incomplete_draft_asks_for_the_missing_field_instead():
    d = RecurringNeedDraft.new("d1", product="tomate", quantity=40, unit="KG")  # recurrence_type manquant
    target = ConfirmationTarget(draft_id=d.draft_id, draft_version=d.version)
    outcome = apply_domain_action(d, ConfirmRecurringNeedDraft(target=target), claim=_always_claim)
    assert outcome.kind == RecurringNeedOutcomeKind.NEEDS_MORE_INFO


def test_a_lost_confirmation_claim_race_is_reported_as_already_executing():
    """Deux workers confirment la MÊME version au même instant : un seul gagne le claim Redis, l'autre
    ne relance jamais l'exécution."""
    d = RecurringNeedDraft.new("d1", product="tomate", quantity=40, unit="KG", recurrence_type="DAILY")
    target = ConfirmationTarget(draft_id=d.draft_id, draft_version=d.version)
    outcome = apply_domain_action(d, ConfirmRecurringNeedDraft(target=target), claim=_never_claim)
    assert outcome.kind == RecurringNeedOutcomeKind.ALREADY_EXECUTING
    assert outcome.draft.status == RecurringNeedDraftStatus.DRAFT  # inchangé, pas d'exécution relancée


def test_confirming_an_already_executed_draft_is_reported_not_repeated():
    d = RecurringNeedDraft.new("d1", product="tomate", quantity=40, unit="KG", recurrence_type="DAILY")
    executing = d._confirm_to_executing()
    executed = executing.with_status(RecurringNeedDraftStatus.EXECUTED)
    target = ConfirmationTarget(draft_id=d.draft_id, draft_version=d.version)
    outcome = apply_domain_action(executed, ConfirmRecurringNeedDraft(target=target), claim=_always_claim)
    assert outcome.kind == RecurringNeedOutcomeKind.ALREADY_EXECUTED


def test_confirming_a_cancelled_draft_is_finalized():
    d = RecurringNeedDraft.new("d1", product="tomate", quantity=40, unit="KG", recurrence_type="DAILY")
    cancelled = d.with_status(RecurringNeedDraftStatus.CANCELLED)
    target = ConfirmationTarget(draft_id=d.draft_id, draft_version=d.version)
    outcome = apply_domain_action(cancelled, ConfirmRecurringNeedDraft(target=target), claim=_always_claim)
    assert outcome.kind == RecurringNeedOutcomeKind.DRAFT_FINALIZED


# ── rejet / annulation ────────────────────────────────────────────────────
#
# Anomalie résiduelle corrigée (2026-09-24) : contrairement à PROCUREMENT/PREORDER (dont le
# REJECT pendant confirmation est un rejet "doux" — le draft reste DRAFT, l'utilisateur peut
# continuer à le corriger), CREATE_RECURRING_NEED n'a pas cette notion : un "non"/"annuler" est
# TOUJOURS définitif. `resolve_domain_action` construisait auparavant un
# `RejectRecurringNeedConfirmation` qui laissait le draft orphelin en `DRAFT` (jamais confirmable
# ensuite faute de `pending_interaction`, mais jamais formellement `CANCELLED` non plus) — REJECT
# route maintenant vers le `CancelRecurringNeedDraft` déjà existant, réutilisé tel quel.

def test_resolve_domain_action_maps_reject_event_to_cancel_not_a_soft_reject():
    """« non » ET « annuler » sont le MÊME `interpreted_event="REJECT"` côté interpréteur
    (`_REJECT_EXACT_PHRASES`) — le domaine ne distingue pas les deux mots, seulement l'événement."""
    action = resolve_domain_action(interpreted_event="REJECT", extracted_entities={}, pending_target=None)
    assert isinstance(action, CancelRecurringNeedDraft)


def test_a_reject_event_cancels_the_draft_definitively():
    d = RecurringNeedDraft.new("d1", product="tomate", quantity=40, unit="KG", recurrence_type="DAILY")
    action = resolve_domain_action(interpreted_event="REJECT", extracted_entities={}, pending_target=None)
    outcome = apply_domain_action(d, action)
    assert outcome.kind == RecurringNeedOutcomeKind.CANCELLED
    assert outcome.draft.status == RecurringNeedDraftStatus.CANCELLED, (
        "un rejet doit RÉELLEMENT persister le statut CANCELLED — jamais laisser le draft "
        "orphelin en DRAFT (l'anomalie corrigée ici)"
    )


def test_cancelling_a_draft_moves_it_to_cancelled():
    d = RecurringNeedDraft.new("d1", product="tomate", quantity=40, unit="KG", recurrence_type="DAILY")
    outcome = apply_domain_action(d, CancelRecurringNeedDraft())
    assert outcome.kind == RecurringNeedOutcomeKind.CANCELLED
    assert outcome.draft.status == RecurringNeedDraftStatus.CANCELLED


def test_cancelling_an_already_cancelled_draft_is_finalized_not_re_cancelled():
    """Idempotence d'un REJECT rejoué (webhook redélivré) contre un draft DÉJÀ `CANCELLED` —
    jamais une 2ᵉ transition (qui lèverait `IllegalDraftTransition`, `CANCELLED` n'ayant aucune
    transition sortante), jamais un crash : même repli `DRAFT_FINALIZED` que pour un CONFIRM
    tardif sur un draft déjà terminal."""
    d = RecurringNeedDraft.new("d1", product="tomate", quantity=40, unit="KG", recurrence_type="DAILY")
    cancelled = apply_domain_action(d, CancelRecurringNeedDraft()).draft
    replay_outcome = apply_domain_action(cancelled, CancelRecurringNeedDraft())
    assert replay_outcome.kind == RecurringNeedOutcomeKind.DRAFT_FINALIZED
    assert replay_outcome.draft.status == RecurringNeedDraftStatus.CANCELLED


def test_confirming_a_cancelled_draft_never_reactivates_it():
    """Confirmation tardive ("ok" arrivé après coup) contre un draft déjà `CANCELLED` — jamais
    une exécution, jamais une réactivation. Complète `test_confirming_a_cancelled_draft_is_
    finalized` ci-dessus en vérifiant explicitement le statut final, pas seulement le kind."""
    d = RecurringNeedDraft.new("d1", product="tomate", quantity=40, unit="KG", recurrence_type="DAILY")
    cancelled = apply_domain_action(d, CancelRecurringNeedDraft()).draft
    target = ConfirmationTarget(draft_id=cancelled.draft_id, draft_version=cancelled.version)
    outcome = apply_domain_action(cancelled, ConfirmRecurringNeedDraft(target=target), claim=_always_claim)
    assert outcome.kind == RecurringNeedOutcomeKind.DRAFT_FINALIZED
    assert outcome.draft.status == RecurringNeedDraftStatus.CANCELLED, "jamais réactivé vers EXECUTING"


def test_updating_a_cancelled_draft_is_finalized_not_modified():
    """Une tentative de MODIFICATION (pas juste de confirmation) sur un draft déjà `CANCELLED`
    doit aussi être refusée — `with_updates` lève `IllegalDraftTransition`, interceptée par
    `apply_domain_action` (`UpdateRecurringNeedDraft` branch) plutôt que de laisser fuiter."""
    d = RecurringNeedDraft.new("d1", product="tomate", quantity=40, unit="KG", recurrence_type="DAILY")
    cancelled = apply_domain_action(d, CancelRecurringNeedDraft()).draft
    outcome = apply_domain_action(cancelled, UpdateRecurringNeedDraft(fields={"quantity": 99}))
    assert outcome.kind == RecurringNeedOutcomeKind.DRAFT_FINALIZED
    assert outcome.draft.quantity == 40, "la quantité d'origine ne doit jamais être altérée après annulation"
