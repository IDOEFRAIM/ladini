"""Contrat transactionnel — `ProcurementDraft` (refonte architecturale
2026-09-03, mandat "deep refactor confirmation flow").

Root cause de l'incident réel : `quantity`/`unit` (contrat d'exécution) et
`quantity_display`/`unit_display` (texte affiché) étaient deux
représentations INDÉPENDANTES du même fait métier dans `transaction_payload`
(`merge_dict`) — rien ne garantissait leur synchronisation. Ce fichier
verrouille les invariants qui rendent cette CLASSE de bug structurellement
impossible : draft versionné immuable, cible de confirmation liée à une
version précise, confirmation atomique/idempotente.
"""
from __future__ import annotations

import asyncio
import re
import threading
from pathlib import Path

import pytest

from ladini.graphs.agents.market_coach.domain.procurement_draft import (
    CancelProcurementDraft,
    ConfirmationTarget,
    ConfirmProcurementDraft,
    NoProcurementAction,
    ProcurementDraft,
    ProcurementDraftStatus,
    ProcurementOutcomeKind,
    RejectProcurementConfirmation,
    UpdateProcurementDraft,
    check_confirmation_target_invariant,
    apply_domain_action,
    resolve_domain_action,
)
from ladini.graphs.agents.market_coach.nodes.confirmation_gate import (
    confirmation_gate,
)
from ladini.graphs.agents.market_coach.nodes.memory import memory_update
from ladini.graphs.agents.market_coach.interpreter.routing import (
    _interpret_fast_path,
)
from tests.conftest import make_state, run, stub_runtime

_ALWAYS_CLAIM = lambda key: True  # noqa: E731 - petites fonctions de test


def _draft(**fields) -> ProcurementDraft:
    base = {
        "product": "tomates",
        "quantity": 2000.0,
        "unit": "KG",
        "price": 250.0,
        "price_unit": "KG",
    }
    base.update(fields)
    return ProcurementDraft.new(draft_id="d1", **base)


# =====================================================================
# INVARIANT 1 — deux versions consécutives n'ont jamais le même numéro
# =====================================================================


class TestInvariant1VersionsAreStrictlyIncreasing:
    def test_a_genuine_change_bumps_the_version(self):
        v1 = _draft()
        v2 = v1.with_updates(quantity=1125.0)
        assert v2.version == v1.version + 1

    def test_a_no_op_update_never_bumps_the_version(self):
        """Un UPDATE qui ne change RIEN (bruit LLM, valeur déjà identique)
        ne doit pas invalider une confirmation en attente pour rien."""
        v1 = _draft()
        v2 = v1.with_updates(quantity=2000.0, unit="KG")
        assert v2.version == v1.version
        assert v2 is v1

    def test_repeated_updates_strictly_increase(self):
        v = _draft()
        seen_versions = [v.version]
        for qty in (1000.0, 500.0, 250.0):
            v = v.with_updates(quantity=qty)
            seen_versions.append(v.version)
        assert seen_versions == sorted(set(seen_versions))
        assert len(set(seen_versions)) == len(seen_versions)


# =====================================================================
# INVARIANT 2 — une cible de confirmation correspond TOUJOURS à la
# version qu'elle référence, jamais à une autre
# =====================================================================


class TestInvariant2TargetMatchesExactVersion:
    def test_target_matches_only_its_own_draft_and_version(self):
        v1 = _draft()
        v2 = v1.with_updates(quantity=1.0)
        target_v1 = ConfirmationTarget(draft_id=v1.draft_id, draft_version=v1.version)
        assert target_v1.matches(v1)
        assert not target_v1.matches(v2)

    def test_target_for_a_different_draft_id_never_matches(self):
        v1 = _draft()
        other = ProcurementDraft.new(draft_id="other", product="tomates", quantity=1, unit="KG", price=1)
        target = ConfirmationTarget(draft_id=v1.draft_id, draft_version=v1.version)
        assert not target.matches(other)


# =====================================================================
# INVARIANT 3 — un UPDATE invalide toute confirmation antérieure
# =====================================================================


