"""RecurringNeedDraft — canonical, versioned transactional entity for the
CREATE_RECURRING_NEED conversational flow (creation of a recurring supply need).

Même pattern que `procurement_draft.py`/`preorder_draft.py` (mandat Phase 2 §1/§3 : réutiliser le
Draft/CAS existant, ne pas en inventer un 4ᵉ) : objet immuable, une seule représentation canonique,
`version` qui bump à chaque mutation réelle, confirmation liée à `(draft_id, version)`
(`ConfirmationTarget`, réutilisé tel quel — pas de 4ᵉ copie), claim Redis pour l'idempotence de la
confirmation, machine à états `DRAFT → CONFIRMED → EXECUTING → EXECUTED/FAILED/EXECUTION_UNKNOWN` ou
`CANCELLED`.

## Ce que ce module NE fait PAS

Il ne modifie jamais un `RecurringNeed` déjà créé — c'est `UPDATE_RECURRING_NEED`
(`services/database/recurring_supply.py::update_recurring_need`) qui s'en charge, SANS draft (l'objet
modifié est déjà persisté et identifié, pas en cours de construction conversationnelle — voir le
rapport de Phase 2 pour la justification, même choix que `PROCUREMENT_UPDATE_REQUEST`).
Il ne fait pas le matching ni la génération des occurrences lui-même — `execution_payload()` produit
le dict que l'outil MCP `create_recurring_need` consomme ; c'est CE service qui, dans une seule
transaction, insère `recurring_needs` PUIS matérialise la fenêtre J→J+7 d'occurrences
(`domain/recurring_supply/recurrence.py::generate_occurrence_dates`, fonction pure).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Dict, List, Optional, Union

from ladini.core.idempotency import claim_once
from ladini.graphs.agents.market_coach.core.confirmation_target import (
    ConfirmationTarget,
)
from ladini.graphs.agents.market_coach.utils import slot_has_value

# =====================================================================
# DRAFT — état transactionnel canonique
# =====================================================================


class RecurringNeedDraftStatus(str, Enum):
    """Voir `ProcurementDraftStatus` pour la justification complète de chaque valeur — même sémantique,
    appliquée ici à la création d'un besoin récurrent plutôt qu'à un appel d'offres."""

    DRAFT = "DRAFT"
    CONFIRMED = "CONFIRMED"
    EXECUTING = "EXECUTING"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    EXECUTION_UNKNOWN = "EXECUTION_UNKNOWN"
    CANCELLED = "CANCELLED"


# `product`/`sub_category_id` : l'un ou l'autre selon que la résolution catalogue a déjà eu lieu
# (voir `flows/buyer/recurring_need.py`) — jamais les deux sources de vérité utilisées simultanément
# côté exécution (`execution_payload` ne renvoie que les champs renseignés).
_FIELD_NAMES = (
    "product",
    "sub_category_id",
    "quantity",
    "unit",
    "recurrence_type",
    "weekly_days",
    "excluded_weekdays",
    "starts_at",
    "ends_at",
    "max_price_per_unit",
    "additional_items",
)
# `starts_at` n'est PAS requis : non renseigné, il est par défaut "demain" au moment de l'exécution
# (mandat §4 — "À partir de demain." dans l'exemple validé), calculé par le service (impur), jamais ici.
_REQUIRED_FOR_COMPLETION = ("quantity", "unit", "recurrence_type")


class IllegalDraftTransition(RuntimeError):
    """Voir `procurement_draft.IllegalDraftTransition` — même contrat."""


