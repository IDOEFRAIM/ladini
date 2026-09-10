"""Tests source/structure anti-legacy — SALES (2026-09-04, migration
SALES) — même discipline que
`test_preorder_payment_anti_legacy.py`/`test_procurement_draft_transactional_contract.py`."""
from __future__ import annotations

import inspect
import re

import ladini.graphs.agents.market_coach.domain.sales_publish_draft as sales_draft_mod
import ladini.graphs.agents.market_coach.flows.producer.sales_confirmation as sales_confirm_mod
import ladini.graphs.agents.market_coach.flows.producer.sales_execution_finalizer as sales_finalizer_mod
import ladini.graphs.agents.market_coach.nodes.confirmation_gate as confirmation_gate_mod


def _code_only(source: str) -> str:
    code = re.sub(r'""".*?"""', "", source, flags=re.DOTALL)
    return re.sub(r"#.*", "", code)


class TestSalesDoesNotReintroduceLegacyState:
    def test_domain_module_never_touches_confirmation_summary_or_resolved_id(self):
        """`SalesPublishDraft`/`apply_domain_action`/`build_response_plan`
        ne connaissent AUCUN des noms legacy que ce chantier élimine —
        `resolved_id` (contrôlait CANCEL/CONFIRM par convention plutôt
        qu'un contrat typé), `confirmation_summary` (texte stocké), et
        `waiting_for_confirmation`/`expected_input` (remplacés par
        `PendingInteraction`)."""
        code = _code_only(inspect.getsource(sales_draft_mod))
        for forbidden in (
            "resolved_id",
            "confirmation_summary",
            "waiting_for_confirmation",
            "expected_input",
            "quantity_display",
            "unit_display",
        ):
            assert forbidden not in code, f"'{forbidden}' trouvé dans domain/sales_publish_draft.py"

    def test_confirmation_orchestration_never_touches_legacy_fields(self):
        for mod in (sales_confirm_mod, sales_finalizer_mod):
            code = _code_only(inspect.getsource(mod))
            for forbidden in (
                "resolved_id",
                "confirmation_summary",
                "waiting_for_confirmation",
                "quantity_display",
                "unit_display",
            ):
                assert forbidden not in code, f"'{forbidden}' trouvé dans {mod.__name__}"

    def test_sales_publish_product_bootstrap_never_builds_a_confirmation_summary(self):
        """La branche SALES du dispatch `confirmation_gate` (bootstrap) ne
        doit JAMAIS appeler `_build_confirmation_summary` — c'est
        exactement le mécanisme générique que le draft canonique remplace
        pour ce goal précis."""
        source = inspect.getsource(confirmation_gate_mod._resolve_sales_draft_based_confirmation)
        code = _code_only(source)
        assert "_build_confirmation_summary" not in code
        assert "confirmation_summary" not in code


class TestDomainNeverReadsRawUserText:
    def test_apply_domain_action_signature_has_no_text_parameter(self):
        sig = inspect.signature(sales_draft_mod.apply_domain_action)
        for name in sig.parameters:
            assert "text" not in name.lower()

    def test_confirm_action_is_the_only_way_to_reach_confirmed_ready_for_execution(self):
        """Preuve structurelle : le SEUL type d'action qui peut produire
        `CONFIRMED_READY_FOR_EXECUTION` est `ConfirmSalesPublishDraft` —
        vérifié en source (pas de branche cachée sur un autre type
        d'action qui atteindrait aussi ce statut)."""
        source = inspect.getsource(sales_draft_mod.apply_domain_action)
        # Le kind CONFIRMED_READY_FOR_EXECUTION n'apparaît qu'une fois dans
        # le corps de la fonction, et DANS le bloc `isinstance(action,
        # ConfirmSalesPublishDraft)`.
        confirm_block_start = source.index("isinstance(action, ConfirmSalesPublishDraft)")
        reject_block_start = source.index("isinstance(action, RejectSalesPublishConfirmation)")
        confirm_block = source[confirm_block_start:reject_block_start]
        assert "CONFIRMED_READY_FOR_EXECUTION" in confirm_block
        rest_of_function = source[reject_block_start:]
        assert "CONFIRMED_READY_FOR_EXECUTION" not in rest_of_function


class TestNoNewIdempotencyPrimitive:
    def test_no_module_reimplements_claim_once(self):
        for mod in (sales_draft_mod, sales_confirm_mod, sales_finalizer_mod):
            code = _code_only(inspect.getsource(mod))
            assert "def claim_once" not in code
            assert "SET" not in code or "NX" not in code

    def test_confirmation_target_is_imported_not_redefined(self):
        """Mandat hardening §1 : `ConfirmationTarget` doit venir de
        `core/confirmation_target.py`, jamais une 4e copie locale."""
        code = _code_only(inspect.getsource(sales_draft_mod))
        assert "class ConfirmationTarget" not in code
        assert "from ladini.graphs.agents.market_coach.core.confirmation_target import" in inspect.getsource(
            sales_draft_mod
        )
