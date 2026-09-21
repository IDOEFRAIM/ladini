"""Télémétrie d'un TOUR de l'agent — alimente `intelligence.agent_turns` / `agent_tool_calls` / `agent_llm_calls`.

PRINCIPE : **observabilité = best effort, métier = prioritaire.**
Rien ici ne peut lever vers l'appelant, bloquer un tour, ni changer un comportement métier :

  * chaque hook est enveloppé (`try/except` → no-op) et sans effet hors d'un tour actif ;
  * l'écriture en base a lieu APRÈS l'envoi de la réponse, sous timeout court, exceptions avalées et comptées ;
  * aucun argument SQL, aucun payload d'outil, aucun prompt/réponse LLM, aucun numéro en clair n'est capté.

Mécanisme d'écriture (compromis) : écriture EN LIGNE après le dispatch plutôt qu'un `create_task()` incontrôlé (perdu à
l'arrêt du worker Celery) ou un bus dédié (Redis Stream + consumer = nouvelle infrastructure à opérer). Coût : quelques
ms de temps worker, jamais de latence utilisateur. Limite assumée : un crash du worker entre l'envoi et l'écriture perd
le tour ; `write_failures()` rend les pertes observables.

Schéma : Drizzle (frontend) est la source de vérité ; ce module n'exécute AUCUN DDL.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import threading
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("ladini.turn_telemetry")

ENQUEUED_AT_HEADER = "ladini_enqueued_at"
# Tâches dont la publication est horodatée (temps d'attente en file) — extensible (les tests y ajoutent la leur).
TRACKED_TASKS: set[str] = {"ladini.api.tasks.process_agent_task"}
_EXCERPT_MAX = 280

_current: ContextVar[Optional["TurnRecorder"]] = ContextVar("ladini_turn_recorder", default=None)
_write_failures = 0
_write_successes = 0
_patched_redis = False
_patch_lock = threading.Lock()


# ── Confidentialité ─────────────────────────────────────────────────────────

_PHONE_RE = re.compile(r"(?<![\w])(?:\+|00)?\d[\d .\-]{6,}\d(?![\w])")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_TOKEN_RE = re.compile(r"\b(?=[A-Za-z0-9_\-]*\d)(?=[A-Za-z0-9_\-]*[A-Za-z])[A-Za-z0-9_\-]{24,}\b")
_OTP_GOAL_RE = re.compile(r"OTP|VERIFY_DELIVERY|PASSWORD|MOT_DE_PASSE", re.I)


def redact_text(text: Optional[str], *, goal: Optional[str] = None) -> Optional[str]:
    """Extrait affichable d'un message : numéros, emails et jetons masqués, tronqué. `None` si le contexte est sensible
    (code OTP de livraison, mot de passe) — on ne garde alors RIEN du contenu."""
    if not text:
        return None
    if goal and _OTP_GOAL_RE.search(goal):
        return "[contenu masqué]"
    out = _EMAIL_RE.sub("[email]", text)
    out = _PHONE_RE.sub("[tel]", out)
    out = _TOKEN_RE.sub("[jeton]", out)
    out = " ".join(out.split())
    return out[:_EXCERPT_MAX] + ("…" if len(out) > _EXCERPT_MAX else "")


def _pepper() -> bytes:
    from ladini.core.settings import settings

    pepper = str(getattr(settings, "AGENT_MONITORING_PHONE_PEPPER", "") or "")
    if not pepper:
        # Repli : dérivé d'un secret d'infrastructure déjà présent. Sans poivre, un HMAC de numéro (espace de
        # numérotation réduit) serait trivialement inversible — d'où l'obligation d'un secret, jamais d'un hash nu.
        pepper = str(getattr(settings, "DATABASE_URL", "") or "ladini-monitoring")
    return pepper.encode()


def hash_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return hmac.new(_pepper(), digits.encode(), hashlib.sha256).hexdigest()


def phone_last4(phone: str) -> Optional[str]:
    digits = re.sub(r"\D", "", phone or "")
    return digits[-4:] if len(digits) >= 4 else None


# ── Enregistreur ────────────────────────────────────────────────────────────

@dataclass
class ToolCallRec:
    seq: int
    tool_name: str
    tool_category: str
    started_at: datetime
    completed_at: datetime
    duration_ms: int
    status: str
    error_code: Optional[str] = None
    error_category: Optional[str] = None


@dataclass
class LlmCallRec:
    seq: int
    kind: str
    provider: Optional[str]
    model: Optional[str]
    duration_ms: int
    status: str
    is_fallback: bool = False
    fallback_from: Optional[str] = None
    error_category: Optional[str] = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    source: str = "gateway"  # 'gateway' | 'adapter' (voir note_llm : évite le double comptage)
    _t_end: float = field(default=0.0, repr=False)


@dataclass
class TurnRecorder:
    phone: str
    message_sid: Optional[str] = None
    task_retries: int = 0
    channel: str = "WHATSAPP"
    user_message: Optional[str] = None
    enqueued_at: Optional[float] = None

    turn_id: uuid.UUID = field(default_factory=uuid.uuid4)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    _t0: float = field(default_factory=time.perf_counter)
    queue_duration_ms: Optional[int] = None

    sql_count: int = 0
    sql_ms: float = 0.0
    sql_max_ms: float = 0.0
    redis_count: int = 0
    redis_ms: float = 0.0
    tools: List[ToolCallRec] = field(default_factory=list)
    llms: List[LlmCallRec] = field(default_factory=list)
    whatsapp_ms: Optional[int] = None
    response_status: str = "SKIPPED"
    response_text: Optional[str] = None

    # Issue métier (posée par l'orchestrateur)
    intent: Optional[str] = None
    intent_confidence: Optional[float] = None
    workflow: Optional[str] = None
    workflow_step: Optional[str] = None
    goal_status: Optional[str] = None
    outcome: str = "COMPLETED"
    user_id: Optional[str] = None
    user_role: Optional[str] = None
    error_code: Optional[str] = None
    error_category: Optional[str] = None
    trace_id: Optional[str] = None

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        if self.enqueued_at:
            self.queue_duration_ms = max(0, int((time.time() - self.enqueued_at) * 1000))

    # -- hooks (appelés depuis n'importe quel thread/tâche du tour) ------------
    def add_sql(self, ms: float) -> None:
        with self._lock:
            self.sql_count += 1
            self.sql_ms += ms
            self.sql_max_ms = max(self.sql_max_ms, ms)

    def add_redis(self, ms: float) -> None:
        with self._lock:
            self.redis_count += 1
            self.redis_ms += ms

    def add_tool(self, rec: ToolCallRec) -> None:
        with self._lock:
            rec.seq = len(self.tools) + 1
            self.tools.append(rec)

    # -- agrégats --------------------------------------------------------------
    @property
    def mcp_ms(self) -> int:
        return sum(t.duration_ms for t in self.tools)

    def llm_ms(self, kind: Optional[str] = None) -> int:
        return sum(c.duration_ms for c in self.llms if kind is None or c.kind == kind)


def begin_turn(rec: TurnRecorder) -> None:
    _current.set(rec)


def end_turn() -> None:
    _current.set(None)


def current() -> Optional[TurnRecorder]:
    return _current.get()


# ── Hooks best-effort (jamais d'exception vers l'appelant) ─────────────────

def note_final(final: Any) -> None:
    """Issue métier d'un tour, lue dans l'état final du graphe (champs standards de l'état de l'agent)."""
    rec = _current.get()
    if rec is None or not isinstance(final, dict):
        return
    try:
        pending = final.get("pending_interaction")
        step = None
        if isinstance(pending, dict):
            step = pending.get("kind") or pending.get("type")
        rec.intent = _s(final.get("detected_intent")) or rec.intent
        conf = final.get("interpreter_confidence")
        rec.intent_confidence = float(conf) if isinstance(conf, (int, float)) else rec.intent_confidence
        rec.workflow = _s(final.get("current_goal")) or _s(final.get("last_terminated_goal")) or rec.workflow
        rec.workflow_step = _s(step) or rec.workflow_step
        rec.goal_status = _s(final.get("goal_status")) or rec.goal_status
        # Même règle que l'orchestrateur pour fermer un tunnel (`_sync_workspace`) : le nettoyeur remet `goal_status` à
        # None quand le but est terminé — on le reconstitue pour que le cockpit sache qu'un workflow a abouti.
        if str(final.get("goal_status") or "").upper() == "COMPLETED" or str(final.get("status") or "").upper() in {"COMPLETED", "SUCCESS"}:
            rec.goal_status = "COMPLETED"
        rec.user_id = _s(final.get("user_id")) or rec.user_id
        rec.user_role = _s(final.get("user_role")) or rec.user_role
        rec.outcome = derive_outcome(final)
    except Exception:  # pragma: no cover - défensif
        logger.debug("note_final ignoré", exc_info=True)


def note_error(code: str, category: str, *, outcome: str = "ERROR") -> None:
    rec = _current.get()
    if rec is None:
        return
    rec.error_code = rec.error_code or code
    rec.error_category = rec.error_category or category
    rec.outcome = outcome


def note_tool(name: str, category: str, started_at: datetime, duration_s: float, status: str,
              error: Optional[BaseException] = None) -> None:
    rec = _current.get()
    if rec is None:
        return
    try:
        ms = int(duration_s * 1000)
        rec.add_tool(ToolCallRec(
            seq=0, tool_name=name, tool_category=category, started_at=started_at,
            completed_at=datetime.now(timezone.utc), duration_ms=ms, status=status,
            error_code=type(error).__name__ if error is not None else None,
            error_category=("SECURITY" if status == "DENIED" else "TOOL") if error is not None else None,
        ))
    except Exception:  # pragma: no cover
        logger.debug("note_tool ignoré", exc_info=True)


def note_llm(*, name: str, model: Optional[str], provider: Optional[str], profile: Optional[str],
             agent_node: Optional[str], latency_s: float, usage: Optional[Dict[str, Any]], error: Optional[str],
             fallback_from: Optional[str]) -> None:
    """Un appel LLM. Le LLM Gateway ET les adaptateurs Groq/Bedrock émettent chacun un événement pour le MÊME appel :
    quand un événement `gateway` arrive, on remplace l'événement `adapter` qui le précède (même modèle, fin proche)."""
    rec = _current.get()
    if rec is None:
        return
    try:
        now = time.perf_counter()
        source = "gateway" if name == "llm_gateway_completion" else "adapter"
        prof = (profile or "").upper()
        node = (agent_node or "").lower()
        if prof == "INTERPRETER" or "interpret" in node:
            kind = "INTERPRETER"
        elif source == "gateway" and prof == "REASONING":
            kind = "RESPONSE"
        else:
            kind = "OTHER"
        call = LlmCallRec(
            seq=0, kind=kind, provider=provider, model=model, duration_ms=int(latency_s * 1000),
            status="ERROR" if error else "SUCCESS", is_fallback=bool(fallback_from), fallback_from=fallback_from,
            error_category="LLM" if error else None,
            prompt_tokens=_i((usage or {}).get("prompt_tokens")), completion_tokens=_i((usage or {}).get("completion_tokens")),
            source=source, _t_end=now,
        )
        with rec._lock:
            if source == "gateway" and rec.llms:
                last = rec.llms[-1]
                if last.source == "adapter" and last.model == model and now - last._t_end <= latency_s + 0.5:
                    rec.llms.pop()
            elif source == "adapter" and rec.llms:
                # doublon inverse improbable ; on garde tel quel
                pass
            call.seq = len(rec.llms) + 1
            rec.llms.append(call)
    except Exception:  # pragma: no cover
        logger.debug("note_llm ignoré", exc_info=True)