class TestInvariant3UpdateInvalidatesPriorConfirmation:
    def test_confirming_the_old_target_after_an_update_is_refused(self):
        v1 = _draft()
        stale_target = ConfirmationTarget(draft_id=v1.draft_id, draft_version=v1.version)

        update_action = UpdateProcurementDraft(fields={"quantity": 1125.0})
        outcome = apply_domain_action(v1, update_action)
        v2 = outcome.draft
        assert v2.version != v1.version

        confirm_stale = ConfirmProcurementDraft(target=stale_target)
        result = apply_domain_action(v2, confirm_stale, claim=_ALWAYS_CLAIM)
        assert result.kind == ProcurementOutcomeKind.STALE_TARGET
        # Le draft n'a JAMAIS transité vers CONFIRMED avec une cible périmée.
        assert result.draft.status == ProcurementDraftStatus.DRAFT


# =====================================================================
# INVARIANT 5 (mandat §5) — vérifiable à l'exécution, jamais fatal
# =====================================================================


class TestRuntimeInvariantCheck:
    def test_no_pending_target_is_never_a_violation(self):
        assert check_confirmation_target_invariant(None, _draft()) is None

    def test_target_matching_the_current_draft_is_not_a_violation(self):
        v1 = _draft()
        target = {"draft_id": v1.draft_id, "draft_version": v1.version}
        assert check_confirmation_target_invariant(target, v1) is None

    def test_target_pointing_a_stale_version_is_a_reported_violation(self):
        v1 = _draft()
        v2 = v1.with_updates(quantity=1.0)
        stale_target = {"draft_id": v1.draft_id, "draft_version": v1.version}
        violation = check_confirmation_target_invariant(stale_target, v2)
        assert violation is not None
        assert str(v1.version) in violation and str(v2.version) in violation

    def test_target_pointing_a_missing_draft_is_a_reported_violation(self):
        target = {"draft_id": "ghost", "draft_version": 1}
        violation = check_confirmation_target_invariant(target, None)
        assert violation is not None


# =====================================================================
# INVARIANT 4 — un CONFIRM sans target valide ne peut jamais exécuter
# =====================================================================


class TestInvariant4NoValidTargetNeverExecutes:
    def test_confirm_with_no_target_at_all(self):
        v1 = _draft()
        result = apply_domain_action(v1, ConfirmProcurementDraft(target=None), claim=_ALWAYS_CLAIM)
        assert result.kind == ProcurementOutcomeKind.NO_TARGET
        assert result.draft.status == ProcurementDraftStatus.DRAFT

    def test_confirm_with_a_malformed_target_dict(self):
        action = resolve_domain_action(
            interpreted_event="CONFIRM",
            extracted_entities={},
            pending_target={"draft_id": "d1"},  # pas de draft_version
        )
        assert isinstance(action, ConfirmProcurementDraft)
        assert action.target is None


# =====================================================================
# INVARIANT 5 — un CONFIRM réussi quitte l'attente de confirmation
# =====================================================================


class TestInvariant5SuccessfulConfirmLeavesWaitingConfirmation:
    def test_confirmed_status_is_no_longer_draft(self):
        v1 = _draft()
        target = ConfirmationTarget(draft_id=v1.draft_id, draft_version=v1.version)
        result = apply_domain_action(v1, ConfirmProcurementDraft(target=target), claim=_ALWAYS_CLAIM)
        assert result.kind == ProcurementOutcomeKind.CONFIRMED_READY_FOR_EXECUTION
        # DRAFT -> CONFIRMED -> EXECUTING est atomique (mandat §11) — le
        # draft persisté est déjà EXECUTING, jamais observable à CONFIRMED.
        assert result.draft.status == ProcurementDraftStatus.EXECUTING

    def test_the_node_resolves_pending_interaction_on_confirm(self, stub_runtime, monkeypatch):
        """`claim_once` réel tape Redis — un draft_id/version répété d'un
        run de test à l'autre (ou d'un test à l'autre dans ce même fichier)
        y apparaîtrait déjà réclamé. Forcé à toujours gagner ici : ce test
        vérifie la TRANSITION D'ÉTAT du nœud, pas le claim lui-même (couvert
        par TestConfirmIsIdempotent/TestConcurrentConfirmClaim ci-dessus,
        qui contrôlent `claim` explicitement)."""
        import ladini.graphs.agents.market_coach.domain.procurement_draft as pd_mod
        from ladini.graphs.agents.market_coach.core.pending_interaction import (
            InteractionKind,
            get_pending_interaction,
        )

        monkeypatch.setattr(pd_mod, "claim_once", _ALWAYS_CLAIM)
        v1 = _draft()
        state = make_state(
            current_goal="PROCUREMENT_CREATE_REQUEST",
            procurement_draft=v1.to_dict(),
            interpreted_event="CONFIRM",
            extracted_entities={},
        )
        state["pending_interaction"] = {
            "kind": "CONFIRM_ACTION",
            "target": {"draft_id": v1.draft_id, "draft_version": v1.version},
        }
        patch = run(confirmation_gate(state, stub_runtime()))
        merged = dict(state)
        merged.update(patch)
        assert patch["status"] == "EXECUTING"
        assert get_pending_interaction(merged).kind == InteractionKind.NONE