_ALLOWED_TRANSITIONS: Dict[RecurringNeedDraftStatus, frozenset] = {
    RecurringNeedDraftStatus.DRAFT: frozenset(
        {RecurringNeedDraftStatus.DRAFT, RecurringNeedDraftStatus.CONFIRMED, RecurringNeedDraftStatus.CANCELLED}
    ),
    RecurringNeedDraftStatus.CONFIRMED: frozenset({RecurringNeedDraftStatus.EXECUTING}),
    RecurringNeedDraftStatus.EXECUTING: frozenset(
        {
            RecurringNeedDraftStatus.EXECUTED,
            RecurringNeedDraftStatus.FAILED,
            RecurringNeedDraftStatus.EXECUTION_UNKNOWN,
        }
    ),
    RecurringNeedDraftStatus.EXECUTED: frozenset(),
    RecurringNeedDraftStatus.FAILED: frozenset(),
    RecurringNeedDraftStatus.EXECUTION_UNKNOWN: frozenset(),
    RecurringNeedDraftStatus.CANCELLED: frozenset(),
}

_TERMINAL_STATUSES = frozenset(
    {
        RecurringNeedDraftStatus.EXECUTED,
        RecurringNeedDraftStatus.FAILED,
        RecurringNeedDraftStatus.EXECUTION_UNKNOWN,
        RecurringNeedDraftStatus.CANCELLED,
    }
)


