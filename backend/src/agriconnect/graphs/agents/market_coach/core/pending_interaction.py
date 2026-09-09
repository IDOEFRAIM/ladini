"""PendingInteraction — source canonique unique pour "qu'attend-on de
l'utilisateur au tour courant ?" (refonte architecturale 2026-09-02).

## Pourquoi ce module existe

Audit confirmé : au moins 6 signaux indépendamment écrits représentaient la
même notion — `expected_input`, `waiting_for_confirmation`, `current_goal`,
`working_memory.available_mapping_kind`, `vendor_selection_context`,
`tier_selection_context` — sans registre unique les faisant avancer
ensemble. Racine du bug réel : "celui de 10 l" (réponse à un menu de
paliers actif) classée `UNKNOWN` par l'interpréteur → le routeur tombait sur
`to_strategy` → `response_strategy` lisait un `expected_input=="CONFIRMATION"`
périmé d'un tour précédent, sans jamais vérifier qu'un menu palier était
encore actif → "Que souhaitez-vous confirmer exactement ?".

## Principe de conception : résolveur, pas nouveau conteneur

Pour le tunnel producteur/palier (`SELECT_PRODUCER`/`SELECT_PRICING_TIER`/
`ENTER_PACKAGE_COUNT`/`ENTER_QUANTITY`), `domain/selection_actions.py::
build_selection_context()` reconstruit DÉJÀ, à neuf à chaque tour, l'état
exact du tunnel depuis `vendor_selection_context`/`tier_selection_context` —
jamais depuis un champ séparé qui pourrait lui-même devenir périmé. Dupliquer
cette logique dans un nouveau champ écrit indépendamment réintroduirait
EXACTEMENT la classe de bug que cette refonte élimine. `get_pending_interaction`
délègue donc en priorité à `build_selection_context` pour ces cas, et ne
retombe sur l'état persisté (`state["pending_interaction"]`, écrit par
`confirmation_gate`/`gps_delivery_gate`/`validator`/`clarification_node`)
que pour les interactions HORS tunnel panier (confirmation, champ générique,
localisation, clarification, menu générique).

## Invariants garantis par construction

1. À tout instant, `get_pending_interaction(state)` retourne EXACTEMENT une
   interaction (jamais deux discriminants "vrais" en même temps) : c'est un
   `if/elif` strict, pas une fusion de signaux.
2. `kind == CONFIRM_ACTION` implique un `context_ref` cohérent — vérifié par
   `check_invariants`, jamais supposé par les lecteurs.
3/4. Un tunnel panier vivant (`SELECT_PRICING_TIER`...) ne peut JAMAIS être
   celui d'un ANCIEN produit : garanti parce que `build_selection_context`
   lit `vendor_selection_context`/`tier_selection_context`, et que
   `goal_planner.py::_purge_transaction_state` + `nodes/memory.py::
   clear_vendor_ctx` réinitialisent maintenant les DEUX symétriquement
   (avant cette refonte, seul `vendor_selection_context` l'était — G-2).
5. `resolve_pending_interaction()` retourne un patch qui marque `NONE` —
   une interaction résolue ne doit jamais rester active au tour suivant.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class InteractionKind(str, Enum):
    NONE = "NONE"
    SELECT_PRODUCER = "SELECT_PRODUCER"
    SELECT_PRICING_TIER = "SELECT_PRICING_TIER"
    ENTER_PACKAGE_COUNT = "ENTER_PACKAGE_COUNT"
    ENTER_QUANTITY = "ENTER_QUANTITY"
    ENTER_FIELD = "ENTER_FIELD"
    CONFIRM_ACTION = "CONFIRM_ACTION"
    PROVIDE_LOCATION = "PROVIDE_LOCATION"
    CLARIFY_INTENT = "CLARIFY_INTENT"
    SELECTION_MENU = "SELECTION_MENU"
    VERIFY_OTP = "VERIFY_OTP"


# Kinds résolus dynamiquement depuis le tunnel panier (jamais depuis l'état
# persisté explicite) — voir docstring de module.
CART_TUNNEL_KINDS = frozenset(
    {
        InteractionKind.SELECT_PRODUCER,
        InteractionKind.SELECT_PRICING_TIER,
        InteractionKind.ENTER_PACKAGE_COUNT,
        InteractionKind.ENTER_QUANTITY,
    }
)


#: (2026-09-09, audit Bloc 2, fermeture Blocker B) : ancien pseudo-goal posé
#: dans `current_goal` pour marquer « en attente de désambiguïsation »,
#: avant l'existence de ce module. `goal_planner`/`memory_update` ne le
#: lisent plus JAMAIS pour décider quoi que ce soit — seul
#: `pending_interaction.kind == SELECTION_MENU and .context_ref ==
#: "intent_disambiguation"` fait foi (source unique, cette classe). Cette
#: constante ne survit que comme valeur ÉCRITE dans `current_goal`, en
#: SORTIE, pendant le tour où le menu de désambiguïsation est ré-affiché
#: (sélection invalide/absente) — preuve exacte du lecteur qui empêche sa
#: suppression complète : `validator` (gelé) traite tout goal absent
#: d'`INTENT_CONFIG` comme un no-op inoffensif (required=[], la réponse déjà
#: posée par goal_planner — `response_strategy="SELECTION_MENU"` — survit
#: intacte) ; `current_goal=None` a un comportement DIFFÉRENT et cassant
#: dans `validator` (branche `if not goal` → force
#: `response_strategy="CLARIFICATION"`, écrase le menu AG-UI). Vérifié
#: empiriquement en chaînant goal_planner → memory_update → validator sur
#: ce scénario avant ce correctif — voir
#: tests/interpreter/test_goal_planner_state_machine.py
#: (TestRule0bisDisambiguation) et tests/nodes/test_memory_stale_menu_snapshot.py.
DISAMBIGUATION_MENU_GOAL_SHIM = "DISAMBIGUATION_PENDING"


class InteractionStatus(str, Enum):
    ACTIVE = "ACTIVE"
    RESOLVED = "RESOLVED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    REPLACED = "REPLACED"


@dataclass(frozen=True)
class PendingInteraction:
    kind: InteractionKind = InteractionKind.NONE
    goal: Optional[str] = None
    field: Optional[str] = None
    context_ref: Optional[str] = None
    candidates: Tuple[str, ...] = ()
    created_at: float = 0.0
    status: InteractionStatus = InteractionStatus.ACTIVE
    # (2026-09-03, refonte transactionnelle) : `target` porte une référence
    # PRÉCISE à l'objet métier concerné — ex. {"draft_id":..., "draft_
    # version":...} pour `domain/procurement_draft.py::ConfirmationTarget`.
    # Générique (dict brut, pas de type domaine importé ici — `core` reste
    # feuille) : n'importe quel domaine versionné peut s'en servir sans
    # coupler ce module à un domaine métier précis. `context_ref` reste le
    # pointeur GROSSIER historique ("confirmation") ; `target`, quand
    # présent, est la référence PRÉCISE (quel draft, quelle version) —
    # kind==CONFIRM_ACTION sans target reste valide pour les flows non
    # encore migrés vers un draft versionné (voir check_invariants).
    target: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind.value,
            "goal": self.goal,
            "field": self.field,
            "context_ref": self.context_ref,
            "candidates": list(self.candidates),
            "created_at": self.created_at,
            "status": self.status.value,
            "target": dict(self.target) if isinstance(self.target, dict) else None,
        }

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "PendingInteraction":
        if not isinstance(d, dict) or not d:
            return cls()
        try:
            kind = InteractionKind(d.get("kind") or "NONE")
        except ValueError:
            kind = InteractionKind.NONE
        try:
            status = InteractionStatus(d.get("status") or "ACTIVE")
        except ValueError:
            status = InteractionStatus.ACTIVE
        raw_target = d.get("target")
        return cls(
            kind=kind,
            goal=d.get("goal"),
            field=d.get("field"),
            context_ref=d.get("context_ref"),
            candidates=tuple(d.get("candidates") or ()),
            created_at=float(d.get("created_at") or 0.0),
            status=status,
            target=dict(raw_target) if isinstance(raw_target, dict) else None,
        )


# =====================================================================
# API de mutation — SEUL point d'écriture autorisé pour l'état persisté
# (les kinds du tunnel panier, eux, ne sont jamais écrits ici — voir
# docstring de module).
# =====================================================================


def set_pending_interaction(
    kind: InteractionKind,
    *,
    goal: Optional[str] = None,
    field_name: Optional[str] = None,
    context_ref: Optional[str] = None,
    candidates: Tuple[str, ...] = (),
    target: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Retourne le patch state à renvoyer par le node appelant. N'écrase
    jamais partiellement — toujours l'interaction ENTIÈRE (reducer
    `replace_value` sur `pending_interaction`, voir core/state.py)."""
    interaction = PendingInteraction(
        kind=kind,
        goal=goal,
        field=field_name,
        context_ref=context_ref,
        candidates=tuple(candidates),
        created_at=time.time(),
        status=InteractionStatus.ACTIVE,
        target=dict(target) if isinstance(target, dict) else None,
    )
    return {"pending_interaction": interaction.to_dict()}


