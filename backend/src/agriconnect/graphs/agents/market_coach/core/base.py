"""MarketCoach — Base shared constants & validation schema."""
from __future__ import annotations

import logging
from typing import Dict, FrozenSet, Tuple

from pydantic import BaseModel, Field, validator

from agriconnect.graphs.agents.market_coach.registry import (
    iter_actions,
    load_all_actions,
)
from agriconnect.graphs.agents.market_coach.interpreter.intent import INTENT_CONFIG


# ── Logger ───────────────────────────────────────────────────────────
logger = logging.getLogger("AgriConnect.Market")


def get_node_logger(name: str) -> logging.Logger:
    """Child logger for a specific node — always under AgriConnect.Market."""
    return logger.getChild(name)


# ── Declarative validation schema ───────────────────────────────────
class FarmRuleConfig(BaseModel):
    write_requires_farm: FrozenSet[str] = Field(default_factory=frozenset)
    read_optional_farm: FrozenSet[str] = Field(default_factory=frozenset)

    @validator("write_requires_farm", "read_optional_farm", pre=True)
    def _normalize(cls, value):
        if not value:
            return frozenset()
        return frozenset(str(item).upper().strip() for item in value if item)

    @property
    def farm_critical_goals(self) -> FrozenSet[str]:
        return frozenset(self.write_requires_farm | self.read_optional_farm)


class IntentConfigEntry(BaseModel):
    required: FrozenSet[str] = Field(default_factory=frozenset)

    @validator("required", pre=True)
    def _normalize_required(cls, value):
        if not value:
            return frozenset()
        return frozenset(str(item).strip().lower() for item in value if item)


class MarketValidationConfig(BaseModel):
    auto_farm_notice: str = Field(default="J'ai configuré votre ferme par défaut pour accéder à votre stock.")
    farm: FarmRuleConfig
    intents: Dict[str, IntentConfigEntry] = Field(default_factory=dict)

    @validator("intents", pre=True)
    def _normalize_intent_keys(cls, value):
        if not value:
            return {}
        normalized: Dict[str, IntentConfigEntry] = {}
        for key, entry in value.items():
            normalized[str(key).upper()] = entry
        return normalized

    @property
    def farm_id_required_goals(self) -> FrozenSet[str]:
        return frozenset(
            key for key, meta in self.intents.items() if "farm_id" in meta.required
        )


_AUTO_FARM_NOTICE = "J'ai configuré votre ferme par défaut pour accéder à votre stock."


def _build_intent_entries() -> Dict[str, IntentConfigEntry]:
    return {
        key.upper(): IntentConfigEntry(required=cfg.get("required") or [])
        for key, cfg in INTENT_CONFIG.items()
    }


load_all_actions()


MARKET_VALIDATION_CONFIG = MarketValidationConfig(
    auto_farm_notice=_AUTO_FARM_NOTICE,
    farm=FarmRuleConfig(
        write_requires_farm={
            "STOCK_REGISTER_HARVEST", "STOCK_RECORD_MOVEMENT", "STOCK_ADJUST",
            "STOCK_REMOVE_PARTIAL", "STOCK_DELETE",
            "CROP_START_CYCLE", "CROP_RECORD_INTERVENTION", "CROP_RECORD_OBSERVATION",
            "CROP_UPDATE_STAGE", "CROP_UPDATE_SOIL",
            "FINANCE_LOG_EXPENSE",
            "FARM_CREATE", "FARM_UPDATE",
            "SALES_PUBLISH_PRODUCT", "SALES_RECORD_DIRECT",
        },
        read_optional_farm={
            "STOCK_GET_SUMMARY", "STOCK_GET_DETAIL", "STOCK_GET_MOVEMENTS",
            "FINANCE_GET_SUMMARY",
            "FARM_GET_MY_LIST",
        },
    ),
    intents=_build_intent_entries(),
)


WRITE_REQUIRES_FARM = MARKET_VALIDATION_CONFIG.farm.write_requires_farm
READ_OPTIONAL_FARM = MARKET_VALIDATION_CONFIG.farm.read_optional_farm
FARM_CRITICAL_GOALS = MARKET_VALIDATION_CONFIG.farm.farm_critical_goals
FARM_ID_REQUIRED_GOALS = MARKET_VALIDATION_CONFIG.farm_id_required_goals


def _derive_goal_sets() -> Tuple[FrozenSet[str], FrozenSet[str]]:
    write: set[str] = set()
    read: set[str] = set()
    for intent, registration in iter_actions():
        if registration.is_write:
            write.add(intent)
        else:
            read.add(intent)
    return frozenset(write), frozenset(read)


_WRITE_GOALS, _READ_GOALS = _derive_goal_sets()


def validate_config_drift() -> None:
    """Ensure INTENT_CONFIG and the action registry stay in sync."""
    registry_keys = frozenset(intent for intent, _ in iter_actions())
    intent_keys = frozenset(MARKET_VALIDATION_CONFIG.intents.keys())

    missing = intent_keys - registry_keys
    extras = registry_keys - intent_keys

    if missing or extras:
        raise RuntimeError(
            "MarketActionRegistry drift detected: missing intents %s | extra intents %s"
            % (sorted(missing), sorted(extras))
        )


__all__ = [
    "logger",
    "get_node_logger",
    "_WRITE_GOALS",
    "_READ_GOALS",
    "_AUTO_FARM_NOTICE",
    "MARKET_VALIDATION_CONFIG",
    "FARM_CRITICAL_GOALS",
    "FARM_ID_REQUIRED_GOALS",
    "WRITE_REQUIRES_FARM",
    "READ_OPTIONAL_FARM",
    "validate_config_drift",
]
