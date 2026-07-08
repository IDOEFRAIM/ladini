# Orchestrator

Bridges the API/WhatsApp events with the agent graph execution. Responsible for loading the workspace, preparing the agent state, invoking the LangGraph, and persisting results.

## Contents

| File | Role |
| --- | --- |
| `orchestrator.py` | Main entrypoint that wires workspace resolution, graph invocation, and output rendering.

## Flow (high level)

1. Normalize inbound request (phone/session/text, attachments).
2. Resolve `workspace` (session) and guard context.
3. Call the MarketCoach graph with the current state.
4. Persist updated state/checkpoint and return AG-UI response.
