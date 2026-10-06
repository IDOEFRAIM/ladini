"""`WorkspaceCheckpointer` — persistance de l'état LangGraph sur `WorkspaceStore`.

C'est LE composant dont dépend la continuité de CHAQUE tunnel (panier,
négociation, formulaire) d'un tour à l'autre. Un bug ici ne casse pas un
message isolé : il perd silencieusement la conversation entière au tour
suivant (`aget_tuple` qui renvoie None = LangGraph repart de zéro). Zéro DB
réelle : `FakeWorkspaceStore` simule `get`/`save` en mémoire.
"""
from __future__ import annotations

import dataclasses

import pytest

from ladini.workspace.checkpointer import WorkspaceCheckpointer, _SerializedValue
from ladini.workspace.models import Workspace
from tests.conftest import run


class FakeWorkspaceStore:
    """Double en mémoire de `WorkspaceStore` : mêmes signatures (`get`/`save`),
    aucune I/O. Permet de distinguer précisément quand le checkpointer touche
    (ou pas) la "base"."""

    def __init__(self) -> None:
        self._rows: dict[str, Workspace] = {}
        self.save_calls = 0
        self.get_calls = 0

    async def get(self, workspace_id: str):
        self.get_calls += 1
        return self._rows.get(workspace_id)

    async def save(self, workspace: Workspace) -> bool:
        self.save_calls += 1
        self._rows[workspace.workspace_id] = workspace
        return True


def make_checkpointer() -> tuple[WorkspaceCheckpointer, FakeWorkspaceStore]:
    store = FakeWorkspaceStore()
    return WorkspaceCheckpointer(store=store), store


def make_checkpoint(checkpoint_id: str = "cp-1", channel_values: dict | None = None, ts: str = "2026-01-01T00:00:00"):
    return {
        "v": 1,
        "id": checkpoint_id,
        "ts": ts,
        "channel_values": channel_values or {"current_goal": "SALES_PUBLISH_PRODUCT"},
        "channel_versions": {},
        "versions_seen": {},
    }


def _decoded_channels(cp: WorkspaceCheckpointer, trimmed: dict) -> dict:
    """Décode le checkpoint retenu et renvoie ses `channel_values` — permet de
    vérifier ce qui a RÉELLEMENT survécu à l'élagage (le checkpoint est stocké
    en blob base64 opaque, pas en JSON lisible)."""
    for bucket in (trimmed.get("namespaces") or {}).values():
        for entry in (bucket.get("checkpoints") or {}).values():
            encoded = entry.get("checkpoint")
            if isinstance(encoded, dict):
                return _SerializedValue(**encoded).decode(cp.serde).get("channel_values", {})
    return {}


def config_for(thread_id: str, *, ns: str = "", checkpoint_id: str | None = None) -> dict:
    conf = {"thread_id": thread_id, "checkpoint_ns": ns}
    if checkpoint_id is not None:
        conf["checkpoint_id"] = checkpoint_id
    return {"configurable": conf}


# =====================================================================
# aget_tuple
# =====================================================================

class TestAgetTuple:
    def test_returns_none_without_thread_id(self):
        cp, _ = make_checkpointer()
        assert run(cp.aget_tuple({"configurable": {}})) is None

    def test_returns_none_when_no_workspace_exists(self):
        cp, _ = make_checkpointer()
        assert run(cp.aget_tuple(config_for("phone-1"))) is None

    def test_returns_none_when_namespace_bucket_is_empty(self):
        cp, store = make_checkpointer()
        run(store.save(Workspace(workspace_id="phone-1", agent_state={"namespaces": {}})))
        assert run(cp.aget_tuple(config_for("phone-1"))) is None

    def test_round_trips_a_previously_put_checkpoint(self):
        cp, _ = make_checkpointer()
        checkpoint = make_checkpoint()
        run(cp.aput(config_for("phone-1"), checkpoint, {}, {}))
        tup = run(cp.aget_tuple(config_for("phone-1")))
        assert tup is not None
        assert tup.checkpoint["channel_values"]["current_goal"] == "SALES_PUBLISH_PRODUCT"

    def test_prefers_the_session_pinned_workspace_over_the_store(self):
        """`attach_workspace` (session RAM) doit primer sur `store.get` — sinon
        des écritures encore non flushées seraient invisibles à la lecture
        suivante dans le MÊME tour."""
        cp, store = make_checkpointer()
        pinned = Workspace(workspace_id="phone-1", agent_state={"namespaces": {}})
        cp.attach_workspace(pinned)
        checkpoint = make_checkpoint()
        run(cp.aput(config_for("phone-1"), checkpoint, {}, {}))
        assert store.get_calls == 0, "ne doit jamais retomber sur le store tant qu'une session est épinglée"
        tup = run(cp.aget_tuple(config_for("phone-1")))
        assert tup is not None

    def test_specific_checkpoint_id_not_in_bucket_returns_none(self):
        cp, _ = make_checkpointer()
        run(cp.aput(config_for("phone-1"), make_checkpoint("cp-1"), {}, {}))
        tup = run(cp.aget_tuple(config_for("phone-1", checkpoint_id="cp-does-not-exist")))
        assert tup is None

    def test_latest_checkpoint_id_used_when_none_specified(self):
        cp, _ = make_checkpointer()
        run(cp.aput(config_for("phone-1"), make_checkpoint("cp-1"), {}, {}))
        run(cp.aput(config_for("phone-1"), make_checkpoint("cp-2"), {}, {}))
        tup = run(cp.aget_tuple(config_for("phone-1")))
        assert tup.config["configurable"]["checkpoint_id"] == "cp-2"


