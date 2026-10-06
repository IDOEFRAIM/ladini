"""Harnais canonique : exécute le VRAI moteur conversationnel, tour après tour.

Pourquoi ce module existe (audit `docs/CONVERSATIONAL_ENGINE_HARDENING_AUDIT_2026-09-24.md`,
§12.1) : les tests multi-tours historiques réassemblaient à la main une SOUS-chaîne de nœuds
(interpreter -> planner -> memory -> validator -> resolver -> cleanup), en sautant
`cognitive_guard`, `response_strategy`, `final_response`, l'orchestrateur et le
checkpointer. Ils ne pouvaient donc pas voir les bugs situés à ces coutures (ex. un REJECT
terminal réécrit en WAITING_INPUT par `response_strategy`).

Ce harnais ne réimplémente RIEN du pipeline :

    requête canal (WebChat `_run` / tâche Celery `process_agent_task`)
      -> `Orchestrator.handle` réel
      -> graphe LangGraph COMPILÉ réel (`build_graph`, via `GraphFactory`)
      -> `WorkspaceCheckpointer` réel
      -> persistance JSON (store en mémoire, même encodage que `WorkspaceStore`)

Seules les frontières EXTERNES sont doublées, de façon déterministe :
  - LLM : `HarnessLLM` (réponse scriptée par tour, ou erreur si non scripté) ;
  - MCP/DB métier : `HarnessRuntime.call_db` (enregistre `(outil, kwargs)`) ;
  - Redis : `FakeRedis` (claims/idempotence réellement exercés, TTL simulés) ;
  - envoi WhatsApp : `RecordingDispatcher` ;
  - base `agri_workspaces` : `InMemoryWorkspaceStore` (aller-retour JSON réel).

L'observation des nœuds se fait par un enrobage PUREMENT passif de `_safe_node` (le
décorateur que `build_graph` applique déjà à chaque nœud) : il enregistre `(nom, patch)`
sans rien modifier — le graphe exécuté est exactement celui de production.
"""
from __future__ import annotations

import asyncio
import copy
import json
import time
import uuid
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from unittest import mock

from ladini.graphs.agents.market_coach.core.pending_interaction import (
    PendingInteraction,
    get_pending_interaction,
)
from ladini.graphs.agents.market_coach.core.state import resolve_current_goal
from ladini.graphs.agents.market_coach.interpreter.intent import INTENT_CONFIG
from ladini.workspace.metadata import filter_metadata_dict
from ladini.workspace.models import Workspace
from ladini.workspace.store import (
    WorkspaceStore,
    _decode_state_blob,
    _encode_state_blob,
)

# =====================================================================
# Doublures des frontières externes
# =====================================================================


class FakeRedis:
    """Sous-ensemble du client `redis` synchrone utilisé par le code applicatif.

    Sémantique réelle respectée : `SET NX` atomique, TTL (horloge simulée avançable
    via `advance`), `INCR`, `DELETE`, `EXISTS`. Suffisant pour exercer pour de vrai les
    claims d'idempotence (tâche, confirmation de draft, cache LLM)."""

    def __init__(self) -> None:
        self._data: Dict[str, Any] = {}
        self._expires: Dict[str, float] = {}
        self._now_offset = 0.0

    # -- horloge ------------------------------------------------------
    def _now(self) -> float:
        return time.monotonic() + self._now_offset

    def advance(self, seconds: float) -> None:
        self._now_offset += seconds

    def _alive(self, key: str) -> bool:
        exp = self._expires.get(key)
        if exp is not None and exp <= self._now():
            self._data.pop(key, None)
            self._expires.pop(key, None)
        return key in self._data

    # -- API ----------------------------------------------------------
    def set(self, key, value, ex=None, px=None, nx=False, xx=False, **_: Any):
        alive = self._alive(key)
        if nx and alive:
            return None
        if xx and not alive:
            return None
        self._data[key] = value if isinstance(value, str) else str(value)
        if ex is not None:
            self._expires[key] = self._now() + float(ex)
        elif px is not None:
            self._expires[key] = self._now() + float(px) / 1000.0
        else:
            self._expires.pop(key, None)
        return True

    def get(self, key):
        return self._data.get(key) if self._alive(key) else None

    def delete(self, *keys):
        n = 0
        for key in keys:
            if self._alive(key):
                n += 1
            self._data.pop(key, None)
            self._expires.pop(key, None)
        return n

    def exists(self, *keys):
        return sum(1 for k in keys if self._alive(k))

    def incr(self, key):
        value = int(self.get(key) or 0) + 1
        self._data[key] = str(value)
        return value

    def expire(self, key, ttl):
        if self._alive(key):
            self._expires[key] = self._now() + float(ttl)
            return True
        return False

    def ttl(self, key):
        if not self._alive(key):
            return -2
        exp = self._expires.get(key)
        return -1 if exp is None else int(exp - self._now())

    def keys(self, pattern: str = "*"):
        import fnmatch

        return [k for k in list(self._data) if self._alive(k) and fnmatch.fnmatch(k, pattern)]


