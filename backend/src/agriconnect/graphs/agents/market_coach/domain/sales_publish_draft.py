"""SalesPublishDraft — canonical, versioned transactional entity for the
SALES_PUBLISH_PRODUCT conversational flow (2026-09-03/04, migration SALES,
même standard que `procurement_draft.py`/`preorder_draft.py`, PAS un
copier-coller — voir le rapport final section C/D pour l'audit qui a
précédé cette construction).

## Pourquoi ce module existe

Audit préalable (rapport final, section C) : `SALES_PUBLISH_PRODUCT`
utilisait encore le mécanisme GÉNÉRIQUE de `nodes/confirmation_gate.py`
(`transaction_payload` mutable + `confirmation_summary` TEXTE STOCKÉ au
moment de `WAITING_CONFIRMATION`) — exactement le pattern qui a produit le
bug historique PROCUREMENT ("le récap affiche la correction, l'exécution
utilise l'ancienne valeur") : le récapitulatif est un texte figé à un
instant T, mais `transaction_payload` (mutable, `merge_dict`) reste lu EN
DIRECT au moment de l'exécution — rien ne garantit qu'ils désignent le
MÊME contenu si une mutation intervient entre les deux. `SalesPublishDraft`
élimine cette classe de bug de la MÊME façon que `ProcurementDraft` :
objet immuable versionné, `render_summary()` = projection PURE de ce même
objet (jamais un texte mémorisé séparément), et le contenu qui EXÉCUTE
(`create_product`) est TOUJOURS celui de la version CONFIRMÉE, jamais une
relecture indépendante de `transaction_payload`.

## Champs — déduits du VRAI workflow (mandat §10 : ne pas copier ProcurementDraft)

`product`/`quantity`/`unit`/`price` (identiques en esprit à PROCUREMENT,
mêmes noms) + `description`/`category_label`/`pricing_tiers` — ces 3
derniers N'EXISTENT PAS côté PROCUREMENT/PREORDER, déduits de
`actions/sales_dto.py::SalesPublishProductPayload` (le contrat RÉEL déjà
utilisé par `actions/sales.py::prep_sales_publish_product`). AUCUN champ
`deadline` (n'a pas de sens pour une publication produit, contrairement à
un appel d'offres) — le draft ne reproduit QUE ce que ce workflow précis
requiert (mandat §10, dernière phrase).

## Médias/photos — délibérément ABSENTS de ce draft (mandat §16)

Audité : `SalesPublishProductPayload` ne porte AUCUN champ image/média —
confirmé par `sales_dto.py`. La photo produit (voir mémoire
[[whatsapp-product-photo-2026-08]]) est un pipeline ENTIÈREMENT découplé
de LangGraph : Twilio → Supabase → `Product.images`, écrit directement en
base, JAMAIS via `transaction_payload`/ce draft. Un média est donc une
RESSOURCE EXTERNE associée au produit APRÈS publication (par `product_id`),
pas un champ transactionnel à versionner ici — inclure un champ média
créerait une DEUXIÈME autorité sur la même donnée (exactement ce que ce
chantier élimine ailleurs). Décision documentée, pas oubliée.

## Ce que ce module NE fait PAS

Il ne remplace pas `nodes/executor.py::mcp_tool_executor` (l'appel MCP réel
`create_product` reste exécuté par le pipeline générique, comme
PROCUREMENT) ni `services/database/producer.py::create_product` (inchangé).
Il ne réinvente PAS `ConfirmationTarget`/`PendingInteraction`/`claim_once` —
réutilisés tels quels depuis `core/confirmation_target.py`/
`core/pending_interaction.py`/`core/idempotency.py` (mandat hardening
transverse, aucune 3e copie)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union

from agriconnect.core.formatting import fmt_num as _fmt_num
from agriconnect.core.idempotency import claim_once
from agriconnect.graphs.agents.market_coach.core.confirmation_target import (
    ConfirmationTarget,
)
from agriconnect.graphs.agents.market_coach.utils import (
    canonical_unit_label,
    slot_has_value,
)

# =====================================================================
# DRAFT — état transactionnel canonique
# =====================================================================


class SalesPublishDraftStatus(str, Enum):
    """Où en est LA TRANSACTION — distinct de `PendingInteraction`, qui dit
    ce qu'attend le PROCHAIN message (même séparation que PROCUREMENT/
    PREORDER). Liste réduite au strict nécessaire (mandat §18 : "garde
    uniquement ceux réellement nécessaires") — pas de `WAITING_CONFIRMATION`
    séparé (c'est un état de `PendingInteraction`, pas du draft), pas de
    `CONFIRMED` observable (fondu dans la transition atomique vers
    `EXECUTING`, comme PROCUREMENT)."""

    DRAFT = "DRAFT"  # champs en cours de collecte/correction, mutable
    EXECUTING = "EXECUTING"  # appel MCP en cours/tenté — persisté, empêche une 2e tentative
    PUBLISHED = "PUBLISHED"  # create_product confirmé réussi — terminal
    FAILED = "FAILED"  # create_product confirmé échoué — terminal
    EXECUTION_UNKNOWN = "EXECUTION_UNKNOWN"  # issue indéterminable — réconciliation requise, jamais un retry aveugle
    CANCELLED = "CANCELLED"  # producteur a rejeté — terminal


_FIELD_NAMES = (
    "product",
    "quantity",
    "unit",
    "price",
    "description",
    "category_label",
    "pricing_tiers",
)
_REQUIRED_FOR_COMPLETION = ("product", "quantity", "price")


class IllegalDraftTransition(RuntimeError):
    """Levée quand une mutation demande une transition absente de
    `_ALLOWED_TRANSITIONS` — même contrat que PROCUREMENT/PREORDER."""


# Machine d'état EXPLICITE et CENTRALISÉE — toute transition absente de
# cette table est REFUSÉE, jamais appliquée silencieusement.
_ALLOWED_TRANSITIONS: Dict[SalesPublishDraftStatus, frozenset] = {
    SalesPublishDraftStatus.DRAFT: frozenset(
        {
            SalesPublishDraftStatus.DRAFT,
            SalesPublishDraftStatus.EXECUTING,
            SalesPublishDraftStatus.CANCELLED,
        }
    ),
    SalesPublishDraftStatus.EXECUTING: frozenset(
        {
            SalesPublishDraftStatus.PUBLISHED,
            SalesPublishDraftStatus.FAILED,
            SalesPublishDraftStatus.EXECUTION_UNKNOWN,
        }
    ),
    SalesPublishDraftStatus.PUBLISHED: frozenset(),
    SalesPublishDraftStatus.FAILED: frozenset(),
    SalesPublishDraftStatus.EXECUTION_UNKNOWN: frozenset(),
    SalesPublishDraftStatus.CANCELLED: frozenset(),
}

_MISSING_FIELD_LABELS = {
    "product": "le nom du produit",
    "quantity": "la quantité",
    "price": "le prix",
}


@dataclass(frozen=True)
class SalesPublishDraft:
    draft_id: str
    version: int
    status: SalesPublishDraftStatus
    product: Optional[str] = None
    quantity: Optional[float] = None
    unit: Optional[str] = None
    price: Optional[float] = None
    description: Optional[str] = None
    category_label: Optional[str] = None
    pricing_tiers: Optional[Tuple[Dict[str, Any], ...]] = None
    created_at: float = 0.0

    # ------------------------------------------------------------
    # (de)sérialisation
    # ------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "version": self.version,
            "status": self.status.value,
            "product": self.product,
            "quantity": self.quantity,
            "unit": self.unit,
            "price": self.price,
            "description": self.description,
            "category_label": self.category_label,
            "pricing_tiers": list(self.pricing_tiers) if self.pricing_tiers else None,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> Optional["SalesPublishDraft"]:
        if not isinstance(d, dict) or not d.get("draft_id"):
            return None
        try:
            status = SalesPublishDraftStatus(d.get("status") or "DRAFT")
        except ValueError:
            status = SalesPublishDraftStatus.DRAFT
        raw_tiers = d.get("pricing_tiers")
        tiers = tuple(raw_tiers) if isinstance(raw_tiers, list) else None
        return cls(
            draft_id=str(d["draft_id"]),
            version=int(d.get("version") or 1),
            status=status,
            product=d.get("product"),
            quantity=d.get("quantity"),
            unit=d.get("unit"),
            price=d.get("price"),
            description=d.get("description"),
            category_label=d.get("category_label"),
            pricing_tiers=tiers,
            created_at=float(d.get("created_at") or 0.0),
        )

    @classmethod
    def new(cls, draft_id: str, **fields: Any) -> "SalesPublishDraft":
        clean = {k: v for k, v in fields.items() if k in _FIELD_NAMES and slot_has_value(v)}
        if "pricing_tiers" in clean and isinstance(clean["pricing_tiers"], list):
            clean["pricing_tiers"] = tuple(clean["pricing_tiers"])
        return cls(
            draft_id=draft_id,
            version=1,
            status=SalesPublishDraftStatus.DRAFT,
            created_at=time.time(),
            **clean,
        )

    # ------------------------------------------------------------
    # mutation — TOUJOURS une nouvelle version, jamais en place
    # ------------------------------------------------------------
    def _transition(self, new_status: SalesPublishDraftStatus, **extra: Any) -> "SalesPublishDraft":
        allowed = _ALLOWED_TRANSITIONS.get(self.status, frozenset())
        if new_status not in allowed:
            raise IllegalDraftTransition(
                f"{self.status.value} -> {new_status.value} n'est pas une "
                f"transition autorisée (draft={self.draft_id}/v{self.version})"
            )
        return replace(self, status=new_status, **extra)

    def with_updates(self, **fields: Any) -> "SalesPublishDraft":
        """Nouvelle version — SEUL point d'écriture des champs métier. Lève
        `IllegalDraftTransition` si `self.status != DRAFT` (mandat §14 :
        aucune correction possible une fois la publication engagée — même
        garantie que PROCUREMENT)."""
        changed: Dict[str, Any] = {}
        for key, value in fields.items():
            if key not in _FIELD_NAMES or not slot_has_value(value):
                continue
            if key == "pricing_tiers" and isinstance(value, list):
                value = tuple(value)
            current = getattr(self, key)
            if isinstance(value, str) and isinstance(current, str):
                same = value.strip().lower() == current.strip().lower()
            else:
                same = value == current
            if not same:
                changed[key] = value
        if not changed:
            return self
        transitioned = self._transition(SalesPublishDraftStatus.DRAFT)
        return replace(transitioned, version=self.version + 1, created_at=time.time(), **changed)

    def with_status(self, status: SalesPublishDraftStatus) -> "SalesPublishDraft":
        """Bump la version à CHAQUE transition persistable — même correctif
        que PROCUREMENT/PREORDER (protège la LIGNE entière via `version`,
        pas seulement les champs métier)."""
        transitioned = self._transition(status)
        return replace(transitioned, version=self.version + 1)

    def _confirm_to_executing(self) -> "SalesPublishDraft":
        """DRAFT → EXECUTING en UNE SEULE transition métier (l'événement
        CONFIRM), jamais observable en état intermédiaire — UN SEUL
        incrément de version."""
        executing = self._transition(SalesPublishDraftStatus.EXECUTING)
        return replace(executing, version=self.version + 1)

    # ------------------------------------------------------------
    # lecture
    # ------------------------------------------------------------
    def is_complete(self) -> bool:
        return all(slot_has_value(getattr(self, f)) for f in _REQUIRED_FOR_COMPLETION)

    def missing_fields(self) -> list:
        return [f for f in _REQUIRED_FOR_COMPLETION if not slot_has_value(getattr(self, f))]

    def render_summary(self) -> str:
        """Projection PURE de CE draft — jamais un texte mémorisé
        séparément (élimine la classe de bug `confirmation_summary`, voir
        docstring du module)."""
        if not slot_has_value(self.product) or not slot_has_value(self.quantity):
            return "Récapitulatif de la publication en cours de construction."
        unit_label = canonical_unit_label(self.unit or "KG")
        quantity_line = f"{_fmt_num(self.quantity)} {unit_label}".strip()
        base = f"Publication de {quantity_line} de {self.product}"
        if slot_has_value(self.price):
            base += f" à {_fmt_num(self.price)} FCFA/{unit_label}"
        lines = [base]
        if self.pricing_tiers:
            for tier in self.pricing_tiers:
                t_qty = tier.get("quantity")
                t_unit = tier.get("unit") or self.unit or "KG"
                t_price = tier.get("price")
                if t_qty is not None and t_price is not None:
                    lines.append(f"  • {_fmt_num(t_qty)} {t_unit} — {_fmt_num(t_price)} FCFA")
        if slot_has_value(self.description):
            lines.append(f"Description : {self.description}")
        if slot_has_value(self.category_label):
            lines.append(f"Catégorie : {self.category_label}")
        return "\n".join(lines)

    def execution_payload(self) -> Dict[str, Any]:
        """Le dict que `prep_sales_publish_product`/`SalesService.publish_product`
        consomme — SEUL point de contact avec le pipeline d'exécution MCP
        existant, volontairement non touché par cette refonte (même
        principe que `ProcurementDraft.execution_payload`)."""
        payload = {f: getattr(self, f) for f in _FIELD_NAMES if slot_has_value(getattr(self, f))}
        if isinstance(payload.get("pricing_tiers"), tuple):
            payload["pricing_tiers"] = list(payload["pricing_tiers"])
        return payload


# =====================================================================
# DOMAIN ACTIONS — ce que l'interprétation d'un message PEUT vouloir dire
# métier, jamais du texte libre au-delà de ce point
# =====================================================================


@dataclass(frozen=True)
class UpdateSalesPublishDraft:
    fields: Dict[str, Any]


@dataclass(frozen=True)
class ConfirmSalesPublishDraft:
    target: Optional[ConfirmationTarget]


@dataclass(frozen=True)
class RejectSalesPublishConfirmation:
    """« non » pendant une confirmation : invalide la cible de confirmation
    sans toucher au contenu — le producteur peut corriger un champ."""


@dataclass(frozen=True)
class CancelSalesPublishDraft:
    pass


@dataclass(frozen=True)
class NoSalesPublishAction:
    reason: Optional[str] = None


DomainAction = Union[
    UpdateSalesPublishDraft,
    ConfirmSalesPublishDraft,
    RejectSalesPublishConfirmation,
    CancelSalesPublishDraft,
    NoSalesPublishAction,
]


class SalesPublishOutcomeKind(str, Enum):
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
    SALES_PUBLISHED = "SALES_PUBLISHED"
    ALREADY_PUBLISHED = "ALREADY_PUBLISHED"
    SALES_FAILED = "SALES_FAILED"
    ALREADY_FAILED = "ALREADY_FAILED"
    SALES_EXECUTION_UNKNOWN = "SALES_EXECUTION_UNKNOWN"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    VERSION_CONFLICT = "VERSION_CONFLICT"


@dataclass(frozen=True)
class SalesPublishOutcome:
    kind: SalesPublishOutcomeKind
    draft: Optional[SalesPublishDraft]
    detail: Optional[str] = None


def _confirm_claim_key(draft: SalesPublishDraft) -> str:
    return f"sales_publish_confirm:{draft.draft_id}:{draft.version}"


def execution_key(draft: SalesPublishDraft) -> str:
    """Clé d'idempotence MÉTIER pour `create_product`, stable par
    draft/version — même limite honnête que PROCUREMENT : passée en
    `idempotency_key=` côté client, mais `create_product` n'a PAS de
    déduplication serveur aujourd'hui (voir `EXECUTION_UNKNOWN`, la garde
    qui compense ce manque côté client)."""
    return f"sales_publish:{draft.draft_id}:{draft.version}"


def apply_domain_action(
    draft: Optional[SalesPublishDraft],
    action: DomainAction,
    *,
    claim=None,
) -> SalesPublishOutcome:
    """LA seule fonction qui transitionne un `SalesPublishDraft`. Pure sur
    le draft (aucune I/O) sauf le claim d'idempotence pour CONFIRM."""
    if claim is None:
        claim = claim_once
    if draft is None:
        return SalesPublishOutcome(kind=SalesPublishOutcomeKind.NO_DRAFT, draft=None)

    if isinstance(action, UpdateSalesPublishDraft):
        try:
            new_draft = draft.with_updates(**action.fields)
        except IllegalDraftTransition:
            return SalesPublishOutcome(kind=SalesPublishOutcomeKind.DRAFT_FINALIZED, draft=draft)
        if new_draft is draft:
            return SalesPublishOutcome(kind=SalesPublishOutcomeKind.DRAFT_UNCHANGED, draft=new_draft)
        kind = (
            SalesPublishOutcomeKind.NEEDS_MORE_INFO
            if not new_draft.is_complete()
            else SalesPublishOutcomeKind.DRAFT_UPDATED
        )
        return SalesPublishOutcome(kind=kind, draft=new_draft)

    if isinstance(action, ConfirmSalesPublishDraft):
        if action.target is None:
            return SalesPublishOutcome(kind=SalesPublishOutcomeKind.NO_TARGET, draft=draft)
        # Politique de retry EXPLICITE par statut, ÉVALUÉE AVANT la
        # correspondance de version — même correctif que PROCUREMENT/
        # PREORDER (with_status bump la version à chaque transition
        # post-confirmation, un ConfirmationTarget capturé à EXECUTING ne
        # "matche" donc plus après coup — voir STALE_TARGET ci-dessous).
        if draft.status == SalesPublishDraftStatus.PUBLISHED:
            return SalesPublishOutcome(kind=SalesPublishOutcomeKind.ALREADY_PUBLISHED, draft=draft)
        if draft.status == SalesPublishDraftStatus.FAILED:
            return SalesPublishOutcome(kind=SalesPublishOutcomeKind.ALREADY_FAILED, draft=draft)
        if draft.status == SalesPublishDraftStatus.EXECUTION_UNKNOWN:
            return SalesPublishOutcome(
                kind=SalesPublishOutcomeKind.RECONCILIATION_REQUIRED, draft=draft
            )
        if draft.status == SalesPublishDraftStatus.EXECUTING:
            return SalesPublishOutcome(kind=SalesPublishOutcomeKind.ALREADY_EXECUTING, draft=draft)
        if draft.status == SalesPublishDraftStatus.CANCELLED:
            return SalesPublishOutcome(kind=SalesPublishOutcomeKind.DRAFT_FINALIZED, draft=draft)
        if draft.status != SalesPublishDraftStatus.DRAFT:
            return SalesPublishOutcome(kind=SalesPublishOutcomeKind.DRAFT_FINALIZED, draft=draft)
        if not action.target.matches(draft):
            return SalesPublishOutcome(
                kind=SalesPublishOutcomeKind.STALE_TARGET,
                draft=draft,
                detail=f"target={action.target.draft_id}/{action.target.draft_version}",
            )
        if not draft.is_complete():
            return SalesPublishOutcome(kind=SalesPublishOutcomeKind.NEEDS_MORE_INFO, draft=draft)
        if not claim(_confirm_claim_key(draft)):
            return SalesPublishOutcome(kind=SalesPublishOutcomeKind.ALREADY_EXECUTING, draft=draft)
        executing = draft._confirm_to_executing()
        return SalesPublishOutcome(
            kind=SalesPublishOutcomeKind.CONFIRMED_READY_FOR_EXECUTION, draft=executing
        )

    if isinstance(action, RejectSalesPublishConfirmation):
        return SalesPublishOutcome(kind=SalesPublishOutcomeKind.CONFIRMATION_REJECTED, draft=draft)

    if isinstance(action, CancelSalesPublishDraft):
        try:
            cancelled = draft.with_status(SalesPublishDraftStatus.CANCELLED)
        except IllegalDraftTransition:
            return SalesPublishOutcome(kind=SalesPublishOutcomeKind.DRAFT_FINALIZED, draft=draft)
        return SalesPublishOutcome(kind=SalesPublishOutcomeKind.CANCELLED, draft=cancelled)

    return SalesPublishOutcome(
        kind=SalesPublishOutcomeKind.DRAFT_UNCHANGED, draft=draft, detail=getattr(action, "reason", None)
    )


def resolve_domain_action(
    *,
    interpreted_event: str,
    extracted_entities: Dict[str, Any],
    pending_target: Optional[Dict[str, Any]],
) -> DomainAction:
    """Interpréteur → `DomainAction` — le domaine ne reçoit QUE ceci,
    jamais "oui"/"non"/"je confirme" en texte libre (mandat §13)."""
    event = str(interpreted_event or "").upper()
    if event == "CONFIRM":
        target = ConfirmationTarget.from_dict(pending_target)
        return ConfirmSalesPublishDraft(target=target)
    if event == "REJECT":
        return RejectSalesPublishConfirmation()
    if event == "CANCEL":
        return CancelSalesPublishDraft()
    fields = {k: v for k, v in (extracted_entities or {}).items() if k in _FIELD_NAMES}
    if fields:
        return UpdateSalesPublishDraft(fields=fields)
    return NoSalesPublishAction(reason=f"unhandled_event:{event}" if event else None)


# =====================================================================
# EXÉCUTION EXTERNE — adaptateur MCP → domaine
# =====================================================================


@dataclass(frozen=True)
class SalesPublishExecutionResult:
    """Le domaine ne dépend JAMAIS directement du format de réponse MCP —
    `adapt_mcp_result` est l'UNIQUE point de contact avec ce format."""

    success: bool
    product_id: Optional[str] = None
    error: Optional[str] = None
    ambiguous: bool = False


def adapt_mcp_result(
    execution_status: Optional[str], execution_result: Optional[Dict[str, Any]]
) -> SalesPublishExecutionResult:
    """`nodes/executor.py::mcp_tool_executor` (générique, inchangé) → ce
    que le domaine comprend. SEUL adaptateur du format `create_product` —
    le reste du module ne connaît que `SalesPublishExecutionResult`."""
    status = str(execution_status or "").upper()
    result = execution_result if isinstance(execution_result, dict) else {}
    if status == "COMPLETED":
        return SalesPublishExecutionResult(
            success=True, product_id=result.get("product_id") or (result.get("data") or {}).get("product_id")
        )
    if status == "ERROR":
        error = str(result.get("message") or result.get("error") or "Erreur inconnue")
        return SalesPublishExecutionResult(success=False, error=error)
    return SalesPublishExecutionResult(success=False, ambiguous=True)


def finalize_after_execution(
    draft: SalesPublishDraft, result: SalesPublishExecutionResult
) -> SalesPublishDraft:
    """EXECUTING → PUBLISHED/FAILED/EXECUTION_UNKNOWN. N'accepte QUE
    `draft.status == EXECUTING`."""
    if result.ambiguous:
        return draft.with_status(SalesPublishDraftStatus.EXECUTION_UNKNOWN)
    if result.success:
        return draft.with_status(SalesPublishDraftStatus.PUBLISHED)
    return draft.with_status(SalesPublishDraftStatus.FAILED)


# =====================================================================
# RESPONSE PLAN — présentation PURE, mécanique côté appelant
# =====================================================================


@dataclass(frozen=True)
class SalesPublishResponsePlan:
    final_response: str
    response_strategy: str
    graph_status: str
    draft: Optional[SalesPublishDraft]
    pending_kind: Optional[str] = None
    pending_field: Optional[str] = None
    pending_target: Optional[Dict[str, Any]] = None
    ready_for_execution: bool = False
    terminal_goal_reset: bool = False
    pending_untouched: bool = False


def _missing_field_prompt(missing) -> str:
    if not missing:
        return "Il me manque encore une information pour continuer."
    return f"J'ai encore besoin de : {_MISSING_FIELD_LABELS.get(missing[0], missing[0])}."


def build_response_plan(
    outcome: SalesPublishOutcome, *, deviation_note: Optional[str] = None
) -> SalesPublishResponsePlan:
    draft = outcome.draft
    kind = outcome.kind

    if kind == SalesPublishOutcomeKind.NO_DRAFT:
        return SalesPublishResponsePlan(
            final_response="", response_strategy="SUCCESS", graph_status="PLANNING",
            draft=None, pending_untouched=True,
        )

    if kind == SalesPublishOutcomeKind.NEEDS_MORE_INFO:
        missing = draft.missing_fields()
        return SalesPublishResponsePlan(
            final_response=_missing_field_prompt(missing),
            response_strategy="ASK_MISSING_FIELD",
            graph_status="WAITING_INPUT",
            draft=draft,
            pending_kind="ENTER_FIELD",
            pending_field=missing[0] if missing else None,
        )

    if kind == SalesPublishOutcomeKind.DRAFT_UPDATED:
        return SalesPublishResponsePlan(
            final_response=f"{draft.render_summary()}\n\nConfirmez-vous ?",
            response_strategy="CONFIRMATION",
            graph_status="WAITING_INPUT",
            draft=draft,
            pending_kind="CONFIRM_ACTION",
            pending_target={"draft_id": draft.draft_id, "draft_version": draft.version},
        )

    if kind == SalesPublishOutcomeKind.DRAFT_UNCHANGED:
        summary = draft.render_summary() if draft else ""
        body = f"{deviation_note}\n\n{summary}" if deviation_note else summary
        return SalesPublishResponsePlan(
            final_response=f"{body}\n\nConfirmez-vous ?" if draft else body,
            response_strategy="CONFIRMATION",
            graph_status="WAITING_INPUT",
            draft=draft,
            pending_kind="CONFIRM_ACTION" if draft else None,
            pending_target=(
                {"draft_id": draft.draft_id, "draft_version": draft.version} if draft else None
            ),
        )

    if kind == SalesPublishOutcomeKind.CONFIRMATION_REJECTED:
        return SalesPublishResponsePlan(
            final_response=(
                "D'accord, ce n'est pas encore confirmé. Dites-moi ce que "
                "vous voulez modifier (prix, quantité, description), ou "
                "répondez *annuler* pour abandonner."
            ),
            response_strategy="SUCCESS", graph_status="PLANNING", draft=draft,
        )

    if kind == SalesPublishOutcomeKind.CANCELLED:
        return SalesPublishResponsePlan(
            final_response="Publication annulée. Que souhaitez-vous faire ?",
            response_strategy="CLARIFICATION", graph_status="COMPLETED",
            draft=None, terminal_goal_reset=True,
        )

    if kind == SalesPublishOutcomeKind.DRAFT_FINALIZED:
        label = {
            SalesPublishDraftStatus.PUBLISHED: "déjà publiée",
            SalesPublishDraftStatus.FAILED: "déjà traitée (échec de publication)",
            SalesPublishDraftStatus.CANCELLED: "déjà annulée",
        }.get(draft.status if draft else None, "déjà finalisée")
        return SalesPublishResponsePlan(
            final_response=f"Cette publication est {label} — rien à modifier ici.",
            response_strategy="SUCCESS", graph_status="COMPLETED", draft=draft,
        )

    if kind in (SalesPublishOutcomeKind.STALE_TARGET, SalesPublishOutcomeKind.VERSION_CONFLICT):
        if draft is None:
            return SalesPublishResponsePlan(
                final_response="", response_strategy="SUCCESS", graph_status="PLANNING", draft=None
            )
        return SalesPublishResponsePlan(
            final_response=(
                f"{draft.render_summary()}\n\nL'offre a changé depuis "
                "— confirmez-vous CETTE version ?"
            ),
            response_strategy="CONFIRMATION",
            graph_status="WAITING_INPUT",
            draft=draft,
            pending_kind="CONFIRM_ACTION",
            pending_target={"draft_id": draft.draft_id, "draft_version": draft.version},
        )

    if kind == SalesPublishOutcomeKind.NO_TARGET:
        return SalesPublishResponsePlan(
            final_response="", response_strategy="SUCCESS", graph_status="PLANNING",
            draft=draft, pending_untouched=True,
        )

    if kind == SalesPublishOutcomeKind.CONFIRMED_READY_FOR_EXECUTION:
        return SalesPublishResponsePlan(
            final_response="",  # rendu par le pipeline d'exécution générique
            response_strategy="SUCCESS", graph_status="EXECUTING",
            draft=draft, ready_for_execution=True,
        )

    if kind == SalesPublishOutcomeKind.ALREADY_EXECUTING:
        return SalesPublishResponsePlan(
            final_response=(
                "Cette publication est en cours de traitement — je vous "
                "confirme dès que c'est fait, inutile de renvoyer *confirme*."
            ),
            response_strategy="SUCCESS", graph_status="COMPLETED", draft=draft,
        )

    if kind == SalesPublishOutcomeKind.ALREADY_PUBLISHED:
        return SalesPublishResponsePlan(
            final_response="Ce produit a déjà été publié — rien à refaire.",
            response_strategy="SUCCESS", graph_status="COMPLETED", draft=draft,
        )

    if kind == SalesPublishOutcomeKind.ALREADY_FAILED:
        return SalesPublishResponsePlan(
            final_response=(
                "Cette publication n'a pas pu aboutir précédemment — "
                "dites-moi si vous voulez relancer une nouvelle publication."
            ),
            response_strategy="SUCCESS", graph_status="COMPLETED", draft=draft,
        )

    if kind == SalesPublishOutcomeKind.RECONCILIATION_REQUIRED:
        return SalesPublishResponsePlan(
            final_response=(
                "Je ne suis pas certain que cette publication ait bien été "
                "enregistrée — notre équipe vérifie et vous recontacte. "
                "Merci de ne pas relancer la même demande entre-temps."
            ),
            response_strategy="SUCCESS", graph_status="COMPLETED", draft=draft,
        )

    if kind == SalesPublishOutcomeKind.SALES_PUBLISHED:
        return SalesPublishResponsePlan(
            final_response=(
                f"✅ {draft.product} est maintenant en vente sur AgriConnect !"
                if draft else "✅ Votre produit a bien été publié."
            ),
            response_strategy="SUCCESS", graph_status="COMPLETED", draft=draft,
        )

    if kind == SalesPublishOutcomeKind.SALES_FAILED:
        return SalesPublishResponsePlan(
            final_response=(
                "La publication de votre produit a échoué. Vous pouvez "
                "relancer une nouvelle publication."
            ),
            response_strategy="ERROR", graph_status="COMPLETED", draft=draft,
        )

    if kind == SalesPublishOutcomeKind.SALES_EXECUTION_UNKNOWN:
        return SalesPublishResponsePlan(
            final_response=(
                "Un incident technique m'empêche de confirmer si votre "
                "produit a bien été publié — notre équipe vérifie et vous "
                "recontacte."
            ),
            response_strategy="ERROR", graph_status="COMPLETED", draft=draft,
        )

    raise AssertionError(f"SalesPublishOutcomeKind non couvert par build_response_plan: {kind!r}")


# =====================================================================
# INVARIANT — vérifiable à l'exécution
# =====================================================================


def check_confirmation_target_invariant(
    pending_target: Optional[Dict[str, Any]],
    draft: Optional[SalesPublishDraft],
) -> Optional[str]:
    """Défensif, log-only — même contrat STRICT que PROCUREMENT (compare
    `draft_id` ET `draft_version`, pas seulement `draft_id` — voir le
    rapport final section A pour la divergence trouvée entre PROCUREMENT
    et PREORDER sur ce point précis, non reproduite ici)."""
    if pending_target is None:
        return None
    target = ConfirmationTarget.from_dict(pending_target)
    if target is None:
        return "pending target malformé"
    if not target.matches(draft):
        draft_desc = f"{draft.draft_id}/{draft.version}" if draft else "None"
        return (
            f"confirmation target ({target.draft_id}/{target.draft_version}) "
            f"ne correspond pas au draft courant ({draft_desc})"
        )
    return None


__all__ = [
    "SalesPublishDraftStatus",
    "SalesPublishDraft",
    "IllegalDraftTransition",
    "UpdateSalesPublishDraft",
    "ConfirmSalesPublishDraft",
    "RejectSalesPublishConfirmation",
    "CancelSalesPublishDraft",
    "NoSalesPublishAction",
    "DomainAction",
    "SalesPublishOutcomeKind",
    "SalesPublishOutcome",
    "execution_key",
    "apply_domain_action",
    "resolve_domain_action",
    "SalesPublishExecutionResult",
    "adapt_mcp_result",
    "finalize_after_execution",
    "SalesPublishResponsePlan",
    "build_response_plan",
    "check_confirmation_target_invariant",
]
