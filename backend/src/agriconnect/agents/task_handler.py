"""TaskHandler — orchestrateur robuste avec validation stricte et circuit breaker."""

from __future__ import annotations

import enum
import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

logger = logging.getLogger("AgriConnect.TaskHandler")


# ---------------------------------------------------------------------------
# États de la machine à états de l'agent
# ---------------------------------------------------------------------------


class GoalState(str, enum.Enum):
    WAITING_INPUT = "WAITING_INPUT"
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    ERROR_RECOVERY = "ERROR_RECOVERY"
    HUMAN_INTERVENTION = "HUMAN_INTERVENTION"


# ---------------------------------------------------------------------------
# Modèles Pydantic pour sécuriser les entrées
# ---------------------------------------------------------------------------


class TaskPayload(BaseModel):
    phone: str = Field(..., min_length=8, max_length=32)
    goal: str = Field(..., min_length=1)
    price: Optional[float] = Field(None, ge=0)
    quantity: Optional[float] = Field(None, ge=0)
    unit: Optional[str] = Field("KG", max_length=12)
    context: Dict[str, Any] = Field(default_factory=dict)
    required_fields: List[str] = Field(default_factory=list)

    @field_validator("phone")
    def _validate_phone(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized.startswith("+"):
            raise ValueError("phone must be in E.164 format (ex: +221...)")
        return normalized

    @field_validator("unit")
    def _upper_unit(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        return value.strip().upper()

    def is_ready(self) -> bool:
        """Schema-driven readiness: checks all required_fields are non-null."""
        if not self.required_fields:
            return True
        ctx = self.context or {}
        for field_name in self.required_fields:
            val = getattr(self, field_name, None) or ctx.get(field_name)
            if val is None or val == "" or val == []:
                return False
        return True

    def get_missing_fields(self) -> List[str]:
        """Returns list of required_fields that are still missing."""
        missing: List[str] = []
        ctx = self.context or {}
        for field_name in self.required_fields:
            val = getattr(self, field_name, None) or ctx.get(field_name)
            if val is None or val == "" or val == []:
                missing.append(field_name)
        return missing


class AgentState(BaseModel):
    payload: TaskPayload
    goal_state: GoalState = GoalState.WAITING_INPUT
    retry_count: int = 0
    goal_stack: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Orchestrateur concret
# ---------------------------------------------------------------------------


class TaskHandler:
    """Orchestrateur deterministe avec validation, FSM stricte et circuit breaker."""

    MAX_RETRIES = 3

    def __init__(self, *, storage, executor):
        self.storage = storage
        self.executor = executor

    async def handle(
        self, raw_payload: Dict[str, Any], *, retry_seed: int = 0
    ) -> AgentState:
        try:
            payload = TaskPayload(**raw_payload)
        except ValidationError as exc:
            logger.warning("Payload invalide: %s", exc)
            return self._fail_fast(exc, raw_payload)

        state = AgentState(payload=payload)
        state.retry_count = max(0, retry_seed)
        state.metadata["history"] = []

        try:
            await self._perceive(state)
            await self._think(state)
            await self._act(state)
        except Exception as exc:
            self._log_fatal(state, exc)
            state.goal_state = GoalState.HUMAN_INTERVENTION
            state.goal_stack.clear()
            raise
        return state

    # ------------------------------------------------------------------
    # Cycle perceive / think / act avec garde-fous
    # ------------------------------------------------------------------

    async def _perceive(self, state: AgentState) -> None:
        try:
            await self.storage.ensure_profile(state.payload.phone)
            state.metadata["history"].append("perceive:ok")
        except Exception as exc:
            state.metadata["history"].append(f"perceive:error:{exc}")
            self._transition(state, GoalState.ERROR_RECOVERY)
            raise

    async def _think(self, state: AgentState) -> None:
        try:
            missing = state.payload.get_missing_fields()
            if missing:
                state.goal_state = GoalState.WAITING_INPUT
                state.metadata["missing_fields"] = missing
                state.metadata["plan"] = {
                    "action": state.payload.goal,
                    "status": "INCOMPLETE",
                }
                state.metadata["history"].append(f"think:incomplete:{missing}")
                return
            self._transition(state, GoalState.WAITING_CONFIRMATION)
            state.metadata["plan"] = {"action": state.payload.goal}
        except Exception as exc:
            state.metadata["history"].append(f"think:error:{exc}")
            self._transition(state, GoalState.ERROR_RECOVERY)
            raise

    async def _act(self, state: AgentState) -> None:
        while state.retry_count < self.MAX_RETRIES:
            try:
                self._transition(state, GoalState.EXECUTING)
                result = await self.executor.run(state.payload.model_dump())
                state.metadata["result"] = result
                self._transition(state, GoalState.COMPLETED)
                state.goal_stack.clear()
                return
            except Exception as exc:
                state.retry_count += 1
                state.metadata.setdefault("errors", []).append(str(exc))
                logger.warning(
                    "Act failure %s/%s: %s", state.retry_count, self.MAX_RETRIES, exc
                )

                if state.retry_count >= self.MAX_RETRIES:
                    self._transition(state, GoalState.HUMAN_INTERVENTION)
                    state.goal_stack.clear()
                    break

                self._transition(state, GoalState.ERROR_RECOVERY)
                await self._request_clarification(state, str(exc))

    async def _request_clarification(self, state: AgentState, reason: str) -> None:
        state.metadata["clarification_prompt"] = (
            "Impossible d'exécuter l'action (" + reason + "). "
            "Pouvez-vous confirmer ou corriger les informations demandées ?"
        )

    # ------------------------------------------------------------------
    # Helpers FSM / erreurs
    # ------------------------------------------------------------------

    def _transition(self, state: AgentState, target: GoalState) -> None:
        if (
            state.goal_state == GoalState.WAITING_CONFIRMATION
            and target == GoalState.COMPLETED
        ):
            raise RuntimeError("Confirmation requise avant completion")
        if target == GoalState.COMPLETED and not state.payload.is_ready():
            missing = state.payload.get_missing_fields()
            raise RuntimeError(f"Payload incomplet: champs manquants {missing}")
        state.goal_state = target

    @staticmethod
    def _fail_fast(exc: ValidationError, raw: Dict[str, Any]) -> AgentState:
        state = AgentState(
            payload=TaskPayload.construct(
                phone="+000000000", goal="INVALID", context={}
            ),
            goal_state=GoalState.ERROR_RECOVERY,
            metadata={
                "clarification_prompt": str(exc),
                "raw_payload": raw,
            },
        )
        return state

    @staticmethod
    def _log_fatal(state: AgentState, exc: Exception) -> None:
        logger.error(
            "Fatal agent error: %s | state=%s", exc, state.json(), exc_info=True
        )