def clear_pending_interaction(reason: str) -> Dict[str, Any]:  # noqa: ARG001 — `reason` documente l'appelant, utile en log/debug
    return {"pending_interaction": None}


def resolve_pending_interaction() -> Dict[str, Any]:
    """Invariant 5 : une interaction résolue ne reste JAMAIS active au tour
    suivant — retourne directement `NONE`, pas un statut RESOLVED persistant
    (qui n'aurait aucun lecteur : le prochain tour doit repartir propre)."""
    return {"pending_interaction": None}


def replace_pending_interaction(
    new_kind: InteractionKind,
    *,
    goal: Optional[str] = None,
    field_name: Optional[str] = None,
    context_ref: Optional[str] = None,
    candidates: Tuple[str, ...] = (),
    target: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Sémantiquement identique à `set_pending_interaction` (le reducer
    `replace_value` remplace de toute façon intégralement) — nom distinct
    pour la lisibilité des call sites qui remplacent explicitement une
    interaction par une autre (ex: confirmation → nouvelle sélection)."""
    return set_pending_interaction(
        new_kind,
        goal=goal,
        field_name=field_name,
        context_ref=context_ref,
        candidates=candidates,
        target=target,
    )


# =====================================================================
# Résolution — lecture canonique
# =====================================================================


def get_pending_interaction(state: Dict[str, Any]) -> PendingInteraction:
    """Résolveur canonique unique — priorité :

    1. Tunnel panier (producteur/palier/quantité) — dérivé à neuf de
       `vendor_selection_context`/`tier_selection_context` via
       `build_selection_context`, jamais périmable par construction.
    2. État persisté explicite (`state["pending_interaction"]`), écrit par
       `confirmation_gate`/`gps_delivery_gate`/`validator`/
       `clarification_node`/`semantic_disambiguation`.
    3. `NONE`.

    Import local de `selection_actions` : évite tout risque de cycle
    (`core` → `domain` reste feuille, mais gardé local par précaution,
    même discipline que `router.py::_cart_guard`)."""
    from agriconnect.graphs.agents.market_coach.domain.selection_actions import (
        ActionType,
        build_selection_context,
    )

    selection_context = build_selection_context(state)
    if selection_context.expected_action is not None:
        mapping = {
            ActionType.SELECT_PRODUCER: (
                InteractionKind.SELECT_PRODUCER,
                "vendor_selection_context",
                tuple(selection_context.producer_ids()),
            ),
            ActionType.SELECT_PRICING_TIER: (
                InteractionKind.SELECT_PRICING_TIER,
                "tier_selection_context",
                tuple(selection_context.tier_ids()),
            ),
            ActionType.SET_PACKAGE_COUNT: (
                InteractionKind.ENTER_PACKAGE_COUNT,
                "tier_selection_context",
                tuple(selection_context.tier_ids()),
            ),
            ActionType.SET_QUANTITY: (
                InteractionKind.ENTER_QUANTITY,
                "vendor_selection_context",
                (),
            ),
        }
        kind, ctx_ref, candidates = mapping[selection_context.expected_action]
        return PendingInteraction(
            kind=kind,
            goal=state.get("current_goal"),
            context_ref=ctx_ref,
            candidates=candidates,
        )

    persisted = state.get("pending_interaction")
    if persisted:
        return PendingInteraction.from_dict(persisted)

    return legacy_confirmation_bridge(state) or PendingInteraction()


def legacy_confirmation_bridge(state: Dict[str, Any]) -> Optional[PendingInteraction]:
    """Pont de transition à DURÉE LIMITÉE — fonction pure isolée ICI et
    nulle part ailleurs (justification exigée par le mandat de refonte §38 —
    pas un "je garde pour compatibilité" générique) : les conversations déjà
    persistées en base AVANT le déploiement de `pending_interaction`
    (workspace Postgres, `workspace/checkpointer.py`) n'ont pas ce champ.
    Sans ce pont, un utilisateur au milieu d'une confirmation au moment du
    déploiement tomberait sur `NONE` au tour suivant — perdant exactement la
    confirmation qu'il était en train de traiter. Dès que `confirmation_gate`
    (seul écrivain de CONFIRM_ACTION) traite ce tour, il réécrit
    `pending_interaction` explicitement et ce pont ne s'applique plus pour
    cette conversation. Ne couvre QUE CONFIRM_ACTION (le seul cas où la
    perte silencieuse d'état a un coût métier réel) — pas les autres kinds,
    qui s'auto-régénèrent en un tour via `validator`.

    Extraite en fonction pure (au lieu d'un bloc inline dans
    `get_pending_interaction`) pour avoir UN SEUL point d'implémentation
    partagé avec `migrations/migrate_pending_interaction.py` — le migrateur
    one-shot qui applique cette même traduction directement aux checkpoints
    déjà persistés, pour permettre de retirer ce pont runtime une fois
    confirmé qu'aucune ligne pré-déploiement ne subsiste (cf. rapport final,
    section "Legacy restant")."""
    legacy_awaiting_confirmation = bool(state.get("waiting_for_confirmation")) or (
        str(state.get("expected_input") or "").upper().strip() == "CONFIRMATION"
    )
    if legacy_awaiting_confirmation and (
        state.get("confirmation_summary") or state.get("transaction_payload")
    ):
        return PendingInteraction(
            kind=InteractionKind.CONFIRM_ACTION,
            goal=state.get("current_goal"),
            context_ref="confirmation",
        )
    return None


# =====================================================================
# Invariants — utilisés par les tests de contrat + un log défensif en prod
# (ne lève JAMAIS d'exception qui casserait un tour réel).
# =====================================================================


def check_invariants(state: Dict[str, Any]) -> List[str]:
    """Retourne la liste des violations détectées (vide = tout est cohérent).
    Volontairement permissif dans les cas ambigus — un faux négatif (violation
    manquée) est préférable à un faux positif qui casserait un test légitime."""
    violations: List[str] = []
    pending = get_pending_interaction(state)

    # Invariant 2 : CONFIRM_ACTION doit avoir un contexte de confirmation
    # cohérent — un `confirmation_summary` construit pour CE goal/payload.
    if pending.kind == InteractionKind.CONFIRM_ACTION:
        if not state.get("confirmation_summary") and not state.get(
            "transaction_payload"
        ):
            violations.append(
                "CONFIRM_ACTION sans confirmation_summary ni transaction_payload"
            )

    # Invariant 3/4 : un kind du tunnel panier doit référencer un contexte
    # encore vivant (non "__reset__"/None) pour LE MÊME champ que celui
    # annoncé — matérialise "pas de réutilisation d'un ancien produit".
    if pending.kind in CART_TUNNEL_KINDS and pending.context_ref:
        ctx = state.get(pending.context_ref)
        live = isinstance(ctx, dict) and bool(ctx) and not ctx.get("__reset__")
        if not live:
            violations.append(
                f"{pending.kind.value} référence {pending.context_ref} mais "
                "ce contexte n'est pas vivant"
            )

    return violations


# =====================================================================
# Pont vers TunnelManager (core/tunnel_manager.py) — catégorie grossière
# d'interruption-tolérance (PRODUCT/PRICE/.../SELECTION/CONFIRMATION/OTP),
# un concept DISTINCT de `InteractionKind` (plus grossier, orienté
# "peut-on interrompre ?" et non "qu'attend-on précisément ?") mais qui doit
# désormais être DÉRIVÉ de `pending_interaction`, jamais lu séparément depuis
# `state["expected_input"]` — sinon on réintroduit exactement la fragmentation
# que cette refonte élimine (mandat §4/§38).
# =====================================================================


def to_tunnel_category(pending: "PendingInteraction") -> str:
    """Traduit un `PendingInteraction` vers le vocabulaire attendu par
    `TunnelManager.evaluate()` (`core/tunnel_manager.py`). Pour `ENTER_FIELD`,
    délègue à `core/slots.py::expected_input_for_field` — la seule table
    canonique nom-de-champ → catégorie (voir [[slot-canonicalization-single-source]]),
    pas une seconde table qui dériverait."""
    from agriconnect.graphs.agents.market_coach.core.slots import (
        expected_input_for_field,
    )

    if pending.kind == InteractionKind.ENTER_FIELD:
        field = pending.field or ""
        mapped = expected_input_for_field(field)
        if mapped != "NONE":
            return mapped
        # (2026-09-02) Champ hors du registre canonique de core/slots.py (ex:
        # "order_id", "cancellation_reason", "update_field" — des mini-flows
        # dédiés, pas des slots métier standards) : retombe sur le nom du
        # champ lui-même plutôt que sur "NONE". Préserve EXACTEMENT le
        # comportement d'avant la migration (l'ancienne valeur brute
        # `expected_input="ORDER_ID"` engageait déjà `has_tunnel=True` dans
        # TunnelManager sans figurer dans SOFT/HARD_EXPECTED_INPUTS) — sans
        # ce repli, tout champ non enregistré perdrait silencieusement sa
        # protection anti-interruption.
        return field.upper().strip() or "NONE"
    # (correctif) : ENTER_PACKAGE_COUNT/ENTER_QUANTITY font partie de
    # CART_TUNNEL_KINDS (même famille "tunnel panier", pour l'invariant
    # 3/4 de check_invariants) mais représentent un NOMBRE attendu, pas un
    # index de menu — catégorie "QUANTITY" pour TunnelManager (déjà dans
    # SLOT_FILLING_INPUTS, core/slots.py), jamais "SELECTION". Les confondre
    # cassait le fast-path "2 bidons" après un palier déjà résolu.
    if pending.kind in (InteractionKind.ENTER_PACKAGE_COUNT, InteractionKind.ENTER_QUANTITY):
        return "QUANTITY"
    if pending.kind in CART_TUNNEL_KINDS or pending.kind == InteractionKind.SELECTION_MENU:
        return "SELECTION"
    if pending.kind == InteractionKind.CONFIRM_ACTION:
        return "CONFIRMATION"
    if pending.kind == InteractionKind.VERIFY_OTP:
        return "OTP_CODE"
    if pending.kind == InteractionKind.PROVIDE_LOCATION:
        return "LOCATION"
    return "NONE"


__all__ = [
    "InteractionKind",
    "InteractionStatus",
    "PendingInteraction",
    "CART_TUNNEL_KINDS",
    "DISAMBIGUATION_MENU_GOAL_SHIM",
    "set_pending_interaction",
    "clear_pending_interaction",
    "resolve_pending_interaction",
    "replace_pending_interaction",
    "get_pending_interaction",
    "legacy_confirmation_bridge",
    "check_invariants",
    "to_tunnel_category",
]