class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Msg(content)


class _Completion:
    def __init__(self, content: str, model: Optional[str]) -> None:
        self.choices = [_Choice(content)]
        self.model = model


LLMScript = Union[None, Dict[str, Any], str, Callable[[Dict[str, Any]], Any], BaseException]


class HarnessLLM:
    """Client LLM déterministe au format OpenAI (`chat.completions.create`).

    `script` (posé par tour) :
      - dict  -> renvoyé en JSON à CHAQUE appel du tour ;
      - str   -> renvoyé tel quel ;
      - callable(kwargs) -> sa valeur de retour (dict/str) ;
      - exception -> levée (panne LLM) ;
      - None  -> `HarnessLLM.Unscripted` levée : un tour non scripté qui appelle
        quand même le LLM échoue de façon VISIBLE (TECHNICAL_FAILURE côté moteur).
    """

    class Unscripted(RuntimeError):
        pass

    def __init__(self) -> None:
        self.script: LLMScript = None
        self.calls: List[Dict[str, Any]] = []

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kwargs: Any) -> _Completion:
        self.calls.append(kwargs)
        script = self.script
        if script is None:
            raise HarnessLLM.Unscripted("appel LLM non scripté pour ce tour")
        if isinstance(script, BaseException):
            raise script
        payload = script(kwargs) if callable(script) else script
        content = payload if isinstance(payload, str) else json.dumps(payload)
        return _Completion(content, model=kwargs.get("model"))


McpResponse = Union[Dict[str, Any], Callable[..., Any], BaseException]


class HarnessRuntime:
    """Doublure de `MarketRuntime` : même surface que celle réellement utilisée par le
    graphe (`call_db`, `llm_gateway`, `profile_answer`, `model_answer`, `llm`,
    `bind_user`, `set_current_goal`), utilisable en `async with` comme
    `build_runtime()` en production."""

    def __init__(self, *, role: str, profile: Optional[Dict[str, Any]] = None) -> None:
        self.llm = HarnessLLM()
        self.model_answer = "harness-model"
        self.current_goal: Optional[str] = None
        self.bound_user: Optional[str] = None
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self.responses: Dict[str, McpResponse] = {}
        self._profile = profile or {
            "id": "11111111-1111-1111-1111-111111111111",
            "name": "Awa",
            "role": role,
            "zone": {"id": "zone-1", "name": "Ouagadougou"},
        }
        self._defaults: Dict[str, McpResponse] = {
            "get_user_by_phone": lambda **_: {"status": "SUCCESS", "data": dict(self._profile)},
            "get_account_status": {"status": "success", "data": {"account_status": "ACTIVE"}},
            "get_prohibited_terms": {"status": "success", "data": {"terms": []}},
            # Le vrai service rend la version du besoin après une mutation (B26) : les chaînes de mutations en dépendent.
            "update_recurring_need": {"status": "success", "outcome": "APPLIED", "need_version": 2000},
        }

    # -- cycle de vie -------------------------------------------------
    async def __aenter__(self) -> "HarnessRuntime":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    def bind_user(self, phone: str) -> None:
        self.bound_user = phone

    def set_current_goal(self, goal: Optional[str]) -> None:
        self.current_goal = goal

    # -- LLM ----------------------------------------------------------
    @property
    def profile_answer(self) -> Any:
        from ladini.graphs.agents.market_coach.llm_gateway.types import LLMProfile

        return LLMProfile.REASONING

    @property
    def llm_gateway(self) -> Any:
        from ladini.graphs.agents.market_coach.llm_gateway import LegacyOverrideGateway

        return LegacyOverrideGateway(self.llm, lambda: self.model_answer)

    # -- MCP ----------------------------------------------------------
    async def call_db(self, tool_name: str, **kwargs: Any) -> Any:
        # Un vrai appel MCP/DB suspend la coroutine : sans ce point de suspension, deux
        # tours concurrents ne pourraient jamais s'entrelacer dans le harnais.
        await asyncio.sleep(0)
        self.calls.append((tool_name, copy.deepcopy(kwargs)))
        configured = self.responses.get(tool_name, self._defaults.get(tool_name))
        if configured is None:
            return {"status": "success", "data": {}, "message": f"{tool_name} ok"}
        if isinstance(configured, BaseException):
            raise configured
        if callable(configured):
            return configured(**kwargs)
        return copy.deepcopy(configured)


