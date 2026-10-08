"""Acceptance-runner validation with synthetic messages, never a live profile."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


spec = importlib.util.spec_from_file_location(
    "verify_live_release", Path(__file__).parents[1] / "scripts" / "verify-live-release.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class SyntheticClient:
    def __init__(self, *, inconsistent=False, duplicates=False):
        self.rows = [{"chat_id": -42, "message_id": (2**30 + i) << 20,
                      "content_type": "messageText", "text": {"value": "private sentinel"},
                      "chat_title": {"value": "private title"}}
                     for i in range(45, 0, -1)]
        self.calls = []
        self.inconsistent, self.duplicates = inconsistent, duplicates

    async def call_tool(self, tool, params):
        self.calls.append((tool, params))
        assert tool in {"telegram_get_chat_history", "telegram_search_chat_messages", "telegram_get_context"}
        if tool == "telegram_get_context":
            index = next(i for i, row in enumerate(self.rows) if row["message_id"] == params["message_id"])
            result = {"messages": list(reversed(self.rows[max(0, index - 2):index + 3]))}
        else:
            start, limit = int(params.get("cursor") or 0), params["limit"]
            if self.inconsistent and limit == 1:
                start += 1
            if self.duplicates and start:
                start -= 1
            end = start + limit
            result = {"items": self.rows[start:end], "next_cursor": str(end) if end < len(self.rows) else None}
        return SimpleNamespace(is_error=False, structured_content=result)


@pytest.mark.asyncio
async def test_acceptance_checks_native_ids_sizes_and_context_without_reporting_content():
    client = SyntheticClient()
    report = await runner.read_checks(client, -42, "needle", 5)
    assert report["history"]["page_counts"] == [20, 20, 5]
    assert report["history"]["page_size_invariant"]
    assert report["search"]["messages"] == 45
    assert report["context"]["anchor_present"]
    assert "private" not in json.dumps(report)
    assert "message_id" not in json.dumps(report)
    bounds = {params["date_to"] for tool, params in client.calls if tool != "telegram_get_context"}
    assert len(bounds) == 1
    assert sum(params.get("limit") == 1 for _, params in client.calls) == 20


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,match", [("duplicates", "duplicate"), ("inconsistent", "page size")])
async def test_acceptance_rejects_duplicate_pages_and_page_size_changes(mode, match):
    with pytest.raises(runner.AcceptanceError, match=match):
        await runner.read_checks(SyntheticClient(**{mode: True}), -42, None, 5)


@pytest.mark.asyncio
async def test_acceptance_rejects_repeated_cursor_instead_of_claiming_completion():
    class LoopClient:
        async def call_tool(self, *_):
            return SimpleNamespace(is_error=False, structured_content={"items": [], "next_cursor": "same"})
    with pytest.raises(runner.AcceptanceError, match="cursor"):
        await runner.pages(LoopClient(), "telegram_get_chat_history", {"chat_id": -42, "limit": 20}, 5)


@pytest.mark.asyncio
async def test_acceptance_rejects_old_shared_service_without_activating_or_connecting(monkeypatch):
    import telegram_search_mcp.service_client as service_client
    async def stale_status(*, connect):
        assert connect is False
        return {"running": True, "version": "0.8.1", "stopping": False}
    monkeypatch.setattr(service_client, "service_status", stale_status)
    with pytest.raises(runner.AcceptanceError, match="service version"):
        await runner.check_service("0.9.3", optional=True)
