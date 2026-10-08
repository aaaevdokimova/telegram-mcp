"""Synthetic TDLib history pages; no runtime profile or Telegram connection."""
from __future__ import annotations

from typing import Any

import pytest

import telegram_search_mcp.tdlib_backend as backend_module
from telegram_search_mcp.policy import Policy
from telegram_search_mcp.tdlib_backend import TDLibBackend


CHAT_ID = -1000000000321
BASE_ID = (2**30) << 20


def message(number: int, *, supported: bool = True) -> dict[str, Any]:
    return {
        "@type": "message",
        "id": BASE_ID + (number << 20),
        "chat_id": CHAT_ID,
        # Identical seconds exercise ordering by full, unrounded message ID.
        "date": 1_700_000_000,
        "content": ({"@type": "messageText", "text": {"text": f"message {number}"}}
                    if supported else {"@type": "messageChatChangeTitle"}),
    }


class HistorySession:
    user_id = 7

    def __init__(self, messages, *, page_size=100, first_page=None):
        self.messages = sorted(messages, key=lambda item: item["id"], reverse=True)
        self.page_size = page_size
        self.first_page = first_page
        self.requests = []
        self.history_requests = []

    def get_chat(self, chat_id, timeout=10.0):
        assert chat_id == CHAT_ID
        return {"id": chat_id, "title": "Synthetic context", "type": {"@type": "chatTypeSupergroup"}}

    def request(self, request, timeout=30.0):
        self.requests.append(request)
        assert request["chat_id"] == CHAT_ID
        if request["@type"] == "getMessage":
            return next(item for item in self.messages if item["id"] == request["message_id"])
        assert request["@type"] == "getChatHistory"
        assert request["only_local"] is False
        self.history_requests.append(request)
        # Only IDs returned by TDLib may be used as history positions.
        index = next(i for i, item in enumerate(self.messages)
                     if item["id"] == request["from_message_id"])
        if len(self.history_requests) == 1 and self.first_page is not None:
            return {"messages": self.first_page}
        # Pinned TDLib OrderedMessage.cpp: offset 0 starts AFTER the exact
        # from_message_id; offset -1 includes it. At the newest boundary the
        # missing newer slots reduce the returned window rather than shifting it.
        start = index + request["offset"] + 1
        end = max(0, start + request["limit"])
        start = max(0, start)
        return {"messages": self.messages[start:min(end, start + self.page_size)]}


def configure(monkeypatch, messages, **kwargs):
    policy = Policy(api_id=12345, expected_user_id=7)
    session = HistorySession(messages, **kwargs)
    monkeypatch.setattr(Policy, "load", classmethod(lambda cls, profile="default": policy))
    backend = TDLibBackend()
    monkeypatch.setattr(backend, "_ready", lambda current, timeout=30.0: session)
    return backend, session


def ids(items):
    return [item.message_id for item in items]


@pytest.mark.parametrize("cold_page", ["anchor", "empty"])
def test_newest_anchor_refills_a_cold_short_history_page(monkeypatch, cold_page):
    messages = [message(1), message(2), message(3)]
    backend, session = configure(monkeypatch, messages,
                                 first_page=[messages[-1]] if cold_page == "anchor" else [])
    result = backend._get_context_sync(chat_id=CHAT_ID, message_id=messages[-1]["id"], before=1, after=5)
    assert ids(result) == [messages[1]["id"], messages[2]["id"]]
    assert len(session.history_requests) > 1


def test_unsupported_events_do_not_consume_supported_neighbor_counts(monkeypatch):
    supported = {1, 3, 6, 8, 11}
    messages = [message(i, supported=i in supported) for i in range(1, 12)]
    backend, _ = configure(monkeypatch, messages)
    result = backend._get_context_sync(chat_id=CHAT_ID, message_id=message(6)["id"], before=2, after=2)
    assert ids(result) == [message(i)["id"] for i in sorted(supported)]


def test_short_pages_continue_until_five_older_neighbors_are_found(monkeypatch):
    backend, session = configure(monkeypatch, [message(i) for i in range(1, 8)], page_size=2)
    result = backend._get_context_sync(chat_id=CHAT_ID, message_id=message(7)["id"], before=5, after=0)
    assert ids(result) == [message(i)["id"] for i in range(2, 8)]
    assert len(session.history_requests) >= 3


def test_oldest_anchor_has_only_available_newer_neighbors(monkeypatch):
    backend, _ = configure(monkeypatch, [message(i) for i in range(1, 4)], page_size=2)
    result = backend._get_context_sync(chat_id=CHAT_ID, message_id=message(1)["id"], before=5, after=5)
    assert ids(result) == [message(i)["id"] for i in range(1, 4)]


