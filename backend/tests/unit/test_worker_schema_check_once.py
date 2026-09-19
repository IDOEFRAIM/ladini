"""`api/tasks.py::_maybe_ensure_schema` — incident réel (2026-09-19).

`ensure_performance_indexes` (une douzaine de `CREATE INDEX`/`ALTER TABLE`
idempotents, sous UNE SEULE transaction externe — voir `services/database/
d.py`) tournait AUPARAVANT une fois PAR PROCESSUS worker forké
(`worker_process_init` se déclenche une fois par enfant prefork, ex. 4 fois
avec `concurrency: 4`). Preuve empirique en prod : 4 exécutions DDL
concurrentes ont fait tuer par le détecteur de deadlock Postgres une requête
`SELECT` ordinaire d'une tâche périodique (`producers_for_auction`,
`DeadlockDetectedError` — cycle AccessExclusiveLock/AccessShareLock).

Ces tests verrouillent le correctif : un jeton Redis SET-NX-EX
(`claim_once`) garantit qu'UN SEUL processus exécute réellement le DDL par
fenêtre de démarrage ; les autres le sautent sans erreur."""
from __future__ import annotations

from ladini.api import tasks


class _ImmediateLoop:
    """Faux event loop : exécute la coroutine immédiatement sans vraie
    boucle asyncio — suffisant ici, `_ensure_schema` est entièrement
    mocké dans ces tests (jamais un vrai appel DB)."""

    def run_until_complete(self, coro):
        try:
            coro.send(None)
        except StopIteration as exc:
            return exc.value
        raise AssertionError("la coroutine mockée n'a pas terminé immédiatement")


async def _noop_ensure_schema():
    return None


class TestSchemaCheckRunsAtMostOncePerStartupWindow:
    def test_the_process_that_wins_the_claim_runs_the_ddl_check(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tasks, "claim_once", lambda key, ttl_seconds=300: True)
        monkeypatch.setattr(
            tasks,
            "_ensure_schema",
            lambda: (calls.append(1), _noop_ensure_schema())[1],
        )
        tasks._maybe_ensure_schema(_ImmediateLoop())
        assert calls == [1], "le processus qui gagne le claim Redis doit exécuter _ensure_schema"

    def test_a_process_that_loses_the_claim_skips_the_ddl_check_entirely(self, monkeypatch):
        """C'est LE cas de l'incident : les workers 2/3/4, arrivés après le
        premier, ne doivent PLUS jamais lancer leur propre passage DDL —
        source des exécutions concurrentes qui ont provoqué le deadlock."""
        calls = []
        monkeypatch.setattr(tasks, "claim_once", lambda key, ttl_seconds=300: False)
        monkeypatch.setattr(
            tasks,
            "_ensure_schema",
            lambda: (calls.append(1), _noop_ensure_schema())[1],
        )
        tasks._maybe_ensure_schema(_ImmediateLoop())
        assert calls == [], "un processus qui perd le claim ne doit JAMAIS exécuter le DDL"

    def test_the_claim_key_and_ttl_match_the_documented_contract(self, monkeypatch):
        """La clé doit être stable (même clé pour tous les processus d'une
        même fenêtre de démarrage) et le TTL doit rester généreux (>= la
        durée observée la plus longue en incident, ~52s) — un TTL trop
        court réintroduirait la course."""
        seen = {}

        def _fake_claim_once(key, ttl_seconds=300):
            seen["key"] = key
            seen["ttl_seconds"] = ttl_seconds
            return True

        monkeypatch.setattr(tasks, "claim_once", _fake_claim_once)
        monkeypatch.setattr(tasks, "_ensure_schema", lambda: _noop_ensure_schema())
        tasks._maybe_ensure_schema(_ImmediateLoop())
        assert seen["key"] == "worker:ensure_performance_indexes:startup"
        assert seen["ttl_seconds"] >= 60, "TTL trop court réintroduirait la course entre workers forkés"

    def test_a_failure_inside_ensure_schema_is_swallowed_never_crashes_worker_startup(self, monkeypatch):
        """Best-effort par conception (docstring de _ensure_schema) — même
        garantie après le correctif : une erreur DDL ne doit jamais
        empêcher le worker de démarrer."""

        async def _boom():
            raise RuntimeError("DB indisponible au démarrage")

        monkeypatch.setattr(tasks, "claim_once", lambda key, ttl_seconds=300: True)
        monkeypatch.setattr(tasks, "_ensure_schema", lambda: _boom())
        tasks._maybe_ensure_schema(_ImmediateLoop())  # ne doit pas lever

    def test_redis_unavailable_fails_open_to_the_historical_behaviour(self, monkeypatch):
        """`claim_once` est fail-open (True) si Redis est indisponible —
        voir core/idempotency.py. Ce test verrouille que _maybe_ensure_schema
        respecte cette sémantique (jamais de blocage du démarrage faute de
        Redis) plutôt que de mal interpréter un None/erreur comme un refus."""
        calls = []
        monkeypatch.setattr(tasks, "claim_once", lambda key, ttl_seconds=300: True)
        monkeypatch.setattr(
            tasks,
            "_ensure_schema",
            lambda: (calls.append(1), _noop_ensure_schema())[1],
        )
        tasks._maybe_ensure_schema(_ImmediateLoop())
        assert calls == [1]