@dataclass(frozen=True)
class RecurringNeedDraft:
    draft_id: str
    version: int
    status: RecurringNeedDraftStatus
    product: Optional[str] = None
    sub_category_id: Optional[str] = None
    quantity: Optional[float] = None
    unit: Optional[str] = None
    recurrence_type: Optional[str] = None
    weekly_days: Optional[List[int]] = None
    excluded_weekdays: Optional[List[int]] = None
    starts_at: Optional[str] = None
    ends_at: Optional[str] = None
    max_price_per_unit: Optional[float] = None
    # Chantier multi-produits (2026-09-23) : `[{"product", "quantity", "unit"}, ...]` —
    # produits SUPPLÉMENTAIRES au-delà de `product`/`quantity`/`unit` ci-dessus, PARTAGEANT
    # la même récurrence/dates/prix max (même principe que `SalesPublishDraft.pricing_tiers` :
    # un champ liste sur UN SEUL draft, jamais explosé en plusieurs drafts — un seul
    # `draft_id`/`version`/`ConfirmationTarget` couvre toute la demande). Chaque item n'est
    # ajouté ici QUE complet (product+quantity+unit) — voir
    # `flows/buyer/recurring_need.py::_clean_additional_items`.
    additional_items: Optional[List[Dict[str, Any]]] = None
    created_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "version": self.version,
            "status": self.status.value,
            **{f: getattr(self, f) for f in _FIELD_NAMES},
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> Optional["RecurringNeedDraft"]:
        if not isinstance(d, dict) or not d.get("draft_id"):
            return None
        try:
            status = RecurringNeedDraftStatus(d.get("status") or "DRAFT")
        except ValueError:
            status = RecurringNeedDraftStatus.DRAFT
        return cls(
            draft_id=str(d["draft_id"]),
            version=int(d.get("version") or 1),
            status=status,
            created_at=float(d.get("created_at") or 0.0),
            **{f: d.get(f) for f in _FIELD_NAMES},
        )

    @classmethod
    def new(cls, draft_id: str, **fields: Any) -> "RecurringNeedDraft":
        clean = {k: v for k, v in fields.items() if k in _FIELD_NAMES and slot_has_value(v)}
        return cls(draft_id=draft_id, version=1, status=RecurringNeedDraftStatus.DRAFT, created_at=time.time(), **clean)

    def _transition(self, new_status: RecurringNeedDraftStatus, **extra: Any) -> "RecurringNeedDraft":
        allowed = _ALLOWED_TRANSITIONS.get(self.status, frozenset())
        if new_status not in allowed:
            raise IllegalDraftTransition(
                f"{self.status.value} -> {new_status.value} n'est pas une transition autorisée "
                f"(draft={self.draft_id}/v{self.version})"
            )
        return replace(self, status=new_status, **extra)

    def with_updates(self, **fields: Any) -> "RecurringNeedDraft":
        """Voir `ProcurementDraft.with_updates` — même contrat exact (bump seulement si un champ change
        réellement, `IllegalDraftTransition` si le draft n'est plus `DRAFT`)."""
        changed: Dict[str, Any] = {}
        for key, value in fields.items():
            if key not in _FIELD_NAMES or not slot_has_value(value):
                continue
            current = getattr(self, key)
            if isinstance(value, str) and isinstance(current, str):
                same = value.strip().lower() == current.strip().lower()
            else:
                same = value == current
            if not same:
                changed[key] = value
        if not changed:
            return self
        transitioned = self._transition(RecurringNeedDraftStatus.DRAFT)
        return replace(transitioned, version=self.version + 1, created_at=time.time(), **changed)

    def with_status(self, status: RecurringNeedDraftStatus) -> "RecurringNeedDraft":
        transitioned = self._transition(status)
        return replace(transitioned, version=self.version + 1)

    def _confirm_to_executing(self) -> "RecurringNeedDraft":
        confirmed = self._transition(RecurringNeedDraftStatus.CONFIRMED)
        executing = confirmed._transition(RecurringNeedDraftStatus.EXECUTING)
        return replace(executing, version=self.version + 1)

    def is_complete(self) -> bool:
        if not all(slot_has_value(getattr(self, f)) for f in _REQUIRED_FOR_COMPLETION):
            return False
        if not slot_has_value(self.product) and not slot_has_value(self.sub_category_id):
            return False
        # WEEKLY_DAYS sans jour n'a pas de sens (même contrainte que le CHECK DB) — demandé à
        # l'utilisateur avant confirmation plutôt que rejeté par la base après coup.
        if self.recurrence_type == "WEEKLY_DAYS" and not self.weekly_days:
            return False
        if not _additional_items_complete(self.additional_items):
            return False
        return True

    def missing_fields(self) -> List[str]:
        missing = [f for f in _REQUIRED_FOR_COMPLETION if not slot_has_value(getattr(self, f))]
        if not slot_has_value(self.product) and not slot_has_value(self.sub_category_id):
            missing.append("product")
        if self.recurrence_type == "WEEKLY_DAYS" and not self.weekly_days and "weekly_days" not in missing:
            missing.append("weekly_days")
        # Un item additionnel incomplet ne devrait normalement jamais atteindre le draft
        # (`_clean_additional_items` le filtre déjà en amont) — filet de sécurité générique
        # plutôt qu'un message dédié pour ce cas censé rester inatteignable en pratique.
        if not _additional_items_complete(self.additional_items) and "quantity" not in missing:
            missing.append("quantity")
        return missing

    def render_summary(self) -> str:
        """Projection PURE (mandat §18 : jamais les mots occurrence/CAS/recurring_need/version).
        Liste CHAQUE produit de la demande (mandat multi-produits 2026-09-23) — jamais seulement
        le premier."""
        if not self.is_complete():
            return "Votre besoin d'approvisionnement en cours de construction."
        lines = [_render_item_line(self.product, self.quantity, self.unit)]
        for item in self.additional_items or []:
            lines.append(_render_item_line(item.get("product"), item.get("quantity"), item.get("unit")))
        lines.append(_render_frequency(self.recurrence_type, self.weekly_days, self.excluded_weekdays))
        if slot_has_value(self.starts_at):
            lines.append(f"À partir du {self.starts_at}.")
        else:
            lines.append("À partir de demain.")
        return "\n".join(lines)

    def execution_payload(self) -> Dict[str, Any]:
        return {f: getattr(self, f) for f in _FIELD_NAMES if slot_has_value(getattr(self, f))}

    def is_terminal(self) -> bool:
        """`True` une fois EXECUTED/FAILED/EXECUTION_UNKNOWN/CANCELLED — les 4
        statuts sans transition sortante dans `_ALLOWED_TRANSITIONS`. Un draft
        terminal n'a plus vocation à rester le draft ACTIF du state — voir
        `flows/buyer/recurring_need.py::_apply_response_plan`."""
        return self.status in _TERMINAL_STATUSES


def _fmt_num(value: Optional[float]) -> str:
    if value is None:
        return ""
    return f"{value:g}"


