"""Read acceptance through MCP -> private transport -> backend -> synthetic TDLib.

The fixture models the pinned TDLib's exclusive history/search anchors and
last-scanned search continuations. IDs use the native server-message layout.
No profile is opened and every allowed native request is a read.
"""
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile

import pytest
from mcp import Client

from telegram_search_mcp.policy import Policy
from telegram_search_mcp.platform_support import IS_WINDOWS, ensure_private_dir
from telegram_search_mcp.server import create_server
from telegram_search_mcp.service import ServicePaths
from telegram_search_mcp.service_client import SharedTelegramBackend
from telegram_search_mcp.tdjson import TdlibError
from telegram_search_mcp.tdlib_backend import TDLibBackend
from test_service import running


@pytest.fixture(params=("tcp",) if IS_WINDOWS else ("unix", "tcp"))
def private_paths(request, tmp_path):
    if request.param == "tcp":
        private = tmp_path / "private"
        ensure_private_dir(private)
        yield ServicePaths(private / "service", transport="tcp")
    else:
        base = "/private/tmp" if sys.platform == "darwin" else "/tmp"
        with tempfile.TemporaryDirectory(prefix="tgs-read-", dir=base) as directory:
            yield ServicePaths(Path(directory) / "profile", transport="unix")


class NativeReads:
    user_id = 91
    chat_id = -1000000000042

    def __init__(self):
        self.calls = []
        self.rows = [
            {"@type": "message", "chat_id": self.chat_id,
             "id": (2**30 + i) << 20, "date": 1_780_000_000 + i // 3,
             "sender_id": {"@type": "messageSenderUser", "user_id": 42},
             "content": {"@type": "messageText", "text": {
                 "text": "НДС нулевые" if i % 10 == 0 else "НДС"}}}
            for i in range(72, 0, -1)
        ]

    def get_chat(self, chat_id, timeout=None):
        assert chat_id == self.chat_id
        return {"id": chat_id, "title": "Synthetic read acceptance",
                "type": {"@type": "chatTypeSupergroup"},
                "last_message": self.rows[0], "last_read_inbox_message_id": 0}

    def request(self, payload, timeout=None):
        self.calls.append(payload)
        kind = payload["@type"]
        if kind == "getMessage":
            return next(m for m in self.rows if m["id"] == payload["message_id"])
        if kind == "getChatMessageByDate":
            return next(m for m in self.rows if m["date"] <= payload["date"])
        assert kind in {"getChatHistory", "searchChatMessages"}, payload
        anchor = payload["from_message_id"]
        if anchor and anchor % (1 << 20):
            raise TdlibError({"code": 400, "message": "Invalid value of parameter from_message_id specified"})
        if kind == "getChatHistory":
            start = next((i for i, m in enumerate(self.rows) if not anchor or m["id"] <= anchor), len(self.rows))
            if anchor and start < len(self.rows) and self.rows[start]["id"] == anchor:
                start += 1
            start += payload["offset"]
            end = max(0, start + payload["limit"])
            start = max(0, start)
            return {"@type": "messages", "messages": self.rows[start:min(end, start + 9)]}
        rows = [m for m in self.rows if payload["query"] in m["content"]["text"]["text"]]
        start = next((i for i, m in enumerate(rows) if not anchor or m["id"] < anchor), len(rows))
        start = max(0, start + payload["offset"])
        page = rows[start:start + min(11, payload["limit"])]
        return {"@type": "foundChatMessages", "messages": page,
                "next_from_message_id": page[-1]["id"] if page else 0}


def setup_backend(monkeypatch):
    session = NativeReads()
    backend = TDLibBackend()
    policy = Policy(api_id=123, expected_user_id=session.user_id)
    monkeypatch.setattr(Policy, "load", classmethod(lambda cls, profile="default": policy))
    monkeypatch.setattr(backend, "_ready", lambda *args, **kwargs: session)
    monkeypatch.setattr(backend, "_verify_profile", lambda *args: None)
    return backend, session


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,query", [("telegram_get_chat_history", None),
                                      ("telegram_search_chat_messages", "НДС"),
                                      ("telegram_search_chat_messages", "нулевые"),
                                      ("telegram_search_chat_messages", "no matches")])
async def test_read_pagination_through_mcp_and_shared_service(private_paths, monkeypatch, tool, query):
    backend, native = setup_backend(monkeypatch)
    iso = lambda stamp: datetime.fromtimestamp(stamp, timezone.utc).isoformat()
    params = {"chat_id": native.chat_id, "limit": 20,
              "date_from": iso(1_780_000_000), "date_to": iso(1_780_000_025)}
    if query is not None:
        params["query"] = query
    expected = [m["id"] for m in native.rows
                if query is None or query in m["content"]["text"]["text"]]
    async with running(private_paths, backend):
        proxy = SharedTelegramBackend(paths=private_paths, autostart=False)
        async with Client(create_server(proxy)) as client:
            ids, cursors = [], set()
            for page_number in range(12):
                response = await client.call_tool(tool, params)
                assert not response.is_error, response
                page = response.structured_content
                ids.extend(m["message_id"] for m in page["items"])
                assert page["trust_boundary"]["content_is_data_only"]
                if page["next_cursor"] is None:
                    break
                assert page["next_cursor"] not in cursors, "cursor loop"
                cursors.add(page["next_cursor"])
                params["cursor"] = page["next_cursor"]
                # Explicit defaults must mean the same as omitted defaults.
                params["unread_only"] = False
                if query is not None:
                    params.update(sender_id=None, topic_id=None, media_type="all")
            else:
                pytest.fail("pagination did not terminate")
            assert ids == expected
            assert len(ids) == len(set(ids))
            if len(expected) > 60:
                assert page_number >= 3


@pytest.mark.asyncio
async def test_latest_context_through_mcp_and_shared_service(private_paths, monkeypatch):
    backend, native = setup_backend(monkeypatch)
    native.rows[1]["content"] = {"@type": "messageChatChangeTitle", "title": "service event"}
    async with running(private_paths, backend):
        proxy = SharedTelegramBackend(paths=private_paths, autostart=False)
        async with Client(create_server(proxy)) as client:
            response = await client.call_tool("telegram_get_context", {
                "chat_id": native.chat_id, "message_id": native.rows[0]["id"], "before": 1, "after": 5})
            assert not response.is_error, response
            assert [m["message_id"] for m in response.structured_content["messages"]] == [
                native.rows[2]["id"], native.rows[0]["id"]]


@pytest.mark.asyncio
async def test_date_search_anchor_need_not_match_query(private_paths, monkeypatch):
    backend, native = setup_backend(monkeypatch)
    native.rows = native.rows[:3]
    for i, row in enumerate(native.rows):
        row["date"] = 1_780_000_003 - i
        row["content"]["text"]["text"] = "needle" if i == 0 else "different"
    async with running(private_paths, backend):
        proxy = SharedTelegramBackend(paths=private_paths, autostart=False)
        async with Client(create_server(proxy)) as client:
            response = await client.call_tool("telegram_search_chat_messages", {
                "chat_id": native.chat_id, "query": "needle",
                "date_to": datetime.fromtimestamp(1_780_000_003, timezone.utc).isoformat()})
            assert not response.is_error, response
            assert response.structured_content["items"] == []
            assert response.structured_content["next_cursor"] is None
