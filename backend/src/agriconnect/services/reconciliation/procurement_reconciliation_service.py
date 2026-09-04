"""`ProcurementReconciliationService` — recovery des `ProcurementDraft`
bloqués en `EXECUTING` (2026-09-03, phase 1 du mandat recovery/reconciliation).

## Rôle EXACT, et où il ne va PAS

Ce module fait EXACTEMENT : trouver les candidats (`find_stale_executing`,
côté appelant — voir `find_stale_executing_candidates` ci-dessous) →
inspecter l'état externe connu (`mcp_idempotency_store.peek`, en LECTURE
SEULE) → décider l'issue → finaliser le draft (CAS, comme partout ailleurs
dans ce chantier). Il ne fait JAMAIS :

- Il n'appelle JAMAIS `create_auction` ni aucun outil MCP en écriture — la
  réconciliation ne PEUT PAS, par construction, recréer une auction (mandat
  §1.5/§1.6/§3 : « aucun retry ne doit générer une deuxième opération
  métier »). Un retry RÉEL du côté écriture nécessiterait de ré-entrer le
  graphe LangGraph comme un tour synthétique (pattern déjà utilisé ailleurs
  dans ce repo pour les sollicitations proactives, Celery Beat + Outbox) —
  DÉLIBÉRÉMENT hors périmètre de cette phase, voir la section « Limite »
  plus bas. Cette classe se limite à CLASSIFIER et FINALISER l'état.
- Il ne vit ni dans `confirmation_gate`, ni `mcp_tool_executor`, ni
  `response_strategy` (consigne explicite du mandat) — nœud/service
  entièrement séparé, invoqué par un worker dédié
  (`workers/procurement_reconciliation_worker.py`).

## Les 3 issues possibles (mandat §1.4)

```
EXECUTING
    │
    ▼
mcp_idempotency_store.peek(execution_key(draft), "create_auction")
    │
    ├── COMPLETED trouvé  → EXTERNAL_EFFECT_FOUND → finalize → EXECUTED
    ├── FAILED trouvé     → CONFIRMED_NO_EFFECT    → finalize → FAILED
    └── PENDING / absent  → AMBIGUOUS              → finalize → EXECUTION_UNKNOWN
```

Réutilise `adapt_mcp_result`/`finalize_after_execution`
(`domain/procurement_draft.py`) — LES MÊMES fonctions que le chemin
d'exécution normal, jamais un adaptateur dupliqué.

## Limite honnête (mandat : ne pas maquiller)

`mcp_idempotency_store` n'a une ligne QUE pour les tentatives qui ont
atteint `AgriDBMCPServer.call_tool` avec une `_idempotency_key` — un crash
survenu AVANT ce point (ex: entre `apply_domain_action` marquant le draft
`EXECUTING` et `mcp_tool_executor` lançant réellement l'appel) laisse
`peek()` renvoyer `None`, indiscernable ici d'« jamais tenté ». Ces deux cas
sont fondus dans `AMBIGUOUS` — PAS traités comme un échec confirmé (ce
serait deviner), donc jamais retentés automatiquement.

## Idempotence (mandat §1.5/§5)

`reconcile_draft` réutilise EXCLUSIVEMENT le CAS déjà en place
(`procurement_draft_store.compare_and_swap`) pour la finalisation — AUCUN
nouveau verrou ad hoc. Deux appels concurrents sur le MÊME `draft_id`/
version produisent EXACTEMENT une seule finalisation persistée ; le
perdant de la course relit ce qui a été écrit plutôt que d'imposer son
propre verdict."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

from agriconnect.core.settings import settings
from agriconnect.core.telemetry import record_procurement_reconciliation_event
from agriconnect.graphs.agents.market_coach.domain.procurement_draft import (
    ProcurementDraft,
    ProcurementDraftStatus,
    ProcurementOutcome,
    ProcurementOutcomeKind,
    adapt_mcp_result,
    build_response_plan,
    execution_key,
    finalize_after_execution,
)
from agriconnect.services.database import mcp_idempotency_store, procurement_draft_store
from agriconnect.services.database.draft_store_support import cas_finalize

logger = logging.getLogger("agriconnect.services.reconciliation.procurement")

# Le SEUL outil produit par le pipeline PROCUREMENT_CREATE_REQUEST — audité
# (tests/architecture/test_procurement_execution_pipeline_closure.py::
# TestSingleExecutionPath) : un unique call site produit `create_auction`.
# Couplage EXPLICITE et documenté, pas un accident : si ce chemin change,
# ce module doit être mis à jour avec lui.
PROCUREMENT_MCP_TOOL_NAME = "create_auction"


class ReconciliationOutcome(str, Enum):
    EXTERNAL_EFFECT_FOUND = "EXTERNAL_EFFECT_FOUND"
    CONFIRMED_NO_EFFECT = "CONFIRMED_NO_EFFECT"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_EXECUTING = "NOT_EXECUTING"  # no-op : déjà finalisé (par CE réconciliateur, un autre, ou le flux normal)


_OUTCOME_KIND_BY_TERMINAL_STATUS = {
    ProcurementDraftStatus.EXECUTED: ProcurementOutcomeKind.PROCUREMENT_EXECUTED,
    ProcurementDraftStatus.FAILED: ProcurementOutcomeKind.PROCUREMENT_FAILED,
    ProcurementDraftStatus.EXECUTION_UNKNOWN: ProcurementOutcomeKind.PROCUREMENT_EXECUTION_UNKNOWN,
}

# Noms d'événements Langfuse (mandat §6) — `RECONCILIATION_RETRY` du mandat
# est délibérément RENOMMÉ `RECONCILIATION_CONFIRMED_NO_EFFECT` : ce module
# ne retente JAMAIS l'écriture lui-même (voir la docstring du module,
# section « Limite honnête ») — nommer l'événement "RETRY" alors qu'aucun
# retry n'a lieu serait exactement le genre de maquillage que ce chantier
# interdit. Le nom honnête reflète ce qui se passe RÉELLEMENT : la ligne est
# finalisée FAILED, un futur mécanisme de retry (hors périmètre) pourra s'y
# brancher.
_RECONCILIATION_EVENT_NAME_BY_OUTCOME = {
    ReconciliationOutcome.EXTERNAL_EFFECT_FOUND: "RECONCILIATION_FOUND_EXTERNAL_EFFECT",
    ReconciliationOutcome.CONFIRMED_NO_EFFECT: "RECONCILIATION_CONFIRMED_NO_EFFECT",
    ReconciliationOutcome.AMBIGUOUS: "RECONCILIATION_UNKNOWN",
}
_RECONCILIATION_REASON_BY_OUTCOME = {
    ReconciliationOutcome.EXTERNAL_EFFECT_FOUND: "mcp_idempotency_record_completed",
    ReconciliationOutcome.CONFIRMED_NO_EFFECT: "mcp_idempotency_record_failed",
    ReconciliationOutcome.AMBIGUOUS: "mcp_idempotency_record_pending_or_absent",
}


@dataclass(frozen=True)
class ReconciliationResult:
    draft_id: str
    version_before: int
    outcome: ReconciliationOutcome
    final_status: Optional[str]
    finalized_draft: Optional[ProcurementDraft]
    persisted: bool


async def find_stale_executing_candidates() -> List[ProcurementDraft]:
    """Point d'entrée §1.2 — seuil configurable (`settings`, jamais codé en
    dur ici)."""
    return await procurement_draft_store.find_stale_executing(
        older_than_seconds=settings.PROCUREMENT_EXECUTING_STALE_SECONDS
    )


async def reconcile_draft(draft: ProcurementDraft, *, attempt: int = 1) -> ReconciliationResult:
    """Trouve/inspecte/décide/finalise — voir la docstring du module pour
    la sémantique exacte des 3 issues et la limite honnête sur `AMBIGUOUS`.

    `attempt` (mandat §6, champ d'observabilité demandé) : numéro de cette
    tentative de réconciliation POUR CE draft — passé par l'appelant (ex: un
    worker qui retente dans le même tick), défaut 1. Ce module ne tient PAS
    lui-même de compteur cross-tick persisté (aucun champ dédié ajouté au
    schéma pour ça — limite assumée, pas cachée : voir le rapport final)."""
    if draft.status != ProcurementDraftStatus.EXECUTING:
        # No-op idempotent : soit déjà réconcilié (par cet appel-ci relancé,
        # un autre worker, ou le flux normal de finalisation), soit jamais
        # entré en EXECUTING — dans tous les cas, rien à faire ici.
        return ReconciliationResult(
            draft_id=draft.draft_id,
            version_before=draft.version,
            outcome=ReconciliationOutcome.NOT_EXECUTING,
            final_status=draft.status.value,
            finalized_draft=draft,
            persisted=False,
        )

    key = execution_key(draft)
    record_procurement_reconciliation_event(
        event_name="STALE_EXECUTING_DETECTED",
        draft_id=draft.draft_id,
        draft_version=draft.version,
        execution_key=key,
        state_before=draft.status.value,
        state_after=None,
        reconciliation_reason="executing_past_stale_threshold",
        external_lookup=None,
        attempt=attempt,
        outcome="PENDING_LOOKUP",
    )

    record = await mcp_idempotency_store.peek(key, PROCUREMENT_MCP_TOOL_NAME)

    external_lookup = "peek_none"
    if record is None:
        outcome = ReconciliationOutcome.AMBIGUOUS
        mcp_result = adapt_mcp_result(None, None)
    elif record.status == "COMPLETED":
        external_lookup = "peek_completed"
        outcome = ReconciliationOutcome.EXTERNAL_EFFECT_FOUND
        raw = record.external_result if isinstance(record.external_result, dict) else {}
        mcp_result = adapt_mcp_result("COMPLETED", raw)
    elif record.status == "FAILED":
        external_lookup = "peek_failed"
        outcome = ReconciliationOutcome.CONFIRMED_NO_EFFECT
        raw = (
            record.external_result
            if isinstance(record.external_result, dict)
            else {"message": "reconciliation_confirmed_no_effect"}
        )
        mcp_result = adapt_mcp_result("ERROR", raw)
    else:  # PENDING — la couche MCP elle-même ne sait pas encore trancher
        external_lookup = "peek_pending"
        outcome = ReconciliationOutcome.AMBIGUOUS
        mcp_result = adapt_mcp_result(None, None)

    finalized_candidate = finalize_after_execution(draft, mcp_result)

    # (2026-09-03, hardening transverse) : `cas_finalize` — motif partagé
    # avec `preorder_reconciliation_service.reconcile_executing_draft` et
    # `flows/buyer/preorder_payment.py::_finalize_and_persist`, qui
    # implémentaient chacun ce même "CAS, et si un autre écrivain a gagné la
    # course, relire ce qu'il a RÉELLEMENT écrit" — idempotence PAR
    # CONSTRUCTION (mandat §1.5/§5), jamais notre propre verdict imposé.
    persisted, finalized = await cas_finalize(
        compare_and_swap=procurement_draft_store.compare_and_swap,
        load=procurement_draft_store.load,
        original=draft,
        finalized=finalized_candidate,
        log_conflict=lambda: logger.warning(
            "PROCUREMENT_RECONCILIATION_VERSION_CONFLICT | draft_id=%s | version=%s",
            draft.draft_id,
            draft.version,
        ),
    )

    outcome_kind = _OUTCOME_KIND_BY_TERMINAL_STATUS.get(finalized.status)
    if outcome_kind is not None:
        # Même chemin de présentation que le flux normal (pas de rendu ici
        # — un worker de fond ne parle à personne — mais la trace
        # d'observabilité DOIT rester cohérente avec ce que verrait un
        # utilisateur si CE tour avait fini par cette issue).
        build_response_plan(ProcurementOutcome(kind=outcome_kind, draft=finalized))

    logger.info(
        "PROCUREMENT_RECONCILIATION_TRACE | draft_id=%s | version_before=%s | "
        "version_after=%s | execution_key=%s | external_lookup=%s | "
        "reconciliation_outcome=%s | final_status=%s | persisted=%s",
        draft.draft_id,
        draft.version,
        finalized.version,
        key,
        external_lookup,
        outcome.value,
        finalized.status.value,
        persisted,
    )
    record_procurement_reconciliation_event(
        event_name=_RECONCILIATION_EVENT_NAME_BY_OUTCOME[outcome],
        draft_id=draft.draft_id,
        draft_version=finalized.version,
        execution_key=key,
        state_before=draft.status.value,
        state_after=finalized.status.value,
        reconciliation_reason=_RECONCILIATION_REASON_BY_OUTCOME[outcome],
        external_lookup=external_lookup,
        external_result=mcp_result.external_id or mcp_result.error,
        attempt=attempt,
        outcome=outcome_kind.value if outcome_kind else outcome.value,
    )

    return ReconciliationResult(
        draft_id=draft.draft_id,
        version_before=draft.version,
        outcome=outcome,
        final_status=finalized.status.value,
        finalized_draft=finalized,
        persisted=persisted,
    )


__all__ = [
    "PROCUREMENT_MCP_TOOL_NAME",
    "ReconciliationOutcome",
    "ReconciliationResult",
    "find_stale_executing_candidates",
    "reconcile_draft",
]