def derive_outcome(final: Dict[str, Any]) -> str:
    """Issue d'un tour d'après l'état final (valeurs de l'état LangGraph de l'agent)."""
    sec = str(final.get("security_status") or "").upper()
    if sec in {"BLOCKED", "ACCOUNT_BLOCKED", "PROHIBITED_PRODUCT", "PROMPT_INJECTION_DETECTED", "SCAM_DETECTED"}:
        return "BLOCKED"
    if final.get("requires_human"):
        return "HUMAN_REQUIRED"
    status = str(final.get("status") or "").upper()
    strategy = str(final.get("response_strategy") or "").upper()
    goal_status = str(final.get("goal_status") or "").upper()
    if status == "ERROR" or strategy == "ERROR":
        return "ERROR"
    if status == "BLOCKED":
        return "BLOCKED"
    if strategy == "CLARIFICATION" or final.get("clarification_reasons"):
        return "CLARIFICATION"
    if status in {"WAITING_INPUT", "WAITING_CONFIRMATION"} or goal_status in {"WAITING_INPUT", "WAITING_CONFIRMATION"}:
        return "WAITING_USER"
    return "COMPLETED"


def _s(v: Any) -> Optional[str]:
    return str(v) if v not in (None, "") else None


def _i(v: Any) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# ── Instrumentation transversale (SQL, Redis, Celery) ──────────────────────