def _additional_items_complete(items: Optional[List[Dict[str, Any]]]) -> bool:
    for item in items or []:
        if not slot_has_value(item.get("product")):
            return False
        if not slot_has_value(item.get("quantity")):
            return False
        if not slot_has_value(item.get("unit")):
            return False
    return True


def _render_item_line(product: Optional[str], quantity: Optional[float], unit: Optional[str]) -> str:
    label = product or "ce produit"
    return f"{label.capitalize()} : {_fmt_num(quantity)} {unit}"


_WEEKDAY_LABELS = {1: "lundi", 2: "mardi", 3: "mercredi", 4: "jeudi", 5: "vendredi", 6: "samedi", 7: "dimanche"}


def _render_frequency(
    recurrence_type: Optional[str], weekly_days: Optional[List[int]], excluded_weekdays: Optional[List[int]]
) -> str:
    excluded = {_WEEKDAY_LABELS[d] for d in (excluded_weekdays or []) if d in _WEEKDAY_LABELS}
    suffix = f" sauf le {', '.join(sorted(excluded))}" if excluded else ""
    if recurrence_type == "DAILY":
        return f"Tous les jours{suffix}."
    if recurrence_type == "WEEKLY_DAYS":
        days = ", ".join(_WEEKDAY_LABELS[d] for d in sorted(weekly_days or []) if d in _WEEKDAY_LABELS)
        return f"Les {days}{suffix}."
    if recurrence_type == "WEEKLY":
        return f"Chaque semaine{suffix}."
    if recurrence_type == "ONE_OFF":
        return "Une seule fois."
    return ""


# =====================================================================
# DOMAIN ACTIONS
# =====================================================================


@dataclass(frozen=True)
class UpdateRecurringNeedDraft:
    fields: Dict[str, Any]


@dataclass(frozen=True)
class ConfirmRecurringNeedDraft:
    target: Optional[ConfirmationTarget]


@dataclass(frozen=True)
class RejectRecurringNeedConfirmation:
    pass


@dataclass(frozen=True)
class CancelRecurringNeedDraft:
    pass


@dataclass(frozen=True)
class NoRecurringNeedAction:
    reason: str


DomainAction = Union[
    UpdateRecurringNeedDraft,
    ConfirmRecurringNeedDraft,
    RejectRecurringNeedConfirmation,
    CancelRecurringNeedDraft,
    NoRecurringNeedAction,
]


def resolve_domain_action(
    *,
    interpreted_event: str,
    extracted_entities: Dict[str, Any],
    pending_target: Optional[Dict[str, Any]],
) -> DomainAction:
    """Voir `procurement_draft.resolve_domain_action` — même contrat exact."""
    event = str(interpreted_event or "").upper().strip()

    if event == "CONFIRM":
        return ConfirmRecurringNeedDraft(target=ConfirmationTarget.from_dict(pending_target))
    if event == "REJECT":
        return RejectRecurringNeedConfirmation()
    if event in {"UPDATE", "ANSWER"}:
        fields = {k: v for k, v in (extracted_entities or {}).items() if k in _FIELD_NAMES and slot_has_value(v)}
        if fields:
            return UpdateRecurringNeedDraft(fields=fields)
        return NoRecurringNeedAction(reason="no_structured_fields")
    return NoRecurringNeedAction(reason=f"unhandled_event:{event}")


# =====================================================================
# DOMAIN OUTCOME + APPLICATION
# =====================================================================