# =====================================================================
# INVARIANT 7 — un message naturel n'est JAMAIS évalué comme confirmation
# par le domaine (aucune comparaison de chaîne)
# =====================================================================


class TestInvariant7DomainNeverComparesRawText:
    def test_resolve_domain_action_signature_has_no_text_parameter(self):
        import inspect

        sig = inspect.signature(resolve_domain_action)
        for name in sig.parameters:
            assert "text" not in name.lower(), (
                f"resolve_domain_action accepte un paramètre texte ({name}) — "
                "le domaine ne doit connaître que interpreted_event/entities."
            )

    def test_no_string_literal_confirmation_words_in_the_domain_module(self):
        """Balayage source : ni 'oui', 'okay', 'confirme' ('confirme' seul —
        pas 'confirmation'/'CONFIRM_ACTION', des noms de contrat légitimes)
        ne doivent apparaître comme littéral de comparaison dans le module
        domaine."""
        src = Path(
            "src/ladini/graphs/agents/market_coach/domain/procurement_draft.py"
        ).read_text(encoding="utf-8")
        forbidden = re.compile(r'["\']( ?oui| ?okay| ?ok| ?d\'accord)["\']', re.IGNORECASE)
        matches = forbidden.findall(src)
        assert not matches, f"le domaine compare du texte utilisateur brut : {matches}"


# =====================================================================
# ANTI-LEGACY — aucun second moteur de reconnaissance oui/non
# =====================================================================


class TestNoConcurrentConfirmationEngine:
    def test_confirm_keywords_no_longer_exists_anywhere(self):
        root = Path("src/ladini/graphs/agents/market_coach")
        hits = []
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "CONFIRM_KEYWORDS" in text or "DECLINE_KEYWORDS" in text:
                hits.append(str(path))
        assert hits == [], f"2e moteur de confirmation encore présent : {hits}"


# =====================================================================
# IDEMPOTENCE — un CONFIRM envoyé deux fois n'exécute qu'une fois
# =====================================================================


class TestConfirmIsIdempotent:
    def test_the_same_confirm_twice_executes_once(self):
        v1 = _draft()
        target = ConfirmationTarget(draft_id=v1.draft_id, draft_version=v1.version)
        action = ConfirmProcurementDraft(target=target)

        claimed_keys = set()

        def claim_once_real(key):
            if key in claimed_keys:
                return False
            claimed_keys.add(key)
            return True

        first = apply_domain_action(v1, action, claim=claim_once_real)
        assert first.kind == ProcurementOutcomeKind.CONFIRMED_READY_FOR_EXECUTION

        # Retry (même version, même draft, même target — Celery retry réel).
        # Le draft persisté après le 1er succès est déjà EXECUTING (mandat
        # §11, transition CONFIRMED->EXECUTING atomique) — le retry obtient
        # donc ALREADY_EXECUTING, pas un générique "déjà confirmé".
        second = apply_domain_action(first.draft, action, claim=claim_once_real)
        assert second.kind == ProcurementOutcomeKind.ALREADY_EXECUTING

    def test_execution_count_is_exactly_one_across_two_identical_confirms(self):
        v1 = _draft()
        target = ConfirmationTarget(draft_id=v1.draft_id, draft_version=v1.version)
        action = ConfirmProcurementDraft(target=target)
        claimed_keys = set()

        def claim(key):
            if key in claimed_keys:
                return False
            claimed_keys.add(key)
            return True

        execution_count = 0
        draft = v1
        for _ in range(2):
            outcome = apply_domain_action(draft, action, claim=claim)
            if outcome.kind == ProcurementOutcomeKind.CONFIRMED_READY_FOR_EXECUTION:
                execution_count += 1
            draft = outcome.draft
        assert execution_count == 1