def attach_sql_listeners(sync_engine: Any) -> None:
    """Compte requêtes / temps SQL du tour. Ne capte JAMAIS le texte de la requête ni ses paramètres."""
    from sqlalchemy import event

    @event.listens_for(sync_engine, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        try:
            if _current.get() is not None:
                context._ladini_t0 = time.perf_counter()
        except Exception:  # pragma: no cover
            pass

    @event.listens_for(sync_engine, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        try:
            t0 = getattr(context, "_ladini_t0", None)
            rec = _current.get()
            if t0 is not None and rec is not None:
                rec.add_sql((time.perf_counter() - t0) * 1000.0)
        except Exception:  # pragma: no cover
            pass


def install_redis_instrumentation() -> None:
    """Un seul wrapper sur `Redis.execute_command` (sync + asyncio) couvre TOUS les clients du dépôt, sans round-trip
    supplémentaire. No-op hors d'un tour."""
    global _patched_redis
    with _patch_lock:
        if _patched_redis:
            return
        try:
            import redis.client as rc

            orig = rc.Redis.execute_command

            def timed(self, *args, **options):  # noqa: ANN001
                rec = _current.get()
                if rec is None:
                    return orig(self, *args, **options)
                t0 = time.perf_counter()
                try:
                    return orig(self, *args, **options)
                finally:
                    try:
                        rec.add_redis((time.perf_counter() - t0) * 1000.0)
                    except Exception:  # pragma: no cover
                        pass

            rc.Redis.execute_command = timed  # type: ignore[method-assign]
            try:
                import redis.asyncio.client as arc

                aorig = arc.Redis.execute_command

                async def atimed(self, *args, **options):  # noqa: ANN001
                    rec = _current.get()
                    if rec is None:
                        return await aorig(self, *args, **options)
                    t0 = time.perf_counter()
                    try:
                        return await aorig(self, *args, **options)
                    finally:
                        try:
                            rec.add_redis((time.perf_counter() - t0) * 1000.0)
                        except Exception:  # pragma: no cover
                            pass

                arc.Redis.execute_command = atimed  # type: ignore[method-assign]
            except Exception:  # pragma: no cover - redis.asyncio absent
                pass
            _patched_redis = True
        except Exception:  # pragma: no cover
            logger.warning("Instrumentation Redis indisponible (non bloquant).", exc_info=True)


def connect_celery_signals() -> None:
    """Horodate la publication de chaque tâche : `queue_duration_ms` = début de traitement − publication."""
    try:
        from celery.signals import before_task_publish

        @before_task_publish.connect(weak=False)
        def _stamp(sender=None, headers=None, **_kw):  # noqa: ANN001
            try:
                if headers is not None and sender in TRACKED_TASKS:
                    headers[ENQUEUED_AT_HEADER] = time.time()
            except Exception:  # pragma: no cover
                pass
    except Exception:  # pragma: no cover
        logger.warning("Signal Celery de télémétrie indisponible (non bloquant).", exc_info=True)


def enqueued_at_from_request(request: Any) -> Optional[float]:
    """Lit l'horodatage de publication posé par `connect_celery_signals` (en-tête personnalisé Celery)."""
    for getter in (
        lambda: getattr(request, ENQUEUED_AT_HEADER, None),
        lambda: (getattr(request, "headers", None) or {}).get(ENQUEUED_AT_HEADER),
    ):
        try:
            v = getter()
            if v:
                return float(v)
        except Exception:  # pragma: no cover
            continue
    return None


# ── Persistance best-effort ────────────────────────────────────────────────

def write_failures() -> int:
    return _write_failures


def write_successes() -> int:
    return _write_successes


async def _persist(rec: TurnRecorder, duration_ms: int, completed_at: datetime) -> None:
    from sqlalchemy import select

    # Le registre complet des modèles est requis : `agent_turns.user_id` référence `auth.users` (résolution de FK au flush).
    import ladini.domain.models  # noqa: F401
    from ladini.core.database import get_sessionmaker
    from ladini.core.settings import settings
    from ladini.domain.telemetry import AgentLlmCall, AgentToolCall, AgentTurn

    phone_hash = hash_phone(rec.phone)
    gap_s = int(getattr(settings, "AGENT_MONITORING_SESSION_GAP_MINUTES", 30)) * 60
    async with get_sessionmaker()() as session:
        # Continuité de session : dernier tour du même utilisateur (1 lecture indexée par `agent_turns_phone_idx`).
        last = (
            await session.execute(
                select(AgentTurn.conversation_id, AgentTurn.completed_at)
                .where(AgentTurn.phone_hash == phone_hash)
                .order_by(AgentTurn.created_at.desc())
                .limit(1)
            )
        ).first()
        if last is not None and (rec.started_at - last.completed_at).total_seconds() <= gap_s:
            conversation_id = last.conversation_id
        else:
            conversation_id = uuid.uuid4()

        goal = rec.workflow or rec.intent
        user_uuid = None
        try:
            user_uuid = uuid.UUID(rec.user_id) if rec.user_id else None
        except ValueError:
            user_uuid = None

        turn = AgentTurn(
            id=rec.turn_id, conversation_id=conversation_id, user_id=user_uuid, phone_hash=phone_hash,
            phone_last4=phone_last4(rec.phone), channel=rec.channel, user_role=rec.user_role,
            message_sid=rec.message_sid, task_retries=rec.task_retries,
            intent=rec.intent, intent_confidence=rec.intent_confidence, workflow=rec.workflow,
            workflow_step=rec.workflow_step, goal_status=rec.goal_status, outcome=rec.outcome,
            started_at=rec.started_at, completed_at=completed_at, duration_ms=duration_ms,
            queue_duration_ms=rec.queue_duration_ms,
            db_query_count=rec.sql_count, db_duration_ms=int(rec.sql_ms), db_max_query_ms=int(rec.sql_max_ms),
            redis_command_count=rec.redis_count, redis_duration_ms=int(rec.redis_ms),
            mcp_call_count=len(rec.tools), mcp_duration_ms=rec.mcp_ms,
            llm_call_count=len(rec.llms), llm_duration_ms=rec.llm_ms(),
            intent_llm_duration_ms=rec.llm_ms("INTERPRETER"), response_llm_duration_ms=rec.llm_ms("RESPONSE"),
            llm_fallback_count=sum(1 for c in rec.llms if c.is_fallback),
            whatsapp_duration_ms=rec.whatsapp_ms, response_status=rec.response_status,
            error_code=rec.error_code, error_category=rec.error_category, trace_id=rec.trace_id,
            user_message_excerpt=redact_text(rec.user_message, goal=goal),
            agent_response_excerpt=redact_text(rec.response_text, goal=goal),
        )
        session.add(turn)
        await session.flush()  # `agent_turns` d'abord : les enfants ont une FK dessus
        session.add_all([
            AgentToolCall(turn_id=rec.turn_id, seq=t.seq, tool_name=t.tool_name, tool_category=t.tool_category,
                          started_at=t.started_at, completed_at=t.completed_at, duration_ms=t.duration_ms,
                          status=t.status, error_code=t.error_code, error_category=t.error_category,
                          trace_id=rec.trace_id)
            for t in rec.tools
        ])
        session.add_all([
            AgentLlmCall(turn_id=rec.turn_id, seq=c.seq, kind=c.kind, provider=c.provider, model=c.model,
                         duration_ms=c.duration_ms, status=c.status, is_fallback=c.is_fallback,
                         fallback_from=c.fallback_from, error_category=c.error_category,
                         prompt_tokens=c.prompt_tokens, completion_tokens=c.completion_tokens)
            for c in rec.llms
        ])
        await session.commit()


async def finish_and_persist(rec: TurnRecorder) -> bool:
    """Ferme le tour puis l'écrit. NE LÈVE JAMAIS : retourne False en cas d'échec (compté, journalisé)."""
    global _write_failures, _write_successes
    # Le contexte est fermé AVANT l'écriture : les requêtes d'écriture de la télémétrie ne se comptent pas elles-mêmes.
    end_turn()
    try:
        from ladini.core.settings import settings

        if not bool(getattr(settings, "AGENT_MONITORING_ENABLED", True)):
            return False
        try:
            from ladini.core import telemetry

            rec.trace_id = rec.trace_id or telemetry.get_trace_id()
        except Exception:  # pragma: no cover
            pass
        completed_at = datetime.now(timezone.utc)
        duration_ms = max(0, int((time.perf_counter() - rec._t0) * 1000))
        timeout = float(getattr(settings, "AGENT_MONITORING_WRITE_TIMEOUT_SECONDS", 2.0))
        await asyncio.wait_for(_persist(rec, duration_ms, completed_at), timeout=timeout)
        _write_successes += 1
        return True
    except Exception as exc:  # noqa: BLE001 — jamais vers l'appelant (timeout inclus ; l'annulation asyncio se propage)
        _write_failures += 1
        logger.warning("TURN_TELEMETRY_WRITE_FAILED | turn=%s | %s: %s", rec.turn_id, type(exc).__name__, exc)
        return False