# =====================================================================
# alist
# =====================================================================

class TestAlist:
    async def _collect(self, cp, config, **kw):
        items = []
        async for item in cp.alist(config, **kw):
            items.append(item)
        return items

    def test_empty_when_config_is_none(self):
        cp, _ = make_checkpointer()
        assert run(self._collect(cp, None)) == []

    def test_empty_when_no_workspace(self):
        cp, _ = make_checkpointer()
        assert run(self._collect(cp, config_for("phone-1"))) == []

    def test_lists_newest_first(self):
        # Sans `attach_workspace`, chaque `aput` passe par le chemin "legacy"
        # qui persiste + élague IMMÉDIATEMENT à 1 seul checkpoint (comportement
        # voulu hors session). Une session RAM (`attach_workspace`) est requise
        # pour observer la fenêtre multi-checkpoints (`_MAX_CHECKPOINTS_PER_NS`).
        cp, _ = make_checkpointer()
        cp.attach_workspace(Workspace(workspace_id="phone-1", agent_state={"namespaces": {}}))
        for cid in ("cp-1", "cp-2", "cp-3"):
            run(cp.aput(config_for("phone-1"), make_checkpoint(cid), {}, {}))
        items = run(self._collect(cp, config_for("phone-1")))
        ids = [i.config["configurable"]["checkpoint_id"] for i in items]
        assert ids == ["cp-3", "cp-2", "cp-1"]

    def test_respects_limit(self):
        cp, _ = make_checkpointer()
        cp.attach_workspace(Workspace(workspace_id="phone-1", agent_state={"namespaces": {}}))
        for cid in ("cp-1", "cp-2", "cp-3"):
            run(cp.aput(config_for("phone-1"), make_checkpoint(cid), {}, {}))
        items = run(self._collect(cp, config_for("phone-1"), limit=1))
        assert len(items) == 1
        assert items[0].config["configurable"]["checkpoint_id"] == "cp-3"

    def test_respects_before(self):
        cp, _ = make_checkpointer()
        cp.attach_workspace(Workspace(workspace_id="phone-1", agent_state={"namespaces": {}}))
        for cid in ("cp-1", "cp-2", "cp-3"):
            run(cp.aput(config_for("phone-1"), make_checkpoint(cid), {}, {}))
        before_cfg = config_for("phone-1", checkpoint_id="cp-3")
        items = run(self._collect(cp, config_for("phone-1"), before=before_cfg))
        ids = [i.config["configurable"]["checkpoint_id"] for i in items]
        assert ids == ["cp-2", "cp-1"]

    def test_filters_by_metadata(self):
        cp, _ = make_checkpointer()
        cp.attach_workspace(Workspace(workspace_id="phone-1", agent_state={"namespaces": {}}))
        run(cp.aput(config_for("phone-1"), make_checkpoint("cp-1"), {"source": "input"}, {}))
        run(cp.aput(config_for("phone-1"), make_checkpoint("cp-2"), {"source": "loop"}, {}))
        items = run(self._collect(cp, config_for("phone-1"), filter={"source": "loop"}))
        ids = [i.config["configurable"]["checkpoint_id"] for i in items]
        assert ids == ["cp-2"]


# =====================================================================
# aput — écriture d'un checkpoint
# =====================================================================

