"""RecurringNeedDraft — canonical, versioned transactional entity for the
CREATE_RECURRING_NEED conversational flow (creation of a recurring supply need).

Même pattern que `procurement_draft.py`/`preorder_draft.py` : objet immuable, `version` qui
bump à chaque mutation réelle, confirmation liée à `(draft_id, version)` (`ConfirmationTarget`),
machine à états `DRAFT -> CONFIRMED -> EXECUTING -> EXECUTED | FAILED`, `EXECUTION_UNKNOWN`
(issue inconnue, reprenable) ou `CANCELLED`.

Durabilité (Phase 2, P0) : chaque mutation est persistée dans `marketplace.recurring_need_drafts`
(`services/database/recurring_need_draft_store.py`) ; EXECUTING y est écrit AVANT l'appel MCP.
`execution_version` (fixée au passage en EXECUTING) identifie LA confirmation : la ligne du draft
sert de registre durable, verrouillée et passée à EXECUTED par `services/database/recurring_supply.py`
dans la même transaction que les besoins. Une confirmation ne produit donc qu'un seul jeu de
besoins, quels que soient les retries. Le claim Redis n'est qu'une optimisation anti-course.

Il ne modifie jamais un `RecurringNeed` déjà créé — c'est `UPDATE_RECURRING_NEED` qui s'en charge,
SANS draft.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union

from ladini.core.idempotency import claim_once
from ladini.domain.product_identity import same_product
from ladini.domain.quantity_unit import default_unit_for_product
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
    # En doute (issue réseau/timeout après envoi) : la réconciliation ou un nouvel « oui »
    # relance la MÊME exécution (garde PostgreSQL, voir `execution_key`) puis tranche.
    RecurringNeedDraftStatus.EXECUTION_UNKNOWN: frozenset(
        {RecurringNeedDraftStatus.EXECUTED, RecurringNeedDraftStatus.FAILED}
    ),
    RecurringNeedDraftStatus.CANCELLED: frozenset(),
}

_TERMINAL_STATUSES = frozenset(
    {
        RecurringNeedDraftStatus.EXECUTED,
        RecurringNeedDraftStatus.FAILED,
        RecurringNeedDraftStatus.CANCELLED,
    }
)

# Statuts où une exécution a été LANCÉE sans issue connue : ni actifs pour l'édition, ni
# terminaux. Une nouvelle confirmation y reprend la même exécution (idempotente en base).
IN_DOUBT_STATUSES = frozenset(
    {RecurringNeedDraftStatus.EXECUTING, RecurringNeedDraftStatus.EXECUTION_UNKNOWN}
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
    # Version à laquelle l'exécution a été lancée (DRAFT -> EXECUTING) : identifie LA
    # confirmation logique, fixe jusqu'à l'issue (EXECUTED/FAILED), même si le statut passe
    # par EXECUTION_UNKNOWN. Clé d'idempotence durable côté PostgreSQL.
    execution_version: Optional[int] = None
    execution_result: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "version": self.version,
            "status": self.status.value,
            **{f: getattr(self, f) for f in _FIELD_NAMES},
            "created_at": self.created_at,
            "execution_version": self.execution_version,
            "execution_result": self.execution_result,
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
            execution_version=int(d["execution_version"]) if d.get("execution_version") is not None else None,
            execution_result=d.get("execution_result") if isinstance(d.get("execution_result"), dict) else None,
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

    def with_updates(self, *, clear: Tuple[str, ...] = (), **fields: Any) -> "RecurringNeedDraft":
        """Voir `ProcurementDraft.with_updates` — même contrat exact (bump seulement si un champ change
        réellement, `IllegalDraftTransition` si le draft n'est plus `DRAFT`).

        `clear` : champs à VIDER explicitement (une valeur vide dans `fields` signifie « rien de
        dit », jamais « effacer » — même distinction ABSENT/DELETE que les reducers d'état)."""
        changed: Dict[str, Any] = {}
        for key in clear:
            if key in _FIELD_NAMES and slot_has_value(getattr(self, key)):
                changed[key] = None
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
        return replace(executing, version=self.version + 1, execution_version=self.version + 1)

    def is_in_doubt(self) -> bool:
        return self.status in IN_DOUBT_STATUSES

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

    def item_count(self) -> int:
        """Nombre de produits portés par la demande (principal + additionnels)."""
        primary = 1 if (slot_has_value(self.product) or slot_has_value(self.sub_category_id)) else 0
        return primary + len(self.additional_items or [])


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
    clear: Tuple[str, ...] = ()


@dataclass(frozen=True)
class CorrectionNeedsScope:
    """La correction ne désigne pas sans ambiguïté l'item visé d'un draft multi-produits
    (« non plutôt 23 bœufs » après « 14 coqs et 20 chèvres ») : on DEMANDE, jamais deviné."""

    fields: Dict[str, Any]
    candidates: Tuple[str, ...]


@dataclass(frozen=True)
class ConfirmRecurringNeedDraft:
    target: Optional[ConfirmationTarget]


@dataclass(frozen=True)
class CancelRecurringNeedDraft:
    """Abandon EXPLICITE et définitif du draft — contrairement à `ProcurementDraft`/
    `PreorderDraft` (dont le REJECT pendant confirmation est un rejet "doux", qui laisse le
    draft en `DRAFT` pour que l'utilisateur continue de le corriger), CREATE_RECURRING_NEED
    n'a pas cette notion de rejet doux : un "non"/"annuler" sur la confirmation d'un besoin
    récurrent est TOUJOURS définitif (mandat lifecycle, 2026-09-24 — anomalie résiduelle
    identifiée après le correctif state-leak : `resolve_domain_action` construisait un
    `RejectRecurringNeedConfirmation` qui laissait le draft en `DRAFT`, orphelin, au lieu de
    `CANCELLED`). Une demande de MODIFICATION ("modifier") n'emprunte de toute façon jamais
    ce chemin — elle n'est structurellement jamais dans le vocabulaire REJECT
    (`interpreter/routing.py::_REJECT_EXACT_PHRASES`), donc aucune confusion possible entre
    "annuler" et "modifier" ici."""

    pass


@dataclass(frozen=True)
class NoRecurringNeedAction:
    reason: str


DomainAction = Union[
    UpdateRecurringNeedDraft,
    CorrectionNeedsScope,
    ConfirmRecurringNeedDraft,
    CancelRecurringNeedDraft,
    NoRecurringNeedAction,
]


_SHARED_FIELDS = ("recurrence_type", "weekly_days", "excluded_weekdays", "starts_at", "ends_at", "max_price_per_unit")
_ITEM_FIELDS = ("product", "quantity", "unit")
CORRECTION_SCOPE_ALL = "ALL"


def _items_of(draft: RecurringNeedDraft) -> List[Dict[str, Any]]:
    return [{"product": draft.product, "quantity": draft.quantity, "unit": draft.unit}] + [
        dict(it) for it in (draft.additional_items or [])
    ]


def _replacement_item(draft_product: Any, item: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(item)
    new_product = item.get("product")
    if new_product and not same_product(new_product, draft_product) and not slot_has_value(item.get("unit")):
        # nouveau produit sans unité dite : l'unité de l'ancien produit n'a plus de sens
        out["unit"] = default_unit_for_product(new_product)
    return out


def plan_correction(
    draft: RecurringNeedDraft, fields: Dict[str, Any], *, scope: Optional[str] = None
) -> Union[UpdateRecurringNeedDraft, CorrectionNeedsScope, "NoRecurringNeedAction"]:
    """Correction d'un draft EN COURS — décision déterministe, le LLM ne fait que proposer
    `scope` (« ALL » = remplacer toute la demande). Règles (politique Phase 2) :
      - paramètres partagés (fréquence, jours, dates, prix max) : toujours appliqués ;
      - demande reformulée en entier (`additional_items` fourni) : remplace tous les items ;
      - draft à UN item, ou scope ALL : l'item est remplacé (items additionnels vidés) ;
      - draft multi-items : l'item NOMMÉ (même produit canonique) est modifié ;
      - sinon (item non désigné) : CorrectionNeedsScope — on demande, jamais deviné.
    """
    fields = {k: v for k, v in fields.items() if k in _FIELD_NAMES and slot_has_value(v)}
    shared = {k: fields[k] for k in _SHARED_FIELDS if k in fields}
    item = {k: fields[k] for k in _ITEM_FIELDS if k in fields}
    if fields.get("additional_items"):
        return UpdateRecurringNeedDraft(fields={**shared, **_replacement_item(draft.product, item),
                                                "additional_items": fields["additional_items"]})
    if not item:
        return UpdateRecurringNeedDraft(fields=shared) if shared else NoRecurringNeedAction(reason="empty_correction")
    items = _items_of(draft)
    if len(items) == 1 or str(scope or "").upper() == CORRECTION_SCOPE_ALL:
        return UpdateRecurringNeedDraft(
            fields={**shared, **_replacement_item(draft.product, item)},
            clear=("additional_items",) if len(items) > 1 else (),
        )
    named = item.get("product")
    for index, existing in enumerate(items):
        if named and same_product(named, existing.get("product")):
            changes = {k: v for k, v in item.items() if k != "product"}
            if not changes:
                break  # produit nommé sans nouvelle valeur (« annule la chèvre ») : non supporté -> demander
            if index == 0:
                return UpdateRecurringNeedDraft(fields={**shared, **changes})
            updated = [dict(it) for it in draft.additional_items or []]
            updated[index - 1] = {**updated[index - 1], **changes}
            return UpdateRecurringNeedDraft(fields={**shared, "additional_items": updated})
    return CorrectionNeedsScope(
        fields=fields, candidates=tuple(str(it.get("product")) for it in items if it.get("product"))
    )


def resolve_domain_action(
    *,
    interpreted_event: str,
    extracted_entities: Dict[str, Any],
    pending_target: Optional[Dict[str, Any]],
) -> DomainAction:
    """Voir `procurement_draft.resolve_domain_action` — même contrat, SAUF pour REJECT : voir
    `CancelRecurringNeedDraft` pour la justification du rejet définitif (pas de rejet "doux"
    ici, contrairement à PROCUREMENT/PREORDER)."""
    event = str(interpreted_event or "").upper().strip()

    if event == "CONFIRM":
        return ConfirmRecurringNeedDraft(target=ConfirmationTarget.from_dict(pending_target))
    if event == "REJECT":
        # Politique Phase 2 : un refus SANS nouvelle valeur termine la proposition
        # (CANCELLED) ; un refus PORTEUR de valeurs (« non, plutôt 23 bœufs ») est une
        # CORRECTION — jamais jetée (voir `plan_correction`, appliquée par le flow).
        fields = {k: v for k, v in (extracted_entities or {}).items() if k in _FIELD_NAMES and slot_has_value(v)}
        if fields:
            return UpdateRecurringNeedDraft(fields=fields)
        return CancelRecurringNeedDraft()
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
    RESUME_EXECUTION = "RESUME_EXECUTION"
    NEEDS_CORRECTION_SCOPE = "NEEDS_CORRECTION_SCOPE"
    VERSION_CONFLICT = "VERSION_CONFLICT"


@dataclass(frozen=True)
class RecurringNeedOutcome:
    kind: RecurringNeedOutcomeKind
    draft: Optional[RecurringNeedDraft]
    detail: Optional[str] = None


def _confirm_claim_key(draft: RecurringNeedDraft) -> str:
    return f"recurring_need_confirm:{draft.draft_id}:{draft.version}"


def confirm_claim_key(draft: RecurringNeedDraft) -> str:
    """Clé du claim Redis de confirmation (optimisation anti-course ; la garantie durable
    est la ligne PostgreSQL du draft). Exposée pour que l'appelant la LIBÈRE si le passage
    durable en EXECUTING échoue — sinon chaque « oui » suivant répondrait « en cours »."""
    return _confirm_claim_key(draft)


def execution_key(draft: RecurringNeedDraft) -> str:
    """Clé d'idempotence métier passée en `idempotency_key=` sur l'appel MCP `create_recurring_need` —
    voir `procurement_draft.execution_key` pour la justification complète (dédup serveur réelle,
    `mcp_idempotency_store`, vérifiée dans `infrastructure/mcp/runtime.py`)."""
    return f"recurring_need:{draft.draft_id}:{draft.execution_version or draft.version}"


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
            new_draft = base.with_updates(clear=action.clear, **action.fields)
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

    if isinstance(action, CorrectionNeedsScope):
        return RecurringNeedOutcome(kind=RecurringNeedOutcomeKind.NEEDS_CORRECTION_SCOPE, draft=draft)

    if isinstance(action, ConfirmRecurringNeedDraft):
        assert draft is not None  # garanti par le garde NO_DRAFT ci-dessus
        if action.target is None:
            return RecurringNeedOutcome(kind=RecurringNeedOutcomeKind.NO_TARGET, draft=draft)
        if draft.status == RecurringNeedDraftStatus.EXECUTED:
            return RecurringNeedOutcome(kind=RecurringNeedOutcomeKind.ALREADY_EXECUTED, draft=draft)
        if draft.status == RecurringNeedDraftStatus.FAILED:
            return RecurringNeedOutcome(kind=RecurringNeedOutcomeKind.ALREADY_FAILED, draft=draft)
        if draft.is_in_doubt():
            # Même demande, issue inconnue : on relance LA MÊME exécution (même
            # `execution_version`) — sans risque de doublon, la garde PostgreSQL rejoue le
            # résultat si elle a déjà abouti. Seule l'identité du draft est vérifiée : la
            # version affichée peut être antérieure (l'état du tour précédent a pu être perdu).
            if action.target.draft_id != draft.draft_id:
                return RecurringNeedOutcome(kind=RecurringNeedOutcomeKind.STALE_TARGET, draft=draft)
            return RecurringNeedOutcome(kind=RecurringNeedOutcomeKind.RESUME_EXECUTION, draft=draft)
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

    if isinstance(action, CancelRecurringNeedDraft):
        if draft is None:
            return RecurringNeedOutcome(kind=RecurringNeedOutcomeKind.NO_DRAFT, draft=None)
        # Un draft déjà hors DRAFT (CANCELLED — replay d'un REJECT déjà traité —, ou tout autre
        # statut terminal/en vol) ne peut pas être re-annulé : `with_status` lèverait
        # `IllegalDraftTransition` (`_ALLOWED_TRANSITIONS[CANCELLED]` est vide). Même garde que
        # `ConfirmRecurringNeedDraft` ci-dessus pour un statut non-DRAFT — réutilise le même
        # DRAFT_FINALIZED, jamais un 2ᵉ mécanisme de statut "déjà terminé".
        if draft.status != RecurringNeedDraftStatus.DRAFT:
            return RecurringNeedOutcome(kind=RecurringNeedOutcomeKind.DRAFT_FINALIZED, draft=draft)
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
    finalized = draft.with_status(target_status)
    if result.success:
        return replace(finalized, execution_result={"external_id": result.external_id})
    return finalized


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

    if kind == RecurringNeedOutcomeKind.NEEDS_CORRECTION_SCOPE:
        products = [it.get("product") for it in _items_of(draft)] if draft else []
        listed = ", ".join(str(p) for p in products if p)
        return RecurringNeedResponsePlan(
            final_response=(
                f"Votre demande contient plusieurs produits ({listed}). Voulez-vous tout remplacer, "
                "ou seulement l'un d'eux ? Précisez le produit à modifier, ou répondez *tout remplacer*."
            ),
            response_strategy="CLARIFICATION",
            graph_status="WAITING_INPUT",
            draft=draft,
            pending_kind="ENTER_FIELD",
            pending_field="correction_scope",
        )

    if kind == RecurringNeedOutcomeKind.CANCELLED:
        return RecurringNeedResponsePlan(
            final_response="D'accord, annulé.",
            response_strategy="SUCCESS",
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

    if kind in (RecurringNeedOutcomeKind.CONFIRMED_READY_FOR_EXECUTION, RecurringNeedOutcomeKind.RESUME_EXECUTION):
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
        # Le draft RESTE actif (en doute) : un nouvel « oui » relance la même exécution,
        # sans jamais créer de doublon (garde PostgreSQL par draft/version).
        return RecurringNeedResponsePlan(
            final_response=(
                "Je n'ai pas reçu la confirmation d'enregistrement (incident technique). "
                "Répondez *oui* pour vérifier et finaliser — aucun doublon ne sera créé."
            ),
            response_strategy="CONFIRMATION",
            graph_status="WAITING_INPUT",
            draft=draft,
            pending_kind="CONFIRM_ACTION" if draft else None,
            pending_target=({"draft_id": draft.draft_id, "draft_version": draft.version} if draft else None),
        )

    raise AssertionError(f"RecurringNeedOutcomeKind non couvert par build_response_plan: {kind!r}")


__all__ = [
    "RecurringNeedDraftStatus",
    "RecurringNeedDraft",
    "IN_DOUBT_STATUSES",
    "IllegalDraftTransition",
    "ConfirmationTarget",
    "UpdateRecurringNeedDraft",
    "CorrectionNeedsScope",
    "plan_correction",
    "ConfirmRecurringNeedDraft",
    "CancelRecurringNeedDraft",
    "NoRecurringNeedAction",
    "DomainAction",
    "resolve_domain_action",
    "RecurringNeedOutcomeKind",
    "RecurringNeedOutcome",
    "apply_domain_action",
    "execution_key",
    "confirm_claim_key",
    "RecurringNeedExecutionResult",
    "adapt_mcp_result",
    "finalize_after_execution",
    "RecurringNeedResponsePlan",
    "build_response_plan",
]