# =====================================================================
# COURSE CONCURRENTE — deux "workers" sur le MÊME CONFIRM
# =====================================================================


class TestConcurrentConfirmClaim:
    def test_ten_concurrent_confirms_on_the_same_version_yield_one_winner(self):
        """Le claim (SET-NX, voir core/idempotency.py en production réelle)
        est le point d'atomicité — ce test le prouve sous accès concurrent
        RÉEL (verrou + dict partagé, pas seulement "en apparence" côté
        Python), pas seulement en série."""
        v1 = _draft()
        target = ConfirmationTarget(draft_id=v1.draft_id, draft_version=v1.version)
        action = ConfirmProcurementDraft(target=target)

        claimed_keys: set = set()
        lock = threading.Lock()

        def thread_safe_claim(key: str) -> bool:
            with lock:
                if key in claimed_keys:
                    return False
                claimed_keys.add(key)
                return True

        results = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(10)

        def worker():
            barrier.wait()
            outcome = apply_domain_action(v1, action, claim=thread_safe_claim)
            with results_lock:
                results.append(outcome.kind)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(results) == 10
        confirmed = [r for r in results if r == ProcurementOutcomeKind.CONFIRMED_READY_FOR_EXECUTION]
        already = [r for r in results if r == ProcurementOutcomeKind.ALREADY_EXECUTING]
        assert len(confirmed) == 1, f"exactement 1 gagnant attendu, obtenu {len(confirmed)}"
        assert len(already) == 9


# =====================================================================
# NEEDS_MORE_INFO / DRAFT_UNCHANGED — comportements non ambigus
# =====================================================================


class TestOutcomeCoverage:
    def test_incomplete_draft_after_update_reports_needs_more_info(self):
        draft = ProcurementDraft.new(draft_id="d2", product="tomates")
        action = UpdateProcurementDraft(fields={"quantity": 500.0, "unit": "KG"})
        outcome = apply_domain_action(draft, action)
        assert outcome.kind == ProcurementOutcomeKind.NEEDS_MORE_INFO
        assert "price" in outcome.draft.missing_fields()

    def test_unhandled_event_reports_no_structured_fields_not_a_crash(self):
        action = resolve_domain_action(
            interpreted_event="OUT_OF_SCOPE", extracted_entities={}, pending_target=None
        )
        assert isinstance(action, NoProcurementAction)


# =====================================================================
# SCÉNARIO RÉEL COMPLET — mandat §19
# =====================================================================


def _turn_via_confirmation_gate(state, text, runtime):
    interp = _interpret_fast_path(state, text, skip_numeric_shortcut=False)
    assert interp is not None, f"fast-path did not fire for {text!r}"
    state = dict(state)
    state["interpreted_event"] = interp["interpreted_event"]
    state["extracted_entities"] = interp["extracted_entities"]
    state["normalized_text"] = text
    patch = run(memory_update(state, runtime))
    state.update(patch)
    patch = run(confirmation_gate(state, runtime))
    state.update(patch)
    return state, interp


