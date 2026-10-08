"""Read diagnostics use synthetic data and isolated private transports, never TDLib."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import re
import secrets
import uuid

import pytest

from telegram_search_mcp.service_client import SharedTelegramBackend, _request
from telegram_search_mcp.service import prepare_service_paths, profile_exclusive
from telegram_search_mcp.tdjson import TdlibError
from telegram_search_mcp.tdlib_backend import CursorError, SetupRequiredError
from telegram_search_mcp.wire import MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, ServiceError, read_frame, write_frame
from telegram_search_mcp.windows_transport import (
    Endpoint, HOST, authenticate_client, authenticate_server, publish_endpoint,
    read_endpoint, remove_endpoint,
)
from test_service import ThreadBackend, running
from test_read_service_regressions import private_paths


SECRET = "PRIVATE_TEXT_AND_TOKEN_123"


async def open_transport(paths):
    if paths.transport == "tcp":
        endpoint = read_endpoint(paths.endpoint)
        reader, writer = await asyncio.open_connection(HOST, endpoint.port)
        await authenticate_client(reader, writer, endpoint.secret)
        return reader, writer
    return await asyncio.open_unix_connection(str(paths.socket))


@asynccontextmanager
async def fake_service(paths, callback):
    prepare_service_paths(paths)
    if paths.transport == "tcp":
        secret = secrets.token_bytes(32)

        async def authenticated(reader, writer):
            await authenticate_server(reader, writer, secret)
            await callback(reader, writer)

        with profile_exclusive(paths=paths):
            server = await asyncio.start_server(authenticated, HOST, 0)
            endpoint = Endpoint(server.sockets[0].getsockname()[1], secret)
            publish_endpoint(paths.endpoint, endpoint)
            try:
                async with server:
                    yield
            finally:
                remove_endpoint(paths.endpoint, endpoint)
    else:
        server = await asyncio.start_unix_server(callback, path=str(paths.socket))
        paths.socket.chmod(0o600)
        async with server:
            yield


@pytest.mark.asyncio
@pytest.mark.parametrize("failure, category, retryable, retry_after", [
    (ValueError(SECRET), "validation", False, None),
    (SetupRequiredError(SECRET), "authorization", False, None),
    (TdlibError({"code": 401, "message": SECRET}), "authorization", False, None),
    (TdlibError({"code": 403, "message": SECRET}), "access", False, None),
    (TdlibError({"code": 429, "message": "Too Many Requests: retry after 17"}), "flood_wait", True, 17),
    (TimeoutError(SECRET), "timeout", True, None),
    (TdlibError({"code": 500, "message": SECRET}), "unavailable", True, None),
    (CursorError(SECRET), "cursor", False, None),
    (RuntimeError(SECRET), "internal", False, None),
])
async def test_read_failure_crosses_socket_with_safe_diagnostics(private_paths, caplog, failure, category, retryable, retry_after):
    class Backend(ThreadBackend):
        async def get_message(self, **params):
            raise failure

    async with running(private_paths, Backend()):
        client = SharedTelegramBackend(paths=private_paths, autostart=False)
        with pytest.raises(Exception) as caught:
            await client.get_message(chat_id=-123, message_id=2**42)
        message = str(caught.value)
        assert "operation=get_message" in message
        assert f"category={category}" in message
        assert f"retryable={str(retryable).lower()}" in message
        if retry_after is not None:
            assert f"retry_after={retry_after}" in message
        assert "draft" not in message.lower()
        assert SECRET not in message
        diagnostic_id = re.search(r"diagnostic_id=([0-9a-f]{32})", message)[1]
        assert diagnostic_id in caplog.text
        assert type(failure).__name__ in caplog.text
        if isinstance(failure, TdlibError):
            assert str(failure.code) in caplog.text
        assert SECRET not in caplog.text


@pytest.mark.asyncio
async def test_completed_tdlib_rpc_error_keeps_service_available(private_paths):
    class Backend(ThreadBackend):
        async def get_message(self, **params):
            if params["message_id"] == 1:
                raise TdlibError({"code": 400, "message": SECRET})
            return await super().get_message(**params)

    async with running(private_paths, Backend()) as (service, backend):
        client = SharedTelegramBackend(paths=private_paths, autostart=False)
        with pytest.raises(ServiceError):
            await client.get_message(chat_id=1, message_id=1)
        assert not service.stopping.is_set()
        assert (await client.get_message(chat_id=1, message_id=2)).message_id == 2
        assert backend.close_count == 0


@pytest.mark.asyncio
async def test_new_service_keeps_exact_legacy_error_shape(private_paths):
    class Backend(ThreadBackend):
        async def get_message(self, **params):
            raise TdlibError({"code": 429, "message": "FLOOD_WAIT_23"})

    async with running(private_paths, Backend()):
        reader, writer = await open_transport(private_paths)
        try:
            ident = uuid.uuid4().hex
            await write_frame(writer, {"protocol": 1, "id": ident, "operation": "get_message",
                "params": {"chat_id": 1, "message_id": 1}, "timeout": 2}, MAX_REQUEST_BYTES)
            response = await read_frame(reader, MAX_RESPONSE_BYTES)
            assert set(response) == {"protocol", "id", "error"}
            assert response["id"] == ident
            assert set(response["error"]) == {"code", "message"}
            assert response["error"]["code"] == "ServiceError"
            assert len(response["error"]["message"]) <= 512
            assert "retry_after=23" in response["error"]["message"]
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.asyncio
async def test_missing_service_is_read_unavailable_without_draft_hint(private_paths):
    with pytest.raises(ServiceError) as caught:
        await _request(private_paths, "get_message", {"chat_id": 1, "message_id": 1}, timeout=1)
    assert "operation=get_message" in str(caught.value)
    assert "category=unavailable" in str(caught.value)
    assert "retryable=true" in str(caught.value)
    assert "draft" not in str(caught.value).lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,params,has_hint", [
    ("get_message", {"chat_id": 1, "message_id": 1}, False),
    ("get_context", {"chat_id": 1, "message_id": 1, "before": 1, "after": 1}, False),
    ("send_message", {"draft_id": "a" * 32}, True),
    ("get_send_status", {"draft_id": "a" * 32}, True),
    ("prepare_message", {"draft_id": "a" * 32, "recipient": "1", "text": "synthetic", "file_path": None}, True),
])
async def test_lost_response_hint_is_only_for_ambiguous_send(private_paths, operation, params, has_hint):
    calls = []

    async def disconnect(reader, writer):
        calls.append(await read_frame(reader, MAX_REQUEST_BYTES))
        writer.close()
        await writer.wait_closed()

    async with fake_service(private_paths, disconnect):
        with pytest.raises(ServiceError) as caught:
            await _request(private_paths, operation, params, timeout=1)
        message = str(caught.value)
        assert f"operation={operation}" in message
        assert "category=unavailable" in message
        assert ("draft ID" in message) is has_hint
        assert f"retryable={str(not has_hint).lower()}" in message
        assert len(calls) == 1, "A dispatched request is never automatically replayed"


@pytest.mark.asyncio
async def test_new_proxy_sanitizes_old_service_read_error(private_paths):
    async def legacy_error(reader, writer):
        request = await read_frame(reader, MAX_REQUEST_BYTES)
        await write_frame(writer, {"protocol": 1, "id": request["id"], "error": {
            "code": "ServiceError", "message": "Telegram operation failed. For an outgoing message, check its existing draft ID before taking any further action."
        }}, MAX_RESPONSE_BYTES)
        writer.close()
        await writer.wait_closed()

    async with fake_service(private_paths, legacy_error):
        with pytest.raises(ServiceError) as caught:
            await _request(private_paths, "get_message", {"chat_id": 1, "message_id": 1}, timeout=1)
        assert "category=internal" in str(caught.value)
        assert "draft" not in str(caught.value).lower()


@pytest.mark.asyncio
async def test_local_validation_preserves_category_without_pydantic_input(private_paths, caplog):
    from telegram_search_mcp.wire import ServiceProtocolError
    with pytest.raises(ServiceProtocolError) as caught:
        await _request(private_paths, "search_chat_messages", {"chat_id": SECRET}, timeout=1)
    message = str(caught.value)
    assert "operation=search_chat_messages" in message
    assert "category=validation" in message
    assert SECRET not in message + caplog.text
    assert "ValidationError" in caplog.text
    # SDKs may print exception chains. The outgoing exception suppresses the
    # original Pydantic error because its string contains the rejected input.
    assert caught.value.__suppress_context__


@pytest.mark.asyncio
async def test_mcp_reports_read_error_instead_of_empty_result(private_paths):
    from mcp import Client
    from telegram_search_mcp.server import create_server

    class Backend(ThreadBackend):
        async def get_message(self, **params):
            raise TdlibError({"code": 403, "message": SECRET})

    async with running(private_paths, Backend()):
        proxy = SharedTelegramBackend(paths=private_paths, autostart=False)
        async with Client(create_server(proxy)) as client:
            result = await client.call_tool("telegram_get_message", {"chat_id": 1, "message_id": 1})
        assert result.is_error
        message = " ".join(item.text for item in result.content if hasattr(item, "text"))
        assert "operation=get_message" in message
        assert "category=access" in message
        assert "draft" not in message.lower()
        assert SECRET not in message


@pytest.mark.parametrize("operation,has_hint", [("get_message", False), ("send_message", True), ("get_send_status", True), ("prepare_message", True)])
def test_internal_failure_hint_is_operation_aware(operation, has_hint):
    from telegram_search_mcp.service import error_result
    result = error_result(RuntimeError(SECRET), operation)
    assert ("draft ID" in result["message"]) is has_hint
    assert "retryable=false" in result["message"]
    assert SECRET not in result["message"]


def test_flood_wait_does_not_echo_unrecognized_tdlib_message(caplog):
    from telegram_search_mcp.service import error_result
    result = error_result(TdlibError({"code": 429, "message": f"FLOOD_WAIT_23 {SECRET}"}), "get_message")
    assert "category=flood_wait" in result["message"]
    assert "retry_after=" not in result["message"]
    assert SECRET not in result["message"] + caplog.text


@pytest.mark.parametrize("message,category", [("Can't access the chat", "access"), ("SESSION_REVOKED", "authorization")])
def test_tdlib_400_known_access_and_authorization_errors(message, category):
    from telegram_search_mcp.service import error_result
    result = error_result(TdlibError({"code": 400, "message": message}), "get_message")
    assert f"category={category}" in result["message"]


@pytest.mark.parametrize("failure", [RuntimeError(SECRET), TimeoutError(SECRET), ConnectionError(SECRET)])
def test_ambiguous_chat_draft_update_requires_reconciliation(failure):
    from telegram_search_mcp.service import error_result
    message = error_result(failure, "set_chat_draft")["message"]
    assert "retryable=false" in message
    assert "get_chat_draft" in message and "same operation_id" in message
    assert "outgoing" not in message and "draft ID" not in message
    assert SECRET not in message
