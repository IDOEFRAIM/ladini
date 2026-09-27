"""`nodes/memory.py::_resolve_unit_value` — the slot-answer validator used
whenever a buyer/producer answers "quelle unité ?" during a conversation.

Analytics Phase C (2026-09-27) regression: `_PRIMARY_CANONICAL_UNITS` was
missing "LITRE" since the unit's addition to the canonical registry
(2026-08-29) — a real, live bug directly relevant to analytics' canonical
unit model (a dairy producer answering "litre" got silently rejected here).
Fixed alongside the `_CANONICAL_UNIT_MAP` gap in
`test_canonical_unit_label.py::TestLitreIsARealCanonicalEntryNotAnAccidentalFallthrough`.

Deliberately a minimal, targeted fix — not a rewrite of the unit engine.
"""
from __future__ import annotations

from ladini.graphs.agents.market_coach.nodes.memory import _resolve_unit_value


class TestResolveUnitValueAcceptsLitre:
    def test_litre_and_common_spellings_are_accepted(self):
        for raw in ("litre", "LITRE", "litres", "l", "L"):
            assert _resolve_unit_value(raw) == "LITRE", raw

    def test_other_canonical_units_are_unaffected(self):
        assert _resolve_unit_value("kg") == "KG"
        assert _resolve_unit_value("tete") == "TETE"
        assert _resolve_unit_value("sac") == "SAC"

    def test_a_truly_unrecognized_unit_is_still_rejected(self):
        assert _resolve_unit_value("bidon") is None

    def test_empty_or_missing_value_is_none(self):
        assert _resolve_unit_value("") is None
        assert _resolve_unit_value(None) is None
