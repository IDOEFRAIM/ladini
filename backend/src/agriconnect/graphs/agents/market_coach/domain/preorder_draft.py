"""PreorderDraft — canonical, versioned transactional entity for the
BUYER_PREORDER_INIT / BUYER_PREORDER_CONFIRM conversational flow
(2026-09-03, migration PREORDER — même standard architectural que
`procurement_draft.py`, PAS un copier-coller).

## Pourquoi ce module existe

Audit (voir `docs/PROCUREMENT_RECOVERY_AND_MCP_IDEMPOTENCY_2026-09-03.md`,
section G) : le workflow précommande actuel (`flows/buyer/preorder.py`)
répartit son état métier sur QUATRE sources concurrentes —
`preorder_workflow` (dict `merge_dict`, aucune version), `active_cart`
(liste séparée), `transaction_payload["resolved_id"]` (pilote le
routage CANCEL/ADD_MORE/CONFIRM par une valeur de convention, jamais un
contrat typé), et le brouillon RÉEL côté serveur (`Order(status=DRAFT)`,
`services/database/buyer.py::create_preorder_draft`/`confirm_preorder_draft`).
Ce module élimine cette dispersion : `PreorderDraft` devient l'UNIQUE
représentation locale du brouillon, versionnée, persistée en CAS
PostgreSQL — exactement le même principe que `ProcurementDraft`, avec des
champs et une machine à état DÉDUITS du workflow réel (pas copiés).

## Différence structurelle majeure avec PROCUREMENT (assumée, pas cachée)

`ProcurementDraft` ne déclenche AUCUN appel externe avant la confirmation —
`create_auction` n'est appelé qu'à l'issue de CONFIRM. PREORDER, lui, a
DEUX appels MCP à effets réels :
  1. `create_preorder_draft` — crée un `Order(status=DRAFT)` server-side,
     appelé à la CRÉATION du brouillon (avant toute confirmation).
  2. `confirm_preorder_draft` — débite le stock, `Order.status` DRAFT→CONFIRMED
     (protégé par `SELECT...FOR UPDATE` + garde `status != DRAFT`, voir
     `services/database/buyer.py:1704` — PRÉSERVÉE, jamais retirée).

Conséquence : la création du draft LOCAL (ligne PostgreSQL
`preorder_drafts`) n'est posée qu'APRÈS le succès de l'appel MCP
`create_preorder_draft` (elle porte alors `order_id`) — le brouillon local
n'existe jamais "en attente" d'un `order_id` qui n'existe pas encore
(pas de statut `CREATING` séparé : voir `flows/buyer/preorder_confirmation.py`
pour l'orchestration de cet appel, hors de ce module qui reste pur sauf le
claim d'idempotence de CONFIRM). Idempotence de la CRÉATION (mandat §15) :
gérée à ce même niveau orchestration, via
`services/database/mcp_idempotency_store.py` (déjà construit pour
PROCUREMENT, réutilisé tel quel — clé `preorder-create:{phone}:{cart_fingerprint}`).

## Ce que ce module NE fait PAS

Il ne remplace pas `services/mcp/gateway.py::PreorderGateway` ni
`services/database/buyer.py::create_preorder_draft`/`confirm_preorder_draft`
(la garde `SELECT...FOR UPDATE`/`status != DRAFT` y reste la protection
FINALE, inchangée). Il ne réinvente pas la résolution GPS
(`flows/buyer/gps_delivery_gate.py::enter_gps_stage`/`resolve_gps_stage`,
réutilisées telles quelles par l'orchestration)."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Dict, Optional, Tuple, Union

from agriconnect.core.idempotency import claim_once
from agriconnect.graphs.agents.market_coach.core.confirmation_target import (
    ConfirmationTarget,
)
from agriconnect.graphs.agents.market_coach.utils import slot_has_value

# =====================================================================
# DRAFT — état transactionnel canonique
# =====================================================================


class PreorderDraftStatus(str, Enum):
    """Où en est LA TRANSACTION précommande — distinct de `PendingInteraction`,
    qui dit ce qu'attend le PROCHAIN message.

    `DRAFT` couvre À LA FOIS "en cours de composition" ET "en attente de
    confirmation utilisateur" (pas de statut séparé "WAITING_CONFIRMATION" —
    c'est `PendingInteraction(CONFIRM_ACTION)` qui porte cette information
    conversationnelle, jamais dupliquée dans `status`, même principe que
    PROCUREMENT). `EXECUTING` couvre la fenêtre de l'appel
    `confirm_preorder_draft` (débit stock, effet financier RÉEL) —
    persisté, empêche structurellement un 2e worker de relancer cet appel."""

    DRAFT = "DRAFT"
    EXECUTING = "EXECUTING"  # confirm_preorder_draft (ou initiate_escrow_payment) en cours/tenté
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    EXECUTION_UNKNOWN = "EXECUTION_UNKNOWN"
    CANCELLED = "CANCELLED"
    # (mandat §17/§18 — plusieurs effets externes, pas cachés derrière un
    # simple succès) : quand `settings.ESCROW_PAYMENT_ENABLED`, la
    # confirmation n'est PAS un débit stock immédiat — `initiate_escrow_payment`
    # RÉSERVE la commande et génère un lien Paydunya ; le débit stock réel
    # (Order DRAFT -> CONFIRMED) n'arrive qu'à l'IPN Paydunya
    # (`EscrowMixin.mark_escrow_paid`) — un système ASYNCHRONE, HORS de ce
    # tour de graphe. `PAYMENT ≠ CONFIRMATION` (mandat §5) : ce statut
    # représente EXACTEMENT "intention confirmée, paiement en attente d'une
    # preuve externe" — jamais assimilé à une exécution réussie.
    #
    # (2026-09-03, clôture escrow/IPN) : n'est PLUS terminal —
    # `workers/payments/paydunya_ipn_task.py` et
    # `workers/crons/order_expiry.py` mettent désormais À JOUR
    # `preorder_draft_store` via `finalize_after_payment` (voir plus bas),
    # fermant le trou identifié dans la migration précédente.
    AWAITING_PAYMENT = "AWAITING_PAYMENT"
    # Paydunya confirme l'échec/l'annulation de la facture (statut
    # "cancelled" re-confirmé serveur-à-serveur) — AUCUN effet externe,
    # distinct de `FAILED` (qui, pour PREORDER, ne survient QUE côté
    # non-escrow — voir `apply_domain_action`) pour que le renderer puisse
    # produire un message spécifique au paiement (mandat §20).
    PAYMENT_FAILED = "PAYMENT_FAILED"
    # `expire_pending_payments` (cron TTL existant, `escrow.py`) a annulé la
    # commande faute de paiement dans le délai — AUCUN effet externe non
    # plus, distinct de `PAYMENT_FAILED` (rejet explicite du provider) pour
    # la même raison de message.
    PAYMENT_EXPIRED = "PAYMENT_EXPIRED"


_FIELD_NAMES = (
    "order_id",
    "items",
    "total_amount",
    "currency",
    "delivery_zone_id",
    "payment_method",
    "delivery_lat",
    "delivery_lon",
)
# Un brouillon est "complet" dès qu'il a des items ET un order_id (posé par
# l'appel MCP create_preorder_draft — voir la docstring du module). Le point
# de livraison n'est PAS requis ici : il arrive plus tard, au moment du
# CONFIRM (mandat §20 — PendingInteraction(PROVIDE_LOCATION) le porte).
_REQUIRED_FOR_COMPLETION = ("order_id", "items")


class IllegalDraftTransition(RuntimeError):
    """Même contrat que `procurement_draft.py::IllegalDraftTransition` —
    violation structurelle (appelant a mal utilisé le contrat), jamais un
    désaccord métier normal (`STALE_TARGET`/`DRAFT_FINALIZED`, qui sont des
    `PreorderOutcome`, jamais des exceptions)."""


# Machine d'état EXPLICITE et CENTRALISÉE — toute transition absente d'ici
# est REFUSÉE, jamais appliquée silencieusement. `DRAFT → DRAFT` représente
# `with_updates` (nouvelle version, même statut). EXECUTED/FAILED/
# EXECUTION_UNKNOWN/CANCELLED sont terminaux : `EXECUTION_UNKNOWN`
# nécessite une réconciliation humaine, jamais un retry aveugle (même
# doctrine que PROCUREMENT).
_ALLOWED_TRANSITIONS: Dict[PreorderDraftStatus, frozenset] = {
    PreorderDraftStatus.DRAFT: frozenset(
        {PreorderDraftStatus.DRAFT, PreorderDraftStatus.EXECUTING, PreorderDraftStatus.CANCELLED}
    ),
    PreorderDraftStatus.EXECUTING: frozenset(
        {
            PreorderDraftStatus.EXECUTED,
            PreorderDraftStatus.FAILED,
            PreorderDraftStatus.EXECUTION_UNKNOWN,
            PreorderDraftStatus.AWAITING_PAYMENT,
        }
    ),
    PreorderDraftStatus.EXECUTED: frozenset(),
    PreorderDraftStatus.FAILED: frozenset(),
    PreorderDraftStatus.EXECUTION_UNKNOWN: frozenset(),
    PreorderDraftStatus.CANCELLED: frozenset(),
    # (2026-09-03, clôture escrow/IPN) : AWAITING_PAYMENT n'est plus
    # terminal — l'IPN (succès/échec) ou le cron d'expiration TTL le fait
    # transiter. `EXECUTION_UNKNOWN` couvre le cas RÉELLEMENT ambigu
    # (statut Paydunya inattendu, ou `mark_escrow_paid` en échec technique)
    # — jamais deviné en EXECUTED/PAYMENT_FAILED (mandat, RÈGLE ABSOLUE).
    PreorderDraftStatus.AWAITING_PAYMENT: frozenset(
        {
            PreorderDraftStatus.EXECUTED,
            PreorderDraftStatus.PAYMENT_FAILED,
            PreorderDraftStatus.PAYMENT_EXPIRED,
            PreorderDraftStatus.EXECUTION_UNKNOWN,
        }
    ),
    PreorderDraftStatus.PAYMENT_FAILED: frozenset(),
    PreorderDraftStatus.PAYMENT_EXPIRED: frozenset(),
}


@dataclass(frozen=True)
class PreorderDraft:
    draft_id: str
    version: int
    status: PreorderDraftStatus
    order_id: Optional[str] = None
    # Identité — posée une seule fois à la création (comme `order_id`),
    # JAMAIS mutable via `with_updates`. Nécessaire pour notifier
    # l'acheteur depuis un contexte HORS conversation (IPN Paydunya, cron
    # d'expiration — 2026-09-03, clôture escrow/IPN) : ces appelants n'ont
    # aucun `state` LangGraph d'où lire le numéro.
    buyer_phone: Optional[str] = None
    items: Tuple[Dict[str, Any], ...] = ()
    total_amount: Optional[float] = None
    currency: str = "XOF"
    delivery_zone_id: Optional[str] = None
    payment_method: str = "CASH"
    delivery_lat: Optional[float] = None
    delivery_lon: Optional[float] = None
    created_at: float = 0.0

    # ------------------------------------------------------------
    # (de)sérialisation
    # ------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "version": self.version,
            "status": self.status.value,
            "order_id": self.order_id,
            "buyer_phone": self.buyer_phone,
            "items": list(self.items),
            "total_amount": self.total_amount,
            "currency": self.currency,
            "delivery_zone_id": self.delivery_zone_id,
            "payment_method": self.payment_method,
            "delivery_lat": self.delivery_lat,
            "delivery_lon": self.delivery_lon,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> Optional["PreorderDraft"]:
        if not isinstance(d, dict) or not d.get("draft_id"):
            return None
        try:
            status = PreorderDraftStatus(d.get("status") or "DRAFT")
        except ValueError:
            status = PreorderDraftStatus.DRAFT
        raw_items = d.get("items") or []
        items = tuple(raw_items) if isinstance(raw_items, list) else ()
        return cls(
            draft_id=str(d["draft_id"]),
            version=int(d.get("version") or 1),
            status=status,
            order_id=d.get("order_id"),
            buyer_phone=d.get("buyer_phone"),
            items=items,
            total_amount=d.get("total_amount"),
            currency=str(d.get("currency") or "XOF"),
            delivery_zone_id=d.get("delivery_zone_id"),
            payment_method=str(d.get("payment_method") or "CASH"),
            delivery_lat=d.get("delivery_lat"),
            delivery_lon=d.get("delivery_lon"),
            created_at=float(d.get("created_at") or 0.0),
        )

    @classmethod
    def new(
        cls,
        draft_id: str,
        *,
        order_id: str,
        items: Any,
        buyer_phone: Optional[str] = None,
        **fields: Any,
    ) -> "PreorderDraft":
        """Construit v1 — appelé UNE FOIS l'appel MCP `create_preorder_draft`
        réussi (voir docstring du module) : `order_id` est donc TOUJOURS
        connu dès `new()`, jamais posé après coup."""
        clean = {k: v for k, v in fields.items() if k in _FIELD_NAMES and slot_has_value(v)}
        clean.pop("order_id", None)
        clean.pop("items", None)
        clean.pop("buyer_phone", None)
        items_tuple = tuple(items) if isinstance(items, (list, tuple)) else ()
        return cls(
            draft_id=draft_id,
            version=1,
            status=PreorderDraftStatus.DRAFT,
            order_id=str(order_id),
            buyer_phone=str(buyer_phone) if buyer_phone else None,
            items=items_tuple,
            created_at=time.time(),
            **clean,
        )

    # ------------------------------------------------------------
    # mutation — TOUJOURS une nouvelle version, jamais en place
    # ------------------------------------------------------------
    def _transition(self, new_status: PreorderDraftStatus, **extra: Any) -> "PreorderDraft":
        allowed = _ALLOWED_TRANSITIONS.get(self.status, frozenset())
        if new_status not in allowed:
            raise IllegalDraftTransition(
                f"{self.status.value} -> {new_status.value} n'est pas une "
                f"transition autorisée (draft={self.draft_id}/v{self.version})"
            )
        return replace(self, status=new_status, **extra)

    def with_updates(self, **fields: Any) -> "PreorderDraft":
        """Nouvelle version — utilisé quand le panier change (cycle
        "ajouter un autre produit", mandat §8) : `order_id`/`items` sont
        généralement TOUS LES DEUX fournis ensemble (un nouvel appel
        `create_preorder_draft` a déjà été fait côté orchestration — voir
        `flows/buyer/preorder_confirmation.py` — AVANT cet appel, jamais
        après : ce module reste pur). Ne bump PAS la version si rien ne
        change réellement. Lève `IllegalDraftTransition` si
        `self.status != DRAFT`."""
        changed: Dict[str, Any] = {}
        for key, value in fields.items():
            if key not in _FIELD_NAMES or not slot_has_value(value):
                continue
            current = getattr(self, key)
            if key == "items":
                value = tuple(value) if isinstance(value, (list, tuple)) else value
            if isinstance(value, str) and isinstance(current, str):
                same = value.strip().lower() == current.strip().lower()
            else:
                same = value == current
            if not same:
                changed[key] = value
        if not changed:
            return self
        transitioned = self._transition(PreorderDraftStatus.DRAFT)
        return replace(transitioned, version=self.version + 1, created_at=time.time(), **changed)

    def with_status(self, status: PreorderDraftStatus) -> "PreorderDraft":
        """Bump la version à CHAQUE transition persistable — même correctif
        que `procurement_draft.py::with_status` (un bug réel de lost-update
        CAS y a été trouvé et corrigé ce même jour ; appliqué ICI dès la
        conception, pas après coup)."""
        transitioned = self._transition(status)
        return replace(transitioned, version=self.version + 1)

    def _confirm_to_executing(
        self, *, delivery_lat: Optional[float], delivery_lon: Optional[float]
    ) -> "PreorderDraft":
        """DRAFT → EXECUTING en UNE SEULE transition métier, avec le point
        de livraison posé dans le MÊME incrément de version — jamais
        observable comme "DRAFT avec livraison mais pas encore EXECUTING"
        (même principe que `ProcurementDraft._confirm_to_executing`)."""
        executing = self._transition(PreorderDraftStatus.EXECUTING)
        return replace(
            executing, version=self.version + 1, delivery_lat=delivery_lat, delivery_lon=delivery_lon
        )

    # ------------------------------------------------------------
    # lecture
    # ------------------------------------------------------------
    def is_complete(self) -> bool:
        return all(slot_has_value(getattr(self, f)) for f in _REQUIRED_FOR_COMPLETION)

    def has_delivery_location(self) -> bool:
        return slot_has_value(self.delivery_lat) and slot_has_value(self.delivery_lon)

    def render_summary(self) -> str:
        """Projection PURE — jamais un texte mémorisé séparément.

        (2026-09-04, audit CART→CHECKOUT) : un item à PALIER
        (`item["tier_id"]` posé — voir `create_preorder_draft`, qui résout
        désormais le palier SERVEUR avant d'écrire `OrderItem`) affiche le
        conditionnement ET la quantité totale en unité de base — jamais
        seulement "Quantité : 3 L" pour 3 BIDONS de 10L (ambigu, laisserait
        croire à 3 litres). Même format que `cart_service.py::render_cart_menu`
        pour un item à palier — un seul gabarit d'affichage, pas deux qui
        pourraient diverger."""
        if not self.items:
            return "Récapitulatif de votre précommande en cours de construction."
        lines = ["📋 *Récapitulatif de votre précommande :*\n"]
        for i, item in enumerate(self.items, start=1):
            if item.get("tier_id") and item.get("base_unit_quantity") is not None:
                packaging_lbl = item.get("packaging") or "paquet"
                tier_qty = item.get("tier_quantity")
                pack_lbl = (
                    f"{packaging_lbl} de {tier_qty} {item.get('unit')}"
                    if tier_qty is not None
                    else packaging_lbl
                )
                lines.append(
                    f"*{i}. {item.get('name')}*\n"
                    f"   {item.get('quantity')} × {pack_lbl} "
                    f"({item.get('price')} FCFA)\n"
                    f"   Quantité totale : {item.get('base_unit_quantity')} {item.get('unit')}"
                )
            else:
                lines.append(
                    f"*{i}. {item.get('name')}*\n"
                    f"   Quantité : {item.get('quantity')} {item.get('unit')}\n"
                    f"   Prix unitaire : {item.get('price')} FCFA"
                )
        # (2026-09-05, Phase 6A) : un panier couvrant plusieurs producteurs
        # produit une commande PAR producteur (chacun livre et encaisse sa
        # part séparément). L'acheteur doit le savoir AVANT de confirmer —
        # sans quoi le récapitulatif annoncerait une commande là où il y en
        # aura deux. Projection pure, dérivée des items déjà présents.
        producer_count = len(
            {
                str(item.get("producer_id"))
                for item in self.items
                if item.get("producer_id")
            }
        )
        if producer_count > 1:
            lines.append(
                f"\nℹ️ _Vos articles proviennent de {producer_count} producteurs : "
                f"cela fera {producer_count} commandes distinctes, chacune livrée "
                "et payée séparément. Une seule confirmation suffit._"
            )
        lines.append(f"\n💰 *TOTAL : {self.total_amount} {self.currency}*")
        return "\n".join(lines)

    def cart_fingerprint(self) -> str:
        """Empreinte stable du contenu panier — utilisée par l'orchestration
        pour la clé d'idempotence de CRÉATION (mandat §15), PAS pour une
        décision de transition ici (ce module reste sans I/O)."""
        return cart_fingerprint(self.items)


def cart_fingerprint(items: Any) -> str:
    """Empreinte stable d'un panier — SEULE implémentation (mandat §27 :
    ne jamais dupliquer une même logique de hash à deux endroits). Prend
    des `items` bruts (liste de dicts, PAS un `PreorderDraft` — utilisable
    AVANT qu'un draft n'existe, pour la clé d'idempotence de création,
    voir `creation_key`)."""
    import hashlib
    import json

    canonical = json.dumps(
        [
            {
                "product_id": it.get("product_id"),
                "quantity": it.get("quantity"),
                "price": it.get("price"),
            }
            for it in (items or [])
        ],
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# `ConfirmationTarget` (2026-09-03, hardening transverse, avant migration
# SALES) — anciennement dupliqué ICI (justifié à l'époque : "le dupliquer
# coûte moins que le coupler" pour 2 copies). Une 3e copie pour SALES
# faisait basculer l'équilibre : extrait vers
# `core/confirmation_target.py` (importé en tête de module), réutilisé
# depuis les 2 domaines existants + SALES. Réexporté sous le même nom.


# =====================================================================
# DOMAIN ACTIONS
# =====================================================================


@dataclass(frozen=True)
class UpdatePreorderDraft:
    """Panier modifié (cycle "ajouter un autre produit") — `order_id`/
    `items`/`total_amount` reflètent un NOUVEL appel `create_preorder_draft`
    déjà effectué côté orchestration (mandat §15 : idempotent lui aussi)."""

    fields: Dict[str, Any]


@dataclass(frozen=True)
class ConfirmPreorderDraft:
    """`delivery_lat`/`delivery_lon` : `None` la 1ère fois (l'utilisateur
    vient de dire "oui", le point de livraison n'est pas encore connu —
    l'orchestration pose alors `PendingInteraction(PROVIDE_LOCATION)`,
    voir mandat §20) ; renseignés le tour où `resolve_gps_stage` (réutilisée
    telle quelle) a résolu un point — CE domaine ne fait AUCUNE résolution
    GPS lui-même."""

    target: Optional[ConfirmationTarget]
    delivery_lat: Optional[float] = None
    delivery_lon: Optional[float] = None


@dataclass(frozen=True)
class RejectPreorderConfirmation:
    """« non » pendant la confirmation — rejet doux, n'affecte pas les
    champs du draft (même principe que PROCUREMENT)."""


@dataclass(frozen=True)
class CancelPreorderDraft:
    """Abandon explicite et définitif (ex: "annuler ma précommande")."""


@dataclass(frozen=True)
class NoPreorderAction:
    reason: str


DomainAction = Union[
    UpdatePreorderDraft,
    ConfirmPreorderDraft,
    RejectPreorderConfirmation,
    CancelPreorderDraft,
    NoPreorderAction,
]


def resolve_domain_action(
    *,
    interpreted_event: str,
    extracted_entities: Dict[str, Any],
    pending_target: Optional[Dict[str, Any]],
    resolved_location: Optional[Tuple[float, float]] = None,
) -> DomainAction:
    """`InterpreterResult` → UNE `DomainAction`. `resolved_location`
    (optionnel) : posé par l'orchestration APRÈS un appel RÉUSSI à
    `resolve_gps_stage` — jamais résolu ici (mandat §20, pas de
    réinvention de la résolution GPS dans le domaine)."""
    event = str(interpreted_event or "").upper().strip()

    if resolved_location is not None:
        lat, lon = resolved_location
        return ConfirmPreorderDraft(
            target=ConfirmationTarget.from_dict(pending_target), delivery_lat=lat, delivery_lon=lon
        )

    if event == "CONFIRM":
        return ConfirmPreorderDraft(target=ConfirmationTarget.from_dict(pending_target))

    if event == "REJECT":
        return RejectPreorderConfirmation()

    if event == "CANCEL":
        return CancelPreorderDraft()

    return NoPreorderAction(reason=f"unhandled_event:{event}")


# =====================================================================
# DOMAIN OUTCOME + APPLICATION — SEULE autorité de mutation du draft
# =====================================================================


class PreorderOutcomeKind(str, Enum):
    DRAFT_UPDATED = "DRAFT_UPDATED"
    # Message reçu pendant une confirmation en attente, mais qui ne
    # correspond à AUCUNE action reconnue (hésitation, question, remarque
    # hors-sujet) — distinct de DRAFT_UPDATED (qui suppose une intention
    # métier reconnue). Déclenche une note LLM adaptative côté orchestration
    # (mandat §19 : jamais le même écran rejoué mot pour mot).
    DRAFT_UNCHANGED = "DRAFT_UNCHANGED"
    NEEDS_MORE_INFO = "NEEDS_MORE_INFO"
    NEEDS_LOCATION = "NEEDS_LOCATION"
    CONFIRMED_READY_FOR_EXECUTION = "CONFIRMED_READY_FOR_EXECUTION"
    STALE_TARGET = "STALE_TARGET"
    NO_TARGET = "NO_TARGET"
    CONFIRMATION_REJECTED = "CONFIRMATION_REJECTED"
    CANCELLED = "CANCELLED"
    NO_DRAFT = "NO_DRAFT"
    DRAFT_FINALIZED = "DRAFT_FINALIZED"
    ALREADY_EXECUTING = "ALREADY_EXECUTING"
    PREORDER_EXECUTED = "PREORDER_EXECUTED"
    ALREADY_EXECUTED = "ALREADY_EXECUTED"
    PREORDER_FAILED = "PREORDER_FAILED"
    ALREADY_FAILED = "ALREADY_FAILED"
    PREORDER_EXECUTION_UNKNOWN = "PREORDER_EXECUTION_UNKNOWN"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    PREORDER_AWAITING_PAYMENT = "PREORDER_AWAITING_PAYMENT"
    PREORDER_PAYMENT_FAILED = "PREORDER_PAYMENT_FAILED"
    PREORDER_PAYMENT_EXPIRED = "PREORDER_PAYMENT_EXPIRED"


@dataclass(frozen=True)
class PreorderOutcome:
    kind: PreorderOutcomeKind
    draft: Optional[PreorderDraft]
    detail: Optional[str] = None


def _confirm_claim_key(draft: PreorderDraft) -> str:
    return f"preorder_confirm:{draft.draft_id}:{draft.version}"


def execution_key(draft: PreorderDraft) -> str:
    """Clé d'idempotence MÉTIER pour `confirm_preorder_draft`, stable par
    draft/version — passée en `idempotency_key=` jusqu'à
    `AgriMCPClient.call_tool` (RÉELLEMENT dédupliquée côté serveur depuis
    ce même chantier, voir `services/database/mcp_idempotency_store.py` —
    contrairement à `create_auction`, `confirm_preorder_draft` a EN PLUS sa
    propre garde `status != DRAFT` — double protection, pas redondante :
    la table d'idempotence protège la REQUÊTE, la garde serveur protège
    l'ÉTAT métier)."""
    return f"preorder:{draft.draft_id}:{draft.version}"


def creation_key(buyer_phone: str, cart_fingerprint: str) -> str:
    """Clé d'idempotence de CRÉATION (mandat §15) — stable across retries
    tant que le panier et l'acheteur sont identiques (PAS basée sur
    `draft_id`, qui n'existe pas encore avant l'appel MCP)."""
    return f"preorder-create:{buyer_phone}:{cart_fingerprint}"


def apply_domain_action(
    draft: Optional[PreorderDraft],
    action: DomainAction,
    *,
    claim=None,
) -> PreorderOutcome:
    """LA seule fonction qui transitionne un `PreorderDraft`. Pure sur le
    draft (aucune I/O) sauf le claim d'idempotence pour CONFIRM."""
    if claim is None:
        claim = claim_once
    if draft is None:
        return PreorderOutcome(kind=PreorderOutcomeKind.NO_DRAFT, draft=None)

    if isinstance(action, UpdatePreorderDraft):
        try:
            new_draft = draft.with_updates(**action.fields)
        except IllegalDraftTransition:
            return PreorderOutcome(kind=PreorderOutcomeKind.DRAFT_FINALIZED, draft=draft)
        if new_draft is draft:
            return PreorderOutcome(kind=PreorderOutcomeKind.DRAFT_UPDATED, draft=new_draft, detail="unchanged")
        return PreorderOutcome(kind=PreorderOutcomeKind.DRAFT_UPDATED, draft=new_draft)

    if isinstance(action, ConfirmPreorderDraft):
        if action.target is None:
            return PreorderOutcome(kind=PreorderOutcomeKind.NO_TARGET, draft=draft)
        # Politique de retry EXPLICITE par statut, ÉVALUÉE AVANT la
        # correspondance de version (même correctif que PROCUREMENT,
        # appliqué ici DÈS la conception — voir with_status()).
        if draft.status == PreorderDraftStatus.EXECUTED:
            return PreorderOutcome(kind=PreorderOutcomeKind.ALREADY_EXECUTED, draft=draft)
        if draft.status == PreorderDraftStatus.FAILED:
            return PreorderOutcome(kind=PreorderOutcomeKind.ALREADY_FAILED, draft=draft)
        if draft.status == PreorderDraftStatus.EXECUTION_UNKNOWN:
            return PreorderOutcome(kind=PreorderOutcomeKind.RECONCILIATION_REQUIRED, draft=draft)
        if draft.status == PreorderDraftStatus.EXECUTING:
            return PreorderOutcome(kind=PreorderOutcomeKind.ALREADY_EXECUTING, draft=draft)
        if draft.status == PreorderDraftStatus.CANCELLED:
            return PreorderOutcome(kind=PreorderOutcomeKind.DRAFT_FINALIZED, draft=draft)
        if draft.status != PreorderDraftStatus.DRAFT:
            return PreorderOutcome(kind=PreorderOutcomeKind.DRAFT_FINALIZED, draft=draft)
        if not action.target.matches(draft):
            return PreorderOutcome(
                kind=PreorderOutcomeKind.STALE_TARGET,
                draft=draft,
                detail=f"target={action.target.draft_id}/{action.target.draft_version}",
            )
        if not draft.is_complete():
            return PreorderOutcome(kind=PreorderOutcomeKind.NEEDS_MORE_INFO, draft=draft)

        effective_lat = action.delivery_lat if action.delivery_lat is not None else draft.delivery_lat
        effective_lon = action.delivery_lon if action.delivery_lon is not None else draft.delivery_lon
        if effective_lat is None or effective_lon is None:
            # Draft INCHANGÉ (mandat §20 : la localisation n'est PERSISTÉE
            # dans le draft QUE quand elle est réellement obtenue) —
            # l'orchestration pose PendingInteraction(PROVIDE_LOCATION)
            # avec la MÊME cible (version inchangée).
            return PreorderOutcome(kind=PreorderOutcomeKind.NEEDS_LOCATION, draft=draft)

        if not claim(_confirm_claim_key(draft)):
            return PreorderOutcome(kind=PreorderOutcomeKind.ALREADY_EXECUTING, draft=draft)
        executing = draft._confirm_to_executing(delivery_lat=effective_lat, delivery_lon=effective_lon)
        return PreorderOutcome(kind=PreorderOutcomeKind.CONFIRMED_READY_FOR_EXECUTION, draft=executing)

    if isinstance(action, RejectPreorderConfirmation):
        return PreorderOutcome(kind=PreorderOutcomeKind.CONFIRMATION_REJECTED, draft=draft)

    if isinstance(action, CancelPreorderDraft):
        try:
            cancelled = draft.with_status(PreorderDraftStatus.CANCELLED)
        except IllegalDraftTransition:
            return PreorderOutcome(kind=PreorderOutcomeKind.DRAFT_FINALIZED, draft=draft)
        return PreorderOutcome(kind=PreorderOutcomeKind.CANCELLED, draft=cancelled)

    # NoPreorderAction (ou tout autre événement non reconnu) — AUCUNE
    # intention métier identifiée, jamais confondu avec un DRAFT_UPDATED
    # (qui suppose une action reconnue).
    return PreorderOutcome(
        kind=PreorderOutcomeKind.DRAFT_UNCHANGED, draft=draft, detail=getattr(action, "reason", None)
    )


# =====================================================================
# EXÉCUTION EXTERNE — adaptateur MCP → domaine
# =====================================================================


@dataclass(frozen=True)
class PreorderExecutionResult:
    """Le domaine ne dépend JAMAIS directement du format de réponse MCP.
    `ambiguous=True` : l'appel a été tenté mais l'issue réelle ne peut pas
    être établie — jamais traité comme succès ni échec net (mandat §17,
    §18 : plusieurs effets externes — statut commande, débit stock — ne
    sont PAS cachés derrière un simple `success=True`, `order_id`/
    `order_number` capturent explicitement CE que l'exécution a produit)."""

    success: bool
    order_id: Optional[str] = None
    order_number: Optional[str] = None
    error: Optional[str] = None
    ambiguous: bool = False
    checkout_url: Optional[str] = None
    ttl_hours: Optional[int] = None


def adapt_escrow_result(
    execution_status: Optional[str], execution_result: Optional[Dict[str, Any]]
) -> PreorderExecutionResult:
    """Adaptateur DÉDIÉ pour `EscrowGateway.initiate_escrow_payment` — un
    effet externe DIFFÉRENT de `confirm_preorder_draft` (réservation +
    lien de paiement, PAS un débit stock immédiat), jamais confondu via le
    même adaptateur (mandat §17 : plusieurs effets externes, pas cachés
    derrière un `success=True` générique)."""
    status = str(execution_status or "").upper()
    result = execution_result if isinstance(execution_result, dict) else {}
    if status in {"COMPLETED", "SUCCESS"}:
        return PreorderExecutionResult(
            success=True,
            order_id=str(result.get("order_id") or "") or None,
            order_number=result.get("order_number"),
            checkout_url=result.get("checkout_url"),
            ttl_hours=result.get("ttl_hours"),
        )
    if status in {"ERROR", "FAILED"}:
        error = str(result.get("message") or result.get("error") or "Erreur inconnue")
        return PreorderExecutionResult(success=False, error=error)
    return PreorderExecutionResult(success=False, ambiguous=True)


def finalize_escrow_initiation(
    draft: PreorderDraft, result: PreorderExecutionResult
) -> PreorderDraft:
    """EXECUTING → AWAITING_PAYMENT/FAILED/EXECUTION_UNKNOWN — PAS EXECUTED
    (voir la docstring de `PreorderDraftStatus.AWAITING_PAYMENT` : le débit
    stock réel arrive plus tard, via l'IPN Paydunya, hors de ce module)."""
    if result.ambiguous:
        return draft.with_status(PreorderDraftStatus.EXECUTION_UNKNOWN)
    if result.success:
        return draft.with_status(PreorderDraftStatus.AWAITING_PAYMENT)
    return draft.with_status(PreorderDraftStatus.FAILED)


def adapt_mcp_result(
    execution_status: Optional[str], execution_result: Optional[Dict[str, Any]]
) -> PreorderExecutionResult:
    """SEUL adaptateur du format `PreorderGateway.confirm_draft` — le reste
    du module ne connaît que `PreorderExecutionResult`."""
    status = str(execution_status or "").upper()
    result = execution_result if isinstance(execution_result, dict) else {}
    if status in {"COMPLETED", "SUCCESS"}:
        return PreorderExecutionResult(
            success=True,
            order_id=str(result.get("order_id") or result.get("id") or "") or None,
            order_number=result.get("order_number"),
        )
    if status in {"ERROR", "FAILED"}:
        error = str(result.get("message") or result.get("error") or "Erreur inconnue")
        return PreorderExecutionResult(success=False, error=error)
    return PreorderExecutionResult(success=False, ambiguous=True)


def finalize_after_execution(
    draft: PreorderDraft, result: PreorderExecutionResult
) -> PreorderDraft:
    """EXECUTING → EXECUTED/FAILED/EXECUTION_UNKNOWN. N'accepte QUE
    `draft.status == EXECUTING`."""
    if result.ambiguous:
        return draft.with_status(PreorderDraftStatus.EXECUTION_UNKNOWN)
    if result.success:
        return draft.with_status(PreorderDraftStatus.EXECUTED)
    return draft.with_status(PreorderDraftStatus.FAILED)


# =====================================================================
# PAIEMENT — adaptateur Paydunya → domaine (2026-09-03, clôture escrow/IPN)
#
# `PAYMENT ≠ CONFIRMATION` (mandat §5) : ce module ne transforme JAMAIS
# "l'utilisateur a dit oui" en effet financier — seul un événement de
# paiement RÉELLEMENT confirmé (IPN re-vérifié serveur-à-serveur par
# `PaydunyaClient.confirm_invoice`, ou expiration TTL déjà tranchée par
# `expire_pending_payments`) peut faire avancer `AWAITING_PAYMENT`.
# =====================================================================


class PaymentOutcomeKind(str, Enum):
    PAID = "PAID"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    # "pending" côté Paydunya — PAS actionnable, distinct de AMBIGUOUS
    # (mandat §8, IPN out-of-order) : ni un succès, ni un échec, ni une
    # anomalie à investiguer — juste "pas encore". `apply_payment_outcome`
    # doit pouvoir le traiter en silence, sans journaliser comme une
    # ambiguïté méritant investigation.
    PENDING = "PENDING"
    # Statut Paydunya inattendu/inconnu, OU absence de confirmation
    # explicite de `mark_escrow_paid` malgré un "completed" — mérite d'être
    # journalisé et transitionné vers `EXECUTION_UNKNOWN` pour investigation.
    AMBIGUOUS = "AMBIGUOUS"


def adapt_payment_outcome(
    paydunya_status: Optional[str], mark_paid_result: Optional[Dict[str, Any]]
) -> PaymentOutcomeKind:
    """SEUL adaptateur du format Paydunya/`mark_escrow_paid` pour le
    paiement — mandat RÈGLE ABSOLUE : ne transforme JAMAIS
    `PAYMENT SUCCESS` en succès d'exécution par supposition ; exige la
    CONFIRMATION explicite (`mark_paid_result["status"] == "success"`),
    jamais le seul statut Paydunya brut. Un statut Paydunya inconnu/
    inattendu est TOUJOURS `AMBIGUOUS`, jamais deviné."""
    status = str(paydunya_status or "").lower()
    if status == "completed":
        if isinstance(mark_paid_result, dict) and str(mark_paid_result.get("status")) == "success":
            return PaymentOutcomeKind.PAID
        return PaymentOutcomeKind.AMBIGUOUS
    if status == "cancelled":
        return PaymentOutcomeKind.FAILED
    if status == "pending":
        # Rien à trancher — pas une issue actionnable (mandat §8, IPN
        # out-of-order) : l'appelant ne doit PAS transitionner le draft.
        return PaymentOutcomeKind.PENDING
    return PaymentOutcomeKind.AMBIGUOUS


def finalize_after_payment(draft: PreorderDraft, outcome: PaymentOutcomeKind) -> PreorderDraft:
    """AWAITING_PAYMENT → EXECUTED/PAYMENT_FAILED/EXECUTION_UNKNOWN.
    N'accepte QUE `draft.status == AWAITING_PAYMENT` (lève
    `IllegalDraftTransition` sinon — l'appelant DOIT vérifier le statut
    AVANT d'appeler, exactement l'idempotence "déjà traité" attendue pour
    un IPN rejoué après que le draft ait déjà transité).

    `outcome == PENDING` NE DOIT JAMAIS atteindre cette fonction — c'est à
    l'appelant (`flows/buyer/preorder_payment.py::apply_payment_outcome`)
    de filtrer ce cas AVANT (rien à trancher, mandat §8). Traité ici par
    défense (jamais un crash) comme `AMBIGUOUS`, mais un appel avec
    `PENDING` signale un bug chez l'appelant, pas un usage normal."""
    if outcome == PaymentOutcomeKind.PAID:
        return draft.with_status(PreorderDraftStatus.EXECUTED)
    if outcome == PaymentOutcomeKind.FAILED:
        return draft.with_status(PreorderDraftStatus.PAYMENT_FAILED)
    return draft.with_status(PreorderDraftStatus.EXECUTION_UNKNOWN)


def finalize_after_payment_expiry(draft: PreorderDraft) -> PreorderDraft:
    """AWAITING_PAYMENT → PAYMENT_EXPIRED — appelé par le cron TTL
    (`expire_pending_payments`, déjà existant), jamais par l'IPN."""
    return draft.with_status(PreorderDraftStatus.PAYMENT_EXPIRED)


# =====================================================================
# PRÉSENTATION — DomainOutcome → ResponsePlan (PURE)
# =====================================================================


@dataclass(frozen=True)
class PreorderResponsePlan:
    final_response: str
    response_strategy: str
    graph_status: str
    draft: Optional[PreorderDraft]
    pending_kind: Optional[str] = None
    pending_field: Optional[str] = None
    pending_target: Optional[Dict[str, Any]] = None
    ready_for_execution: bool = False
    terminal: bool = False
    pending_untouched: bool = False
    # `CANCELLED`/`PREORDER_FAILED` : aucun stock n'a été débité — le
    # panier de l'utilisateur reste EXACTEMENT ce qu'il voulait, une
    # ré-confirmation ne devrait pas l'obliger à tout ressaisir. Distinct
    # de `EXECUTED`/`AWAITING_PAYMENT`, où les items ont réellement été
    # "dépensés" (stock réservé/débité) — le panier DOIT être vidé.
    preserve_cart_on_terminal: bool = False


def build_response_plan(
    outcome: PreorderOutcome, *, deviation_note: Optional[str] = None
) -> PreorderResponsePlan:
    draft = outcome.draft
    kind = outcome.kind

    if kind == PreorderOutcomeKind.NO_DRAFT:
        return PreorderResponsePlan(
            final_response="Je ne trouve pas de précommande en cours.",
            response_strategy="CLARIFICATION",
            graph_status="WAITING_INPUT",
            draft=None,
        )

    if kind == PreorderOutcomeKind.DRAFT_UPDATED:
        target = ConfirmationTarget(draft_id=draft.draft_id, draft_version=draft.version)
        return PreorderResponsePlan(
            final_response=(
                f"{draft.render_summary()}\n\n"
                "_Répondez *OUI* pour confirmer, *NON* pour annuler, "
                "ou ajoutez un autre produit._"
            ),
            response_strategy="CONFIRMATION",
            graph_status="WAITING_INPUT",
            draft=draft,
            pending_kind="CONFIRM_ACTION",
            pending_target=target.to_dict(),
        )

    if kind == PreorderOutcomeKind.DRAFT_UNCHANGED:
        target = ConfirmationTarget(draft_id=draft.draft_id, draft_version=draft.version)
        note = f"{deviation_note}\n\n" if deviation_note else ""
        return PreorderResponsePlan(
            final_response=(
                f"{note}{draft.render_summary()}\n\n"
                "_Répondez *OUI* pour confirmer, *NON* pour annuler, "
                "ou ajoutez un autre produit._"
            ),
            response_strategy="CONFIRMATION",
            graph_status="WAITING_INPUT",
            draft=draft,
            pending_kind="CONFIRM_ACTION",
            pending_target=target.to_dict(),
        )

    if kind == PreorderOutcomeKind.NEEDS_MORE_INFO:
        return PreorderResponsePlan(
            final_response="Il manque des informations pour finaliser votre précommande.",
            response_strategy="ASK_MISSING_FIELD",
            graph_status="WAITING_INPUT",
            draft=draft,
        )

    if kind == PreorderOutcomeKind.NEEDS_LOCATION:
        target = ConfirmationTarget(draft_id=draft.draft_id, draft_version=draft.version)
        return PreorderResponsePlan(
            final_response="📍 Merci de partager votre position pour la livraison.",
            response_strategy="ASK_MISSING_FIELD",
            graph_status="WAITING_INPUT",
            draft=draft,
            pending_kind="PROVIDE_LOCATION",
            pending_target=target.to_dict(),
        )

    if kind == PreorderOutcomeKind.CONFIRMED_READY_FOR_EXECUTION:
        return PreorderResponsePlan(
            final_response="",  # rendu par le pipeline d'exécution, pas ici
            response_strategy="SUCCESS",
            graph_status="EXECUTING",
            draft=draft,
            ready_for_execution=True,
        )

    if kind == PreorderOutcomeKind.STALE_TARGET:
        target = ConfirmationTarget(draft_id=draft.draft_id, draft_version=draft.version)
        note = f"{deviation_note}\n\n" if deviation_note else ""
        return PreorderResponsePlan(
            final_response=f"{note}{draft.render_summary()}\n\nConfirmez-vous cette version ?",
            response_strategy="CONFIRMATION",
            graph_status="WAITING_INPUT",
            draft=draft,
            pending_kind="CONFIRM_ACTION",
            pending_target=target.to_dict(),
        )

    if kind == PreorderOutcomeKind.VERSION_CONFLICT:
        target = ConfirmationTarget(draft_id=draft.draft_id, draft_version=draft.version) if draft else None
        return PreorderResponsePlan(
            final_response=(
                f"{draft.render_summary()}\n\nConfirmez-vous cette version ?"
                if draft
                else "Cette précommande a changé entre-temps."
            ),
            response_strategy="CONFIRMATION",
            graph_status="WAITING_INPUT",
            draft=draft,
            pending_kind="CONFIRM_ACTION" if draft else None,
            pending_target=target.to_dict() if target else None,
        )

    if kind == PreorderOutcomeKind.NO_TARGET:
        return PreorderResponsePlan(
            final_response="Je ne sais pas à quoi correspond votre réponse — pouvez-vous préciser ?",
            response_strategy="CLARIFICATION",
            graph_status="WAITING_INPUT",
            draft=draft,
            pending_untouched=True,
        )

    if kind == PreorderOutcomeKind.CONFIRMATION_REJECTED:
        note = deviation_note or "D'accord, la précommande n'est pas confirmée."
        return PreorderResponsePlan(
            final_response=note,
            response_strategy="CLARIFICATION",
            graph_status="WAITING_INPUT",
            draft=draft,
            pending_untouched=True,
        )

    if kind == PreorderOutcomeKind.CANCELLED:
        return PreorderResponsePlan(
            final_response="↩️ Précommande annulée. Votre panier est toujours disponible.",
            response_strategy="SUCCESS",
            graph_status="COMPLETED",
            draft=draft,
            terminal=True,
            preserve_cart_on_terminal=True,
        )

    if kind == PreorderOutcomeKind.DRAFT_FINALIZED:
        return PreorderResponsePlan(
            final_response="Cette précommande a déjà été traitée — rien à modifier.",
            response_strategy="SUCCESS",
            graph_status="COMPLETED",
            draft=draft,
            terminal=True,
        )

    if kind == PreorderOutcomeKind.ALREADY_EXECUTING:
        return PreorderResponsePlan(
            final_response=(
                "Cette précommande est en cours de traitement — je vous confirme "
                "dès que c'est fait, inutile de renvoyer *confirme*."
            ),
            response_strategy="SUCCESS",
            graph_status="COMPLETED",
            draft=draft,
        )

    if kind == PreorderOutcomeKind.ALREADY_EXECUTED:
        return PreorderResponsePlan(
            final_response="Cette précommande a déjà été confirmée et enregistrée — rien à refaire.",
            response_strategy="SUCCESS",
            graph_status="COMPLETED",
            draft=draft,
        )

    if kind == PreorderOutcomeKind.ALREADY_FAILED:
        return PreorderResponsePlan(
            final_response=(
                "Cette précommande n'a pas pu être enregistrée précédemment — "
                "dites-moi si vous voulez recommencer."
            ),
            response_strategy="SUCCESS",
            graph_status="COMPLETED",
            draft=draft,
        )

    if kind == PreorderOutcomeKind.RECONCILIATION_REQUIRED:
        return PreorderResponsePlan(
            final_response=(
                "Je vérifie l'état de votre précommande précédente avant de continuer — "
                "un instant."
            ),
            response_strategy="SUCCESS",
            graph_status="COMPLETED",
            draft=draft,
        )

    if kind == PreorderOutcomeKind.PREORDER_EXECUTED:
        return PreorderResponsePlan(
            final_response=(
                f"✅ *Précommande confirmée !*\n\n📦 Référence : *{draft.order_id}*\n"
                f"💰 Total : {draft.total_amount} {draft.currency}\n"
                f"💵 *Paiement à la livraison.*"
            ),
            response_strategy="SUCCESS",
            graph_status="COMPLETED",
            draft=draft,
            terminal=True,
        )

    if kind == PreorderOutcomeKind.PREORDER_FAILED:
        return PreorderResponsePlan(
            final_response=(
                "Impossible de confirmer votre précommande — aucun stock n'a été débité. "
                "Votre panier est conservé, vous pouvez recommencer."
            ),
            response_strategy="ERROR",
            graph_status="COMPLETED",
            draft=draft,
            terminal=True,
            preserve_cart_on_terminal=True,
        )

    if kind == PreorderOutcomeKind.PREORDER_AWAITING_PAYMENT:
        checkout_url, _, ttl = (outcome.detail or "|24").partition("|")
        link_line = f" : {checkout_url}" if checkout_url else "."
        return PreorderResponsePlan(
            final_response=(
                f"Votre commande #{draft.order_id} est réservée pendant "
                f"{ttl or 24}h. Payez via ce lien sécurisé{link_line}"
            ),
            response_strategy="SUCCESS",
            graph_status="COMPLETED",
            draft=draft,
            terminal=True,
        )

    if kind == PreorderOutcomeKind.PREORDER_PAYMENT_FAILED:
        return PreorderResponsePlan(
            final_response=(
                f"Le paiement de votre commande #{draft.order_id} n'a pas abouti — "
                "aucune somme n'a été débitée. Vous pouvez recommencer une précommande."
            ),
            response_strategy="ERROR",
            graph_status="COMPLETED",
            draft=draft,
            terminal=True,
        )

    if kind == PreorderOutcomeKind.PREORDER_PAYMENT_EXPIRED:
        return PreorderResponsePlan(
            final_response=(
                f"Le délai de paiement de votre commande #{draft.order_id} est dépassé — "
                "elle a été annulée, aucune somme n'a été débitée."
            ),
            response_strategy="ERROR",
            graph_status="COMPLETED",
            draft=draft,
            terminal=True,
        )

    if kind == PreorderOutcomeKind.PREORDER_EXECUTION_UNKNOWN:
        return PreorderResponsePlan(
            final_response=(
                "Votre précommande est en cours de vérification — vous recevrez "
                "une confirmation sous peu."
            ),
            response_strategy="SUCCESS",
            graph_status="COMPLETED",
            draft=draft,
            terminal=True,
        )

    # Filet — ne devrait jamais être atteint (tous les kinds sont couverts).
    return PreorderResponsePlan(
        final_response="Une erreur est survenue.",
        response_strategy="ERROR",
        graph_status="COMPLETED",
        draft=draft,
    )


def check_confirmation_target_invariant(
    pending_target: Optional[Dict[str, Any]],
    draft: Optional[PreorderDraft],
) -> Optional[str]:
    """Défensif, log-only — même contrat que PROCUREMENT."""
    if pending_target is None:
        return None
    target = ConfirmationTarget.from_dict(pending_target)
    if target is None:
        return f"pending_target malformé: {pending_target!r}"
    if draft is None:
        return f"target pointe draft_id={target.draft_id} mais aucun draft chargé"
    if target.draft_id != draft.draft_id:
        return f"target.draft_id={target.draft_id} != draft.draft_id={draft.draft_id}"
    return None


__all__ = [
    "PreorderDraftStatus",
    "PreorderDraft",
    "cart_fingerprint",
    "ConfirmationTarget",
    "IllegalDraftTransition",
    "UpdatePreorderDraft",
    "ConfirmPreorderDraft",
    "RejectPreorderConfirmation",
    "CancelPreorderDraft",
    "NoPreorderAction",
    "DomainAction",
    "resolve_domain_action",
    "PreorderOutcomeKind",
    "PreorderOutcome",
    "apply_domain_action",
    "execution_key",
    "creation_key",
    "PreorderExecutionResult",
    "adapt_mcp_result",
    "finalize_after_execution",
    "adapt_escrow_result",
    "finalize_escrow_initiation",
    "PaymentOutcomeKind",
    "adapt_payment_outcome",
    "finalize_after_payment",
    "finalize_after_payment_expiry",
    "PreorderResponsePlan",
    "build_response_plan",
    "check_confirmation_target_invariant",
]