class TestAput:
    def test_creates_a_workspace_when_none_exists(self):
        cp, store = make_checkpointer()
        run(cp.aput(config_for("brand-new-phone"), make_checkpoint(), {}, {}))
        assert "brand-new-phone" in store._rows

    def test_returns_the_resuming_config(self):
        cp, _ = make_checkpointer()
        result = run(cp.aput(config_for("phone-1"), make_checkpoint("cp-42"), {}, {}))
        assert result["configurable"]["thread_id"] == "phone-1"
        assert result["configurable"]["checkpoint_id"] == "cp-42"

    def test_missing_thread_id_raises(self):
        cp, _ = make_checkpointer()
        with pytest.raises(ValueError):
            run(cp.aput({"configurable": {}}, make_checkpoint(), {}, {}))

    def test_drains_pending_writes_from_aput_writes_into_the_bucket(self):
        # Chemin session : le chemin legacy (détaché) élague les `writes` à
        # la persistance (voir `_prune_for_persistence`), donc ce round-trip
        # exige une session RAM épinglée pour observer les writes survivre.
        cp, _ = make_checkpointer()
        cp.attach_workspace(Workspace(workspace_id="phone-1", agent_state={"namespaces": {}}))
        cfg = config_for("phone-1", checkpoint_id="cp-1")
        run(cp.aput_writes(cfg, [("validator", {"ok": True})], task_id="task-1"))
        run(cp.aput(config_for("phone-1"), make_checkpoint("cp-1"), {}, {}))
        tup = run(cp.aget_tuple(config_for("phone-1")))
        assert tup.pending_writes is not None
        assert tup.pending_writes[0] == ("task-1", "validator", {"ok": True})

    def test_write_cache_is_cleared_after_drain(self):
        cp, _ = make_checkpointer()
        cfg = config_for("phone-1", checkpoint_id="cp-1")
        run(cp.aput_writes(cfg, [("validator", {"ok": True})], task_id="task-1"))
        run(cp.aput(config_for("phone-1"), make_checkpoint("cp-1"), {}, {}))
        assert cp._write_cache == {}

    def test_trims_beyond_max_checkpoints_per_namespace(self):
        cp, _ = make_checkpointer()
        for i in range(8):
            run(cp.aput(config_for("phone-1"), make_checkpoint(f"cp-{i}"), {}, {}))
        tup = run(cp.aget_tuple(config_for("phone-1")))
        assert tup is not None
        items = []

        async def _collect():
            async for it in cp.alist(config_for("phone-1")):
                items.append(it)
        run(_collect())
        assert len(items) <= 5, "la fenêtre en mémoire doit rester bornée (_MAX_CHECKPOINTS_PER_NS)"

    def test_attached_session_stays_in_ram_without_a_store_save(self):
        """Chemin session : aput ne doit PAS appeler `store.save` à chaque
        nœud — sinon ~15 écritures DB par tour au lieu d'une seule au flush."""
        cp, store = make_checkpointer()
        cp.attach_workspace(Workspace(workspace_id="phone-1", agent_state={"namespaces": {}}))
        run(cp.aput(config_for("phone-1"), make_checkpoint(), {}, {}))
        assert store.save_calls == 0

    def test_detached_legacy_path_persists_immediately(self):
        """Sans session attachée (chemin legacy), aput doit persister
        immédiatement (pas de flush différé possible)."""
        cp, store = make_checkpointer()
        run(cp.aput(config_for("phone-1"), make_checkpoint(), {}, {}))
        assert store.save_calls == 1

    def test_attach_hook_is_invoked_as_a_circuit_breaker(self):
        calls = []
        cp, _ = make_checkpointer()
        cp.attach_workspace(
            Workspace(workspace_id="phone-1", agent_state={"namespaces": {}}),
            on_checkpoint=lambda payload: calls.append(payload),
        )
        run(cp.aput(config_for("phone-1"), make_checkpoint(), {}, {}))
        assert len(calls) == 1


# =====================================================================
# aput_writes
# =====================================================================

class TestAputWrites:
    def test_noop_on_empty_writes(self):
        cp, _ = make_checkpointer()
        run(cp.aput_writes(config_for("phone-1", checkpoint_id="cp-1"), [], task_id="t"))
        assert cp._write_cache == {}

    def test_missing_thread_id_raises(self):
        cp, _ = make_checkpointer()
        with pytest.raises(ValueError):
            run(cp.aput_writes({"configurable": {"checkpoint_id": "cp-1"}}, [("c", 1)], task_id="t"))

    def test_missing_checkpoint_id_raises(self):
        cp, _ = make_checkpointer()
        with pytest.raises(ValueError):
            run(cp.aput_writes({"configurable": {"thread_id": "phone-1"}}, [("c", 1)], task_id="t"))

    def test_duplicate_task_and_index_key_is_not_overwritten(self):
        cp, _ = make_checkpointer()
        cfg = config_for("phone-1", checkpoint_id="cp-1")
        run(cp.aput_writes(cfg, [("channel_a", "first")], task_id="task-1"))
        run(cp.aput_writes(cfg, [("channel_a", "second")], task_id="task-1"))
        cache_key = ("phone-1", "", "cp-1")
        assert cp._write_cache[cache_key]["task-1:0"]["value"]["type_tag"] is not None
        decoded = cp._write_cache[cache_key]["task-1:0"]
        assert _SerializedValue(**decoded["value"]).decode(cp.serde) == "first"


# =====================================================================
# adelete_thread
# =====================================================================

class TestAdeleteThread:
    def test_noop_when_no_workspace(self):
        cp, store = make_checkpointer()
        run(cp.adelete_thread("ghost-phone"))
        assert store.save_calls == 0

    def test_wipes_agent_state_and_persists(self):
        cp, store = make_checkpointer()
        run(cp.aput(config_for("phone-1"), make_checkpoint(), {}, {}))
        run(cp.adelete_thread("phone-1"))
        assert store._rows["phone-1"].agent_state == {}
        assert store.save_calls >= 1

    def test_clears_pending_write_cache_for_the_thread(self):
        cp, _ = make_checkpointer()
        cfg = config_for("phone-1", checkpoint_id="cp-1")
        run(cp.aput_writes(cfg, [("c", "v")], task_id="t"))
        assert cp._write_cache
        run(cp.adelete_thread("phone-1"))
        assert all(k[0] != "phone-1" for k in cp._write_cache)


