"""`TurnTrace` — diagnostic structuré d'UN tour, capturé AVANT le nettoyage de fin de tour
(Phase 2 hardening, commit 11).

## Le problème que ce module ferme

L'audit (2026-09-24) documente que la télémétrie existante (`ladini.core.turn_telemetry::
note_final`) lit l'état APRÈS que le graphe entier — `post_response_cleanup` compris — ait
tourné : `detected_intent`, `interpreter_confidence`, `current_goal` y sont donc souvent déjà
remis à `None` (voir `nodes/cleanup.py::post_response_cleanup`, qui efface délibérément ces
champs ÉPHÉMÈRES pour ne pas faire grossir l'état indéfiniment d'un tour à l'autre — un
comportement CORRECT pour l'état métier, mais qui rend le diagnostic aveugle). Ce module ne
remplace PAS `turn_telemetry.py` (métriques de performance SQL/Redis/LLM/outils, persistées
dans `intelligence.agent_turns`) — il capture un instantané COMPLÉMENTAIRE, orienté état
conversationnel, au SEUL point où toute l'information existe encore : l'entrée de
`post_response_cleanup` lui-même.

## Confidentialité par défaut

Ne capture JAMAIS le message utilisateur brut, la réponse complète, un numéro en clair, ou une
donnée métier sensible — voir `_hash_conversation_id` (réutilise `turn_telemetry.hash_phone`,
même pemphrase/pepper, pas un second schéma de hachage concurrent) et l'absence délibérée de
tout champ "texte" dans `TurnTrace`.

## `classify_turn` en mode SHADOW (mandat C6 §21-22, branché ici comme prévu)

`core/turn_policy.py::classify_turn` reste **SHADOW ONLY** : ce module l'appelle pour
PEUPLER `TurnTrace.turn_decision`, jamais pour décider quoi que ce soit. La garde
architecturale de `turn_policy.py` (`tests/unit/test_turn_policy_classification.py::
test_turn_policy_is_shadow_only_not_authoritative`) autorise explicitement CE module
(`turn_trace.py`) comme second importeur légitime, au même titre que `turn_telemetry.py`.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional

from ladini.graphs.agents.market_coach.core.draft_registry import DRAFT_REGISTRY
from ladini.graphs.agents.market_coach.core.pending_interaction import (
    PendingInteraction,
    get_pending_interaction,
)
from ladini.graphs.agents.market_coach.core.state import resolve_current_goal
from ladini.graphs.agents.market_coach.core.turn_policy import classify_turn

logger = logging.getLogger("ladini.market_coach.turn_trace")


def _hash_conversation_id(phone: Optional[str]) -> str:
    """Identifiant de conversation opaque — jamais le numéro en clair. Réutilise LE
    hachage déjà canonique (`turn_telemetry.hash_phone`, HMAC-SHA256 poivré) plutôt qu'un
    second schéma : une seule fonction sait comment un numéro devient un identifiant."""
    from ladini.core.turn_telemetry import hash_phone

    return str(hash_phone(phone or ""))


def _tunnel_label(goal: Optional[str]) -> Optional[str]:
    """Repli minimal quand aucun draft actif ne nomme déjà le flow (voir `flow` dans
    `TurnTrace`) — délibérément approximatif (le nom du goal lui-même), jamais une seconde
    table de correspondance goal->tunnel qui dupliquerait celle, informelle, déjà éclatée
    dans `core/goals.py`/les flows eux-mêmes."""
    return goal


@dataclass(frozen=True)
class TurnTrace:
    """Instantané IMMUTABLE d'un tour — un `dataclass(frozen=True)`, jamais muté après
    construction (voir `TurnTraceBuilder`, qui accumule les deux moitiés — début/fin de
    tour — avant de le construire UNE fois)."""

    turn_id: str
    conversation_id_hash: str
    #: `None` sur un canal sans identifiant transport stable (WebChat, à ce jour — voir
    #: docstring de module et `TurnTraceBuilder.start`). JAMAIS confondu avec `turn_id`
    #: (toujours généré, un par tour) ni `conversation_id_hash` (stable sur toute la
    #: conversation) — trois identifiants distincts, documentés ici pour lever toute
    #: ambiguïté une fois pour toutes.
    message_id: Optional[str]
    channel: str

    intent: Optional[str]
    confidence: Optional[float]
    interpreted_event: Optional[str]

    goal_before: Optional[str]
    goal_after: Optional[str]
    tunnel_before: Optional[str]
    tunnel_after: Optional[str]

    #: SHADOW ONLY — voir docstring de module. Jamais consulté par un routeur/flow.
    turn_decision: Optional[str]
    turn_decision_reason: Optional[str]

    pending_before_kind: Optional[str]
    pending_before_field: Optional[str]
    pending_after_kind: Optional[str]
    pending_after_field: Optional[str]

    draft_type: Optional[str]
    draft_id: Optional[str]
    draft_version: Optional[int]
    draft_status: Optional[str]

    item_count: int
    ambiguous_group_count: int

    flow: Optional[str]
    outcome: Optional[str]

    duration_ms: Optional[int]
    error_class: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _draft_snapshot(state: Dict[str, Any]) -> Dict[str, Any]:
    """Le premier draft du registre (`core/draft_registry.py`) trouvé actif dans l'état —
    au plus UN est actif à la fois par construction (`test_each_draft_has_exactly_one_
    owning_goal`, commit 5) : jamais besoin d'en choisir plusieurs."""
    for spec in DRAFT_REGISTRY:
        raw = state.get(spec.state_key)
        if isinstance(raw, dict) and raw:
            return {
                "draft_type": spec.state_key,
                "draft_id": raw.get("draft_id"),
                "draft_version": raw.get("version"),
                "draft_status": raw.get("status"),
                "item_count": 1 + len(raw.get("additional_items") or []),
            }
    return {
        "draft_type": None,
        "draft_id": None,
        "draft_version": None,
        "draft_status": None,
        "item_count": 0,
    }


