"""Independent quota-consent regressions; no Telegram client or live quota used."""
from __future__ import annotations

import copy
from dataclasses import replace
import time

import pytest
from mcp import Client

from telegram_search_mcp.public_posts import PublicPostSearch, CONFIRMATION_LIMIT
from telegram_search_mcp.server import create_server
from telegram_search_mcp.tdjson import TdlibError
from telegram_search_mcp.wire import ServiceProtocolError, validate_request


def quota(**changes):
    return {"@type": "publicPostSearchLimits", "daily_free_query_count": 7,
            "remaining_free_query_count": 4, "next_free_query_in": 0,
            "star_count": 99, "is_current_query_free": False, **changes}


def empty_page(offset=""):
    return {"@type": "foundPublicPosts", "messages": [], "next_offset": offset,
            "search_limits": None, "are_limits_exceeded": False}


class SyntheticSession:
    """Accept only quota checks and explicitly free public searches."""

    user_id = 42

    def __init__(self):
        self.quota = quota()
        self.response = empty_page()
        self.calls = []

    def request(self, payload, timeout=None):
        self.calls.append(copy.deepcopy(payload))
        if payload["@type"] == "getPublicPostSearchLimits":
            response = self.quota
        elif payload["@type"] == "searchPublicPosts":
            assert payload["star_count"] == 0
            response = self.response
        else:
            raise AssertionError(f"Unexpected native request: {payload['@type']}")
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)


def prepared(search, session, query="specific topic"):
    result = search.get_public_search_quota(session, query=query)
    assert result["status"] == "ok"
    assert result["confirmation_token"]
    assert result["search_performed"] is False and result["stars_authorized"] == 0
    return result["confirmation_token"]


def search_calls(session):
    return [call for call in session.calls if call["@type"] == "searchPublicPosts"]


def test_quota_check_reports_account_values_without_search_or_reservation():
    search, session = PublicPostSearch(), SyntheticSession()
    result = search.get_public_search_quota(session, query="  specific topic  ")
    assert result["normalized_query"] == "specific topic"
    assert result["quota"]["daily_free_query_count"] == 7
    assert result["quota"]["remaining_free_query_count"] == 4
    assert result["free_search_available"] is True
    assert session.calls == [{"@type": "getPublicPostSearchLimits", "query": "specific topic"}]
    assert session.quota == quota()


@pytest.mark.parametrize("confirmation", [{}, {"user_confirmed": True},
                                           {"user_confirmed": False}])
def test_direct_initial_search_cannot_consume_quota_without_preparation_and_consent(confirmation):
    search, session = PublicPostSearch(), SyntheticSession()
    result = search.search(session, query="specific topic", **confirmation)
    assert result["status"] == "confirmation_required"
    assert result["search_performed"] is False
    assert not session.calls


@pytest.mark.parametrize("confirmed", [False, None])
def test_prepared_token_alone_is_not_consent(confirmed):
    search, session = PublicPostSearch(), SyntheticSession()
    token = prepared(search, session)
    before = len(session.calls)
    args = {} if confirmed is None else {"user_confirmed": confirmed}
    result = search.search(session, query="specific topic", confirmation_token=token, **args)
    assert result["status"] == "confirmation_required" and not result["search_performed"]
    assert len(session.calls) == before


@pytest.mark.parametrize("misuse", ["query", "account", "expired", "forged", "restart"])
def test_bad_confirmation_is_rejected_before_any_native_request(misuse):
    search, session = PublicPostSearch(), SyntheticSession()
    token = prepared(search, session)
    query = "specific topic"
    if misuse == "query":
        query = "different topic"
    elif misuse == "account":
        session.user_id = 84
    elif misuse == "expired":
        search.confirmations[token] = replace(search.confirmations[token], expires=time.monotonic() - 1)
    elif misuse == "forged":
        token = "quota:" + "f" * 48
    else:
        search = PublicPostSearch()
    before = len(session.calls)
    result = search.search(session, query=query, confirmation_token=token, user_confirmed=True)
    assert result["status"] == "confirmation_required"
    assert result["reason"] == "invalid_confirmation_token" and not result["search_performed"]
    assert len(session.calls) == before


