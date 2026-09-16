"""`docker-compose.prod.yml` — invariants de déploiement multi-node (chantier
Hetzner scale-out, 2026-09-16, §44).

Preuves statiques (parsing YAML direct, sans résoudre les `${...}` — ils
restent de simples chaînes pour PyYAML, donc rien à interpoler ici) que la
structure du fichier réel respecte les invariants du chantier :

  - aucun Redis local en production (un Redis par node casserait
    idempotence/verrous/broker Celery dès 2 nodes) ;
  - REDIS_URL est une variable OBLIGATOIRE (`:?...`), jamais un défaut
    silencieux vers un hôte local ;
  - chaque service porte le PROFIL (rôle de node) attendu — `app` pour
    mcp/api/worker/pgbouncer (scalable), `scheduler` pour `beat` (doit rester
    un singleton cluster-wide, voir `scripts/validate_inventory.py`), `admin`
    pour `flower` ; `autoheal` reste SANS profil (toujours démarré, quel que
    soit le rôle du node).

Distinct de `docker compose config --quiet` (déjà fait en CI, `cicd.yml`,
prouve que le fichier est syntaxiquement valide et résolvable) — ce test
prouve la STRUCTURE (qui a quel rôle), pas seulement la validité YAML."""
from __future__ import annotations

from pathlib import Path

import yaml

_COMPOSE_PATH = Path(__file__).resolve().parents[3] / "docker-compose.prod.yml"


def _load_compose() -> dict:
    return yaml.safe_load(_COMPOSE_PATH.read_text(encoding="utf-8"))


class TestNoLocalRedisInProduction:
    def test_no_redis_service_defined(self):
        compose = _load_compose()
        assert "redis" not in (compose.get("services") or {}), (
            "docker-compose.prod.yml ne doit plus définir de service `redis` local — "
            "voir docker-compose.dev.yml pour le seul contexte légitime (dev/test)."
        )

    def test_redis_url_is_a_mandatory_variable_not_a_silent_local_default(self):
        compose = _load_compose()
        full_env = (compose.get("x-full-app-env") or {}).get("REDIS_URL", "")
        assert ":?" in full_env, (
            "REDIS_URL doit être une variable OBLIGATOIRE (`${REDIS_URL:?...}`), "
            "jamais un défaut implicite vers un Redis local."
        )
        assert "@redis:" not in full_env, (
            "REDIS_URL ne doit plus pointer vers un hostname Docker local `redis` par défaut."
        )


class TestNodeRoleProfiles:
    """Un `profiles:` par service = un rôle de node (§9/§19). `autoheal` est
    la seule exception délibérée (toujours actif, quel que soit le rôle)."""

    _EXPECTED = {
        "mcp": {"app"},
        "api": {"app"},
        "worker": {"app"},
        "pgbouncer": {"app"},
        "beat": {"scheduler"},
        "flower": {"admin"},
    }

    def test_each_service_carries_its_expected_role_profile(self):
        services = _load_compose()["services"]
        for name, expected_profiles in self._EXPECTED.items():
            assert name in services, f"service '{name}' introuvable"
            actual = set(services[name].get("profiles") or [])
            assert actual == expected_profiles, (
                f"service '{name}' : profils={actual!r}, attendu={expected_profiles!r}"
            )

    def test_autoheal_has_no_profile_always_runs(self):
        """`autoheal` doit démarrer sur TOUT node, quel que soit son rôle —
        un `profiles:` le limiterait à un seul rôle, cassant l'auto-healing
        sur les autres."""
        services = _load_compose()["services"]
        assert "profiles" not in services["autoheal"]

    def test_exactly_one_service_owns_the_scheduler_profile(self):
        """Miroir applicatif de `scripts/validate_inventory.py` (qui
        vérifie l'INVENTAIRE de nodes) : côté Compose, un seul SERVICE doit
        porter le rôle `scheduler` — sinon deux services beat-like
        pourraient être démarrés simultanément sur un même node avec
        `--profile scheduler`, doublant les tâches planifiées."""
        services = _load_compose()["services"]
        scheduler_services = [
            name
            for name, svc in services.items()
            if "scheduler" in (svc.get("profiles") or [])
        ]
        assert scheduler_services == ["beat"], (
            f"le rôle 'scheduler' doit appartenir à EXACTEMENT un service (beat), "
            f"trouvé : {scheduler_services}"
        )


class TestBeatScheduleSurvivesRestart:
    def test_beat_schedule_state_is_backed_by_a_named_volume(self):
        """Incident potentiel (2026-09-16) : `--schedule=/tmp/...` (sans
        volume) perdait l'état "dernière exécution" de chaque tâche
        planifiée à chaque redémarrage du conteneur beat. Un volume nommé
        le préserve (beat reste un singleton sur SON node, §9 — il ne migre
        pas d'un node à l'autre en cours de route, donc un volume LOCAL au
        node suffit)."""
        compose = _load_compose()
        beat = compose["services"]["beat"]
        volumes = beat.get("volumes") or []
        assert any("beat_schedule" in str(v) for v in volumes)
        schedule_arg = next(
            (a for a in beat["command"] if str(a).startswith("--schedule=")), None
        )
        assert schedule_arg is not None
        assert "/tmp" not in schedule_arg
