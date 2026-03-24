from agriconnect.workers.tasks import marketplace as marketplace_tasks


class FakeServer:
    def __init__(self, pending_actions):
        self.pending_actions = pending_actions
        self.calls = []

    def call_tool_sync(self, name, arguments=None, timeout=10.0):
        arguments = arguments or {}
        self.calls.append((name, arguments))

        if name == "get_pending_actions":
            return {"ok": True, "data": {"status": "ok", "data": self.pending_actions}}

        if name == "update_action_status":
            return {"ok": True, "data": {"status": "ok", "data": {"id": arguments.get("action_id")}}}

        return {"ok": True, "data": {"status": "ok", "data": {}}}


def test_process_pending_actions_executes_run_matching(monkeypatch):
    fake_server = FakeServer(
        [
            {
                "id": "act-1",
                "agent_name": "MarketplaceBackgroundAgent",
                "action_type": "RUN_MATCHING",
                "payload": {"product_name": "mais", "location": "zone-1"},
            }
        ]
    )

    async def _fake_generate(self, product_name, zone_id=None, limit=10):
        return [{"id": "match-1", "product_id": "prod-1", "score": 0.8}]

    monkeypatch.setattr(marketplace_tasks, "_get_server", lambda: fake_server)
    monkeypatch.setattr(
        marketplace_tasks.MarketplaceBackgroundAgent,
        "generate_matches_for_product",
        _fake_generate,
    )

    result = marketplace_tasks.process_pending_actions.run(limit=10)

    assert result["status"] == "success"
    assert result["executed"] == 1
    assert any(
        name == "update_action_status" and args.get("new_status") == "EXECUTED"
        for name, args in fake_server.calls
    )


def test_process_pending_actions_marks_invalid_payload_failed(monkeypatch):
    fake_server = FakeServer(
        [
            {
                "id": "act-2",
                "agent_name": "MarketplaceBackgroundAgent",
                "action_type": "RUN_MATCHING",
                "payload": {},
            }
        ]
    )

    monkeypatch.setattr(marketplace_tasks, "_get_server", lambda: fake_server)

    result = marketplace_tasks.process_pending_actions.run(limit=10)

    assert result["status"] == "success"
    assert result["failed"] == 1
    assert any(
        name == "update_action_status" and args.get("new_status") == "FAILED"
        for name, args in fake_server.calls
    )