class InMemoryWorkspaceStore(WorkspaceStore):
    """`WorkspaceStore` sans PostgreSQL mais avec le MÊME encodage que la vraie table :
    métadonnées filtrées (`filter_metadata_dict`) puis JSON, état LangGraph encodé par
    `_encode_state_blob` (compression incluse) puis décodé par `_decode_state_blob`.
    Un tour suivant relit donc exactement ce qu'il relirait en production (tuples ->
    listes, objets non sérialisables refusés...)."""

    def __init__(self) -> None:  # pas d'appel au parent : aucune connexion DB
        self.rows: Dict[str, Dict[str, Any]] = {}
        self.save_count = 0
        self.fail_saves = False

    async def get(self, workspace_id: str) -> Optional[Workspace]:
        row = self.rows.get(workspace_id)
        if row is None:
            return None
        data = dict(row)
        data["metadata"] = filter_metadata_dict(json.loads(row["metadata"]))
        data["langgraph_state"] = _decode_state_blob(json.loads(row["langgraph_state"]))
        return Workspace.from_dict(data)

    async def save(self, workspace: Workspace) -> bool:
        if self.fail_saves:
            return False
        workspace.touch()
        state_json, _metrics = _encode_state_blob(workspace.agent_state or {})
        self.rows[workspace.workspace_id] = {
            "workspace_id": workspace.workspace_id,
            "workspace_type": workspace.workspace_type,
            "active_goal": workspace.active_goal,
            "active_form": workspace.active_form,
            "locked_agent": workspace.locked_agent,
            "metadata": json.dumps(filter_metadata_dict(workspace.metadata)),
            "langgraph_state": state_json,
            "updated_at": workspace.updated_at,
        }
        self.save_count += 1
        return True

    async def _reset_workspace_row(self, workspace_id: str, workspace_type: str = "producer") -> bool:
        self.rows.pop(workspace_id, None)
        return True


class RecordingDispatcher:
    """Remplace `ResponseDispatcher` : enregistre ce qui AURAIT été envoyé.
    `fail_next` simule une panne d'envoi (pour les tests de retry)."""

    def __init__(self) -> None:
        self.sent: List[Tuple[str, Any]] = []
        self.fail_next = 0

    async def dispatch(self, phone: str, plan: Any) -> List[Dict[str, Any]]:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise ConnectionError("dispatch WhatsApp simulé en échec")
        self.sent.append((phone, plan))
        return [{"status": "message_sent"}]


# =====================================================================
# Résultat d'un tour
# =====================================================================

_END_OF_TURN_NODES = frozenset({"state_cleaner", "final_response", "post_response_cleanup"})


def _tunnel_of(goal: Optional[str]) -> Optional[str]:
    if not goal:
        return None
    return (INTENT_CONFIG.get(str(goal).upper()) or {}).get("tunnel")