class RecurringNeedOutcomeKind(str, Enum):
    DRAFT_UPDATED = "DRAFT_UPDATED"
    DRAFT_UNCHANGED = "DRAFT_UNCHANGED"
    NEEDS_MORE_INFO = "NEEDS_MORE_INFO"
    CONFIRMED_READY_FOR_EXECUTION = "CONFIRMED_READY_FOR_EXECUTION"
    STALE_TARGET = "STALE_TARGET"
    NO_TARGET = "NO_TARGET"
    CONFIRMATION_REJECTED = "CONFIRMATION_REJECTED"
    CANCELLED = "CANCELLED"
    NO_DRAFT = "NO_DRAFT"
    DRAFT_FINALIZED = "DRAFT_FINALIZED"
    ALREADY_EXECUTING = "ALREADY_EXECUTING"
    RECURRING_NEED_CREATED = "RECURRING_NEED_CREATED"
    ALREADY_EXECUTED = "ALREADY_EXECUTED"
    RECURRING_NEED_FAILED = "RECURRING_NEED_FAILED"
    ALREADY_FAILED = "ALREADY_FAILED"
    RECURRING_NEED_EXECUTION_UNKNOWN = "RECURRING_NEED_EXECUTION_UNKNOWN"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    VERSION_CONFLICT = "VERSION_CONFLICT"


@dataclass(frozen=True)
class RecurringNeedOutcome:
    kind: RecurringNeedOutcomeKind
    draft: Optional[RecurringNeedDraft]
    detail: Optional[str] = None


def _confirm_claim_key(draft: RecurringNeedDraft) -> str:
    return f"recurring_need_confirm:{draft.draft_id}:{draft.version}"


def execution_key(draft: RecurringNeedDraft) -> str:
    """Clé d'idempotence métier passée en `idempotency_key=` sur l'appel MCP `create_recurring_need` —
    voir `procurement_draft.execution_key` pour la justification complète (dédup serveur réelle,
    `mcp_idempotency_store`, vérifiée dans `infrastructure/mcp/runtime.py`)."""
    return f"recurring_need:{draft.draft_id}:{draft.version}"


def apply_domain_action(
    draft: Optional[RecurringNeedDraft],
    action: DomainAction,
    *,
    claim=None,
) -> RecurringNeedOutcome:
    """LA seule fonction qui transitionne un `RecurringNeedDraft` — voir
    `procurement_draft.apply_domain_action` pour la justification de chaque branche, identique ici."""
    if claim is None:
        claim = claim_once
    if draft is None and not isinstance(action, UpdateRecurringNeedDraft):
        return RecurringNeedOutcome(kind=RecurringNeedOutcomeKind.NO_DRAFT, draft=None)

    if isinstance(action, UpdateRecurringNeedDraft):
        base = draft or RecurringNeedDraft.new(draft_id=_new_draft_id())
        try:
            new_draft = base.with_updates(**action.fields)
        except IllegalDraftTransition:
            return RecurringNeedOutcome(kind=RecurringNeedOutcomeKind.DRAFT_FINALIZED, draft=base)
        if new_draft is base and draft is not None:
            return RecurringNeedOutcome(kind=RecurringNeedOutcomeKind.DRAFT_UNCHANGED, draft=new_draft)
        kind = (
            RecurringNeedOutcomeKind.NEEDS_MORE_INFO
            if not new_draft.is_complete()
            else RecurringNeedOutcomeKind.DRAFT_UPDATED
        )
        return RecurringNeedOutcome(kind=kind, draft=new_draft)

    if isinstance(action, ConfirmRecurringNeedDraft):
        if action.target is None:
            return RecurringNeedOutcome(kind=RecurringNeedOutcomeKind.NO_TARGET, draft=draft)
        if draft.status == RecurringNeedDraftStatus.EXECUTED:
            return RecurringNeedOutcome(kind=RecurringNeedOutcomeKind.ALREADY_EXECUTED, draft=draft)
        if draft.status == RecurringNeedDraftStatus.FAILED:
            return RecurringNeedOutcome(kind=RecurringNeedOutcomeKind.ALREADY_FAILED, draft=draft)
        if draft.status == RecurringNeedDraftStatus.EXECUTION_UNKNOWN:
            return RecurringNeedOutcome(kind=RecurringNeedOutcomeKind.RECONCILIATION_REQUIRED, draft=draft)
        if draft.status == RecurringNeedDraftStatus.EXECUTING:
            return RecurringNeedOutcome(kind=RecurringNeedOutcomeKind.ALREADY_EXECUTING, draft=draft)
        if draft.status == RecurringNeedDraftStatus.CANCELLED:
            return RecurringNeedOutcome(kind=RecurringNeedOutcomeKind.DRAFT_FINALIZED, draft=draft)
        if draft.status != RecurringNeedDraftStatus.DRAFT:
            return RecurringNeedOutcome(kind=RecurringNeedOutcomeKind.DRAFT_FINALIZED, draft=draft)
        if not action.target.matches(draft):
            return RecurringNeedOutcome(
                kind=RecurringNeedOutcomeKind.STALE_TARGET,
                draft=draft,
                detail=f"target={action.target.draft_id}/{action.target.draft_version}",
            )
        if not draft.is_complete():
            return RecurringNeedOutcome(kind=RecurringNeedOutcomeKind.NEEDS_MORE_INFO, draft=draft)
        if not claim(_confirm_claim_key(draft)):
            return RecurringNeedOutcome(kind=RecurringNeedOutcomeKind.ALREADY_EXECUTING, draft=draft)
        executing = draft._confirm_to_executing()
        return RecurringNeedOutcome(kind=RecurringNeedOutcomeKind.CONFIRMED_READY_FOR_EXECUTION, draft=executing)

    if isinstance(action, RejectRecurringNeedConfirmation):
        if draft is None:
            return RecurringNeedOutcome(kind=RecurringNeedOutcomeKind.NO_DRAFT, draft=None)
        return RecurringNeedOutcome(kind=RecurringNeedOutcomeKind.CONFIRMATION_REJECTED, draft=draft)

    if isinstance(action, CancelRecurringNeedDraft):
        if draft is None:
            return RecurringNeedOutcome(kind=RecurringNeedOutcomeKind.NO_DRAFT, draft=None)
        return RecurringNeedOutcome(
            kind=RecurringNeedOutcomeKind.CANCELLED, draft=draft.with_status(RecurringNeedDraftStatus.CANCELLED)
        )

    return RecurringNeedOutcome(kind=RecurringNeedOutcomeKind.DRAFT_UNCHANGED, draft=draft, detail=action.reason)