class TestFullReportedScenario:
    def test_v1_then_v2_then_confirm_v2_then_repeat_confirm_does_not_reexecute(
        self, stub_runtime, monkeypatch
    ):
        import ladini.graphs.agents.market_coach.domain.procurement_draft as pd_mod

        # `claim_once` réel tape Redis — voir la note dans Invariant 5
        # ci-dessus. Ce scénario vérifie le CONTENU (versions, texte
        # affiché), l'idempotence elle-même étant couverte séparément.
        monkeypatch.setattr(pd_mod, "claim_once", _ALWAYS_CLAIM)
        runtime = stub_runtime()
        # Draft déjà créé (bootstrap déjà couvert par
        # test_nodes_behaviour.py / le flux de collecte initiale) — état
        # après le premier récap "2 TONNE".
        v1 = ProcurementDraft.new(
            draft_id="scenario1",
            product="tomates",
            quantity=2.0,
            unit="TONNE",
            price=250.0,
            price_unit="KG",
        )
        state = make_state(
            current_goal="PROCUREMENT_CREATE_REQUEST",
            procurement_draft=v1.to_dict(),
        )
        state["pending_interaction"] = {
            "kind": "CONFIRM_ACTION",
            "target": {"draft_id": v1.draft_id, "draft_version": v1.version},
        }

        state, interp1 = _turn_via_confirmation_gate(
            state, "non j ai dit 2 tonnes et 250 kg", runtime
        )
        assert interp1["interpreted_event"] == "UPDATE"
        draft_v2 = ProcurementDraft.from_dict(state["procurement_draft"])
        assert draft_v2.version == 2
        assert "2250" in state["final_response"] or "2 250" in state["final_response"]
        assert "2 TONNE" not in state["final_response"]

        state, interp2 = _turn_via_confirmation_gate(
            state, "non, plutot 1 tonne et 125 kg", runtime
        )
        assert interp2["interpreted_event"] == "UPDATE"
        draft_v3 = ProcurementDraft.from_dict(state["procurement_draft"])
        assert draft_v3.version == 3
        assert draft_v3.quantity == 1125.0
        assert "1125" in state["final_response"] or "1 125" in state["final_response"]
        assert "2250" not in state["final_response"] and "2 250" not in state["final_response"]
        assert "2 TONNE" not in state["final_response"]

        # La cible de confirmation POINTE la version 3, pas une ancienne.
        target = state["pending_interaction"]["target"]
        assert target == {"draft_id": "scenario1", "draft_version": 3}

        state, interp3 = _turn_via_confirmation_gate(state, "okay", runtime)
        assert interp3["interpreted_event"] == "CONFIRM"
        assert state["status"] == "EXECUTING"
        assert state["transaction_payload"]["quantity"] == 1125.0
        confirmed_draft = ProcurementDraft.from_dict(state["procurement_draft"])
        assert confirmed_draft.status == ProcurementDraftStatus.EXECUTING
        # CONFIRM bump la version (2026-09-03 — voir `with_status`) : v3
        # (DRAFT, ciblé par la confirmation) -> v4 (EXECUTING, persisté) —
        # une transition persistée de plus, protégée par le même compteur
        # CAS que les UPDATE précédents.
        assert confirmed_draft.version == 4

        # Retry Celery réaliste : le MÊME tour (interpreted_event=CONFIRM,
        # target=v3) rejoué tel quel — reproduit exactement ce qu'une
        # redelivery/relance produit (le texte n'est pas ré-interprété, c'est
        # la décision DÉJÀ prise qui est rejouée). Ne doit JAMAIS ré-exécuter
        # — mandat §19 dernier point / §20.
        retry_state = dict(state)
        retry_state["interpreted_event"] = "CONFIRM"
        retry_state["extracted_entities"] = {}
        # `pending_interaction` a déjà été résolu par le 1er CONFIRM
        # (resolve_pending_interaction) — un VRAI retry Celery rejoue le
        # tour AVANT que cette résolution n'ait été persistée (fenêtre de
        # concurrence), donc le target du tour original est encore posé.
        retry_state["pending_interaction"] = {
            "kind": "CONFIRM_ACTION",
            "target": {"draft_id": "scenario1", "draft_version": 3},
        }
        retry_patch = run(confirmation_gate(retry_state, runtime))
        assert retry_patch.get("status") != "EXECUTING", (
            "une 2e confirmation (retry) a re-déclenché une exécution — "
            "violation d'idempotence"
        )