def test_counts_apply_to_each_side_independently(monkeypatch):
    backend, _ = configure(monkeypatch, [message(i) for i in range(1, 14)])
    result = backend._get_context_sync(chat_id=CHAT_ID, message_id=message(7)["id"], before=1, after=5)
    assert ids(result) == [message(i)["id"] for i in range(6, 13)]


def test_zero_neighbors_returns_only_anchor_without_history_read(monkeypatch):
    backend, session = configure(monkeypatch, [message(1), message(2), message(3)])
    result = backend._get_context_sync(chat_id=CHAT_ID, message_id=message(2)["id"], before=0, after=0)
    assert ids(result) == [message(2)["id"]]
    assert not session.history_requests


def test_unsupported_anchor_does_not_return_unrelated_neighbors(monkeypatch):
    backend, _ = configure(monkeypatch, [message(1), message(2, supported=False), message(3)])
    assert backend._get_context_sync(chat_id=CHAT_ID, message_id=message(2)["id"], before=1, after=1) == ()


def test_one_item_newer_pages_preserve_the_nearest_five_supported_neighbors(monkeypatch):
    supported = {1, 3, 5, 7, 9, 11, 13}
    backend, _ = configure(monkeypatch,
                           [message(i, supported=i in supported) for i in range(1, 14)],
                           page_size=1)
    result = backend._get_context_sync(chat_id=CHAT_ID, message_id=message(1)["id"], before=0, after=5)
    assert ids(result) == [message(i)["id"] for i in [1, 3, 5, 7, 9, 11]]


def test_older_pages_continue_through_multiple_unsupported_pages(monkeypatch):
    backend, session = configure(monkeypatch,
                                 [message(i, supported=i in {1, 15}) for i in range(1, 16)],
                                 page_size=2)
    result = backend._get_context_sync(chat_id=CHAT_ID, message_id=message(15)["id"], before=1, after=0)
    assert ids(result) == [message(1)["id"], message(15)["id"]]
    assert len(session.history_requests) == 7


def test_single_message_history_terminates_without_duplicates(monkeypatch):
    backend, session = configure(monkeypatch, [message(1)])
    result = backend._get_context_sync(chat_id=CHAT_ID, message_id=message(1)["id"], before=5, after=5)
    assert ids(result) == [message(1)["id"]]
    assert len(session.history_requests) <= 4


def test_scan_budget_exhaustion_is_an_error_instead_of_incomplete_context(monkeypatch):
    backend, _ = configure(monkeypatch,
                           [message(i, supported=i in {1, 10}) for i in range(1, 11)])
    monkeypatch.setattr(backend_module, "MAX_RAW_PAGES", 1)
    monkeypatch.setattr(backend_module, "RAW_PAGE_SIZE", 2)
    with pytest.raises(TimeoutError, match="context scan limit"):
        backend._get_context_sync(chat_id=CHAT_ID, message_id=message(10)["id"], before=1, after=0)


@pytest.mark.parametrize("before,after", [(6, 0), (0, 6), (-1, 0), (0, True)])
def test_context_limits_are_enforced_before_any_tdlib_access(monkeypatch, before, after):
    backend, session = configure(monkeypatch, [message(1)])
    with pytest.raises(ValueError, match="between 0 and 5"):
        backend._get_context_sync(chat_id=CHAT_ID, message_id=message(1)["id"], before=before, after=after)
    assert session.requests == []


def test_foreign_chat_messages_are_not_exposed(monkeypatch):
    backend, _ = configure(monkeypatch, [message(1), message(2)],
                           first_page=[{**message(1), "chat_id": -1000000000654}])
    result = backend._get_context_sync(chat_id=CHAT_ID, message_id=message(2)["id"], before=1, after=0)
    assert ids(result) == [message(1)["id"], message(2)["id"]]
    assert all(item.chat_id == CHAT_ID for item in result)


def test_context_keeps_read_errors_instead_of_returning_partial_results(monkeypatch):
    backend, session = configure(monkeypatch, [message(1)])
    request = session.request

    def unavailable(payload, timeout=30.0):
        if payload["@type"] == "getChatHistory":
            raise TimeoutError("Synthetic read timeout")
        return request(payload, timeout=timeout)

    monkeypatch.setattr(session, "request", unavailable)
    with pytest.raises(TimeoutError, match="Synthetic read timeout"):
        backend._get_context_sync(chat_id=CHAT_ID, message_id=message(1)["id"], before=1, after=1)