# =====================================================================
# WRAPPERS SYNCHRONES
# =====================================================================

class TestSyncWrappers:
    def test_put_and_get_tuple_round_trip(self):
        cp, _ = make_checkpointer()
        cp.put(config_for("phone-1"), make_checkpoint("cp-9"), {}, {})
        tup = cp.get_tuple(config_for("phone-1"))
        assert tup.config["configurable"]["checkpoint_id"] == "cp-9"

    def test_list_returns_an_iterable(self):
        cp, _ = make_checkpointer()
        cp.put(config_for("phone-1"), make_checkpoint("cp-1"), {}, {})
        items = list(cp.list(config_for("phone-1")))
        assert len(items) == 1

    def test_put_writes_and_delete_thread_do_not_raise(self):
        cp, _ = make_checkpointer()
        cp.put_writes(config_for("phone-1", checkpoint_id="cp-1"), [("c", "v")], task_id="t")
        cp.delete_thread("phone-1")

    def test_sync_api_inside_a_running_loop_raises(self):
        cp, _ = make_checkpointer()

        async def _inside():
            cp.get_tuple(config_for("phone-1"))

        with pytest.raises(RuntimeError):
            run(_inside())


# =====================================================================
# _trim_bucket / _resolve_checkpoint_id / _match_metadata
# =====================================================================

class TestInternalHelpers:
    def test_trim_bucket_drops_oldest_beyond_the_window(self):
        cp, _ = make_checkpointer()
        bucket = {"checkpoints": {f"cp-{i}": {} for i in range(7)}, "writes": {f"cp-{i}": {} for i in range(7)}}
        cp._trim_bucket(bucket)
        assert len(bucket["checkpoints"]) == 5

    def test_trim_bucket_drops_orphan_writes(self):
        cp, _ = make_checkpointer()
        bucket = {"checkpoints": {"cp-1": {}}, "writes": {"cp-1": {}, "cp-orphan": {}}}
        cp._trim_bucket(bucket)
        assert "cp-orphan" not in bucket["writes"]

    def test_resolve_checkpoint_id_from_config(self):
        cp, _ = make_checkpointer()
        assert cp._resolve_checkpoint_id(config_for("p", checkpoint_id="cp-2"), {"cp-1", "cp-2"}) == "cp-2"

    def test_resolve_checkpoint_id_falls_back_to_max_when_unspecified(self):
        cp, _ = make_checkpointer()
        assert cp._resolve_checkpoint_id(config_for("p"), {"cp-1", "cp-9", "cp-5"}) == "cp-9"

    def test_resolve_checkpoint_id_returns_none_on_empty_ids(self):
        cp, _ = make_checkpointer()
        assert cp._resolve_checkpoint_id(config_for("p"), set()) is None

    def test_resolve_checkpoint_id_from_config_not_in_ids_returns_none(self):
        cp, _ = make_checkpointer()
        assert cp._resolve_checkpoint_id(config_for("p", checkpoint_id="ghost"), {"cp-1"}) is None

    def test_match_metadata_true_on_subset_match(self):
        cp, _ = make_checkpointer()
        assert cp._match_metadata({"source": "loop", "step": 3}, {"source": "loop"}) is True

    def test_match_metadata_false_on_mismatch(self):
        cp, _ = make_checkpointer()
        assert cp._match_metadata({"source": "loop"}, {"source": "input"}) is False

    def test_run_sync_with_none_coro_returns_none(self):
        cp, _ = make_checkpointer()
        assert cp._run_sync(None) is None

    def test_thread_id_with_no_config_returns_none(self):
        cp, _ = make_checkpointer()
        assert cp._thread_id(None) is None

    def test_namespace_with_no_config_returns_default(self):
        cp, _ = make_checkpointer()
        assert cp._namespace(None) == ""

    def test_clone_state_falls_back_to_empty_shell_on_non_dict_agent_state(self):
        cp, _ = make_checkpointer()
        workspace = Workspace(workspace_id="phone-1")
        workspace.agent_state = "not-a-dict"  # type: ignore[assignment]
        cloned = cp._clone_state(workspace)
        assert cloned == {"version": 1, "namespaces": {}}


# =====================================================================
# GARDES DE FORME MALFORMÉE — namespaces/buckets/checkpoints inattendus
# =====================================================================