def _ambiguous_group_count(state: Dict[str, Any]) -> int:
    """Nombre de groupes ambigus portés par CE tour — source unique : le champ du contrat
    NEW_TASK (`interpreter/new_task_contract.py::NewTaskAmbiguousGroup`) tel qu'accumulé
    dans `extracted_entities`/`transaction_payload`, jamais recalculé indépendamment."""
    for key in ("extracted_entities", "transaction_payload"):
        groups = (state.get(key) or {}).get("ambiguous_groups") if isinstance(state.get(key), dict) else None
        if isinstance(groups, list):
            return len(groups)
    return 0


@dataclass
class _TurnTraceBuilder:
    """Mutable le temps d'accumuler début + fin de tour — jamais exposé tel quel en dehors
    de ce module (voir `TurnTrace`, la vue figée qu'il produit)."""

    turn_id: str
    conversation_id_hash: str
    message_id: Optional[str]
    channel: str
    _t0: float = field(default_factory=time.perf_counter)

    goal_before: Optional[str] = None
    tunnel_before: Optional[str] = None
    pending_before: PendingInteraction = field(default_factory=PendingInteraction)


_current: ContextVar[Optional[_TurnTraceBuilder]] = ContextVar(
    "ladini_turn_trace_builder", default=None
)


def start(
    *,
    conversation_phone: Optional[str],
    message_id: Optional[str],
    channel: str,
    before_state: Optional[Dict[str, Any]],
) -> None:
    """Ouvre la capture — appelé par `Orchestrator.handle()` juste après avoir chargé le
    Workspace, donc AVANT que le graphe ne tourne : `before_state` est reconstruit par
    l'appelant depuis `ws.metadata`/`ws.active_goal` (l'état métier PLAT tel que la dernière
    persistance l'a laissé — PAS `ws.agent_state`, qui est le blob de checkpoint LangGraph
    brut), le seul endroit où lire "goal AVANT ce tour" sans le confondre avec l'état déjà
    transformé par CE tour."""
    before_state = before_state if isinstance(before_state, dict) else {}
    builder = _TurnTraceBuilder(
        turn_id=str(uuid.uuid4()),
        conversation_id_hash=_hash_conversation_id(conversation_phone),
        message_id=message_id,
        channel=channel,
        goal_before=resolve_current_goal(before_state),
        pending_before=get_pending_interaction(before_state),
    )
    builder.tunnel_before = _tunnel_label(builder.goal_before)
    _current.set(builder)


