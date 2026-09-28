"""Garde CI permanente (Phase 2 hardening, commit 7, mandat §3) : TOUT `ENTER_FIELD` créé avec
un nom de champ LITTÉRAL dans le code doit correspondre à un champ CONNU — un slot scalaire
générique (`core/slots.py`), une clarification STRUCTURED ou un mini-flow dédié
(`core/field_registry.py::FIELD_REGISTRY`). Détecte : une typo de field, un nouveau field créé
sans être enregistré, un field retiré du registre mais encore créé quelque part.

Portée délibérément limitée aux littéraux (`field_name="xyz"`) — un `field_name=variable`
(valeur dynamique, ex: `missing[0]` dérivé de `RecurringNeedDraft.missing_fields()`) ne peut pas
être résolu par une analyse AST sans dataflow complet ("ne construis pas un framework
surdimensionné", mandat §2). Ces sources dynamiques sont couvertes SÉPARÉMENT par
`test_dynamic_missing_fields_are_all_known_fields` ci-dessous, qui vérifie directement, pour
CHAQUE domaine, que l'ensemble FERMÉ des valeurs que `missing_fields()` peut produire est un
sous-ensemble des champs connus — la même garantie, obtenue à la source plutôt que par AST."""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

from ladini.graphs.agents.market_coach import core as _core_pkg
from ladini.graphs.agents.market_coach.core.field_registry import (
    FIELD_REGISTRY,
    STRUCTURED_FIELDS,
    is_known_field,
)
from ladini.graphs.agents.market_coach.flows.buyer.recurring_need import (
    _STRUCTURED_FIELD_RESOLVERS,
)

_PACKAGE_ROOT = Path(inspect.getfile(_core_pkg)).parent.parent


def _iter_enter_field_literals():
    """(file, lineno, literal_field_name) pour chaque site qui crée un `ENTER_FIELD` avec un
    `field_name=<littéral>` — scan de TOUT le package `market_coach`."""
    for path in _PACKAGE_ROOT.rglob("*.py"):
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            func_name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if func_name not in ("set_pending_interaction", "replace_pending_interaction"):
                continue
            is_enter_field = any(
                isinstance(arg, ast.Attribute) and arg.attr == "ENTER_FIELD" for arg in node.args
            )
            if not is_enter_field:
                continue
            for kw in node.keywords:
                if kw.arg == "field_name" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    yield path.relative_to(_PACKAGE_ROOT), node.lineno, kw.value.value


class TestEveryLiteralEnterFieldIsKnown:
    def test_registry_is_not_empty(self):
        assert len(FIELD_REGISTRY) >= 2

    def test_every_literal_field_name_created_in_the_codebase_is_known(self):
        sites = list(_iter_enter_field_literals())
        assert sites, "le scan AST n'a trouvé aucun site — vérifier qu'il n'est pas cassé silencieusement"
        unknown = [(str(f), n, field) for f, n, field in sites if not is_known_field(field)]
        assert unknown == [], (
            "ENTER_FIELD créé avec un field_name inconnu de core/field_registry.py ET de "
            f"core/slots.py (typo, ou nouveau champ jamais enregistré) : {unknown}"
        )


class TestStructuredFieldsHaveADedicatedResolver:
    """Un champ déclaré STRUCTURED sans résolveur câblé referait exactement le bug H2 : créé,
    mais toujours pas consommable malgré la déclaration. Verrouille
    `core/field_registry.py::STRUCTURED_FIELDS` contre
    `flows/buyer/recurring_need.py::_STRUCTURED_FIELD_RESOLVERS` (aujourd'hui l'unique
    propriétaire de champs STRUCTURED — un futur domaine qui en ajoute doit exposer sa propre
    table de dispatch et l'ajouter ici)."""

    def test_every_structured_field_has_a_resolver_in_recurring_need(self):
        # Deux domaines propriétaires de champs STRUCTURED, chacun avec SA table de dispatch :
        # l'approvisionnement récurrent (`_STRUCTURED_FIELD_RESOLVERS`) et le vertical slice
        # commercial de SALES_PUBLISH_PRODUCT (`COMMERCIAL_STRUCTURED_FIELDS`, Phase B1).
        from ladini.domain.commercial_offer_flow import COMMERCIAL_STRUCTURED_FIELDS

        assert set(_STRUCTURED_FIELD_RESOLVERS) | set(COMMERCIAL_STRUCTURED_FIELDS) == STRUCTURED_FIELDS
        assert not (set(_STRUCTURED_FIELD_RESOLVERS) & set(COMMERCIAL_STRUCTURED_FIELDS))

    def test_no_resolver_is_registered_for_an_undeclared_field(self):
        for field in _STRUCTURED_FIELD_RESOLVERS:
            assert field in STRUCTURED_FIELDS, (
                f"'{field}' a un résolveur mais n'est pas déclaré STRUCTURED dans "
                "core/field_registry.py"
            )


class TestDynamicMissingFieldsAreAllKnownFields:
    """Complète le scan AST (portée aux littéraux) pour les champs `pending_field=missing[0]`
    dérivés de `<Draft>.missing_fields()` — un ensemble FERMÉ par domaine, entièrement
    énumérable par construction (chaque domaine liste ses propres champs requis)."""

    def test_recurring_need_missing_fields_are_all_known(self):
        from ladini.graphs.agents.market_coach.domain.recurring_need_draft import (
            RecurringNeedDraft,
        )

        # Toutes les combinaisons plausibles de champs manquants, y compris weekly_days
        # (recurrence_type=WEEKLY_DAYS) — voir TestP_WeeklyDays dans les tests de
        # characterization pour la reproduction bout-en-bout de ce gap précis.
        for recurrence_type in ("WEEKLY", "DAILY", "WEEKLY_DAYS", "MONTHLY", "ONE_OFF", None):
            draft = RecurringNeedDraft.new(draft_id="registry-check", recurrence_type=recurrence_type)
            for field in draft.missing_fields():
                assert is_known_field(field), f"missing_fields() a produit '{field}', inconnu du registre"

    def test_procurement_missing_fields_are_all_known(self):
        from ladini.graphs.agents.market_coach.domain.procurement_draft import (
            ProcurementDraft,
        )

        draft = ProcurementDraft.new(draft_id="registry-check")
        for field in draft.missing_fields():
            assert is_known_field(field), f"missing_fields() a produit '{field}', inconnu du registre"

    def test_sales_publish_missing_fields_are_all_known(self):
        from ladini.graphs.agents.market_coach.domain.sales_publish_draft import (
            SalesPublishDraft,
        )

        draft = SalesPublishDraft.new(draft_id="registry-check")
        for field in draft.missing_fields():
            assert is_known_field(field), f"missing_fields() a produit '{field}', inconnu du registre"