class TestMalformedShapeGuards:
    """Le stockage passe par du JSONB peu typé : un bug ailleurs (ou une
    migration partielle) peut produire des formes inattendues. Chaque
    fonction qui traverse `namespaces` doit les ignorer proprement plutôt
    que de faire planter tout le tour."""

    def test_prune_handles_state_without_a_namespaces_key(self):
        cp, _ = make_checkpointer()
        trimmed, metrics = cp._prune_for_persistence({"some_other_field": 1})
        assert trimmed["namespaces"] == {}
        assert metrics["payload_bytes"] > 0

    def test_prune_skips_non_dict_buckets(self):
        cp, _ = make_checkpointer()
        trimmed, _ = cp._prune_for_persistence({"namespaces": {"buyer": "not-a-dict"}})
        assert trimmed["namespaces"]["buyer"] == "not-a-dict"

    def test_prune_skips_non_dict_checkpoints_field(self):
        cp, _ = make_checkpointer()
        trimmed, _ = cp._prune_for_persistence({"namespaces": {"buyer": {"checkpoints": "not-a-dict"}}})
        assert trimmed["namespaces"]["buyer"]["checkpoints"] == "not-a-dict"

    def test_shrink_protected_subkeys_skips_non_dict_bucket(self):
        cp, _ = make_checkpointer()
        assert cp._shrink_oversized_protected_subkeys({"buyer": "not-a-dict"}) == set()

    def test_shrink_protected_subkeys_skips_non_dict_checkpoints(self):
        cp, _ = make_checkpointer()
        assert cp._shrink_oversized_protected_subkeys({"buyer": {"checkpoints": "not-a-dict"}}) == set()

    def test_shrink_protected_subkeys_skips_non_dict_encoded_checkpoint(self):
        cp, _ = make_checkpointer()
        ns = {"buyer": {"checkpoints": {"cp1": {"checkpoint": "not-a-dict"}}}}
        assert cp._shrink_oversized_protected_subkeys(ns) == set()

    def test_shrink_protected_subkeys_skips_non_dict_channel_values(self):
        cp, _ = make_checkpointer()
        checkpoint = make_checkpoint(channel_values="not-a-dict")
        enc = _SerializedValue.encode(cp.serde, checkpoint)
        ns = {"buyer": {"checkpoints": {"cp1": {"checkpoint": dataclasses.asdict(enc)}}}}
        assert cp._shrink_oversized_protected_subkeys(ns) == set()

    def test_shrink_protected_subkeys_swallows_decode_failure(self):
        cp, _ = make_checkpointer()
        ns = {"buyer": {"checkpoints": {"cp1": {"checkpoint": {"type_tag": "json", "payload_b64": "***not-b64***"}}}}}
        assert cp._shrink_oversized_protected_subkeys(ns) == set()

    def test_shrink_checkpoints_skips_non_dict_bucket(self):
        cp, _ = make_checkpointer()
        assert cp._shrink_oversized_checkpoints({"buyer": "not-a-dict"}) == set()

    def test_shrink_checkpoints_skips_non_dict_checkpoints_field(self):
        cp, _ = make_checkpointer()
        assert cp._shrink_oversized_checkpoints({"buyer": {"checkpoints": "not-a-dict"}}) == set()

    def test_shrink_checkpoints_skips_non_dict_channel_values(self):
        cp, _ = make_checkpointer()
        checkpoint = make_checkpoint(channel_values="not-a-dict")
        enc = _SerializedValue.encode(cp.serde, checkpoint)
        ns = {"buyer": {"checkpoints": {"cp1": {"checkpoint": dataclasses.asdict(enc)}}}}
        assert cp._shrink_oversized_checkpoints(ns) == set()

    def test_shrink_checkpoints_swallows_decode_failure(self):
        cp, _ = make_checkpointer()
        ns = {"buyer": {"checkpoints": {"cp1": {"checkpoint": {"type_tag": "json", "payload_b64": "***not-b64***"}}}}}
        assert cp._shrink_oversized_checkpoints(ns) == set()

    def test_describe_channel_sizes_skips_non_dict_bucket(self):
        cp, _ = make_checkpointer()
        assert cp._describe_channel_sizes({"buyer": "not-a-dict"}) == []

    def test_describe_channel_sizes_skips_non_dict_checkpoints_field(self):
        cp, _ = make_checkpointer()
        assert cp._describe_channel_sizes({"buyer": {"checkpoints": "not-a-dict"}}) == []

    def test_describe_channel_sizes_skips_non_dict_channel_values(self):
        cp, _ = make_checkpointer()
        checkpoint = make_checkpoint(channel_values="not-a-dict")
        enc = _SerializedValue.encode(cp.serde, checkpoint)
        ns = {"buyer": {"checkpoints": {"cp1": {"checkpoint": dataclasses.asdict(enc)}}}}
        assert cp._describe_channel_sizes(ns) == []

    def test_describe_channel_sizes_falls_back_to_minus_one_on_unserializable_value(self, monkeypatch):
        """Une valeur qui échappe à `json.dumps` (même avec `default=str`)
        pendant le diagnostic ne doit jamais faire planter le calcul — juste
        être signalée avec une taille sentinelle (-1)."""
        cp, _ = make_checkpointer()
        checkpoint = make_checkpoint(channel_values={"weird": "anything"})
        enc = _SerializedValue.encode(cp.serde, checkpoint)
        ns = {"buyer": {"checkpoints": {"cp1": {"checkpoint": dataclasses.asdict(enc)}}}}

        import ladini.workspace.checkpointer as ckpt_module
        original_dumps = ckpt_module.json.dumps

        def _boom(value, *args, **kwargs):
            if value == "anything":
                raise TypeError("simulated unserializable value")
            return original_dumps(value, *args, **kwargs)

        monkeypatch.setattr(ckpt_module.json, "dumps", _boom)
        lines = cp._describe_channel_sizes(ns)
        assert lines == ["checkpoint=cp1 channel=weird bytes=-1"]

    def test_build_summary_with_non_mapping_namespaces_returns_bare_summary(self):
        cp, _ = make_checkpointer()
        summary = cp._build_summary({"namespaces": "not-a-mapping"}, {})
        assert summary["namespaces"] == {}
        assert summary["total_checkpoints"] == 0

    def test_build_summary_skips_non_mapping_bucket(self):
        cp, _ = make_checkpointer()
        summary = cp._build_summary({"namespaces": {"buyer": "not-a-mapping"}}, {})
        assert summary["namespaces"] == {}

    def test_build_summary_reports_zero_count_when_no_checkpoints(self):
        cp, _ = make_checkpointer()
        summary = cp._build_summary({"namespaces": {"buyer": {"checkpoints": {}}}}, {})
        assert summary["namespaces"]["buyer"] == {"latest": None, "count": 0}


