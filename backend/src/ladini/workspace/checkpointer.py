from __future__ import annotations

import asyncio
import base64
import copy
import json
import logging
import time
from dataclasses import asdict, dataclass
from typing import (
    Any,
    AsyncIterator,
    Callable,
    Dict,
    Iterable,
    Mapping,
    MutableMapping,
    Sequence,
)

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

from ladini.workspace.metadata import LANGGRAPH_STATE_KEY
from ladini.workspace.models import Workspace
from ladini.workspace.store import WorkspaceStore

logger = logging.getLogger("Ladini.Workspace.Checkpointer")


_DEFAULT_NS = ""
_STATE_VERSION = 1
# In-memory checkpoint window per namespace during a single process lifetime.
_MAX_CHECKPOINTS_PER_NS = 5
# Only the latest checkpoint is persisted to Postgres; older ones are pruned.
_MAX_PERSISTED_CHECKPOINTS = 1
# Hard cap on the full persisted JSONB blob (post-prune safety net).
_MAX_PERSISTED_BYTES = 480_000  # 480 KB — must align with store._MAX_STATE_BYTES
# Soft cap for conversational history retained per checkpoint.
_MAX_MESSAGE_WINDOW = 32
_MESSAGE_KEYS = (
    "messages",
    "conversation_history",
    "turn_history",
    "history",
    "events",
)


