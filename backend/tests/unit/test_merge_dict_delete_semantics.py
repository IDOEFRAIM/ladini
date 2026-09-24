"""Sémantique formelle d'un patch sur un canal `merge_dict` (agents/reducers.py).

    ABSENT  -> inchangé
    valeur  -> remplacée (dict imbriqué fusionné, liste remplacée)
    None    -> clé conservée, valeur None (« connu, vide » ; lu comme absent)
    DELETE  -> clé supprimée
    __reset__ -> canal remplacé intégralement
"""
from __future__ import annotations

import json
import typing

import pytest

from ladini.agents.reducers import (
    DELETE,
    clear_keys,
    mark_deleted,
    merge_dict,
    replace_value,
    strip_deleted,
)
from ladini.graphs.agents.market_coach.core.state import MarketAgentState

OLD = {"a": 1, "b": {"x": 1, "y": 2}, "items": [1, 2], "keep": "k"}


def test_absent_key_is_unchanged():
    assert merge_dict(OLD, {"a": 2})["keep"] == "k"


def test_value_replaces_and_nested_dicts_merge():
    merged = merge_dict(OLD, {"a": 2, "b": {"x": 9}})
    assert merged["a"] == 2 and merged["b"] == {"x": 9, "y": 2}


def test_list_is_replaced_never_concatenated():
    assert merge_dict(OLD, {"items": [3]})["items"] == [3]
    assert merge_dict(OLD, {"items": []})["items"] == []


def test_none_keeps_the_key_with_an_empty_value():
    merged = merge_dict(OLD, {"a": None})
    assert "a" in merged and merged["a"] is None


def test_delete_removes_the_key():
    merged = merge_dict(OLD, {"a": DELETE})
    assert "a" not in merged and merged["keep"] == "k"


def test_delete_removes_a_nested_key():
    merged = merge_dict(OLD, {"b": {"x": DELETE}})
    assert merged["b"] == {"y": 2}


def test_delete_of_a_missing_key_is_a_noop():
    assert merge_dict(OLD, {"zzz": DELETE}) == OLD


def test_removing_a_key_from_the_patch_does_not_delete_it():
    """Le piège à l'origine du bug B1 — documenté par un test, pour toujours."""
    patch = dict(OLD)
    patch.pop("a")
    assert merge_dict(OLD, patch)["a"] == 1


def test_reset_replaces_the_channel_and_never_stores_delete_markers():
    merged = merge_dict(OLD, {"__reset__": True, "a": DELETE, "z": 1})
    assert merged == {"z": 1}


def test_delete_markers_never_reach_the_stored_value():
    merged = merge_dict({}, {"new": {"p": DELETE, "q": 1}})
    assert merged == {"new": {"q": 1}}
    assert DELETE not in json.dumps(merged)


def test_clear_keys_and_mark_deleted_build_delete_patches():
    assert clear_keys("a", "b") == {"a": DELETE, "b": DELETE}
    patch = {"a": 1, "c": 3}
    assert mark_deleted(patch, "a") is patch and patch == {"a": DELETE, "c": 3}
    assert merge_dict(OLD, clear_keys("a", "keep")) == {"b": {"x": 1, "y": 2}, "items": [1, 2]}


def test_strip_deleted_hides_markers_from_local_readers():
    assert strip_deleted({"a": DELETE, "b": {"c": DELETE, "d": 1}}) == {"b": {"d": 1}}


def test_replace_value_channels_treat_delete_as_clear():
    assert replace_value("old", DELETE) is None


def test_delete_marker_survives_a_checkpoint_json_round_trip():
    """LangGraph sérialise les écritures en attente : le marqueur doit rester égal à lui-même."""
    assert json.loads(json.dumps({"k": DELETE}))["k"] == DELETE


@pytest.mark.parametrize(
    "channel",
    sorted(
        name
        for name, ann in typing.get_type_hints(MarketAgentState, include_extras=True).items()
        if merge_dict in (getattr(ann, "__metadata__", ()) or ())
    ),
)
def test_every_merge_channel_honours_the_four_semantics(channel):
    reducer = typing.get_type_hints(MarketAgentState, include_extras=True)[channel].__metadata__[0]
    old = {"gone": 1, "empty": 2, "kept": 3}
    merged = reducer(old, {"gone": DELETE, "empty": None, "new": 4})
    assert merged == {"empty": None, "kept": 3, "new": 4}