# =====================================================================
# export_state
# =====================================================================

class TestExportState:
    def test_returns_none_without_a_workspace(self):
        cp, _ = make_checkpointer()
        assert run(cp.export_state("ghost")) is None

    def test_returns_a_deep_copy_of_agent_state(self):
        cp, store = make_checkpointer()
        run(store.save(Workspace(workspace_id="phone-1", agent_state={"current_goal": "X"})))
        exported = run(cp.export_state("phone-1"))
        exported["current_goal"] = "MUTATED"
        reread = run(cp.export_state("phone-1"))
        assert reread["current_goal"] == "X", "une mutation du retour ne doit jamais fuiter dans l'état stocké"

    def test_prefers_pinned_session_workspace(self):
        cp, store = make_checkpointer()
        run(store.save(Workspace(workspace_id="phone-1", agent_state={"current_goal": "FROM_STORE"})))
        cp.attach_workspace(Workspace(workspace_id="phone-1", agent_state={"current_goal": "FROM_SESSION"}))
        assert run(cp.export_state("phone-1"))["current_goal"] == "FROM_SESSION"


# =====================================================================
# attach_workspace / detach_workspace
# =====================================================================

class TestAttachDetach:
    def test_detach_removes_pin_and_hook(self):
        cp, store = make_checkpointer()
        cp.attach_workspace(Workspace(workspace_id="phone-1", agent_state={"namespaces": {}}), on_checkpoint=lambda p: None)
        cp.detach_workspace("phone-1")
        assert "phone-1" not in cp._session_workspaces
        assert "phone-1" not in cp._session_hooks
        # Une fois détaché, aget_tuple doit retomber sur le store.
        run(cp.aput(config_for("phone-1"), make_checkpoint(), {}, {}))
        assert store.save_calls >= 1


# =====================================================================
# _prune_for_persistence / finalize_for_persistence — le cœur anti-bloat
# =====================================================================

