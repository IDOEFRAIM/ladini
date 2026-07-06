from __future__ import annotations

import asyncio
import base64
import copy
import logging
from dataclasses import asdict, dataclass
from typing import Any, AsyncIterator, Dict, Iterable, Mapping, MutableMapping, Sequence

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
    get_checkpoint_metadata,
)

from agriconnect.workspace.metadata import LANGGRAPH_STATE_KEY
from agriconnect.workspace.models import Workspace
from agriconnect.workspace.store import WorkspaceStore

logger = logging.getLogger("AgriConnect.Workspace.Checkpointer")


_DEFAULT_NS = ""
_STATE_VERSION = 1
_MAX_CHECKPOINTS_PER_NS = 20
_MAX_WRITES_PER_CHECKPOINT = 200


@dataclass(slots=True)
class _SerializedValue:
    type_tag: str
    payload_b64: str

    @classmethod
    def encode(cls, serializer, value: Any) -> "_SerializedValue":
        type_tag, raw = serializer.dumps_typed(value)
        return cls(type_tag, base64.b64encode(raw).decode("ascii"))

    def decode(self, serializer) -> Any:
        return serializer.loads_typed((self.type_tag, base64.b64decode(self.payload_b64.encode("ascii"))))


class WorkspaceCheckpointer(BaseCheckpointSaver):
    """LangGraph checkpointer backed by WorkspaceStore metadata."""

    def __init__(
        self,
        *,
        store: WorkspaceStore | None = None,
        serde=None,
        state_key: str = LANGGRAPH_STATE_KEY,
    ) -> None:
        super().__init__(serde=serde)
        self.store = store or WorkspaceStore()
        self.state_key = state_key

    # ----------------------------
    # Orchestrator helper API
    # ----------------------------
    async def export_state(self, thread_id: str) -> Dict[str, Any] | None:
        workspace = await self.store.get(thread_id)
        if not workspace:
            return None
        state = workspace.agent_state
        return copy.deepcopy(state) if isinstance(state, dict) else None

    # ----------------------------
    # Async LangGraph contract
    # ----------------------------
    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id = self._thread_id(config)
        if not thread_id:
            return None
        namespace = self._namespace(config)
        workspace = await self.store.get(thread_id)
        if not workspace:
            return None
        state = workspace.agent_state
        if not isinstance(state, dict):
            return None
        bucket = self._namespace_bucket(state, namespace, create=False)
        if not bucket or not bucket["checkpoints"]:
            return None

        checkpoint_id = self._resolve_checkpoint_id(config, bucket["checkpoints"].keys())
        if not checkpoint_id:
            return None

        return self._build_tuple(thread_id, namespace, checkpoint_id, bucket)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: Mapping[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        if config is None:
            return
        thread_id = self._thread_id(config)
        if not thread_id:
            return
        namespace = self._namespace(config)
        workspace = await self.store.get(thread_id)
        if not workspace:
            return
        state = workspace.metadata.get(self.state_key)
        if not isinstance(state, dict):
            return
        bucket = self._namespace_bucket(state, namespace, create=False)
        if not bucket:
            return

        before_id = get_checkpoint_id(before) if before else None
        emitted = 0
        checkpoints = bucket["checkpoints"]
        for checkpoint_id in sorted(checkpoints.keys(), reverse=True):
            if before_id and checkpoint_id >= before_id:
                continue
            entry = checkpoints[checkpoint_id]
            metadata = entry.get("metadata", {})
            if filter and not self._match_metadata(metadata, filter):
                continue
            yield self._build_tuple(thread_id, namespace, checkpoint_id, bucket)
            emitted += 1
            if limit is not None and emitted >= limit:
                break

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        thread_id = self._require_thread_id(config)
        namespace = self._namespace(config)
        workspace = await self._load_or_create_workspace(thread_id)
        state = self._clone_state(workspace)
        bucket = self._namespace_bucket(state, namespace)
        self._trim_bucket(bucket)

        encoded_checkpoint = _SerializedValue.encode(self.serde, checkpoint)
        sanitized_meta = get_checkpoint_metadata(config, metadata)
        parent_id = config.get("configurable", {}).get("checkpoint_id")

        checkpoint_id = checkpoint["id"]
        bucket["checkpoints"][checkpoint_id] = {
            "checkpoint": asdict(encoded_checkpoint),
            "metadata": sanitized_meta,
            "parent": parent_id,
            "ts": checkpoint.get("ts"),
        }
        bucket.setdefault("writes", {}).setdefault(checkpoint_id, {})

        await self._persist_state(workspace, state)
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": namespace,
                "checkpoint_id": checkpoint["id"],
            }
        }

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id = self._require_thread_id(config)
        namespace = self._namespace(config)
        checkpoint_id = self._require_checkpoint_id(config)
        if not writes:
            return

        workspace = await self._load_or_create_workspace(thread_id)
        state = self._clone_state(workspace)
        bucket = self._namespace_bucket(state, namespace)
        write_bucket = bucket.setdefault("writes", {}).setdefault(checkpoint_id, {})

        for idx, (channel, value) in enumerate(writes):
            key = f"{task_id}:{idx}"
            if key in write_bucket:
                continue
            write_bucket[key] = {
                "task_id": task_id,
                "channel": channel,
                "value": asdict(_SerializedValue.encode(self.serde, value)),
                "task_path": task_path,
                "index": idx,
            }
            self._trim_writes(write_bucket)

        await self._persist_state(workspace, state)

    async def adelete_thread(self, thread_id: str) -> None:
        workspace = await self.store.get(thread_id)
        if not workspace:
            return
        if workspace.agent_state:
            workspace.agent_state = {}
            await self.store.save(workspace)

    # ----------------------------
    # Optional sync wrappers
    # ----------------------------
    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:  # type: ignore[override]
        return self._run_sync(self.aget_tuple(config))

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: Mapping[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterable[CheckpointTuple]:  # type: ignore[override]
        return self._run_sync(self._collect_list(config, filter=filter, before=before, limit=limit))

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:  # type: ignore[override]
        return self._run_sync(self.aput(config, checkpoint, metadata, new_versions))

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:  # type: ignore[override]
        return self._run_sync(self.aput_writes(config, writes, task_id, task_path))

    def delete_thread(self, thread_id: str) -> None:  # type: ignore[override]
        return self._run_sync(self.adelete_thread(thread_id))

    # ----------------------------
    # Internal helpers
    # ----------------------------
    async def _collect_list(
        self,
        config: RunnableConfig | None,
        *,
        filter: Mapping[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> list[CheckpointTuple]:
        items: list[CheckpointTuple] = []
        async for item in self.alist(config, filter=filter, before=before, limit=limit):
            items.append(item)
        return items

    def _run_sync(self, coro):
        if coro is None:
            return None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            raise RuntimeError("WorkspaceCheckpointer sync API used inside running event loop")
        return asyncio.run(coro)

    def _thread_id(self, config: RunnableConfig | None) -> str | None:
        if not config:
            return None
        return str(config.get("configurable", {}).get("thread_id") or "").strip() or None

    def _require_thread_id(self, config: RunnableConfig) -> str:
        thread_id = self._thread_id(config)
        if not thread_id:
            raise ValueError("WorkspaceCheckpointer requires configurable.thread_id")
        return thread_id

    def _namespace(self, config: RunnableConfig | None) -> str:
        if not config:
            return _DEFAULT_NS
        value = config.get("configurable", {}).get("checkpoint_ns")
        return str(value or _DEFAULT_NS)

    def _require_checkpoint_id(self, config: RunnableConfig) -> str:
        checkpoint_id = config.get("configurable", {}).get("checkpoint_id")
        if not checkpoint_id:
            raise ValueError("WorkspaceCheckpointer requires checkpoint_id for writes")
        return str(checkpoint_id)

    async def _load_or_create_workspace(self, thread_id: str) -> Workspace:
        workspace = await self.store.get(thread_id)
        if workspace:
            return workspace
        return Workspace(workspace_id=thread_id, workspace_type="producer")

    def _clone_state(self, workspace: Workspace) -> Dict[str, Any]:
        state = workspace.agent_state
        if isinstance(state, dict):
            return copy.deepcopy(state)
        return {"version": _STATE_VERSION, "namespaces": {}}

    def _namespace_bucket(
        self,
        state: MutableMapping[str, Any],
        namespace: str,
        *,
        create: bool = True,
    ) -> Dict[str, Any] | None:
        namespaces = state.setdefault("namespaces", {})
        bucket = namespaces.get(namespace)
        if bucket is None and create:
            bucket = {"checkpoints": {}, "writes": {}}
            namespaces[namespace] = bucket
        if not bucket:
            return None
        bucket.setdefault("checkpoints", {})
        bucket.setdefault("writes", {})
        self._trim_bucket(bucket)
        return bucket

    def _trim_bucket(self, bucket: MutableMapping[str, Any]) -> None:
        checkpoints = bucket.get("checkpoints") or {}
        writes = bucket.get("writes") or {}
        while len(checkpoints) > _MAX_CHECKPOINTS_PER_NS:
            oldest_key = next(iter(checkpoints))
            checkpoints.pop(oldest_key, None)
            writes.pop(oldest_key, None)
        for ck_id in list(writes.keys()):
            if ck_id not in checkpoints:
                writes.pop(ck_id, None)

    def _trim_writes(self, write_bucket: MutableMapping[str, Any]) -> None:
        while len(write_bucket) > _MAX_WRITES_PER_CHECKPOINT:
            oldest_key = next(iter(write_bucket))
            write_bucket.pop(oldest_key, None)

    async def _persist_state(self, workspace: Workspace, state: Dict[str, Any]) -> None:
        workspace.metadata = dict(workspace.metadata or {})
        workspace.agent_state = state
        await self.store.save(workspace)

    def _resolve_checkpoint_id(self, config: RunnableConfig, ids: Iterable[str]) -> str | None:
        if checkpoint_id := get_checkpoint_id(config):
            return checkpoint_id if checkpoint_id in ids else None
        try:
            return max(ids)
        except ValueError:
            return None

    def _match_metadata(self, metadata: Mapping[str, Any], query: Mapping[str, Any]) -> bool:
        for key, expected in query.items():
            if metadata.get(key) != expected:
                return False
        return True

    def _build_tuple(
        self,
        thread_id: str,
        namespace: str,
        checkpoint_id: str,
        bucket: Mapping[str, Any],
    ) -> CheckpointTuple:
        entry = bucket["checkpoints"][checkpoint_id]
        checkpoint = _SerializedValue(**entry["checkpoint"]).decode(self.serde)
        metadata = entry.get("metadata", {})
        parent_id = entry.get("parent")
        writes_bucket = bucket.get("writes", {}).get(checkpoint_id, {})
        pending_writes = [
            (
                payload["task_id"],
                payload["channel"],
                _SerializedValue(**payload["value"]).decode(self.serde),
            )
            for _, payload in sorted(writes_bucket.items(), key=lambda item: item[1].get("index", 0))
        ] or None

        parent_config = (
            {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": namespace,
                    "checkpoint_id": parent_id,
                }
            }
            if parent_id
            else None
        )

        return CheckpointTuple(
            config={
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": namespace,
                    "checkpoint_id": checkpoint_id,
                }
            },
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config=parent_config,
            pending_writes=pending_writes,
        )