@pytest.mark.parametrize("native_response", [TimeoutError(),
    TdlibError({"code": 429, "message": "FLOOD_WAIT_30"}), {"@type": "unexpected"}])
def test_native_failure_cannot_reuse_consent_for_another_search_attempt(native_response):
    search, session = PublicPostSearch(), SyntheticSession()
    token = prepared(search, session)
    session.response = native_response
    result = search.search(session, query="specific topic", confirmation_token=token, user_confirmed=True)
    assert result["status"] != "ok" and len(search_calls(session)) == 1
    before = len(session.calls)
    replay = search.search(session, query="specific topic", confirmation_token=token, user_confirmed=True)
    assert replay["status"] == "confirmation_required"
    assert replay["reason"] == "invalid_confirmation_token"
    assert len(session.calls) == before


@pytest.mark.parametrize("change", [{"remaining_free_query_count": 3}, {"next_free_query_in": 30},
                                   {"is_current_query_free": True}])
def test_changed_quota_requires_new_snapshot_and_consent_without_running_search(change):
    search, session = PublicPostSearch(), SyntheticSession()
    token = prepared(search, session)
    session.quota = quota(**change)
    result = search.search(session, query="specific topic", confirmation_token=token, user_confirmed=True)
    assert result["status"] == "quota_changed" and result["reason"] == "quota_changed"
    assert not result["search_performed"] and not search_calls(session)
    for key, value in change.items():
        assert result["quota"][key] == value
    before = len(session.calls)
    replay = search.search(session, query="specific topic", confirmation_token=token, user_confirmed=True)
    assert replay["status"] == "confirmation_required" and len(session.calls) == before


def test_server_cached_free_query_still_requires_initial_user_consent():
    search, session = PublicPostSearch(), SyntheticSession()
    session.quota = quota(remaining_free_query_count=0, next_free_query_in=3600, is_current_query_free=True)
    token = prepared(search, session)
    blocked = search.search(session, query="specific topic", confirmation_token=token)
    assert blocked["status"] == "confirmation_required" and not search_calls(session)
    approved = search.search(session, query="specific topic", confirmation_token=token, user_confirmed=True)
    assert approved["status"] == "ok" and len(search_calls(session)) == 1


@pytest.mark.parametrize("change", [{"next_free_query_in": 3540}, {"star_count": 9999}])
def test_countdown_and_unavailable_paid_option_do_not_change_cached_free_consent(change):
    search, session = PublicPostSearch(), SyntheticSession()
    session.quota = quota(remaining_free_query_count=0, next_free_query_in=3600, is_current_query_free=True)
    token = prepared(search, session)
    session.quota.update(change)
    approved = search.search(session, query="specific topic", confirmation_token=token, user_confirmed=True)
    assert approved["status"] == "ok" and len(search_calls(session)) == 1
    assert search_calls(session)[0]["star_count"] == approved["stars_authorized"] == 0


def test_verified_continuation_needs_no_new_consent_but_cannot_authorize_new_query():
    search, session = PublicPostSearch(), SyntheticSession()
    token = prepared(search, session)
    session.response = empty_page("native offset")
    first = search.search(session, query="specific topic", confirmation_token=token, user_confirmed=True)
    cursor = first["next_cursor"]
    assert cursor and first["count"] == 0
    session.quota = quota(remaining_free_query_count=0, next_free_query_in=3600)
    session.response = empty_page()
    continued = search.search(session, query="specific topic", cursor=cursor)
    assert continued["status"] == "ok"
    assert [call["offset"] for call in search_calls(session)] == ["", "native offset"]
    before = len(session.calls)
    new_query = search.search(session, query="different topic", user_confirmed=True)
    assert new_query["status"] == "confirmation_required" and len(session.calls) == before