@dataclass(frozen=True)
class RecurringNeedExecutionResult:
    success: bool
    external_id: Optional[str] = None
    error: Optional[str] = None
    ambiguous: bool = False


def adapt_mcp_result(
    execution_status: Optional[str], execution_result: Optional[Dict[str, Any]]
) -> RecurringNeedExecutionResult:
    status = str(execution_status or "").upper()
    result = execution_result if isinstance(execution_result, dict) else {}
    if status == "COMPLETED":
        return RecurringNeedExecutionResult(success=True, external_id=result.get("recurring_need_id"))
    if status == "ERROR":
        error = str(result.get("message") or result.get("error") or "Erreur inconnue")
        return RecurringNeedExecutionResult(success=False, error=error)
    return RecurringNeedExecutionResult(success=False, ambiguous=True)


def finalize_after_execution(draft: RecurringNeedDraft, result: RecurringNeedExecutionResult) -> RecurringNeedDraft:
    if result.ambiguous:
        target_status = RecurringNeedDraftStatus.EXECUTION_UNKNOWN
    elif result.success:
        target_status = RecurringNeedDraftStatus.EXECUTED
    else:
        target_status = RecurringNeedDraftStatus.FAILED
    return draft.with_status(target_status)


def _new_draft_id() -> str:
    import uuid

    return uuid.uuid4().hex[:12]


# =====================================================================
# RESPONSE PLAN — DomainOutcome → ce qui doit être communiqué (mandat §18 :
# jamais les mots occurrence/CAS/recurring_need/version dans le texte utilisateur)
# =====================================================================

_MISSING_FIELD_LABELS = {
    "product": "le produit souhaité",
    "quantity": "la quantité",
    "unit": "l'unité (kg, unités...)",
    "recurrence_type": "la fréquence (chaque jour, certains jours, chaque semaine, une seule fois)",
    "weekly_days": "les jours de la semaine concernés",
}