@dataclass
class TurnResult:
    """Tout ce qu'un test doit pouvoir inspecter sur un tour — sans relire le graphe."""

    text: str
    channel: str
    message_id: Optional[str]
    before: Dict[str, Any]
    after: Dict[str, Any]
    nodes: List[Tuple[str, Dict[str, Any]]]
    mcp_calls: List[Tuple[str, Dict[str, Any]]]
    llm_calls: int
    response: str
    interactive: Optional[Dict[str, Any]] = None
    dispatched: List[Any] = field(default_factory=list)
    error: Optional[BaseException] = None

    # -- chemin -------------------------------------------------------
    @property
    def visited(self) -> List[str]:
        return [name for name, _ in self.nodes]

    def patch_of(self, node: str) -> Dict[str, Any]:
        """Dernier patch renvoyé par `node` pendant ce tour (`{}` si non visité)."""
        for name, patch in reversed(self.nodes):
            if name == node:
                return patch
        return {}

    def turn_value(self, key: str) -> Any:
        """Dernière valeur écrite pour `key` AVANT le nettoyage de fin de tour — la valeur
        « du tour », que `post_response_cleanup` efface ensuite (event, intent...)."""
        value = None
        for name, patch in self.nodes:
            if name in _END_OF_TURN_NODES or not isinstance(patch, dict):
                continue
            if key in patch:
                value = patch[key]
        return value

    # -- interprétation / décision ------------------------------------
    @property
    def interpretation(self) -> Dict[str, Any]:
        return self.patch_of("input_interpreter")

    @property
    def event(self) -> Optional[str]:
        return self.turn_value("interpreted_event")

    @property
    def intent(self) -> Optional[str]:
        return self.turn_value("detected_intent")

    @property
    def decision(self) -> Dict[str, Any]:
        return dict(self.turn_value("cognitive_decision") or {})

    @property
    def strategy(self) -> Optional[str]:
        return self.turn_value("response_strategy")

    @property
    def turn_status(self) -> Optional[str]:
        return self.turn_value("status")

    # -- goal / tunnel ------------------------------------------------
    @property
    def goal_before(self) -> Optional[str]:
        return resolve_current_goal(self.before)

    @property
    def goal_after(self) -> Optional[str]:
        return resolve_current_goal(self.after)

    @property
    def tunnel_before(self) -> Optional[str]:
        return _tunnel_of(self.goal_before)

    @property
    def tunnel_after(self) -> Optional[str]:
        return _tunnel_of(self.goal_after)

    # -- attente / drafts ---------------------------------------------
    @property
    def pending_before(self) -> PendingInteraction:
        return get_pending_interaction(self.before)

    @property
    def pending_after(self) -> PendingInteraction:
        return get_pending_interaction(self.after)

    def draft(self, name: str = "recurring_need_draft") -> Optional[Dict[str, Any]]:
        value = self.after.get(name)
        return value if isinstance(value, dict) and value else None

    @property
    def transaction_payload(self) -> Dict[str, Any]:
        return dict(self.after.get("transaction_payload") or {})

    def mcp_tools(self) -> List[str]:
        return [tool for tool, _ in self.mcp_calls]


@dataclass
class ConcurrentResult:
    before: Dict[str, Any]
    after: Dict[str, Any]
    responses: List[Any]
    #: Conservés à titre indicatif seulement (débogage) — PAS la source de `interleaved` (voir
    #: `send_concurrently`, qui explique pourquoi cette métrique par tâche asyncio est un faux
    #: ami : un seul tour séquentiel produit déjà `task_segments > distinct_turns`).
    task_segments: int
    distinct_turns: int
    #: (start, end) en `time.monotonic()` de CHAQUE appel à `Orchestrator.handle` — la source
    #: réelle de `interleaved`, ci-dessous.
    handle_intervals: List[Tuple[float, float]] = field(default_factory=list)
    _interleaved: bool = False

    @property
    def interleaved(self) -> bool:
        """Vrai si deux appels à `Orchestrator.handle` (de la MÊME conversation) se sont
        chevauchés dans le temps — la seule définition sans ambiguïté de "deux tours ont
        muté l'état en même temps". Calculé sur de VRAIS intervalles de `time.monotonic()`,
        pas sur une heuristique d'identité de tâche asyncio (voir `send_concurrently`)."""
        return self._interleaved


# =====================================================================
# Harnais
# =====================================================================


def new_task(intent: str, confidence: float = 0.95, **entities: Any) -> Dict[str, Any]:
    """Sortie du micro-prompt NEW_TASK (`interpreter/new_task_contract.py`)."""
    return {"disposition": "NEW_TASK", "intent": intent, "confidence": confidence, "entities": entities}


_METADATA_ONLY_CHANNELS = frozenset({"__start__"})