def test_confirmation_cache_is_bounded_and_evicted_token_cannot_search():
    search, session = PublicPostSearch(), SyntheticSession()
    oldest = prepared(search, session)
    for index in range(CONFIRMATION_LIMIT):
        prepared(search, session, query=f"specific topic {index}")
    assert len(search.confirmations) <= CONFIRMATION_LIMIT
    before = len(session.calls)
    blocked = search.search(session, query="specific topic", confirmation_token=oldest, user_confirmed=True)
    assert blocked["status"] == "confirmation_required" and not search_calls(session)
    assert len(session.calls) == before


@pytest.mark.parametrize("value", [1, "true", "false", [], None])
def test_wire_never_coerces_non_boolean_values_into_consent(value):
    with pytest.raises(ServiceProtocolError):
        validate_request({"protocol": 1, "id": "a" * 32, "operation": "search_public_posts",
            "params": {"query": "specific topic", "cursor": None, "limit": 20,
                       "confirmation_token": "quota:" + "f" * 48, "user_confirmed": value}, "timeout": 20})


def test_wire_accepts_explicit_boolean_consent_and_separate_quota_check():
    for operation, params in [
        ("get_public_search_quota", {"query": "specific topic"}),
        ("search_public_posts", {"query": "specific topic", "cursor": None, "limit": 20,
            "confirmation_token": "public-confirm:" + "f" * 48, "user_confirmed": True}),
    ]:
        request = {"protocol": 1, "id": "a" * 32, "operation": operation, "params": params, "timeout": 20}
        assert validate_request(request) == request


class SyntheticBackend:
    def __init__(self):
        self.search = PublicPostSearch()
        self.session = SyntheticSession()

    async def get_public_search_quota(self, **params):
        return self.search.get_public_search_quota(self.session, **params)

    async def search_public_posts(self, **params):
        return self.search.search(self.session, **params)


@pytest.mark.asyncio
async def test_connected_client_receives_consent_sequence_and_read_only_quota_tool():
    async with Client(create_server(SyntheticBackend())) as client:
        instructions = client.instructions
        assert instructions and instructions.index("history first") < instructions.index("telegram_get_public_search_quota")
        assert "WAIT for explicit consent" in instructions[:512]
        assert "Never offer or spend Stars" in instructions
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        check = tools["telegram_get_public_search_quota"]
        assert check.annotations.read_only_hint is True
        assert check.annotations.idempotent_hint is True
        public = tools["telegram_search_public_posts"]
        assert public.annotations.read_only_hint is False
        assert set(public.input_schema["properties"]) == {
            "query", "cursor", "limit", "confirmation_token", "user_confirmed"}
        assert public.input_schema["properties"]["user_confirmed"]["default"] is False
        assert "WAIT" in public.description and "WAIT" in check.description


@pytest.mark.asyncio
async def test_mcp_old_direct_call_and_quota_tool_never_execute_search_until_confirmed():
    backend = SyntheticBackend()
    async with Client(create_server(backend)) as client:
        old_call = await client.call_tool("telegram_search_public_posts", {"query": "specific topic"})
        assert not old_call.is_error
        assert old_call.structured_content["status"] == "confirmation_required"
        assert not backend.session.calls
        check = await client.call_tool("telegram_get_public_search_quota", {"query": "specific topic"})
        assert not check.is_error and not search_calls(backend.session)
        token = check.structured_content["confirmation_token"]
        approved = await client.call_tool("telegram_search_public_posts", {
            "query": "specific topic", "confirmation_token": token, "user_confirmed": True})
        assert not approved.is_error and approved.structured_content["status"] == "ok"
        assert len(search_calls(backend.session)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_consent", [1, "true", "false", None])
async def test_mcp_rejects_coerced_consent_before_backend(invalid_consent):
    backend = SyntheticBackend()
    async with Client(create_server(backend)) as client:
        check = await client.call_tool("telegram_get_public_search_quota", {"query": "specific topic"})
        before = len(backend.session.calls)
        result = await client.call_tool("telegram_search_public_posts", {
            "query": "specific topic", "confirmation_token": check.structured_content["confirmation_token"],
            "user_confirmed": invalid_consent})
        assert result.is_error and len(backend.session.calls) == before