def capture_pre_cleanup(state: Dict[str, Any]) -> None:
    """Appelé UNE fois, à l'entrée de `nodes/cleanup.py::post_response_cleanup` — AVANT que
    cette fonction n'applique son propre reset. Best-effort, jamais bloquant : une erreur ici
    ne doit jamais faire échouer le tour réel (voir le `try/except` dans `finish`, même
    discipline que `turn_telemetry.py`)."""
    builder = _current.get()
    if builder is None:
        return
    try:
        goal_after = resolve_current_goal(state)
        pending_after = get_pending_interaction(state)
        draft = _draft_snapshot(state)
        decision = classify_turn(
            interpreted_event=state.get("interpreted_event"),
            cognitive_decision=state.get("cognitive_decision"),
            goal_before=builder.goal_before,
            goal_after=goal_after,
            normalized_text=str(state.get("normalized_text") or ""),
        )
        flow = draft["draft_type"].removesuffix("_draft") if draft["draft_type"] else _tunnel_label(
            goal_after or builder.goal_before
        )
        snapshot = {
            "intent": state.get("detected_intent"),
            "confidence": state.get("interpreter_confidence"),
            "interpreted_event": state.get("interpreted_event"),
            "goal_after": goal_after,
            "tunnel_after": _tunnel_label(goal_after),
            "turn_decision": decision.action.value,
            "turn_decision_reason": decision.reason,
            "pending_after_kind": pending_after.kind.value,
            "pending_after_field": pending_after.field,
            "flow": flow,
            "outcome": state.get("response_strategy") or state.get("status"),
            "ambiguous_group_count": _ambiguous_group_count(state),
            # (Phase 2 hardening, commit 11) : `_safe_node` (utils.py) AVALE toute
            # exception de nœud et la convertit en patch d'état (`status="ERROR"`,
            # `error_class=type(exc).__name__`) — le graphe "réussit" ensuite du point
            # de vue d'`Orchestrator.handle()`, qui prend alors la branche `else:` et
            # appelle `finish()` SANS argument. Sans ce repli, ces erreurs (la
            # majorité — un nœud qui lève est le cas courant) resteraient avec
            # `error_class=None`. Priorité à l'argument explicite de `finish()` (voir
            # là-bas) : celui-ci couvre les erreurs qui échappent à TOUS les nœuds
            # (le graphe entier, `asyncio.wait_for`, le circuit breaker).
            "state_error_class": state.get("error_class"),
            **draft,
        }
        # Stocké tel quel sur le builder (attributs dynamiques, jamais lus ailleurs) —
        # simple porteur temporaire entre les deux points de capture.
        for key, value in snapshot.items():
            setattr(builder, f"_post_{key}", value)
    except Exception:  # pragma: no cover - défensif, jamais vers l'appelant
        logger.debug("turn_trace.capture_pre_cleanup ignoré", exc_info=True)


def finish(*, error_class: Optional[str] = None) -> Optional[TurnTrace]:
    """Ferme la capture et journalise le `TurnTrace` en une ligne structurée — jamais de
    nouvelle table/migration pour ce commit (mandat §23) : un log structuré est déjà
    diagnosticable (agrégation de logs), et n'engage aucun schéma Drizzle. Retourne le
    `TurnTrace` (utile aux tests) ; l'appelant réel (`Orchestrator.handle`) ignore la valeur
    de retour."""
    builder = _current.get()
    _current.set(None)
    if builder is None:
        return None
    try:
        duration_ms = max(0, int((time.perf_counter() - builder._t0) * 1000))

        def get(key: str, default: Any = None) -> Any:
            return getattr(builder, f"_post_{key}", default)
        trace = TurnTrace(
            turn_id=builder.turn_id,
            conversation_id_hash=builder.conversation_id_hash,
            message_id=builder.message_id,
            channel=builder.channel,
            intent=get("intent"),
            confidence=get("confidence"),
            interpreted_event=get("interpreted_event"),
            goal_before=builder.goal_before,
            goal_after=get("goal_after"),
            tunnel_before=builder.tunnel_before,
            tunnel_after=get("tunnel_after"),
            turn_decision=get("turn_decision"),
            turn_decision_reason=get("turn_decision_reason"),
            pending_before_kind=builder.pending_before.kind.value,
            pending_before_field=builder.pending_before.field,
            pending_after_kind=get("pending_after_kind"),
            pending_after_field=get("pending_after_field"),
            draft_type=get("draft_type"),
            draft_id=get("draft_id"),
            draft_version=get("draft_version"),
            draft_status=get("draft_status"),
            item_count=get("item_count", 0),
            ambiguous_group_count=get("ambiguous_group_count", 0),
            flow=get("flow"),
            outcome=get("outcome"),
            duration_ms=duration_ms,
            error_class=error_class or get("state_error_class"),
        )
        logger.info("TURN_TRACE | %s", trace.to_dict())
        global _last_trace
        _last_trace = trace
        return trace
    except Exception:  # pragma: no cover - défensif, jamais vers l'appelant
        logger.debug("turn_trace.finish ignoré", exc_info=True)
        return None


#: Dernier `TurnTrace` émis — introspection pour les TESTS uniquement (même discipline que
#: `turn_telemetry.write_failures`/`write_successes` : un accesseur best-effort, jamais lu
#: par un chemin métier de production). `Orchestrator.handle()` ignore la valeur de retour
#: de `finish()` ; les tests, eux, ont besoin d'un moyen d'inspecter ce qui vient d'être émis
#: sans reparser une ligne de log.
_last_trace: Optional[TurnTrace] = None


def last_trace() -> Optional[TurnTrace]:
    return _last_trace


def current_builder() -> Optional[_TurnTraceBuilder]:
    """Exposé pour les tests uniquement (vérifier qu'une capture est bien en cours)."""
    return _current.get()


__all__ = [
    "TurnTrace",
    "start",
    "capture_pre_cleanup",
    "finish",
    "current_builder",
    "last_trace",
]