class ConversationHarness:
    """Une conversation (un téléphone), plusieurs tours, sur le moteur réel.

    Usage::

        with ConversationHarness(role="BUYER") as conv:
            t1 = conv.send("je veux 14 coqs chaque semaine", llm=new_task(...))
            t2 = conv.send("oui")
            assert t2.draft() is None

    `channel` : "whatsapp" (webhook -> tâche Celery `process_agent_task`, exécutée en
    eager avec retries Celery réels) ou "webchat" (`api/routes/webchat.py::_run`,
    synchrone, dans le process API). Modifiable par tour (`send(..., channel=)`).
    """

    def __init__(
        self,
        *,
        role: str = "BUYER",
        phone: str = "+22670000001",
        channel: str = "whatsapp",
        profile: Optional[Dict[str, Any]] = None,
        store: Optional[InMemoryWorkspaceStore] = None,
        redis: Optional[FakeRedis] = None,
    ) -> None:
        self.role = role.upper()
        self.phone = phone
        self.channel = channel
        self.runtime = HarnessRuntime(role=self.role, profile=profile)
        self.store = store or InMemoryWorkspaceStore()
        self.redis = redis or FakeRedis()
        self.dispatcher = RecordingDispatcher()
        from tests.harness.recurring import (
            InMemoryDraftTable,
            RecurringSupplyServerDouble,
        )

        self.drafts = InMemoryDraftTable()
        self.server = RecurringSupplyServerDouble(self.drafts)
        self.runtime.responses["create_recurring_need"] = self.server.create_recurring_need
        self.runtime.responses["create_recurring_needs"] = self.server.create_recurring_needs
        self.runtime.responses["get_recurring_start_policy"] = self.server.get_recurring_start_policy
        self.turns: List[TurnResult] = []
        self._node_log: List[Tuple[str, Dict[str, Any]]] = []
        self._task_log: List[int] = []
        self._stack: Optional[ExitStack] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.orchestrator: Any = None

    # -- installation -------------------------------------------------
    def __enter__(self) -> "ConversationHarness":
        from ladini.api import tasks as api_tasks
        from ladini.api.celery_app import celery_app
        from ladini.api.routes import webchat as webchat_route
        from ladini.core import idempotency, turn_telemetry
        from ladini.graphs.agents.market_coach.core import graph_builder
        from ladini.orchestrator import orchestrator as orchestrator_module
        from ladini.orchestrator.orchestrator import Orchestrator

        self._loop = asyncio.new_event_loop()
        stack = ExitStack()
        self._stack = stack

        orchestrator = Orchestrator()
        orchestrator.resolver.store = self.store
        orchestrator._checkpointer.store = self.store
        self.orchestrator = orchestrator

        original_safe_node = graph_builder._safe_node
        node_log = self._node_log
        task_log = self._task_log

        def _observed_safe_node(fn, name):
            wrapped = original_safe_node(fn, name)

            async def _observed(state, mc_runtime, **kw):
                out = await wrapped(state, mc_runtime, **kw)
                try:
                    snapshot = copy.deepcopy(out)
                except Exception:  # pragma: no cover - patch non copiable
                    snapshot = dict(out or {})
                node_log.append((name, snapshot if isinstance(snapshot, dict) else {}))
                task = asyncio.current_task()
                task_log.append(id(task) if task is not None else 0)
                return out

            return _observed

        async def _no_persist(_rec):  # télémétrie : jamais de DB en test
            return None

        stack.enter_context(mock.patch.object(graph_builder, "_safe_node", _observed_safe_node))
        stack.enter_context(
            mock.patch.object(orchestrator_module, "build_runtime", lambda *a, **k: self.runtime)
        )
        stack.enter_context(mock.patch.object(idempotency, "_client", self.redis))
        stack.enter_context(mock.patch.object(api_tasks, "get_dispatcher", lambda: self.dispatcher))
        stack.enter_context(mock.patch.object(api_tasks, "_loop", self._loop))
        stack.enter_context(mock.patch.object(api_tasks, "_orchestrator", orchestrator))
        stack.enter_context(mock.patch.object(webchat_route, "_orchestrator", orchestrator))
        stack.enter_context(mock.patch.object(turn_telemetry, "finish_and_persist", _no_persist))
        from tests.harness.recurring import install_draft_table

        install_draft_table(
            self.drafts, lambda obj, name, value: stack.enter_context(mock.patch.object(obj, name, value))
        )
        # Celery en mode eager : `.apply()` exécute la tâche dans ce process et ses
        # `autoretry_for` rejouent RÉELLEMENT la tâche (sans broker, sans countdown).
        previous_eager = celery_app.conf.task_always_eager
        celery_app.conf.task_always_eager = True
        stack.callback(setattr, celery_app.conf, "task_always_eager", previous_eager)
        return self

    def __exit__(self, *exc: Any) -> bool:
        if self._stack is not None:
            self._stack.close()
        if self._loop is not None:
            self._loop.close()
        return False

    # -- état persisté --------------------------------------------------
    def _run(self, coro):
        assert self._loop is not None, "utiliser ConversationHarness comme context manager"
        return self._loop.run_until_complete(coro)

    def state(self) -> Dict[str, Any]:
        """État RÉELLEMENT relu par le tour suivant (dernier checkpoint persisté,
        après aller-retour JSON du store)."""
        tup = self._run(
            self.orchestrator._checkpointer.aget_tuple({"configurable": {"thread_id": self.phone}})
        )
        if tup is None:
            return {}
        values = dict(tup.checkpoint.get("channel_values") or {})
        for key in _METADATA_ONLY_CHANNELS:
            values.pop(key, None)
        return values

    def seed(self, values: Dict[str, Any]) -> Dict[str, Any]:
        """Pose un état initial (appliqué avec les reducers officiels du graphe) — pour
        reproduire un état hérité (ex. vieille interaction catalogue encore active)."""
        graph = self.orchestrator._graph_factory.get_graph(
            self.role, mc_runtime=self.runtime, checkpointer=self.orchestrator._checkpointer
        )
        config = {"configurable": {"thread_id": self.phone}}
        self._run(graph.aupdate_state(config, values, as_node="post_response_cleanup"))
        ws = self._run(self.store.get(self.phone))
        if ws is not None:
            ws.workspace_type = "buyer" if self.role == "BUYER" else "producer"
            self._run(self.store.save(ws))
        return self.state()

    # -- un tour ------------------------------------------------------
    def send(
        self,
        text: str,
        *,
        llm: LLMScript = None,
        channel: Optional[str] = None,
        message_id: Optional[str] = None,
        interactive_id: Optional[str] = None,
    ) -> TurnResult:
        channel = (channel or self.channel).lower()
        before = self.state()
        self.runtime.llm.script = llm
        llm_calls_before = len(self.runtime.llm.calls)
        mcp_before = len(self.runtime.calls)
        sent_before = len(self.dispatcher.sent)
        self._node_log.clear()
        self._task_log.clear()

        response, interactive, error = "", None, None
        if channel == "webchat":
            response, interactive, error = self._send_webchat(text)
        elif channel == "whatsapp":
            message_id = message_id or f"wamid.{uuid.uuid4().hex}"
            response, interactive, error = self._send_whatsapp(text, message_id, interactive_id)
        else:  # pragma: no cover - usage invalide
            raise ValueError(f"canal inconnu : {channel}")

        result = TurnResult(
            text=text,
            channel=channel,
            message_id=message_id,
            before=before,
            after=self.state(),
            nodes=list(self._node_log),
            mcp_calls=list(self.runtime.calls[mcp_before:]),
            llm_calls=len(self.runtime.llm.calls) - llm_calls_before,
            response=response,
            interactive=interactive,
            dispatched=list(self.dispatcher.sent[sent_before:]),
            error=error,
        )
        self.runtime.llm.script = None
        self.turns.append(result)
        return result

    def send_concurrently(
        self, messages: List[Tuple[str, LLMScript]], *, channel: str = "webchat"
    ) -> "ConcurrentResult":
        """Envoie plusieurs messages de la MÊME conversation « en même temps » (même
        boucle, `asyncio.gather`) — comme deux requêtes webchat simultanées, ou deux
        workers Celery. Le script LLM est choisi par texte du message.

        Détection de l'entrelacement (Phase 2 hardening, commit 9) : par INTERVALLES DE
        TEMPS réels autour de la SECTION CRITIQUE de `core/conversation_lock.py::
        conversation_turn_lock` (entre l'acquisition et la libération du verrou) — PAS
        autour de l'appel `Orchestrator.handle` lui-même (un 2ᵉ appel EN ATTENTE du verrou
        est déjà "en vol" pendant que le 1er tourne — ce n'est pas un chevauchement du
        TRAVAIL réel), ni par identité de tâche asyncio (`id(asyncio.current_task())` : faux
        ami vérifié empiriquement — l'exécuteur LangGraph fait déjà tourner les nœuds d'UN
        SEUL tour séquentiel sur plusieurs tâches asyncio internes différentes, donnant un
        faux positif permanent indépendant de toute vraie concurrence)."""
        from ladini.api.routes.webchat import WebChatRequest, _run
        from ladini.orchestrator import orchestrator as orchestrator_module

        assert channel == "webchat", "seul le canal synchrone peut être rejoué en parallèle ici"
        scripts = {text: script for text, script in messages}

        def _by_message(kwargs: Dict[str, Any]) -> Any:
            prompt = json.dumps(kwargs.get("messages") or [], ensure_ascii=False)
            for text, script in scripts.items():
                if text in prompt:
                    payload = script(kwargs) if callable(script) else script
                    if payload is None:
                        raise HarnessLLM.Unscripted(text)
                    return payload
            raise HarnessLLM.Unscripted("aucun message reconnu dans le prompt")

        before = self.state()
        self.runtime.llm.script = _by_message
        self._node_log.clear()
        self._task_log.clear()
        workspace_type = "buyer" if self.role == "BUYER" else "producer"

        intervals: List[Tuple[float, float]] = []
        original_lock = orchestrator_module.conversation_turn_lock

        @asynccontextmanager
        async def _timed_lock(conversation_id: str, *, timeout_seconds: float):
            async with original_lock(conversation_id, timeout_seconds=timeout_seconds) as acquired:
                start = time.monotonic()
                try:
                    yield acquired
                finally:
                    intervals.append((start, time.monotonic()))

        async def _all():
            with mock.patch.object(orchestrator_module, "conversation_turn_lock", _timed_lock):
                return await asyncio.gather(
                    *[
                        _run(WebChatRequest(message=text, phone_number=self.phone), workspace_type)
                        for text, _ in messages
                    ],
                    return_exceptions=True,
                )

        outs = self._run(_all())
        self.runtime.llm.script = None
        intervals.sort()
        interleaved = any(
            intervals[i][1] > intervals[i + 1][0] for i in range(len(intervals) - 1)
        )
        compressed: List[int] = []
        for task_id in self._task_log:
            if not compressed or compressed[-1] != task_id:
                compressed.append(task_id)
        return ConcurrentResult(
            before=before,
            after=self.state(),
            responses=[o.reply if not isinstance(o, BaseException) else o for o in outs],
            task_segments=len(compressed),
            distinct_turns=len(set(self._task_log)),
            handle_intervals=intervals,
            _interleaved=interleaved,
        )

    def _send_webchat(self, text: str):
        from ladini.api.routes.webchat import WebChatRequest, _run

        request = WebChatRequest(message=text, phone_number=self.phone)
        workspace_type = "buyer" if self.role == "BUYER" else "producer"
        try:
            out = self._run(_run(request, workspace_type))
        except Exception as exc:  # pragma: no cover - remonté au test via TurnResult
            return "", None, exc
        return out.reply, out.interactive, None

    def _send_whatsapp(self, text: str, message_id: str, interactive_id: Optional[str]):
        """Même décision de rôle que `api/routes/whatsapp_webhook.py` §5 : workspace
        existant -> son type, forcé ; sinon aucun rôle transmis."""
        from ladini.api.tasks import process_agent_task

        ws = self._run(self.store.get(self.phone))
        kwargs: Dict[str, Any] = {
            "phone_number": self.phone,
            "user_query": text,
            "interactive_id": interactive_id,
            "message_sid": message_id,
        }
        if ws is not None:
            kwargs["workspace_type"] = ws.workspace_type
            kwargs["force_role"] = True
        eager = process_agent_task.apply(kwargs=kwargs)
        if eager.failed():
            return "", None, eager.result
        sent = self.dispatcher.sent[-1][1] if self.dispatcher.sent else None
        text_out = ""
        interactive = None
        if sent is not None:
            items = getattr(sent, "items", None) or []
            first = items[0] if items else None
            text_out = getattr(first, "text", "") or ""
            interactive = getattr(first, "interactive", None)
        return text_out, interactive, None


__all__ = [
    "ConcurrentResult",
    "ConversationHarness",
    "FakeRedis",
    "HarnessLLM",
    "HarnessRuntime",
    "InMemoryWorkspaceStore",
    "RecordingDispatcher",
    "TurnResult",
    "new_task",
]
