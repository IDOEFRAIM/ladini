"""ProcurementDraft — canonical, versioned transactional entity for the
PROCUREMENT_CREATE_REQUEST conversational flow (auction creation).

## Pourquoi ce module existe (2026-09-03, refonte architecturale)

Incident réel : un utilisateur corrigeant sa quantité pendant la
confirmation ("2 tonnes et 250 kg" puis "1 tonne et 125 kg") voyait le
récapitulatif rester figé sur la 1ère valeur. Root cause immédiate :
`quantity`/`unit` (contrat d'exécution) et `quantity_display`/`unit_display`
(texte affiché) étaient deux représentations INDÉPENDANTES du même fait
métier, maintenues séparément dans `transaction_payload` (`merge_dict`,
sémantique "fusionne, ne remplace jamais") — rien ne garantissait qu'elles
restent synchronisées. Un correctif ponctuel (`refresh_display=True`,
[[precommande-architecture-consolidation-2026-08]] et suivants) a réparé LE
symptôme observé, mais la classe de bug reste possible pour tout autre champ
tant que l'état transactionnel n'a pas UNE représentation canonique unique.

Ce module élimine la classe de bug structurellement :

- `ProcurementDraft` est un objet immuable UNIQUE portant TOUS les champs
  métier (`product`/`quantity`/`unit`/`price`/`price_unit`/`deadline`).
  Il n'existe qu'une seule paire (quantity, unit) — jamais de copie
  "display" séparée. `render_summary()` est une PROJECTION PURE de cet
  objet, jamais un texte mémorisé indépendamment.
- Toute mutation produit une NOUVELLE version (`with_updates` bump
  `version`, jamais de mutation en place) — un draft déjà proposé à la
  confirmation ne peut donc jamais changer sous les pieds de l'utilisateur ;
  seule une NOUVELLE version existe après une correction.
- `ConfirmationTarget(draft_id, draft_version)` — ce que
  `PendingInteraction(kind=CONFIRM_ACTION).target` référence — lie la
  confirmation à UNE version précise. `apply_domain_action` refuse
  d'exécuter si la cible ne correspond plus à la version courante
  (`STALE_TARGET`) : un ancien target ne peut jamais confirmer une nouvelle
  version, et une nouvelle mutation invalide silencieusement toute
  confirmation en attente (elle vise une version qui n'existe plus comme
  version COURANTE).
- La confirmation est une réservation logique et idempotente d'une
  version : `draft.status` transite DRAFT → CONFIRMED → EXECUTING en un
  seul appel, gardé par un claim atomique (`core/idempotency.py`, Redis
  SET-NX-EX — sûr sous concurrence RÉELLE, pas seulement au niveau Python).
  Un 2e CONFIRM sur la même version (retry Celery, double webhook, deux
  workers concurrents) obtient une réponse informative dédiée par statut
  (`ALREADY_EXECUTING`/`ALREADY_EXECUTED`/`ALREADY_FAILED`/
  `RECONCILIATION_REQUIRED`), jamais une ré-exécution (mandat §12).

## Ce que ce module NE fait PAS

Il ne remplace pas `nodes/executor.py::mcp_tool_executor` ni
`actions/procure.py::prep_procurement_create_request` — l'appel MCP réel
(`create_auction`) reste inchangé, exécuté par le pipeline générique une
fois `ConfirmProcurementDraft` a produit `CONFIRMED_READY_FOR_EXECUTION` et
que l'appelant a posé `transaction_payload = draft.execution_payload()`
(voir `flows/buyer/procurement_confirmation.py`).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Dict, Optional, Union

from ladini.core.formatting import fmt_num as _fmt_num
from ladini.core.idempotency import claim_once
from ladini.graphs.agents.market_coach.core.confirmation_target import (
    ConfirmationTarget,
)
from ladini.graphs.agents.market_coach.utils import (
    canonical_unit_label,
    slot_has_value,
)

# =====================================================================
# DRAFT — état transactionnel canonique
# =====================================================================


class ProcurementDraftStatus(str, Enum):
    """Où en est LA TRANSACTION — distinct de `PendingInteraction`, qui dit
    ce qu'attend le PROCHAIN message (mandat §14/§8).

    (2026-09-03, clôture du pipeline d'exécution) : `EXECUTING` EST
    persisté — c'est ce qui rend `EXECUTING + CONFIRM → ALREADY_EXECUTING`
    (mandat §12) possible : un 2e worker qui voit ce statut ne relance
    jamais l'effet externe, qu'il soit réellement en cours ou bloqué par un
    crash passé (les deux cas produisent la MÊME réponse sûre : ne rien
    refaire). `EXECUTION_UNKNOWN` existe pour le cas où la finalisation
    elle-même ne peut pas trancher succès/échec (mandat §5) — jamais
    fabriqué comme un faux `EXECUTED`/`FAILED`."""

    DRAFT = "DRAFT"  # champs en cours de collecte/correction, mutable
    CONFIRMED = "CONFIRMED"  # utilisateur a validé CETTE version — transitoire, voir EXECUTING
    EXECUTING = "EXECUTING"  # appel MCP en cours/tenté — persisté, empêche une 2e tentative
    EXECUTED = "EXECUTED"  # écriture MCP confirmée réussie — terminal
    FAILED = "FAILED"  # écriture MCP confirmée échouée — terminal (≠ WAITING_CONFIRMATION, mandat §13)
    EXECUTION_UNKNOWN = "EXECUTION_UNKNOWN"  # issue de l'appel MCP indéterminable — nécessite réconciliation, jamais un retry aveugle
    CANCELLED = "CANCELLED"  # utilisateur a rejeté — terminal


_FIELD_NAMES = ("product", "quantity", "unit", "price", "price_unit", "deadline")
_REQUIRED_FOR_COMPLETION = ("product", "quantity", "unit", "price")


class IllegalDraftTransition(RuntimeError):
    """Levée quand une mutation demande une transition absente de
    `_ALLOWED_TRANSITIONS` (mandat §6/§7) : détecte ET empêche l'exécution,
    ne se contente jamais de journaliser puis continuer. Réservée aux
    violations structurelles (un appelant a mal utilisé le contrat) —
    distincte de `ProcurementOutcomeKind.STALE_TARGET`/`DRAFT_FINALIZED`,
    qui sont des désaccords MÉTIER normaux (l'utilisateur a agi en retard),
    jamais des exceptions : `apply_domain_action` intercepte cette
    exception et la traduit en `ProcurementOutcome`, jamais laissée fuiter
    jusqu'au nœud du graphe."""


# Machine d'état EXPLICITE et CENTRALISÉE (mandat §7/§10) — toute transition
# absente de cette table est REFUSÉE par `_transition`, jamais appliquée
# silencieusement. `DRAFT → DRAFT` représente `with_updates` (nouvelle
# version, même statut) : légal UNIQUEMENT depuis DRAFT — un draft déjà
# CONFIRMED/EXECUTING/EXECUTED/FAILED/EXECUTION_UNKNOWN/CANCELLED est un
# instantané figé, jamais remuté. `CONFIRMED → EXECUTING` est appliqué en
# UN SEUL appel (`apply_domain_action`, jamais observable séparément —
# voir docstring de `ProcurementDraftStatus.EXECUTING`). EXECUTED/FAILED/
# EXECUTION_UNKNOWN/CANCELLED sont terminaux pour ce module : aucune
# transition automatique sortante — `EXECUTION_UNKNOWN` nécessite une
# réconciliation humaine (mandat §5), jamais un retry aveugle du code.
_ALLOWED_TRANSITIONS: Dict[ProcurementDraftStatus, frozenset] = {
    ProcurementDraftStatus.DRAFT: frozenset(
        {
            ProcurementDraftStatus.DRAFT,
            ProcurementDraftStatus.CONFIRMED,
            ProcurementDraftStatus.CANCELLED,
        }
    ),
    ProcurementDraftStatus.CONFIRMED: frozenset({ProcurementDraftStatus.EXECUTING}),
    ProcurementDraftStatus.EXECUTING: frozenset(
        {
            ProcurementDraftStatus.EXECUTED,
            ProcurementDraftStatus.FAILED,
            ProcurementDraftStatus.EXECUTION_UNKNOWN,
        }
    ),
    ProcurementDraftStatus.EXECUTED: frozenset(),
    ProcurementDraftStatus.FAILED: frozenset(),
    ProcurementDraftStatus.EXECUTION_UNKNOWN: frozenset(),
    ProcurementDraftStatus.CANCELLED: frozenset(),
}


@dataclass(frozen=True)
class ProcurementDraft:
    draft_id: str
    version: int
    status: ProcurementDraftStatus
    product: Optional[str] = None
    quantity: Optional[float] = None
    unit: Optional[str] = None
    price: Optional[float] = None
    price_unit: Optional[str] = None
    deadline: Optional[str] = None
    created_at: float = 0.0

    # ------------------------------------------------------------
    # (de)sérialisation — pour le canal d'état `procurement_draft`
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
            "price_unit": self.price_unit,
            "deadline": self.deadline,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> Optional["ProcurementDraft"]:
        if not isinstance(d, dict) or not d.get("draft_id"):
            return None
        try:
            status = ProcurementDraftStatus(d.get("status") or "DRAFT")
        except ValueError:
            status = ProcurementDraftStatus.DRAFT
        return cls(
            draft_id=str(d["draft_id"]),
            version=int(d.get("version") or 1),
            status=status,
            product=d.get("product"),
            quantity=d.get("quantity"),
            unit=d.get("unit"),
            price=d.get("price"),
            price_unit=d.get("price_unit"),
            deadline=d.get("deadline"),
            created_at=float(d.get("created_at") or 0.0),
        )

    @classmethod
    def new(cls, draft_id: str, **fields: Any) -> "ProcurementDraft":
        clean = {k: v for k, v in fields.items() if k in _FIELD_NAMES and slot_has_value(v)}
        return cls(
            draft_id=draft_id,
            version=1,
            status=ProcurementDraftStatus.DRAFT,
            created_at=time.time(),
            **clean,
        )

    # ------------------------------------------------------------
    # mutation — TOUJOURS une nouvelle version, jamais en place, TOUJOURS
    # via `_transition` (aucun autre point du fichier n'appelle `replace()`
    # directement sur `status` — machine d'état centralisée, mandat §7)
    # ------------------------------------------------------------
    def _transition(self, new_status: ProcurementDraftStatus, **extra: Any) -> "ProcurementDraft":
        allowed = _ALLOWED_TRANSITIONS.get(self.status, frozenset())
        if new_status not in allowed:
            raise IllegalDraftTransition(
                f"{self.status.value} -> {new_status.value} n'est pas une "
                f"transition autorisée (draft={self.draft_id}/v{self.version})"
            )
        return replace(self, status=new_status, **extra)

    def with_updates(self, **fields: Any) -> "ProcurementDraft":
        """Applique des changements de CE tour et retourne une NOUVELLE
        version. Ne bump la version QUE si un champ change réellement — un
        UPDATE qui ne change rien (bruit LLM, champ déjà identique) ne doit
        pas invalider une confirmation en attente pour rien. Lève
        `IllegalDraftTransition` si `self.status != DRAFT` — un draft déjà
        CONFIRMED/EXECUTED/FAILED/CANCELLED ne se corrige jamais en place ;
        `apply_domain_action` intercepte et traduit en `DRAFT_FINALIZED`."""
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
        transitioned = self._transition(ProcurementDraftStatus.DRAFT)
        return replace(transitioned, version=self.version + 1, created_at=time.time(), **changed)

    def with_status(self, status: ProcurementDraftStatus) -> "ProcurementDraft":
        """Transition de statut PERSISTABLE — bump la version (2026-09-03,
        clôture du gap de persistance) : la colonne `version` en base
        protège la LIGNE entière (statut ET payload), pas seulement les
        champs métier — un `compare_and_swap` dont le statut change mais
        pas la version laisserait une écriture concurrente PÉRIMÉE
        matcher le même `WHERE version = :expected` et écraser la
        transition qu'on vient de faire (bug réel trouvé par
        `tests/architecture/test_procurement_draft_persistence.py::
        TestRealThreadConcurrencyAgainstCompareAndSwap`). Pour une
        transition composée en plusieurs hops internes non-observables
        (ex: CONFIRM = DRAFT→CONFIRMED→EXECUTING), voir
        `_confirm_to_executing` qui ne bump qu'UNE fois pour l'ENSEMBLE."""
        transitioned = self._transition(status)
        return replace(transitioned, version=self.version + 1)

    def _confirm_to_executing(self) -> "ProcurementDraft":
        """DRAFT → CONFIRMED → EXECUTING comme UNE SEULE transition
        métier (l'événement CONFIRM), jamais observable en CONFIRMED
        intermédiaire — donc UN SEUL incrément de version pour les deux
        hops, pas deux (contrairement à deux appels `with_status`
        successifs)."""
        confirmed = self._transition(ProcurementDraftStatus.CONFIRMED)
        executing = confirmed._transition(ProcurementDraftStatus.EXECUTING)
        return replace(executing, version=self.version + 1)

    # ------------------------------------------------------------
    # lecture
    # ------------------------------------------------------------
    def is_complete(self) -> bool:
        return all(slot_has_value(getattr(self, f)) for f in _REQUIRED_FOR_COMPLETION)

    def missing_fields(self) -> list:
        return [f for f in _REQUIRED_FOR_COMPLETION if not slot_has_value(getattr(self, f))]

    def render_summary(self) -> str:
        """Projection PURE — aucun état mémorisé séparément (mandat §8).
        Toujours dérivée de CE draft, jamais d'un texte d'un tour antérieur."""
        if not slot_has_value(self.quantity) or not slot_has_value(self.product):
            return "Récapitulatif de l'appel d'offres en cours de construction."
        unit_label = canonical_unit_label(self.unit or "KG")
        quantity_line = f"{_fmt_num(self.quantity)} {unit_label}".strip()
        price_unit_label = canonical_unit_label(self.price_unit or self.unit or "KG")
        base = f"Lancement d'un appel d'offres pour {quantity_line} de {self.product}"
        if slot_has_value(self.price):
            return f"{base} au prix plafond de {_fmt_num(self.price)} FCFA/{price_unit_label}."
        return f"{base}."

    def execution_payload(self) -> Dict[str, Any]:
        """Le dict `transaction_payload`-shaped que
        `actions/procure.py::prep_procurement_create_request` (inchangé)
        consomme — SEUL point de contact avec le pipeline d'exécution MCP
        existant, volontairement non touché par cette refonte."""
        payload = {f: getattr(self, f) for f in _FIELD_NAMES if slot_has_value(getattr(self, f))}
        return payload


# `ConfirmationTarget` (2026-09-03, hardening transverse) — extrait vers
# `core/confirmation_target.py` (importé en tête de module), réutilisé (pas
# redéfini) : cette copie locale était identique à celle de
# `preorder_draft.py`, et une 3e copie allait apparaître pour SALES.
# Réexporté sous le même nom pour ne rien casser côté appelants
# (`from .procurement_draft import ConfirmationTarget` continue de
# fonctionner).


# =====================================================================
# DOMAIN ACTIONS — ce que l'interprétation d'un message PEUT vouloir dire
# métier, jamais du texte libre au-delà de ce point (mandat §7, §9, §11)
# =====================================================================


@dataclass(frozen=True)
class UpdateProcurementDraft:
    fields: Dict[str, Any]


@dataclass(frozen=True)
class ConfirmProcurementDraft:
    target: Optional[ConfirmationTarget]


@dataclass(frozen=True)
class RejectProcurementConfirmation:
    """« non » pendant une confirmation : invalide la cible de confirmation
    courante SANS toucher aux champs du draft — l'utilisateur reste libre de
    corriger au tour suivant. Distinct d'un abandon complet : celui-ci est
    déjà géré EN AMONT par `goal_planner` RULE 1 (un REJECT SANS confirmation
    en attente efface le goal entièrement) — ce module n'est jamais atteint
    dans ce cas, donc il n'a besoin de représenter que le rejet "doux"."""

    pass


@dataclass(frozen=True)
class CancelProcurementDraft:
    """Abandon EXPLICITE et définitif du draft (ex: "annuler l'appel
    d'offres"), distinct d'un simple REJECT pendant la confirmation — voir
    `RejectProcurementConfirmation`. Non câblé à un événement interpréteur
    aujourd'hui (aucun intent dédié "CANCEL" n'existe encore) ; conservé
    dans ce contrat pour quand ce signal existera, plutôt que de surcharger
    REJECT avec deux sémantiques différentes."""

    pass


@dataclass(frozen=True)
class NoProcurementAction:
    reason: str


DomainAction = Union[
    UpdateProcurementDraft,
    ConfirmProcurementDraft,
    RejectProcurementConfirmation,
    CancelProcurementDraft,
    NoProcurementAction,
]


def resolve_domain_action(
    *,
    interpreted_event: str,
    extracted_entities: Dict[str, Any],
    pending_target: Optional[Dict[str, Any]],
) -> DomainAction:
    """`InterpreterResult` (event + entités structurées) → UNE `DomainAction`.

    Aucune comparaison de chaîne ici — `interpreted_event` est déjà le
    résultat canonique produit par l'interpréteur (fast-path OU LLM,
    contrat identique, voir `interpreter/routing.py`). Ce point de passage
    ne réinterprète jamais le texte utilisateur (mandat §9, §11)."""
    event = str(interpreted_event or "").upper().strip()

    if event == "CONFIRM":
        return ConfirmProcurementDraft(target=ConfirmationTarget.from_dict(pending_target))

    if event == "REJECT":
        return RejectProcurementConfirmation()

    if event in {"UPDATE", "ANSWER"}:
        fields = {
            k: v
            for k, v in (extracted_entities or {}).items()
            if k in _FIELD_NAMES and slot_has_value(v)
        }
        if fields:
            return UpdateProcurementDraft(fields=fields)
        return NoProcurementAction(reason="no_structured_fields")

    return NoProcurementAction(reason=f"unhandled_event:{event}")


# =====================================================================
# DOMAIN OUTCOME + APPLICATION — la SEULE autorité de mutation du draft
# =====================================================================


class ProcurementOutcomeKind(str, Enum):
    DRAFT_UPDATED = "DRAFT_UPDATED"
    DRAFT_UNCHANGED = "DRAFT_UNCHANGED"
    NEEDS_MORE_INFO = "NEEDS_MORE_INFO"
    CONFIRMED_READY_FOR_EXECUTION = "CONFIRMED_READY_FOR_EXECUTION"
    STALE_TARGET = "STALE_TARGET"
    NO_TARGET = "NO_TARGET"
    CONFIRMATION_REJECTED = "CONFIRMATION_REJECTED"
    CANCELLED = "CANCELLED"
    NO_DRAFT = "NO_DRAFT"
    # (mandat §7) : une tentative de MUTER un draft déjà terminal
    # (CONFIRMED/EXECUTING/EXECUTED/FAILED/EXECUTION_UNKNOWN/CANCELLED) —
    # `IllegalDraftTransition` interceptée et traduite ici, jamais laissée
    # fuiter comme exception jusqu'au nœud du graphe.
    DRAFT_FINALIZED = "DRAFT_FINALIZED"
    # (2026-09-03, clôture du pipeline d'exécution, mandat §12) : politique
    # de retry EXPLICITE par statut — un CONFIRM retrouvant un draft déjà
    # engagé dans l'exécution obtient TOUJOURS une réponse informative
    # distincte, jamais une ré-exécution, jamais la même étiquette
    # générique pour des situations différentes.
    ALREADY_EXECUTING = "ALREADY_EXECUTING"  # EXECUTING + CONFIRM
    PROCUREMENT_EXECUTED = "PROCUREMENT_EXECUTED"  # finalisation réussie (terminal)
    ALREADY_EXECUTED = "ALREADY_EXECUTED"  # EXECUTED + CONFIRM (retry tardif)
    PROCUREMENT_FAILED = "PROCUREMENT_FAILED"  # finalisation en échec net (terminal)
    ALREADY_FAILED = "ALREADY_FAILED"  # FAILED + CONFIRM (retry tardif)
    PROCUREMENT_EXECUTION_UNKNOWN = "PROCUREMENT_EXECUTION_UNKNOWN"  # finalisation ambiguë (terminal, réconciliation requise)
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"  # EXECUTION_UNKNOWN + CONFIRM
    # (2026-09-03, persistance transactionnelle, mandat §4/§7) : la décision
    # domaine était VALIDE au moment de `apply_domain_action` (target/statut
    # cohérents) mais l'écriture atomique en base
    # (`services/database/procurement_draft_store.py::compare_and_swap`) a
    # échoué — un AUTRE écrivain a persisté une version concurrente entre
    # notre lecture et notre écriture. Distinct de `STALE_TARGET` (qui
    # détecte l'incohérence AVANT toute tentative d'écriture, à partir de
    # l'état déjà en mémoire) : celui-ci est LA preuve que la protection
    # existe au niveau de la persistance elle-même, pas seulement en Python.
    VERSION_CONFLICT = "VERSION_CONFLICT"


@dataclass(frozen=True)
class ProcurementOutcome:
    kind: ProcurementOutcomeKind
    draft: Optional[ProcurementDraft]
    detail: Optional[str] = None


def _confirm_claim_key(draft: ProcurementDraft) -> str:
    return f"procurement_confirm:{draft.draft_id}:{draft.version}"


def execution_key(draft: ProcurementDraft) -> str:
    """Clé d'idempotence MÉTIER, stable par draft/version (mandat §4) —
    destinée à `idempotency_key=` sur l'appel MCP `create_auction` (voir
    `infrastructure/mcp/client.py::AgriMCPClient.call_tool`, qui accepte
    déjà ce paramètre). Un retry du MÊME draft/version réutilise TOUJOURS
    la même clé. NOTE HONNÊTE (audité, pas supposé) : le serveur MCP reçoit
    cette clé (`_idempotency_key` dans les arguments) mais
    `AgriDBMCPServer.call_tool` (infrastructure/mcp/runtime.py) la retire
    AVANT dispatch vers `create_auction`, qui n'a donc AUCUNE déduplication
    côté serveur aujourd'hui — la clé est déjà correcte et prête pour le
    jour où ce chantier serveur sera fait, mais ne garantit PAS
    l'exactly-once tant que ce n'est pas le cas (voir
    `ProcurementDraftStatus.EXECUTION_UNKNOWN`, la garde qui compense ce
    manque côté client)."""
    return f"procurement:{draft.draft_id}:{draft.version}"


def apply_domain_action(
    draft: Optional[ProcurementDraft],
    action: DomainAction,
    *,
    claim=None,
) -> ProcurementOutcome:
    """LA seule fonction qui transitionne un `ProcurementDraft`. Pure sur le
    draft lui-même (aucune I/O) sauf le claim d'idempotence pour CONFIRM
    (délibérément impur — c'est le point d'atomicité transactionnelle,
    mandat §12/§20/§21)."""
    if claim is None:
        # Résolu au moment de l'APPEL, pas à la définition de la fonction —
        # un défaut `claim=claim_once` figerait la référence au chargement
        # du module et rendrait tout monkeypatch de test sur
        # `procurement_draft.claim_once` sans effet.
        claim = claim_once
    if draft is None and not isinstance(action, UpdateProcurementDraft):
        return ProcurementOutcome(kind=ProcurementOutcomeKind.NO_DRAFT, draft=None)

    if isinstance(action, UpdateProcurementDraft):
        base = draft or ProcurementDraft.new(draft_id=_new_draft_id())
        try:
            new_draft = base.with_updates(**action.fields)
        except IllegalDraftTransition:
            # Un message arrive après que CE draft a déjà été
            # confirmé/exécuté/annulé (retry tardif, message hors-ordre) —
            # jamais muté en place : le draft terminal reste intact, tel quel.
            return ProcurementOutcome(kind=ProcurementOutcomeKind.DRAFT_FINALIZED, draft=base)
        if new_draft is base and draft is not None:
            return ProcurementOutcome(kind=ProcurementOutcomeKind.DRAFT_UNCHANGED, draft=new_draft)
        kind = (
            ProcurementOutcomeKind.NEEDS_MORE_INFO
            if not new_draft.is_complete()
            else ProcurementOutcomeKind.DRAFT_UPDATED
        )
        return ProcurementOutcome(kind=kind, draft=new_draft)

    if isinstance(action, ConfirmProcurementDraft):
        if action.target is None:
            return ProcurementOutcome(kind=ProcurementOutcomeKind.NO_TARGET, draft=draft)
        # Politique de retry EXPLICITE par statut (mandat §12) — ÉVALUÉE
        # AVANT la correspondance de version (2026-09-03, correctif suite
        # au bump de version introduit par `with_status` : un retry Celery
        # rejoue le MÊME `ConfirmationTarget` que celui qui a fait passer
        # le draft en EXECUTING — cette version-là n'est donc plus celle du
        # draft PERSISTÉ, par construction, une fois la confirmation
        # appliquée). `STALE_TARGET` ne peut avoir de sens QUE tant que le
        # draft est encore `DRAFT` (une édition CONCURRENTE, pas la
        # confirmation elle-même, a fait avancer la version) — une fois
        # sorti de DRAFT, seul le statut compte pour décider de la réponse,
        # jamais un numéro de version devenu obsolète par la confirmation
        # elle-même. AUCUN comportement implicite : chaque statut connu
        # produit exactement UNE réponse informative distincte, jamais une
        # ré-exécution.
        if draft.status == ProcurementDraftStatus.EXECUTED:
            return ProcurementOutcome(kind=ProcurementOutcomeKind.ALREADY_EXECUTED, draft=draft)
        if draft.status == ProcurementDraftStatus.FAILED:
            return ProcurementOutcome(kind=ProcurementOutcomeKind.ALREADY_FAILED, draft=draft)
        if draft.status == ProcurementDraftStatus.EXECUTION_UNKNOWN:
            return ProcurementOutcome(
                kind=ProcurementOutcomeKind.RECONCILIATION_REQUIRED, draft=draft
            )
        if draft.status == ProcurementDraftStatus.EXECUTING:
            # Couvre À LA FOIS "une autre requête traite CETTE confirmation
            # en ce moment même" et "un crash a laissé ce draft bloqué ici
            # dans le passé" — les deux produisent la MÊME réponse sûre :
            # ne jamais relancer l'effet externe (mandat §11/§16).
            return ProcurementOutcome(kind=ProcurementOutcomeKind.ALREADY_EXECUTING, draft=draft)
        if draft.status == ProcurementDraftStatus.CANCELLED:
            return ProcurementOutcome(kind=ProcurementOutcomeKind.DRAFT_FINALIZED, draft=draft)
        if draft.status != ProcurementDraftStatus.DRAFT:
            return ProcurementOutcome(kind=ProcurementOutcomeKind.DRAFT_FINALIZED, draft=draft)
        # status == DRAFT à partir d'ici : la version EXACTE visée compte —
        # une édition concurrente (UPDATE) peut avoir fait avancer le draft
        # pendant que ce CONFIRM visait une version déjà périmée.
        if not action.target.matches(draft):
            return ProcurementOutcome(
                kind=ProcurementOutcomeKind.STALE_TARGET,
                draft=draft,
                detail=f"target={action.target.draft_id}/{action.target.draft_version}",
            )
        if not draft.is_complete():
            return ProcurementOutcome(kind=ProcurementOutcomeKind.NEEDS_MORE_INFO, draft=draft)
        if not claim(_confirm_claim_key(draft)):
            # Un autre worker/tentative a gagné la course sur CETTE version
            # exactement au même instant — mandat §21.
            return ProcurementOutcome(kind=ProcurementOutcomeKind.ALREADY_EXECUTING, draft=draft)
        # DRAFT → CONFIRMED → EXECUTING en UN SEUL appel, jamais observable
        # séparément (voir docstring de `ProcurementDraftStatus.EXECUTING`)
        # — le draft PERSISTÉ à l'issue de cette fonction est déjà
        # `EXECUTING`, empêchant structurellement un 2e worker de relancer
        # `create_auction` (mandat §1/§11).
        executing = draft._confirm_to_executing()
        return ProcurementOutcome(
            kind=ProcurementOutcomeKind.CONFIRMED_READY_FOR_EXECUTION, draft=executing
        )

    if isinstance(action, RejectProcurementConfirmation):
        if draft is None:
            return ProcurementOutcome(kind=ProcurementOutcomeKind.NO_DRAFT, draft=None)
        # Rejet "doux" : le draft n'est PAS modifié (aucun champ, aucune
        # version bump) — seul l'appelant (orchestration node) cesse de
        # reposer un `ConfirmationTarget`, ce qui invalide la confirmation
        # sans toucher au contenu. Voir docstring de la classe.
        return ProcurementOutcome(kind=ProcurementOutcomeKind.CONFIRMATION_REJECTED, draft=draft)

    if isinstance(action, CancelProcurementDraft):
        if draft is None:
            return ProcurementOutcome(kind=ProcurementOutcomeKind.NO_DRAFT, draft=None)
        return ProcurementOutcome(
            kind=ProcurementOutcomeKind.CANCELLED,
            draft=draft.with_status(ProcurementDraftStatus.CANCELLED),
        )

    # NoProcurementAction
    return ProcurementOutcome(
        kind=ProcurementOutcomeKind.DRAFT_UNCHANGED, draft=draft, detail=action.reason
    )


@dataclass(frozen=True)
class ProcurementExecutionResult:
    """Le domaine ne dépend JAMAIS directement du format de réponse MCP
    (mandat §3) — `adapt_mcp_result` ci-dessous est l'UNIQUE point de
    contact avec ce format. `ambiguous=True` signifie : l'appel MCP a été
    tenté mais son issue réelle ne peut pas être établie avec certitude
    (crash du worker, exception réseau après envoi, statut inattendu) —
    JAMAIS traité comme un succès ni un échec net."""

    success: bool
    external_id: Optional[str] = None
    error: Optional[str] = None
    ambiguous: bool = False


def adapt_mcp_result(
    execution_status: Optional[str], execution_result: Optional[Dict[str, Any]]
) -> ProcurementExecutionResult:
    """`nodes/executor.py::mcp_tool_executor` (générique, inchangé) → ce
    que le domaine comprend. SEUL adaptateur du format MCP — le reste du
    module ne connaît que `ProcurementExecutionResult`."""
    status = str(execution_status or "").upper()
    result = execution_result if isinstance(execution_result, dict) else {}
    if status == "COMPLETED":
        return ProcurementExecutionResult(success=True, external_id=result.get("auction_id"))
    if status == "ERROR":
        error = str(result.get("message") or result.get("error") or "Erreur inconnue")
        return ProcurementExecutionResult(success=False, error=error)
    # Tout le reste (le tour s'est arrêté avant que l'exécuteur ne tranche
    # clairement — crash, statut inattendu, jamais atteint) est AMBIGU par
    # construction, jamais interprété comme un échec net.
    return ProcurementExecutionResult(success=False, ambiguous=True)


def finalize_after_execution(
    draft: ProcurementDraft, result: ProcurementExecutionResult
) -> ProcurementDraft:
    """EXECUTING → EXECUTED/FAILED/EXECUTION_UNKNOWN — la transition
    terminale que ce module ne peut pas appliquer lui-même en un seul appel
    de bout en bout : elle dépend du résultat du VRAI appel MCP
    (`create_auction`), exécuté par `nodes/executor.py::mcp_tool_executor`
    (nœud différent, même tour de graphe — voir
    `flows/buyer/procurement_execution_finalizer.py` pour le câblage).
    Exposée ici pour que l'appelant n'ait jamais besoin de connaître
    `_ALLOWED_TRANSITIONS` lui-même.

    N'accepte QUE `draft.status == EXECUTING` — appeler ceci sur un draft
    déjà EXECUTED/FAILED/EXECUTION_UNKNOWN lève `IllegalDraftTransition`
    (idempotence : l'appelant doit vérifier `draft.status` avant de
    ré-appeler, jamais supposer que finaliser deux fois est sans risque)."""
    if result.ambiguous:
        target_status = ProcurementDraftStatus.EXECUTION_UNKNOWN
    elif result.success:
        target_status = ProcurementDraftStatus.EXECUTED
    else:
        target_status = ProcurementDraftStatus.FAILED
    return draft.with_status(target_status)


def _new_draft_id() -> str:
    import uuid

    return uuid.uuid4().hex[:12]


# =====================================================================
# RESPONSE PLAN — DomainOutcome → ce qui doit être communiqué (mandat §3)
#
# `build_response_plan` est PURE : à `outcome`/`deviation_note` égaux, elle
# retourne TOUJOURS le même plan. Elle ne lit `state` nulle part, ne décide
# d'aucune donnée métier — elle TRADUIT un `ProcurementOutcome` déjà tranché
# par `apply_domain_action`. Le seul appel impur du cycle (la note LLM de
# déviation) est calculé par l'APPELANT, avant cet appel — jamais ici.
# =====================================================================

_MISSING_FIELD_LABELS = {
    "product": "le produit recherché",
    "quantity": "la quantité souhaitée",
    "unit": "l'unité (kg, tonnes...)",
    "price": "le prix plafond que vous êtes prêt à payer",
}


@dataclass(frozen=True)
class ProcurementResponsePlan:
    """Ce qui doit arriver à l'utilisateur et à l'état du graphe — un
    renderer/nœud ne fait qu'appliquer ce plan MÉCANIQUEMENT (mandat §3) :
    aucune des clés ci-dessous n'est décidée en dehors de
    `build_response_plan`."""

    final_response: str
    response_strategy: str
    graph_status: str
    draft: Optional[ProcurementDraft]
    pending_kind: Optional[str] = None  # nom InteractionKind, None = résolu/effacé
    pending_field: Optional[str] = None
    pending_target: Optional[Dict[str, Any]] = None
    ready_for_execution: bool = False
    terminal_goal_reset: bool = False
    # True = ce plan n'a pas d'avis sur pending_interaction — ne PAS
    # l'effacer (NO_TARGET/NO_DRAFT : l'événement ne concernait pas la
    # confirmation en cours, quelle qu'elle soit ; l'effacer effacerait une
    # interaction légitime SANS RAPPORT, ex. un ENTER_FIELD d'un autre slot).
    # Distinct de `pending_kind=None`, qui signifie explicitement "résoudre/
    # effacer" (une décision, pas une abstention).
    pending_untouched: bool = False


def _missing_field_prompt(missing) -> str:
    if not missing:
        return "Il me manque encore une information pour continuer."
    return f"J'ai encore besoin de : {_MISSING_FIELD_LABELS.get(missing[0], missing[0])}."


def build_response_plan(
    outcome: ProcurementOutcome, *, deviation_note: Optional[str] = None
) -> ProcurementResponsePlan:
    draft = outcome.draft
    kind = outcome.kind

    if kind == ProcurementOutcomeKind.NO_DRAFT:
        return ProcurementResponsePlan(
            final_response="",
            response_strategy="SUCCESS",
            graph_status="PLANNING",
            draft=None,
            pending_untouched=True,
        )

    if kind == ProcurementOutcomeKind.NEEDS_MORE_INFO:
        missing = draft.missing_fields()
        return ProcurementResponsePlan(
            final_response=_missing_field_prompt(missing),
            response_strategy="ASK_MISSING_FIELD",
            graph_status="WAITING_INPUT",
            draft=draft,
            pending_kind="ENTER_FIELD",
            pending_field=missing[0] if missing else None,
        )

    if kind == ProcurementOutcomeKind.DRAFT_UPDATED:
        return ProcurementResponsePlan(
            final_response=f"{draft.render_summary()}\n\nConfirmez-vous ?",
            response_strategy="CONFIRMATION",
            graph_status="WAITING_INPUT",
            draft=draft,
            pending_kind="CONFIRM_ACTION",
            pending_target={"draft_id": draft.draft_id, "draft_version": draft.version},
        )

    if kind == ProcurementOutcomeKind.DRAFT_UNCHANGED:
        summary = draft.render_summary() if draft else ""
        body = f"{deviation_note}\n\n{summary}" if deviation_note else summary
        return ProcurementResponsePlan(
            final_response=f"{body}\n\nConfirmez-vous ?" if draft else body,
            response_strategy="CONFIRMATION",
            graph_status="WAITING_INPUT",
            draft=draft,
            pending_kind="CONFIRM_ACTION" if draft else None,
            pending_target=(
                {"draft_id": draft.draft_id, "draft_version": draft.version} if draft else None
            ),
        )

    if kind == ProcurementOutcomeKind.CONFIRMATION_REJECTED:
        return ProcurementResponsePlan(
            final_response=(
                "D'accord, ce n'est pas encore confirmé. Dites-moi ce que "
                "vous voulez modifier (prix, quantité, date), ou répondez "
                "*annuler* pour abandonner."
            ),
            response_strategy="SUCCESS",
            graph_status="PLANNING",
            draft=draft,
        )

    if kind == ProcurementOutcomeKind.CANCELLED:
        return ProcurementResponsePlan(
            final_response="Opération annulée. Que souhaitez-vous faire ?",
            response_strategy="CLARIFICATION",
            graph_status="COMPLETED",
            draft=None,
            terminal_goal_reset=True,
        )

    if kind == ProcurementOutcomeKind.DRAFT_FINALIZED:
        label = {
            ProcurementDraftStatus.EXECUTED: "déjà confirmée et enregistrée",
            ProcurementDraftStatus.FAILED: "déjà traitée (échec d'enregistrement)",
            ProcurementDraftStatus.CANCELLED: "déjà annulée",
        }.get(draft.status if draft else None, "déjà finalisée")
        return ProcurementResponsePlan(
            final_response=f"Cette précommande est {label} — rien à modifier ici.",
            response_strategy="SUCCESS",
            graph_status="COMPLETED",
            draft=draft,
        )

    if kind == ProcurementOutcomeKind.STALE_TARGET:
        if draft is None:
            return ProcurementResponsePlan(
                final_response="", response_strategy="SUCCESS", graph_status="PLANNING", draft=None
            )
        return ProcurementResponsePlan(
            final_response=(
                f"{draft.render_summary()}\n\nLa proposition a changé depuis "
                "— confirmez-vous CETTE version ?"
            ),
            response_strategy="CONFIRMATION",
            graph_status="WAITING_INPUT",
            draft=draft,
            pending_kind="CONFIRM_ACTION",
            pending_target={"draft_id": draft.draft_id, "draft_version": draft.version},
        )

    if kind == ProcurementOutcomeKind.VERSION_CONFLICT:
        # Même traitement que STALE_TARGET côté utilisateur (re-présenter
        # la version RÉELLEMENT persistée) — mais `draft` porte ici la
        # version RELUE depuis la DB après le conflit (voir l'appelant,
        # flows/buyer/procurement_confirmation.py), jamais celle qui a
        # perdu la course.
        if draft is None:
            return ProcurementResponsePlan(
                final_response="", response_strategy="SUCCESS", graph_status="PLANNING", draft=None
            )
        return ProcurementResponsePlan(
            final_response=(
                f"{draft.render_summary()}\n\nLa proposition a changé depuis "
                "— confirmez-vous CETTE version ?"
            ),
            response_strategy="CONFIRMATION",
            graph_status="WAITING_INPUT",
            draft=draft,
            pending_kind="CONFIRM_ACTION",
            pending_target={"draft_id": draft.draft_id, "draft_version": draft.version},
        )

    if kind == ProcurementOutcomeKind.NO_TARGET:
        # L'événement (un CONFIRM sans cible active) ne concernait pas ce
        # goal — `pending_interaction` peut légitimement porter tout autre
        # chose (un ENTER_FIELD d'un slot sans rapport) : jamais effacé ici.
        return ProcurementResponsePlan(
            final_response="",
            response_strategy="SUCCESS",
            graph_status="PLANNING",
            draft=draft,
            pending_untouched=True,
        )

    if kind == ProcurementOutcomeKind.CONFIRMED_READY_FOR_EXECUTION:
        return ProcurementResponsePlan(
            final_response="",  # rendu par le pipeline d'exécution générique, pas ici
            response_strategy="SUCCESS",
            graph_status="EXECUTING",
            draft=draft,
            ready_for_execution=True,
        )

    # --- Politique de retry post-confirmation (mandat §12) — réponse
    # informative dédiée par statut, jamais de ré-exécution. ---

    if kind == ProcurementOutcomeKind.ALREADY_EXECUTING:
        return ProcurementResponsePlan(
            final_response=(
                "Cette précommande est en cours de traitement — je vous "
                "confirme dès que c'est fait, inutile de renvoyer *confirme*."
            ),
            response_strategy="SUCCESS",
            graph_status="COMPLETED",
            draft=draft,
        )

    if kind == ProcurementOutcomeKind.ALREADY_EXECUTED:
        return ProcurementResponsePlan(
            final_response="Cette précommande a déjà été confirmée et enregistrée — rien à refaire.",
            response_strategy="SUCCESS",
            graph_status="COMPLETED",
            draft=draft,
        )

    if kind == ProcurementOutcomeKind.ALREADY_FAILED:
        return ProcurementResponsePlan(
            final_response=(
                "Cette précommande n'a pas pu être enregistrée précédemment — "
                "dites-moi si vous voulez relancer un nouvel appel d'offres."
            ),
            response_strategy="SUCCESS",
            graph_status="COMPLETED",
            draft=draft,
        )

    if kind == ProcurementOutcomeKind.RECONCILIATION_REQUIRED:
        return ProcurementResponsePlan(
            final_response=(
                "Je ne suis pas certain que cette précommande ait bien été "
                "enregistrée — notre équipe vérifie et vous recontacte. "
                "Merci de ne pas relancer la même demande entre-temps."
            ),
            response_strategy="SUCCESS",
            graph_status="COMPLETED",
            draft=draft,
        )

    # --- Issues terminales de la finalisation post-exécution ---

    if kind == ProcurementOutcomeKind.PROCUREMENT_EXECUTED:
        return ProcurementResponsePlan(
            final_response=(
                f"✅ Votre appel d'offres pour {draft.render_summary().split('pour ', 1)[-1]}"
                if draft
                else "✅ Votre appel d'offres a bien été enregistré."
            ),
            response_strategy="SUCCESS",
            graph_status="COMPLETED",
            draft=draft,
        )

    if kind == ProcurementOutcomeKind.PROCUREMENT_FAILED:
        return ProcurementResponsePlan(
            final_response=(
                "L'enregistrement de votre appel d'offres a échoué. "
                "Vous pouvez relancer une nouvelle demande."
            ),
            response_strategy="ERROR",
            graph_status="COMPLETED",
            draft=draft,
        )

    if kind == ProcurementOutcomeKind.PROCUREMENT_EXECUTION_UNKNOWN:
        return ProcurementResponsePlan(
            final_response=(
                "Un incident technique m'empêche de confirmer si votre appel "
                "d'offres a bien été enregistré — notre équipe vérifie et "
                "vous recontacte."
            ),
            response_strategy="ERROR",
            graph_status="COMPLETED",
            draft=draft,
        )

    raise AssertionError(f"ProcurementOutcomeKind non couvert par build_response_plan: {kind!r}")


# =====================================================================
# INVARIANT — vérifiable à l'exécution (mandat §5)
# =====================================================================


def check_confirmation_target_invariant(
    pending_target: Optional[Dict[str, Any]],
    draft: Optional[ProcurementDraft],
) -> Optional[str]:
    """Retourne une violation textuelle, ou None si l'invariant tient :
    `PendingInteraction(kind=CONFIRM_ACTION).target` doit TOUJOURS pointer
    la version COURANTE du draft — jamais une version périmée. Un `UPDATE`
    qui produit une nouvelle version SANS reposer un nouveau target laisse
    cet invariant violé, ce que cette fonction rend détectable (log défensif
    en prod, assertion dans les tests) plutôt que silencieusement incorrect."""
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
    "ProcurementDraftStatus",
    "ProcurementDraft",
    "IllegalDraftTransition",
    "ConfirmationTarget",
    "UpdateProcurementDraft",
    "ConfirmProcurementDraft",
    "RejectProcurementConfirmation",
    "CancelProcurementDraft",
    "NoProcurementAction",
    "DomainAction",
    "resolve_domain_action",
    "ProcurementOutcomeKind",
    "ProcurementOutcome",
    "apply_domain_action",
    "execution_key",
    "ProcurementExecutionResult",
    "adapt_mcp_result",
    "finalize_after_execution",
    "ProcurementResponsePlan",
    "build_response_plan",
    "check_confirmation_target_invariant",
]
