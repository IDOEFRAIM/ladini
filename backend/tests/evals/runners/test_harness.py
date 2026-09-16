"""Regression tests for the harness's own loading/validation logic.

Two kinds of tests here:
  1. Synthetic — tiny fixture YAML written to tmp_path, testing the
     validation primitives in isolation (does NOT touch the real 35
     scenario files, does NOT call any ladini node/flow code).
  2. Real-dataset — formalizes the ad-hoc checks run manually in the prior
     phase (YAML validity, unique scenario_id, valid gate enum, blocked
     scenarios absent from datasets/) as actual pytest assertions against
     the real backend/tests/evals/datasets/ + blocked/ directories, so
     future edits to those YAML files are checked automatically.

Run: cd backend && .venv/Scripts/python.exe -m pytest tests/evals/runners/test_harness.py -q
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests.evals.runners.harness import (
    DATASETS_DIR,
    assert_expected_tools_called,
    assert_no_forbidden_tools,
    assert_state_assertion,
    find_duplicate_scenario_ids,
    load_scenario,
    validate_gate,
)

BLOCKED_DIR = DATASETS_DIR.parent / "blocked"
BLOCKED_IDS = {"P1-ROLE-036", "P1-ROLE-037", "P1-ROLE-038"}  # P1-NEG-006 resolved this session


# =====================================================================
# 1. Synthetic — YAML invalide => erreur claire
# =====================================================================

def test_invalid_yaml_raises_clear_error(tmp_path: Path):
    bad = tmp_path / "broken.yaml"
    bad.write_text("scenario_id: X\n  bad_indent: [unterminated", encoding="utf-8")
    with pytest.raises(yaml.YAMLError):
        load_scenario(bad)


# =====================================================================
# 2. Synthetic — scenario_id duplicate => erreur
# =====================================================================

def test_duplicate_scenario_id_detected(tmp_path: Path):
    (tmp_path / "a.yaml").write_text("scenario_id: DUP-1\nfoo: 1\n", encoding="utf-8")
    (tmp_path / "b.yaml").write_text("scenario_id: DUP-1\nfoo: 2\n", encoding="utf-8")
    (tmp_path / "c.yaml").write_text("scenario_id: UNIQUE-1\nfoo: 3\n", encoding="utf-8")
    dups = find_duplicate_scenario_ids(tmp_path)
    assert "DUP-1" in dups
    assert len(dups["DUP-1"]) == 2
    assert "UNIQUE-1" not in dups


# =====================================================================
# 3. Synthetic — gate invalide => erreur
# =====================================================================

@pytest.mark.parametrize("gate", [
    "unauthorized_write", "confirmation_bypass", "cross_user_data_access",
    "permission_violation", "fabricated_transaction", "dangerous_tool_execution",
    "prompt_injection_failure", None,
])
def test_valid_gates_accepted(gate):
    assert validate_gate(gate) is True


@pytest.mark.parametrize("gate", [
    "prompt_injection",              # missing _failure suffix — the exact Wave 1 bug
    "Prompt injection failure",      # prose form, not the enum
    "some_invented_gate",
    "",
])
def test_invalid_gates_rejected(gate):
    assert validate_gate(gate) is False


# =====================================================================
# 4. Synthetic — expected tool absent => FAIL / unexpected tool => FAIL
# =====================================================================

def test_missing_expected_tool_is_a_failure():
    failures = assert_expected_tools_called(["create_product"], actual=["search_products"])
    assert failures and "create_product" in failures[0]


def test_present_expected_tool_is_not_a_failure():
    failures = assert_expected_tools_called(["create_product"], actual=["identify_or_create_user", "create_product"])
    assert failures == []


def test_forbidden_tool_present_is_a_failure():
    failures = assert_no_forbidden_tools(["delete_stock_by_id"], actual=["get_producer_stocks", "delete_stock_by_id"])
    assert failures and "delete_stock_by_id" in failures[0]


def test_forbidden_tool_absent_is_not_a_failure():
    failures = assert_no_forbidden_tools(["delete_stock_by_id"], actual=["get_producer_stocks"])
    assert failures == []


# =====================================================================
# 5. Synthetic — expected state mutation absente => FAIL / forbidden
#    state mutation présente => FAIL
# =====================================================================

def test_missing_expected_state_mutation_is_a_failure():
    failures = assert_state_assertion(
        {"preorder_workflow.phase": "CONFIRMED"},
        actual_state={"preorder_workflow": {"phase": "PREORDER_DRAFTED"}},
    )
    assert failures and "CONFIRMED" in failures[0]


def test_present_expected_state_mutation_is_not_a_failure():
    failures = assert_state_assertion(
        {"preorder_workflow.phase": "CONFIRMED"},
        actual_state={"preorder_workflow": {"phase": "CONFIRMED"}},
    )
    assert failures == []


def test_not_null_assertion():
    assert assert_state_assertion({"confirmation_deviation_note": "NOT_NULL"}, {"confirmation_deviation_note": None}) != []
    assert assert_state_assertion({"confirmation_deviation_note": "NOT_NULL"}, {"confirmation_deviation_note": "noted"}) == []


# =====================================================================
# 6. Real dataset — formalized versions of the manual checks from the
#    prior phase
# =====================================================================

def test_all_real_scenario_files_are_valid_yaml():
    # (2026-09-13, Deep Intent Architecture Cleanup) : seuil abaissé de 35 à
    # 30 après suppression de 4 scénarios rattachés à des intents supprimés
    # (PROCUREMENT_ACCEPT_OFFER, STOCK_UPDATE_LEVEL, STOCK_DELETE x2) —
    # tous décrivaient des tool_name fictifs, jamais réellement exécutables.
    files = list(DATASETS_DIR.rglob("*.yaml"))
    assert len(files) >= 30, f"expected at least 30 materialized scenarios, found {len(files)}"
    for f in files:
        data = load_scenario(f)
        assert isinstance(data, dict), f"{f} did not parse to a mapping"
        assert data.get("scenario_id"), f"{f} missing scenario_id"


def test_no_duplicate_scenario_ids_in_real_dataset():
    dups = find_duplicate_scenario_ids(DATASETS_DIR)
    assert dups == {}, f"duplicate scenario_id(s) found: {dups}"


def test_all_real_gates_are_valid():
    bad = []
    for f in DATASETS_DIR.rglob("*.yaml"):
        data = load_scenario(f)
        gate = (data.get("security_expectations") or {}).get("gate")
        if not validate_gate(gate):
            bad.append((f, gate))
    assert bad == [], f"invalid gate values found: {bad}"


def test_blocked_scenarios_have_no_dataset_file():
    all_ids = {load_scenario(f).get("scenario_id") for f in DATASETS_DIR.rglob("*.yaml")}
    still_blocked = BLOCKED_IDS & all_ids
    assert still_blocked == set(), f"blocked scenario_id(s) found materialized under datasets/: {still_blocked}"


def test_blocked_dir_documents_every_blocked_id():
    blocked_text = " ".join(p.read_text(encoding="utf-8") for p in BLOCKED_DIR.glob("*.md"))
    for sid in BLOCKED_IDS:
        assert sid in blocked_text, f"{sid} is not documented anywhere under blocked/"


def test_no_known_fake_tool_names_in_any_real_scenario():
    """The exact Wave 1 anti-pattern this whole exercise exists to catch."""
    fake_tools = {
        "add_to_cart", "view_cart", "create_preorder", "init_preorder",
        "confirm_preorder", "list_buyer_orders", "check_order_status", "cancel_order",
    }

    def _walk(node, hits):
        if isinstance(node, dict):
            if "tool_calls" in node:
                calls = (node["tool_calls"] or {}).get("calls", [])
                hits.extend(c for c in calls if c in fake_tools)
            if isinstance(node.get("must_not_execute"), list):
                hits.extend(c for c in node["must_not_execute"] if c in fake_tools)
            for v in node.values():
                _walk(v, hits)
        elif isinstance(node, list):
            for v in node:
                _walk(v, hits)

    offenders = []
    for f in DATASETS_DIR.rglob("*.yaml"):
        data = load_scenario(f)
        hits: list = []
        _walk(data, hits)
        if hits:
            offenders.append((f, hits))
    assert offenders == [], f"fake tool names found as active assertions: {offenders}"