@dataclass(slots=True)
class _SerializedValue:
    type_tag: str
    payload_b64: str

    @classmethod
    def encode(cls, serializer, value: Any) -> "_SerializedValue":
        type_tag, raw = serializer.dumps_typed(value)
        return cls(type_tag, base64.b64encode(raw).decode("ascii"))

    def decode(self, serializer) -> Any:
        return serializer.loads_typed(
            (self.type_tag, base64.b64decode(self.payload_b64.encode("ascii")))
        )


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
        # In-memory write cache: (thread_id, namespace, checkpoint_id) → {entry_key: payload}
        # aput_writes populates this; aput drains it into the persisted bucket.
        # Avoids a DB round-trip on every intermediate node write (~15+ per turn).
        self._write_cache: Dict[tuple, Dict[str, Any]] = {}
        # Active workspaces injected by the orchestrator (dirty writes stay in RAM)
        self._session_workspaces: Dict[str, Workspace] = {}
        self._session_hooks: Dict[str, Callable[[Dict[str, Any]], None]] = {}

    # ----------------------------
    # Orchestrator helper API
    # ----------------------------
    async def export_state(self, thread_id: str) -> Dict[str, Any] | None:
        workspace = self._session_workspaces.get(thread_id)
        if workspace is None:
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
        workspace = self._session_workspaces.get(thread_id)
        if workspace is None:
            workspace = await self.store.get(thread_id)
        if not workspace:
            return None
        state = workspace.agent_state
        if not isinstance(state, dict):
            return None
        bucket = self._namespace_bucket(state, namespace, create=False)
        if not bucket or not bucket["checkpoints"]:
            return None

        checkpoint_id = self._resolve_checkpoint_id(
            config, bucket["checkpoints"].keys()
        )
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
        workspace = self._session_workspaces.get(thread_id)
        if workspace is None:
            workspace = await self.store.get(thread_id)
        if not workspace:
            return
        state = workspace.agent_state  # BUG FIX: was metadata.get(state_key)
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
        # Drain any in-memory writes accumulated by aput_writes for this checkpoint.
        cache_key = (thread_id, namespace, checkpoint_id)
        cached_writes = self._write_cache.pop(cache_key, {})
        if cached_writes:
            bucket.setdefault("writes", {}).setdefault(checkpoint_id, {}).update(
                cached_writes
            )
        else:
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
        """Accumulate intermediate node writes in memory — NO database call.

        LangGraph invokes this after every node execution (~15 times per user
        turn).  Persisting each write to Postgres would cost 30 DB round-trips
        per turn and inflate the JSONB blob to hundreds of MB.

        Writes are held in ``_write_cache`` and flushed into the persisted
        bucket on the next ``aput`` (superstep checkpoint).
        """
        if not writes:
            return
        thread_id = self._require_thread_id(config)
        namespace = self._namespace(config)
        checkpoint_id = self._require_checkpoint_id(config)

        cache_key = (thread_id, namespace, checkpoint_id)
        bucket = self._write_cache.setdefault(cache_key, {})

        for idx, (channel, value) in enumerate(writes):
            key = f"{task_id}:{idx}"
            if key in bucket:
                continue
            bucket[key] = {
                "task_id": task_id,
                "channel": channel,
                "value": asdict(_SerializedValue.encode(self.serde, value)),
                "task_path": task_path,
                "index": idx,
            }

    async def adelete_thread(self, thread_id: str) -> None:
        # Clear in-memory write cache for this thread.
        stale_keys = [k for k in self._write_cache if k[0] == thread_id]
        for k in stale_keys:
            self._write_cache.pop(k, None)

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
        return self._run_sync(
            self._collect_list(config, filter=filter, before=before, limit=limit)
        )

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
            raise RuntimeError(
                "WorkspaceCheckpointer sync API used inside running event loop"
            )
        return asyncio.run(coro)

    def _thread_id(self, config: RunnableConfig | None) -> str | None:
        if not config:
            return None
        return (
            str(config.get("configurable", {}).get("thread_id") or "").strip() or None
        )

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
        workspace = self._session_workspaces.get(thread_id)
        if workspace:
            return workspace
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

    async def _persist_state(self, workspace: Workspace, state: Dict[str, Any]) -> None:
        workspace.metadata = dict(workspace.metadata or {})

        if workspace.workspace_id in self._session_workspaces:
            # ── Chemin session (normal) : staging RAM PEU COÛTEUX ──────────
            # LangGraph appelle aput après CHAQUE nœud (~7-15×/tour). On garde
            # ici l'état BRUT sans le pruning lourd (deepcopy + json.dumps +
            # summary + windows) : ce travail n'est utile qu'à l'écriture DB,
            # qui n'a lieu qu'UNE fois, au flush (voir finalize_for_persistence).
            # Le bucket est déjà borné par _trim_bucket (≤ 5 checkpoints).
            workspace.agent_state = state
            workspace.mark_dirty()
            hook = self._session_hooks.get(workspace.workspace_id)
            if hook:
                hook({})  # circuit-breaker : compte les checkpoints par tour
            logger.debug(
                "Workspace checkpoint staged in RAM | workspace=%s | namespaces=%s",
                workspace.workspace_id,
                len(state.get("namespaces", {})) if isinstance(state, dict) else 0,
            )
        else:
            # ── Chemin direct/legacy (checkpointer non attaché) ────────────
            # Pas de session RAM : on prune + persiste immédiatement.
            pruned_state, metrics = self._prune_for_persistence(state)
            workspace.agent_state = pruned_state
            await self.store.save(workspace)
            logger.info(
                "Workspace state persisted (legacy flow) | workspace=%s | bytes=%s | checkpoints=%s | truncated=%s",
                workspace.workspace_id,
                metrics.get("payload_bytes"),
                metrics.get("total_checkpoints"),
                metrics.get("truncated", False),
            )

    def finalize_for_persistence(self, workspace: Workspace) -> Dict[str, Any]:
        """Applique le pruning lourd UNE fois, juste avant l'écriture DB (flush).

        Déplace hors de la boucle par-nœud le coût de sérialisation/résumé :
        appelé une seule fois par tour au lieu de ~7-15 fois. Idempotent.
        """
        state = workspace.agent_state
        if not isinstance(state, dict):
            return {"payload_bytes": 0}
        pruned_state, metrics = self._prune_for_persistence(state)
        workspace.agent_state = pruned_state
        return metrics

    def attach_workspace(
        self,
        workspace: Workspace,
        *,
        on_checkpoint: Callable[[Dict[str, Any]], None] | None = None,
    ) -> None:
        """Pin a workspace in-memory for the duration of an orchestration."""

        self._session_workspaces[workspace.workspace_id] = workspace
        if on_checkpoint:
            self._session_hooks[workspace.workspace_id] = on_checkpoint

    def detach_workspace(self, workspace_id: str) -> None:
        self._session_workspaces.pop(workspace_id, None)
        self._session_hooks.pop(workspace_id, None)

    def _resolve_checkpoint_id(
        self, config: RunnableConfig, ids: Iterable[str]
    ) -> str | None:
        if checkpoint_id := get_checkpoint_id(config):
            return checkpoint_id if checkpoint_id in ids else None
        try:
            return max(ids)
        except ValueError:
            return None

    def _match_metadata(
        self, metadata: Mapping[str, Any], query: Mapping[str, Any]
    ) -> bool:
        for key, expected in query.items():
            if metadata.get(key) != expected:
                return False
        return True

    def _prune_for_persistence(
        self, state: Dict[str, Any]
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        """Produce a minimal snapshot safe to store in Postgres.

        Strategy (in order):
        1. Drop *all* write logs — they are ephemeral and can reach hundreds of MB.
        2. Keep only the single latest checkpoint per namespace
           (``_MAX_PERSISTED_CHECKPOINTS = 1``).
        3. Apply a sliding-window strategy on conversation history so Postgres
           never receives the full transcript.
        4. Attach a synthetic ``summary`` block describing the retained data so
           orchestration/monitoring layers can introspect without inflating size.
        5. If STILL too big, drop known-safe, non-essential CHANNELS from
           inside the remaining checkpoint itself (see ``_shrink_channel_values``)
           — debug/ephemeral fields, never the live-tunnel ones (``active_cart``,
           ``preorder_workflow``, ``transaction_payload``, ``current_goal``...).
        6. Only as an absolute last resort, if the payload is STILL too big even
           after (5), fall back to a summary-only payload (namespaces wiped).
           ⚠️ This is intentionally a LAST resort, not a first response: wiping
           `namespaces` means the NEXT turn's `aget_tuple` finds no checkpoint at
           all, so LangGraph restarts every channel at its type default — this is
           what silently emptied `active_cart` mid-precommande (buyer selection
           "1" was then read as "cart_items=0", branching into "add a product"
           instead of "confirm order"). Step (5) exists specifically to avoid
           reaching this branch in the common case.
        """

        if not isinstance(state, dict):
            return {"version": _STATE_VERSION, "namespaces": {}}, {"payload_bytes": 0}

        trimmed = copy.deepcopy(state)
        namespaces = trimmed.get("namespaces")
        if not isinstance(namespaces, dict):
            trimmed["namespaces"] = {}
            namespaces = trimmed["namespaces"]

        for bucket in namespaces.values():
            if not isinstance(bucket, dict):
                continue

            # Drop write logs entirely — they can explode to hundreds of MB.
            if "writes" in bucket:
                bucket["writes"] = {}

            checkpoints = bucket.get("checkpoints")
            if not isinstance(checkpoints, dict):
                continue

            if (
                _MAX_PERSISTED_CHECKPOINTS
                and len(checkpoints) > _MAX_PERSISTED_CHECKPOINTS
            ):
                keep = set(list(checkpoints.keys())[-_MAX_PERSISTED_CHECKPOINTS:])
                for key in list(checkpoints.keys()):
                    if key not in keep:
                        checkpoints.pop(key, None)

        windows, window_meta = self._apply_message_windows(trimmed, state)
        summary = self._build_summary(state, window_meta)
        trimmed["summary"] = summary
        if windows:
            trimmed["message_window"] = windows

        payload_bytes = len(json.dumps(trimmed, ensure_ascii=False).encode("utf-8"))
        metrics: Dict[str, Any] = {
            "payload_bytes": payload_bytes,
            "windowed_fields": window_meta,
            "total_namespaces": len(namespaces),
            "total_checkpoints": summary.get("total_checkpoints", 0),
        }

        if payload_bytes > _MAX_PERSISTED_BYTES:
            shrunk_channels = self._shrink_oversized_checkpoints(namespaces)
            if shrunk_channels:
                payload_bytes = len(
                    json.dumps(trimmed, ensure_ascii=False).encode("utf-8")
                )
                metrics["payload_bytes"] = payload_bytes
                metrics["shrunk_channels"] = shrunk_channels
                logger.warning(
                    "WorkspaceCheckpointer payload exceeded %s bytes — dropped ephemeral "
                    "channels %s to avoid a full wipe (new size %s)",
                    _MAX_PERSISTED_BYTES,
                    sorted(shrunk_channels),
                    payload_bytes,
                )

        # Tier 2 : les canaux PROTÉGÉS (working_memory en tête — jamais
        # candidats à `_shrink_oversized_checkpoints`, qui ne touche que les
        # canaux éphémères) ne devraient JAMAIS porter une valeur individuelle
        # énorme : ce sont des flags, des libellés de menu, des ids courts.
        # Un incident vécu en prod (585-6-cb...) : deux flows y stockaient
        # sans le savoir des dumps bruts de réponse MCP (`bids_cache`/
        # `stocks_cache`) jamais lus nulle part, jamais nettoyés par aucun des
        # 3 mécanismes de cleanup existants (nodes/cleaner.py, nodes/
        # cleanup.py, goal_planner._clear_goal_lock) — 600 Ko à eux seuls,
        # assez pour déclencher le wipe total ci-dessous. La source a été
        # corrigée, mais compter uniquement sur "chaque flow pense à
        # nettoyer sa propre clé" a déjà échoué deux fois (voir aussi
        # `fetched_data_cache`, jamais écrit mais toujours "nettoyé"). Ce
        # filet de sécurité générique élague toute clé individuelle
        # anormalement grosse dans un canal protégé AVANT le wipe complet —
        # il protège contre la PROCHAINE fuite de ce type, pas seulement
        # celle déjà connue.
        if payload_bytes > _MAX_PERSISTED_BYTES:
            shrunk_subkeys = self._shrink_oversized_protected_subkeys(namespaces)
            if shrunk_subkeys:
                payload_bytes = len(
                    json.dumps(trimmed, ensure_ascii=False).encode("utf-8")
                )
                metrics["payload_bytes"] = payload_bytes
                metrics["shrunk_subkeys"] = shrunk_subkeys
                logger.warning(
                    "WorkspaceCheckpointer payload still exceeded %s bytes — dropped "
                    "oversized individual keys %s inside protected channels to avoid a "
                    "full wipe (new size %s)",
                    _MAX_PERSISTED_BYTES,
                    sorted(shrunk_subkeys),
                    payload_bytes,
                )

        # Tier 3 : DERNIER RECOURS NON DESTRUCTIF — ne garder que les canaux
        # DURABLES (source unique : core/state_profile.py). Avant ce palier,
        # l'échec des tiers 1-2 menait DIRECTEMENT au wipe total : la
        # conversation redémarrait à zéro au tour suivant (panier vidé,
        # tunnel perdu), le pire résultat possible pour l'utilisateur. Or
        # les ~50 champs DURABLE sont par nature minuscules (drapeaux,
        # identifiants courts, panier) : tout ce qui peut faire exploser la
        # taille est, par construction, ÉPHÉMÈRE ou DÉRIVÉ, donc jetable
        # sans conséquence sur la continuité conversationnelle. Ce palier
        # rend donc le wipe total pratiquement inatteignable.
        if payload_bytes > _MAX_PERSISTED_BYTES:
            dropped_non_durable = self._keep_only_durable_channels(namespaces)
            if dropped_non_durable:
                payload_bytes = len(
                    json.dumps(trimmed, ensure_ascii=False).encode("utf-8")
                )
                metrics["payload_bytes"] = payload_bytes
                metrics["dropped_non_durable"] = dropped_non_durable
                logger.warning(
                    "WorkspaceCheckpointer payload still exceeded %s bytes — kept ONLY "
                    "durable channels, dropped %s. Tunnel continuity preserved "
                    "(new size %s)",
                    _MAX_PERSISTED_BYTES,
                    sorted(dropped_non_durable),
                    payload_bytes,
                )

        if payload_bytes > _MAX_PERSISTED_BYTES:
            logger.error(
                "WorkspaceCheckpointer payload STILL exceeds %s bytes (%s) even after "
                "keeping only durable channels — persisting summary only (last resort: "
                "this wipes the live tunnel state — active_cart/current_goal/"
                "transaction_payload — for the NEXT turn, since aget_tuple() will find "
                "no checkpoint to resume from). A DURABLE channel is pathologically "
                "oversized; see the per-channel breakdown below.",
                _MAX_PERSISTED_BYTES,
                payload_bytes,
            )
            # Diagnostic uniquement (aucun effet sur le payload persisté) : le
            # shrink des canaux "sûrs" (_SHRINKABLE_CHANNELS) n'a pas suffi, ce
            # qui veut dire que le vrai gonflement vient d'un canal PROTÉGÉ
            # (active_cart/working_memory/transaction_payload/stable_entities/
            # ...) — sans savoir LEQUEL, on ne peut pas corriger sans risquer
            # de supprimer un champ dont le tunnel a réellement besoin. On
            # journalise donc la taille de chaque canal pour que la PROCHAINE
            # occurrence en prod désigne le coupable exact.
            try:
                for _line in self._describe_channel_sizes(namespaces):
                    logger.warning(
                        "WorkspaceCheckpointer channel size breakdown | %s", _line
                    )
            except Exception:
                logger.debug(
                    "Channel size breakdown failed (non-blocking)", exc_info=True
                )
            trimmed = {
                "version": _STATE_VERSION,
                "namespaces": {},
                "summary": summary,
                "message_window": windows,
                "truncated": True,
            }
            payload_bytes = len(json.dumps(trimmed, ensure_ascii=False).encode("utf-8"))
            metrics["payload_bytes"] = payload_bytes
            metrics["truncated"] = True

        metrics["summary_ts"] = summary.get("ts")
        return trimmed, metrics

    # Channels whose loss is safe / low-impact — all are recomputed from
    # scratch on the next turn that needs them, and NONE of them carry
    # cross-turn tunnel continuity. NEVER add active_cart, preorder_workflow,
    # transaction_payload, current_goal, working_memory, stable_entities, or
    # anything else a resumed conversation depends on to this list.
    _SHRINKABLE_CHANNELS = frozenset(
        {
            "tool_execution_history",
            "execution_result",
            "raw_analysis",
            "cognitive_decision",
            "intent_competition",
            "fallback_recommendations",
            "chat_history",
            "conversation_history",
            "messages",
            "turn_history",
            "events",
        }
    )

    # No legitimate value inside a protected dict-shaped channel (flags,
    # goal names, short ids, rendered menu TEXT) should ever approach this —
    # anything bigger is leaked/misplaced raw data (a full MCP response, a
    # product catalog...), not conversational bookkeeping.
    _MAX_PROTECTED_SUBKEY_BYTES = 10_000  # 10 KB
    _PROTECTED_DICT_CHANNELS = frozenset(
        {
            "working_memory",
            "stable_entities",
            "volatile_entities",
        }
    )

    def _shrink_oversized_protected_subkeys(self, namespaces: Dict[str, Any]) -> set:
        """Tier-2 defense: prune individual oversized keys inside protected
        dict-shaped channels (``_PROTECTED_DICT_CHANNELS``) — these channels
        themselves are never wiped wholesale (needed for tunnel continuity),
        but no single key inside them should legitimately be this large.
        Runs only when dropping whole ``_SHRINKABLE_CHANNELS`` wasn't enough.

        Returns the set of ``"<channel>.<key>"`` labels actually dropped.
        """
        dropped: set = set()
        for bucket in namespaces.values():
            if not isinstance(bucket, dict):
                continue
            checkpoints = bucket.get("checkpoints")
            if not isinstance(checkpoints, dict):
                continue
            for checkpoint_id, entry in checkpoints.items():
                encoded = entry.get("checkpoint")
                if not isinstance(encoded, dict):
                    continue
                try:
                    checkpoint = _SerializedValue(**encoded).decode(self.serde)
                    channel_values = checkpoint.get("channel_values")
                    if not isinstance(channel_values, dict):
                        continue
                    local_dropped = False
                    for channel_name in self._PROTECTED_DICT_CHANNELS:
                        sub = channel_values.get(channel_name)
                        if not isinstance(sub, dict):
                            continue
                        for key, value in list(sub.items()):
                            try:
                                size = len(
                                    json.dumps(
                                        value, ensure_ascii=False, default=str
                                    ).encode("utf-8")
                                )
                            except Exception:
                                continue
                            if size > self._MAX_PROTECTED_SUBKEY_BYTES:
                                sub[key] = None
                                dropped.add(f"{channel_name}.{key}")
                                local_dropped = True
                    if local_dropped:
                        entry["checkpoint"] = asdict(
                            _SerializedValue.encode(self.serde, checkpoint)
                        )
                except Exception:
                    logger.debug(
                        "Protected sub-key shrink failed for checkpoint %s (non-blocking)",
                        checkpoint_id,
                        exc_info=True,
                    )
                    continue
        return dropped

    def _shrink_oversized_checkpoints(self, namespaces: Dict[str, Any]) -> set:
        """Drop known-safe, high-volume channels from the checkpoint(s) still
        held after steps 1-4, in place, to avoid the destructive full-wipe
        fallback. Decodes/re-encodes via ``self.serde`` since the checkpoint is
        stored as an opaque base64 blob (``_SerializedValue``), not raw JSON.

        Returns the set of channel names actually dropped (empty if nothing
        could be shrunk, e.g. unexpected checkpoint shape — caller then falls
        through to the last-resort wipe).
        """
        dropped: set = set()
        for bucket in namespaces.values():
            if not isinstance(bucket, dict):
                continue
            checkpoints = bucket.get("checkpoints")
            if not isinstance(checkpoints, dict):
                continue
            for checkpoint_id, entry in checkpoints.items():
                encoded = entry.get("checkpoint")
                if not isinstance(encoded, dict):
                    continue
                try:
                    checkpoint = _SerializedValue(**encoded).decode(self.serde)
                    channel_values = checkpoint.get("channel_values")
                    if not isinstance(channel_values, dict):
                        continue
                    local_dropped = False
                    for channel in self._SHRINKABLE_CHANNELS:
                        if channel in channel_values:
                            del channel_values[channel]
                            dropped.add(channel)
                            local_dropped = True
                    if local_dropped:
                        entry["checkpoint"] = asdict(
                            _SerializedValue.encode(self.serde, checkpoint)
                        )
                except Exception:
                    # Never let a shrink attempt break persistence — worst case
                    # the caller falls through to the last-resort full wipe.
                    logger.debug(
                        "Channel shrink failed for checkpoint %s (non-blocking)",
                        checkpoint_id,
                        exc_info=True,
                    )
                    continue
        return dropped

    def _keep_only_durable_channels(self, namespaces: Dict[str, Any]) -> set:
        """Tier-3 defense, run only when tiers 1-2 weren't enough and the sole
        remaining alternative is the destructive full wipe.

        Strips every channel that is NOT durable from the retained
        checkpoint(s), keeping the conversation resumable. The durable set is
        read from ``core/state_profile.py`` — the SAME single source the
        cleanup nodes use to decide what survives a turn — rather than a
        hand-maintained copy here, which would inevitably drift (a lesson
        already paid for in this codebase with parallel slot tables).

        LangGraph's own bookkeeping channels (``__start__``, ``branch:...``)
        are NEVER dropped: they are not application state, they are what makes
        the checkpoint loadable at all.

        Returns the set of channel names actually dropped (empty if nothing
        could be dropped — caller then falls through to the full wipe).
        """
        # Import local : `state_profile` ne dépend que de la stdlib, mais le
        # faire ici évite toute contrainte d'ordre d'import entre le paquet
        # `workspace` et le paquet `graphs` (qui importe déjà
        # `workspace.context_guard`).
        from ladini.graphs.agents.market_coach.core.state_profile import (
            _DURABLE_FIELDS,
        )

        dropped: set = set()
        for bucket in namespaces.values():
            if not isinstance(bucket, dict):
                continue
            checkpoints = bucket.get("checkpoints")
            if not isinstance(checkpoints, dict):
                continue
            for checkpoint_id, entry in checkpoints.items():
                encoded = entry.get("checkpoint")
                if not isinstance(encoded, dict):
                    continue
                try:
                    checkpoint = _SerializedValue(**encoded).decode(self.serde)
                    channel_values = checkpoint.get("channel_values")
                    if not isinstance(channel_values, dict):
                        continue
                    local_dropped = False
                    for channel in list(channel_values.keys()):
                        name = str(channel)
                        # Canaux internes LangGraph : jamais de l'état métier,
                        # indispensables au rechargement du checkpoint.
                        if name.startswith("__") or ":" in name:
                            continue
                        if name in _DURABLE_FIELDS:
                            continue
                        del channel_values[channel]
                        dropped.add(name)
                        local_dropped = True
                    if local_dropped:
                        entry["checkpoint"] = asdict(
                            _SerializedValue.encode(self.serde, checkpoint)
                        )
                except Exception:
                    # Never let this attempt break persistence — worst case the
                    # caller falls through to the last-resort full wipe.
                    logger.debug(
                        "Durable-only shrink failed for checkpoint %s (non-blocking)",
                        checkpoint_id,
                        exc_info=True,
                    )
                    continue
        return dropped

    def _describe_channel_sizes(
        self, namespaces: Dict[str, Any], top_n: int = 12
    ) -> list:
        """Decode the retained checkpoint(s) and return '<channel>: <bytes>'
        lines sorted largest-first — diagnostic-only, called right before the
        last-resort full wipe to identify which PROTECTED channel (one that
        `_shrink_oversized_checkpoints` never touches) is actually oversized.
        """
        lines: list = []
        for bucket in namespaces.values():
            if not isinstance(bucket, dict):
                continue
            checkpoints = bucket.get("checkpoints")
            if not isinstance(checkpoints, dict):
                continue
            for checkpoint_id, entry in checkpoints.items():
                encoded = entry.get("checkpoint")
                if not isinstance(encoded, dict):
                    continue
                checkpoint = _SerializedValue(**encoded).decode(self.serde)
                channel_values = checkpoint.get("channel_values")
                if not isinstance(channel_values, dict):
                    continue
                sizes = []
                for channel, value in channel_values.items():
                    try:
                        size = len(
                            json.dumps(value, ensure_ascii=False, default=str).encode(
                                "utf-8"
                            )
                        )
                    except Exception:
                        size = -1
                    sizes.append((channel, size))
                sizes.sort(key=lambda pair: pair[1], reverse=True)
                for channel, size in sizes[:top_n]:
                    lines.append(
                        f"checkpoint={checkpoint_id} channel={channel} bytes={size}"
                    )
        return lines

    def _apply_message_windows(
        self,
        trimmed: Dict[str, Any],
        original: Mapping[str, Any],
    ) -> tuple[Dict[str, Any], Dict[str, Dict[str, int]]]:
        """Keep a bounded window of conversational history per known field."""

        windows: Dict[str, Any] = {}
        meta: Dict[str, Dict[str, int]] = {}

        for key in _MESSAGE_KEYS:
            source = original.get(key) if isinstance(original, Mapping) else None
            target = trimmed.get(key)

            data = (
                source
                if isinstance(source, list)
                else (target if isinstance(target, list) else None)
            )
            if not data:
                continue

            total = len(data)
            window = data[-_MAX_MESSAGE_WINDOW:]
            trimmed[key] = window
            windows[key] = window
            meta[key] = {"total": total, "kept": len(window)}

        return windows, meta

    def _build_summary(
        self,
        state: Mapping[str, Any],
        window_meta: Mapping[str, Dict[str, int]],
    ) -> Dict[str, Any]:
        namespaces = state.get("namespaces") if isinstance(state, Mapping) else {}
        summary: Dict[str, Any] = {
            "version": _STATE_VERSION,
            "ts": int(time.time()),
            "namespaces": {},
            "total_checkpoints": 0,
            "window_meta": window_meta,
        }

        if not isinstance(namespaces, Mapping):
            return summary

        total = 0
        for ns, bucket in namespaces.items():
            if not isinstance(bucket, Mapping):
                continue
            checkpoints = bucket.get("checkpoints")
            if not isinstance(checkpoints, Mapping) or not checkpoints:
                summary["namespaces"][ns] = {"latest": None, "count": 0}
                continue
            ids = list(checkpoints.keys())
            latest = max(ids)
            count = len(ids)
            total += count
            summary["namespaces"][ns] = {"latest": latest, "count": count}

        summary["total_checkpoints"] = total
        return summary

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
        # Merge persisted writes with any in-memory cache (cache wins on conflict).
        persisted_writes = bucket.get("writes", {}).get(checkpoint_id, {})
        cache_key = (thread_id, namespace, checkpoint_id)
        cached_writes = self._write_cache.get(cache_key, {})
        writes_bucket = {**persisted_writes, **cached_writes}
        pending_writes = [
            (
                payload["task_id"],
                payload["channel"],
                _SerializedValue(**payload["value"]).decode(self.serde),
            )
            for _, payload in sorted(
                writes_bucket.items(), key=lambda item: item[1].get("index", 0)
            )
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
