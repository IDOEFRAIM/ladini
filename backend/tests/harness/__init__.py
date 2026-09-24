"""Harnais de test canonique du moteur conversationnel (voir `conversation.py`)."""

from tests.harness.conversation import (  # noqa: F401
    ConversationHarness,
    FakeRedis,
    HarnessLLM,
    HarnessRuntime,
    InMemoryWorkspaceStore,
    TurnResult,
    new_task,
)