@dataclass(frozen=True)
class RecurringNeedResponsePlan:
    final_response: str
    response_strategy: str
    graph_status: str
    draft: Optional[RecurringNeedDraft]
    pending_kind: Optional[str] = None
    pending_field: Optional[str] = None
    pending_target: Optional[Dict[str, Any]] = None
    ready_for_execution: bool = False
    terminal_goal_reset: bool = False
    pending_untouched: bool = False


def _missing_field_prompt(missing: List[str]) -> str:
    if not missing:
        return "Il me manque encore une information."
    return f"D'accord — {_MISSING_FIELD_LABELS.get(missing[0], missing[0])} ?"


def build_response_plan(
    outcome: RecurringNeedOutcome, *, deviation_note: Optional[str] = None
) -> RecurringNeedResponsePlan:
    draft = outcome.draft
    kind = outcome.kind

    if kind == RecurringNeedOutcomeKind.NO_DRAFT:
        return RecurringNeedResponsePlan(
            final_response="", response_strategy="SUCCESS", graph_status="PLANNING", draft=None, pending_untouched=True
        )

    if kind == RecurringNeedOutcomeKind.NEEDS_MORE_INFO:
        missing = draft.missing_fields()
        return RecurringNeedResponsePlan(
            final_response=_missing_field_prompt(missing),
            response_strategy="ASK_MISSING_FIELD",
            graph_status="WAITING_INPUT",
            draft=draft,
            pending_kind="ENTER_FIELD",
            pending_field=missing[0] if missing else None,
        )

    if kind in (RecurringNeedOutcomeKind.DRAFT_UPDATED, RecurringNeedOutcomeKind.DRAFT_UNCHANGED):
        summary = draft.render_summary() if draft else ""
        body = f"{deviation_note}\n\n{summary}" if deviation_note else summary
        return RecurringNeedResponsePlan(
            final_response=f"{body}\n\nConfirmer ?\n\n1. Oui\n2. Modifier\n3. Annuler" if draft else body,
            response_strategy="CONFIRMATION",
            graph_status="WAITING_INPUT",
            draft=draft,
            pending_kind="CONFIRM_ACTION" if draft else None,
            pending_target=({"draft_id": draft.draft_id, "draft_version": draft.version} if draft else None),
        )

    if kind == RecurringNeedOutcomeKind.CONFIRMATION_REJECTED:
        return RecurringNeedResponsePlan(
            final_response="D'accord, ce n'est pas encore confirmé. Que voulez-vous changer ?",
            response_strategy="SUCCESS",
            graph_status="PLANNING",
            draft=draft,
        )

    if kind == RecurringNeedOutcomeKind.CANCELLED:
        return RecurringNeedResponsePlan(
            final_response="D'accord, annulé.",
            response_strategy="CLARIFICATION",
            graph_status="COMPLETED",
            draft=None,
            terminal_goal_reset=True,
        )

    if kind == RecurringNeedOutcomeKind.DRAFT_FINALIZED:
        label = {
            RecurringNeedDraftStatus.EXECUTED: "déjà créé",
            RecurringNeedDraftStatus.FAILED: "déjà traité (échec)",
            RecurringNeedDraftStatus.CANCELLED: "déjà annulé",
        }.get(draft.status if draft else None, "déjà finalisé")
        return RecurringNeedResponsePlan(
            final_response=f"Ce besoin est {label} — rien à modifier ici.",
            response_strategy="SUCCESS",
            graph_status="COMPLETED",
            draft=draft,
        )

    if kind in (RecurringNeedOutcomeKind.STALE_TARGET, RecurringNeedOutcomeKind.VERSION_CONFLICT):
        if draft is None:
            return RecurringNeedResponsePlan(final_response="", response_strategy="SUCCESS", graph_status="PLANNING", draft=None)
        return RecurringNeedResponsePlan(
            final_response=f"{draft.render_summary()}\n\nÇa a changé entre-temps — confirmer cette version ?\n\n1. Oui\n2. Modifier\n3. Annuler",
            response_strategy="CONFIRMATION",
            graph_status="WAITING_INPUT",
            draft=draft,
            pending_kind="CONFIRM_ACTION",
            pending_target={"draft_id": draft.draft_id, "draft_version": draft.version},
        )

    if kind == RecurringNeedOutcomeKind.NO_TARGET:
        return RecurringNeedResponsePlan(
            final_response="", response_strategy="SUCCESS", graph_status="PLANNING", draft=draft, pending_untouched=True
        )

    if kind == RecurringNeedOutcomeKind.CONFIRMED_READY_FOR_EXECUTION:
        return RecurringNeedResponsePlan(
            final_response="", response_strategy="SUCCESS", graph_status="EXECUTING", draft=draft, ready_for_execution=True
        )

    if kind == RecurringNeedOutcomeKind.ALREADY_EXECUTING:
        return RecurringNeedResponsePlan(
            final_response="C'est en cours d'enregistrement — inutile de renvoyer *confirme*.",
            response_strategy="SUCCESS",
            graph_status="COMPLETED",
            draft=draft,
        )

    if kind == RecurringNeedOutcomeKind.ALREADY_EXECUTED:
        return RecurringNeedResponsePlan(
            final_response="Ce besoin a déjà été créé — rien à refaire.",
            response_strategy="SUCCESS",
            graph_status="COMPLETED",
            draft=draft,
        )

    if kind == RecurringNeedOutcomeKind.ALREADY_FAILED:
        return RecurringNeedResponsePlan(
            final_response="Ça n'a pas pu être enregistré — voulez-vous réessayer ?",
            response_strategy="SUCCESS",
            graph_status="COMPLETED",
            draft=draft,
        )

    if kind == RecurringNeedOutcomeKind.RECONCILIATION_REQUIRED:
        return RecurringNeedResponsePlan(
            final_response="Je ne suis pas sûr que ça ait bien été enregistré — on vérifie et on vous recontacte.",
            response_strategy="SUCCESS",
            graph_status="COMPLETED",
            draft=draft,
        )

    if kind == RecurringNeedOutcomeKind.RECURRING_NEED_CREATED:
        return RecurringNeedResponsePlan(
            final_response=f"C'est noté !\n\n{draft.render_summary()}" if draft else "C'est noté !",
            response_strategy="SUCCESS",
            graph_status="COMPLETED",
            draft=draft,
        )

    if kind == RecurringNeedOutcomeKind.RECURRING_NEED_FAILED:
        return RecurringNeedResponsePlan(
            final_response="Je n'ai pas pu enregistrer ce besoin. Vous pouvez réessayer.",
            response_strategy="ERROR",
            graph_status="COMPLETED",
            draft=draft,
        )

    if kind == RecurringNeedOutcomeKind.RECURRING_NEED_EXECUTION_UNKNOWN:
        return RecurringNeedResponsePlan(
            final_response="Un incident technique m'empêche de confirmer l'enregistrement — on vérifie.",
            response_strategy="ERROR",
            graph_status="COMPLETED",
            draft=draft,
        )

    raise AssertionError(f"RecurringNeedOutcomeKind non couvert par build_response_plan: {kind!r}")


__all__ = [
    "RecurringNeedDraftStatus",
    "RecurringNeedDraft",
    "IllegalDraftTransition",
    "ConfirmationTarget",
    "UpdateRecurringNeedDraft",
    "ConfirmRecurringNeedDraft",
    "RejectRecurringNeedConfirmation",
    "CancelRecurringNeedDraft",
    "NoRecurringNeedAction",
    "DomainAction",
    "resolve_domain_action",
    "RecurringNeedOutcomeKind",
    "RecurringNeedOutcome",
    "apply_domain_action",
    "execution_key",
    "RecurringNeedExecutionResult",
    "adapt_mcp_result",
    "finalize_after_execution",
    "RecurringNeedResponsePlan",
    "build_response_plan",
]