class TestPruneForPersistence:
    def test_non_dict_state_yields_empty_namespaces(self):
        cp, _ = make_checkpointer()
        trimmed, metrics = cp._prune_for_persistence("not-a-dict")  # type: ignore[arg-type]
        assert trimmed["namespaces"] == {}
        assert metrics["payload_bytes"] == 0

    def test_keeps_only_the_single_latest_checkpoint_per_namespace(self):
        cp, _ = make_checkpointer()
        for cid in ("cp-1", "cp-2", "cp-3"):
            run(cp.aput(config_for("phone-1"), make_checkpoint(cid), {}, {}))
        workspace = cp._session_workspaces.get("phone-1") or run(cp.store.get("phone-1"))
        state = workspace.agent_state
        trimmed, metrics = cp._prune_for_persistence(state)
        bucket = trimmed["namespaces"][""]
        assert len(bucket["checkpoints"]) == 1
        assert metrics["total_checkpoints"] == 1

    def test_drops_write_logs_entirely(self):
        cp, _ = make_checkpointer()
        cfg = config_for("phone-1", checkpoint_id="cp-1")
        run(cp.aput_writes(cfg, [("c", "v")], task_id="t"))
        run(cp.aput(config_for("phone-1"), make_checkpoint("cp-1"), {}, {}))
        workspace = run(cp.store.get("phone-1"))
        trimmed, _ = cp._prune_for_persistence(workspace.agent_state)
        assert trimmed["namespaces"][""]["writes"] == {}

    def test_attaches_a_summary_block(self):
        cp, _ = make_checkpointer()
        run(cp.aput(config_for("phone-1"), make_checkpoint(), {}, {}))
        workspace = run(cp.store.get("phone-1"))
        trimmed, metrics = cp._prune_for_persistence(workspace.agent_state)
        assert "summary" in trimmed
        assert trimmed["summary"]["total_checkpoints"] == 1
        assert metrics["summary_ts"] == trimmed["summary"]["ts"]

    def test_applies_a_sliding_window_on_message_history(self):
        cp, _ = make_checkpointer()
        state = {
            "namespaces": {},
            "messages": [{"i": i} for i in range(50)],
        }
        trimmed, metrics = cp._prune_for_persistence(state)
        assert len(trimmed["message_window"]["messages"]) == 32
        assert metrics["windowed_fields"]["messages"] == {"total": 50, "kept": 32}

    def test_shrinks_shrinkable_channels_before_wiping_when_oversized(self, monkeypatch):
        # `attach_workspace` : garder l'état BRUT (non encore élagué) en RAM,
        # pour appeler `_prune_for_persistence` nous-mêmes sur un état qui
        # contient encore le canal éphémère volumineux à élaguer.
        cp, _ = make_checkpointer()
        cp.attach_workspace(Workspace(workspace_id="phone-1", agent_state={"namespaces": {}}))
        monkeypatch.setattr("ladini.workspace.checkpointer._MAX_PERSISTED_BYTES", 500)
        big_history = [{"role": "user", "content": "x" * 200} for _ in range(20)]
        checkpoint = make_checkpoint(channel_values={
            "current_goal": "SALES_PUBLISH_PRODUCT",
            "conversation_history": big_history,
        })
        run(cp.aput(config_for("phone-1"), checkpoint, {}, {}))
        workspace = cp._session_workspaces["phone-1"]
        trimmed, metrics = cp._prune_for_persistence(workspace.agent_state)
        assert metrics.get("shrunk_channels"), "les canaux ephemeres doivent avoir ete elagues"
        assert not metrics.get("truncated"), "ne doit pas atteindre le wipe complet si le shrink a suffi"

    def test_full_wipe_is_last_resort_when_shrink_is_not_enough(self, monkeypatch):
        """Si même après l'élagage des canaux éphémères ET des sous-clés
        protégées la taille dépasse toujours la limite (fuite dans un canal
        PROTÉGÉ, ex: `working_memory`), le dernier recours (wipe complet des
        namespaces) doit s'activer — mais SEULEMENT dans ce cas extrême."""
        cp, _ = make_checkpointer()
        monkeypatch.setattr("ladini.workspace.checkpointer._MAX_PERSISTED_BYTES", 100)
        checkpoint = make_checkpoint(channel_values={
            "working_memory": {"active_goal": "X", "leaked": "y" * 5000},
        })
        run(cp.aput(config_for("phone-1"), checkpoint, {}, {}))
        workspace = run(cp.store.get("phone-1"))
        trimmed, metrics = cp._prune_for_persistence(workspace.agent_state)
        assert metrics.get("truncated") is True
        assert trimmed["namespaces"] == {}
        assert "summary" in trimmed, "le résumé doit survivre même au wipe complet"

    def test_a_bloated_non_durable_channel_no_longer_wipes_the_tunnel(self, monkeypatch):
        """Chantier mémoire 2026-08-19 — palier 3 (`_keep_only_durable_channels`).

        Avant : un canal NI éphémère-connu (`_SHRINKABLE_CHANNELS`) NI durable
        — ex. `extracted_entities`, un merge_dict qui peut gonfler — passait au
        travers des paliers 1 et 2 et déclenchait le WIPE COMPLET : la
        conversation redémarrait de zéro au tour suivant (panier vidé, goal
        perdu). Maintenant il est simplement retiré, et la continuité du
        tunnel survit."""
        cp, _ = make_checkpointer()
        monkeypatch.setattr("ladini.workspace.checkpointer._MAX_PERSISTED_BYTES", 2000)
        checkpoint = make_checkpoint(channel_values={
            # Durables : doivent SURVIVRE.
            "current_goal": "BUYER_PREORDER_INIT",
            "active_cart": [{"name": "tomates", "quantity": 50}],
            "working_memory": {"active_goal": "BUYER_PREORDER_INIT"},
            # Ni durable ni "shrinkable" : le coupable, doit être retiré.
            "extracted_entities": {"junk": "z" * 40_000},
        })
        run(cp.aput(config_for("phone-1"), checkpoint, {}, {}))
        workspace = run(cp.store.get("phone-1"))
        trimmed, metrics = cp._prune_for_persistence(workspace.agent_state)

        assert not metrics.get("truncated"), "le wipe complet ne doit plus être atteint"
        assert trimmed["namespaces"], "les namespaces doivent survivre"

        # `extracted_entities` n'est NI dans `_SHRINKABLE_CHANNELS` NI dans les
        # champs durables : le palier 3 est le SEUL capable de le retirer. Le
        # voir disparaître pendant que les canaux durables survivent prouve
        # donc que c'est bien lui qui s'est déclenché (et non un autre palier).
        survivors = _decoded_channels(cp, trimmed)
        assert survivors["current_goal"] == "BUYER_PREORDER_INIT"
        assert survivors["active_cart"] == [{"name": "tomates", "quantity": 50}]
        assert "extracted_entities" not in survivors

    def test_langgraph_internal_channels_are_never_dropped(self, monkeypatch):
        """Les canaux de bookkeeping LangGraph (`__start__`, `branch:...`) ne
        sont pas de l'état métier : les retirer rendrait le checkpoint
        illisible. Ils doivent survivre au palier 3 même s'ils ne figurent
        évidemment pas dans la liste des champs durables."""
        cp, _ = make_checkpointer()
        monkeypatch.setattr("ladini.workspace.checkpointer._MAX_PERSISTED_BYTES", 2000)
        checkpoint = make_checkpoint(channel_values={
            "current_goal": "BUYER_PREORDER_INIT",
            "__start__": {"internal": True},
            "branch:to:cart": "x",
            "extracted_entities": {"junk": "z" * 40_000},
        })
        run(cp.aput(config_for("phone-1"), checkpoint, {}, {}))
        workspace = run(cp.store.get("phone-1"))
        trimmed, _metrics = cp._prune_for_persistence(workspace.agent_state)

        survivors = _decoded_channels(cp, trimmed)
        assert "__start__" in survivors
        assert "branch:to:cart" in survivors
        assert "extracted_entities" not in survivors

    def test_finalize_for_persistence_mutates_workspace_agent_state_in_place(self):
        cp, _ = make_checkpointer()
        run(cp.aput(config_for("phone-1"), make_checkpoint(), {}, {}))
        workspace = run(cp.store.get("phone-1"))
        original_namespace_count = len(workspace.agent_state.get("namespaces", {}))
        metrics = cp.finalize_for_persistence(workspace)
        assert isinstance(metrics, dict)
        assert "summary" in workspace.agent_state
        assert len(workspace.agent_state["namespaces"]) == original_namespace_count

    def test_finalize_for_persistence_on_non_dict_state_returns_zero_bytes(self):
        cp, _ = make_checkpointer()
        workspace = Workspace(workspace_id="phone-1")
        workspace.agent_state = None  # type: ignore[assignment]
        assert cp.finalize_for_persistence(workspace) == {"payload_bytes": 0}


