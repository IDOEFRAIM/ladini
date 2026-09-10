"""DIRECTION DE DÉPENDANCE — le domaine partagé ne vit plus dans `graphs`
(Phase 9, 2026-09-05).

## Le problème corrigé

Des règles métier utilisées par la couche transactionnelle vivaient dans
le paquet de l'orchestration conversationnelle :

```
services/database/{buyer,producer,product,escrow}.py
        ↓  (inversion)
graphs/agents/market_coach/domain/pricing_tiers.py
graphs/agents/market_coach/domain/order_policy.py
graphs/agents/market_coach/services/domain/quantity_unit.py
```

Ces trois modules sont PURS (aucune dépendance à LangGraph, au
`PendingInteraction`, au `ResponsePlan`, au LLM, au routage ni au rendu)
et sont désormais sous `ladini.domain`, accessible aux deux couches :

```
        ladini.domain   (règles métier partagées)
           ↙            ↘
     services/          graphs/
```

## Ce que ces tests NE prétendent pas

Les trois modules `*_draft` restent volontairement dans `graphs` : ils
définissent leur propre `ResponsePlan` et importent
`core/confirmation_target` + `utils` — ce sont des machines à états
CONVERSATIONNELLES, pas du domaine partagé. Les déplacer entraînerait
l'orchestration avec elles (voir le rapport de phase §5)."""
from __future__ import annotations

import ast
import os

import pytest

SRC = os.path.join("src", "ladini")

#: Modules extraits en Phase 9 — plus aucune couche basse ne doit les
#: importer depuis `graphs`.
SHARED_DOMAIN_MODULES = ("pricing_tiers", "order_policy", "quantity_unit")

#: Couches situées SOUS l'orchestration conversationnelle.
LOWER_LAYERS = (
    "services/",
    "core/",
    "workers/",
    "infrastructure/",
    "protocols/",
    "domain/",
    "agents/",
    "api/",
)


def _iter_python_files(prefixes):
    for root, _, files in os.walk(SRC):
        for f in files:
            if not f.endswith(".py"):
                continue
            path = os.path.join(root, f)
            rel = path.replace(os.sep, "/").split(SRC + "/")[-1]
            if rel.startswith(prefixes):
                yield path, rel


def _imported_modules(path):
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
    except (OSError, SyntaxError, UnicodeDecodeError):
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            yield node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name


class TestLowerLayersDoNotImportSharedRulesFromGraphs:
    def test_no_lower_layer_imports_a_shared_module_from_graphs(self):
        offenders = []
        for path, rel in _iter_python_files(LOWER_LAYERS):
            for mod in _imported_modules(path):
                if "graphs" not in mod:
                    continue
                if any(mod.endswith("." + m) for m in SHARED_DOMAIN_MODULES):
                    offenders.append(f"{rel} → {mod}")
        assert not offenders, (
            "inversion de dépendance réintroduite (règle métier partagée "
            f"importée depuis graphs) : {offenders}"
        )

    @pytest.mark.parametrize("module", SHARED_DOMAIN_MODULES)
    def test_shared_module_lives_in_the_neutral_package(self, module):
        assert os.path.exists(os.path.join(SRC, "domain", f"{module}.py"))

    @pytest.mark.parametrize("module", SHARED_DOMAIN_MODULES)
    def test_no_duplicate_implementation_remains_under_graphs(self, module):
        """§19 : une seule implémentation. L'ancien emplacement ne doit pas
        contenir de copie (ni de shim de compatibilité oublié)."""
        stale = [
            rel
            for _, rel in _iter_python_files(("graphs/",))
            if rel.endswith(f"/{module}.py")
        ]
        assert not stale, f"implémentation dupliquée sous graphs : {stale}"


class TestSharedDomainStaysIndependent:
    """Le domaine partagé ne doit dépendre d'aucune couche technique :
    c'est ce qui le rend importable des deux côtés sans créer de cycle."""

    FORBIDDEN = ("graphs", "api", "workers", "infrastructure", "protocols")

    @pytest.mark.parametrize("module", SHARED_DOMAIN_MODULES)
    def test_module_does_not_import_upper_or_technical_layers(self, module):
        path = os.path.join(SRC, "domain", f"{module}.py")
        bad = []
        for mod in _imported_modules(path):
            if not mod.startswith("ladini"):
                continue
            tail = mod[len("ladini."):]
            if tail.split(".")[0] in self.FORBIDDEN:
                bad.append(mod)
        assert not bad, f"{module} dépend d'une couche interdite : {bad}"


class TestConversationalDraftsDeliberatelyStay:
    """Contre-preuve : les modules `*_draft` ne sont PAS du domaine partagé.
    Ce test documente la décision et échouerait si quelqu'un les déplaçait
    sans traiter leur couplage conversationnel."""

    @pytest.mark.parametrize(
        "module", ("preorder_draft", "procurement_draft", "sales_publish_draft")
    )
    def test_draft_module_is_conversational_and_stays_in_graphs(self, module):
        path = os.path.join(
            SRC, "graphs", "agents", "market_coach", "domain", f"{module}.py"
        )
        assert os.path.exists(path), f"{module} a été déplacé sans décision"
        source = open(path, encoding="utf-8").read()
        # Preuve du couplage : plan de réponse + cible de confirmation.
        assert "ResponsePlan" in source
        assert "confirmation_target" in source
