"""`.github/workflows/cicd.yml` / `release.yml` — non-régression du gate
CI -> release (2026-09-18, incident premier déploiement Hetzner, §predeploy).

`scripts/predeploy_check.sh` n'exécute PLUS la suite de tests applicatifs
(architecture métier, PII redaction, idempotency, métriques worker) — le
node de prod n'a ni Poetry ni les dépendances backend, et ne doit jamais les
installer juste pour ça (voir l'en-tête de predeploy_check.sh). La garantie
"une release ne peut pas être publiée si un test applicatif échoue" repose
donc ENTIÈREMENT sur ces deux workflows GitHub Actions : si l'un d'eux
régresse silencieusement (ex: quelqu'un remplace `pytest tests` par un
sous-ensemble, ou retire le `workflow_run`/la vérification de `conclusion`),
plus rien ne bloque une image cassée avant GHCR — et predeploy_check.sh, sur
le node, ne peut structurellement plus le détecter. Ce test verrouille cette
chaîne de garanties au niveau des fichiers YAML eux-mêmes."""
from __future__ import annotations

from pathlib import Path

import yaml

_WORKFLOWS_DIR = Path(__file__).resolve().parents[3] / ".github" / "workflows"


def _load(name: str) -> dict:
    return yaml.safe_load((_WORKFLOWS_DIR / name).read_text(encoding="utf-8"))


def _on(workflow: dict) -> dict:
    """`on:` YAML 1.1 gotcha : PyYAML (comme tout loader YAML 1.1 strict)
    interprète la clé nue `on` comme le booléen `True`, pas la chaîne
    `"on"` — `yaml.safe_load` d'un workflow GitHub Actions donne donc
    `workflow[True]`, jamais `workflow["on"]`. Centralisé ici pour ne pas
    piéger un futur test qui referait l'erreur `workflow["on"]` (KeyError
    silencieux si absent, mais surtout confusion pérenne)."""
    return workflow.get("on", workflow.get(True))


class TestCicdRunsTheFullApplicationTestSuite:
    def test_the_test_job_runs_pytest_against_the_whole_tests_directory(self):
        """Doit rester `pytest tests` (le dossier ENTIER) — pas une liste de
        fichiers explicite, qui pourrait dériver silencieusement d'un test
        nouvellement ajouté (ex: un futur test architecture/PII/idempotency)
        sans jamais être exécuté ni en CI ni sur le node."""
        cicd = _load("cicd.yml")
        steps = cicd["jobs"]["test"]["steps"]
        run_steps = [s["run"] for s in steps if "run" in s]
        pytest_steps = [r for r in run_steps if "pytest" in r]
        assert pytest_steps, "aucune étape n'invoque pytest dans le job 'test'"
        assert any(
            "pytest tests" in r for r in pytest_steps
        ), f"pytest doit cibler le dossier 'tests' entier, trouvé : {pytest_steps!r}"

    def test_docker_build_job_depends_on_test_and_migration_safety(self):
        """`docker-build` doit rester gated par `needs: [test, ...]` — sans
        ça, la validation des images/compose tournerait même si les tests
        échouent (régression réelle corrigée le 2026-09-17, voir le
        commentaire de ce job dans cicd.yml)."""
        cicd = _load("cicd.yml")
        needs = cicd["jobs"]["docker-build"].get("needs") or []
        assert "test" in needs
        assert "migration-safety" in needs


class TestReleaseIsGatedOnCiSuccess:
    def test_release_triggers_on_ci_workflow_run(self):
        release = _load("release.yml")
        on = _on(release)
        assert "workflow_run" in on, "release.yml doit rester déclenché par workflow_run"
        assert "CI" in (on["workflow_run"].get("workflows") or []), (
            "release.yml doit suivre le workflow nommé 'CI' (voir `name:` dans cicd.yml)"
        )

    def test_build_push_job_refuses_to_run_unless_ci_succeeded(self):
        """Non-régression de l'incident documenté en tête de release.yml
        (2026-09-17) : `workflow_run` se déclenche même quand CI a ÉCHOUÉ ou
        a été annulée — c'est au job lui-même de vérifier `conclusion`.
        Sans ce `if:`, un CI rouge n'empêchait rien de partir sur GHCR."""
        release = _load("release.yml")
        condition = release["jobs"]["build-push"].get("if", "")
        assert "workflow_run.conclusion" in condition
        assert "success" in condition

    def test_cicd_workflow_is_named_ci_matching_release_yaml_reference(self):
        """`release.yml` référence le workflow par son `name:` ("CI"), pas
        son nom de fichier — si `cicd.yml` renommait son `name:`, le
        `workflow_run` de release.yml ne se déclencherait plus JAMAIS, sans
        aucune erreur visible (échec silencieux : plus aucune release ne
        sortirait, ou pire, l'ancien comportement `push:` en parallèle
        pourrait être réintroduit sans que ce couplage ne le remarque)."""
        cicd = _load("cicd.yml")
        release = _load("release.yml")
        assert cicd["name"] == "CI"
        assert "CI" in _on(release)["workflow_run"]["workflows"]