# =====================================================================
# _shrink_oversized_checkpoints — canaux éphémères (tier 1)
# =====================================================================

class TestShrinkOversizedCheckpoints:
    def test_drops_only_shrinkable_channels(self):
        cp, ns = self._make_ns(cp_module_only=True)
        checkpoint = make_checkpoint(channel_values={
            "current_goal": "KEEP_ME",
            "raw_analysis": {"huge": "x" * 1000},
            "chat_history": ["a", "b"],
        })
        enc = _SerializedValue.encode(cp.serde, checkpoint)
        namespaces = {"buyer": {"checkpoints": {"cp1": {"checkpoint": dataclasses.asdict(enc)}}}}
        dropped = cp._shrink_oversized_checkpoints(namespaces)
        assert dropped == {"raw_analysis", "chat_history"}
        decoded = _SerializedValue(**namespaces["buyer"]["checkpoints"]["cp1"]["checkpoint"]).decode(cp.serde)
        assert "current_goal" in decoded["channel_values"]
        assert "raw_analysis" not in decoded["channel_values"]

    def test_returns_empty_set_when_nothing_to_shrink(self):
        cp, _ = self._make_ns(cp_module_only=True)
        checkpoint = make_checkpoint(channel_values={"current_goal": "X"})
        enc = _SerializedValue.encode(cp.serde, checkpoint)
        namespaces = {"buyer": {"checkpoints": {"cp1": {"checkpoint": dataclasses.asdict(enc)}}}}
        assert cp._shrink_oversized_checkpoints(namespaces) == set()

    def test_malformed_checkpoint_entry_is_skipped_without_raising(self):
        cp, _ = self._make_ns(cp_module_only=True)
        namespaces = {"buyer": {"checkpoints": {"cp1": {"checkpoint": "not-a-dict-payload"}}}}
        assert cp._shrink_oversized_checkpoints(namespaces) == set()

    @staticmethod
    def _make_ns(cp_module_only=False):
        cp, _ = make_checkpointer()
        return cp, None


# =====================================================================
# _describe_channel_sizes — diagnostic avant le wipe
# =====================================================================

class TestDescribeChannelSizes:
    def test_lists_channels_sorted_largest_first(self):
        cp, _ = make_checkpointer()
        checkpoint = make_checkpoint(channel_values={
            "small": "x",
            "large": "y" * 500,
        })
        enc = _SerializedValue.encode(cp.serde, checkpoint)
        namespaces = {"buyer": {"checkpoints": {"cp1": {"checkpoint": dataclasses.asdict(enc)}}}}
        lines = cp._describe_channel_sizes(namespaces)
        assert lines[0].startswith("checkpoint=cp1 channel=large")

    def test_empty_when_no_checkpoints(self):
        cp, _ = make_checkpointer()
        assert cp._describe_channel_sizes({"buyer": {"checkpoints": {}}}) == []
