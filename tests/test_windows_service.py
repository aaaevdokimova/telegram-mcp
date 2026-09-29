"""Authenticated loopback transport tests, also run on native Windows CI.

Only explicit temporary directories and fake backends are used. TCP transport
is selectable independently of the host OS; filesystem security/locks always
use the real host implementation rather than an OS identity monkeypatch.
"""
from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import time

import pytest

from telegram_search_mcp.backend import RawMessage
from telegram_search_mcp import service_client
from telegram_search_mcp.platform_support import assert_private_path, ensure_private_dir, open_private_file
from telegram_search_mcp.service import LocalService, ServicePaths, profile_exclusive, service_lock_held
from telegram_search_mcp.service_client import SharedTelegramBackend, _request, service_status, stop_service
from telegram_search_mcp.windows_transport import (
    Endpoint, HOST, MAGIC, authenticate_client, publish_endpoint, read_endpoint,
)
from telegram_search_mcp.wire import MAX_REQUEST_BYTES, ServiceBusyError, ServiceError, ServiceProtocolError


class FakeBackend:
    def __init__(self):
        self.active = 0
        self.maximum = 0
        self.closed = False
        self.calls = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def get_message(self, *, chat_id, message_id):
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        self.calls.append(message_id)
        self.entered.set()
        try:
            await self.release.wait()
            await asyncio.sleep(0.01)
            return RawMessage(chat_id, "fake", message_id, None, datetime.now(timezone.utc),
                              json.dumps({"pid": os.getpid(), "maximum": self.maximum}), "messageText")
        finally:
            self.active -= 1

    async def close(self):
        assert not self.active
        self.closed = True


@pytest.fixture
def tcp_paths(tmp_path):
    # pytest's own root may have inherited Windows ACLs. Every owned test path
    # below this subdirectory is created with the real private-directory helper.
    private = tmp_path / "private"
    ensure_private_dir(private)
    return ServicePaths(private / "service", transport="tcp")


@contextlib.asynccontextmanager
async def running(paths, backend=None, **kwargs):
    backend = backend or FakeBackend()
    service = LocalService(backend, paths=paths, **kwargs)
    task = asyncio.create_task(service.run())
    ready = asyncio.create_task(service.ready.wait())
    try:
        done, _ = await asyncio.wait({task, ready}, timeout=3, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            await task
        assert ready in done
        yield service, backend
    finally:
        ready.cancel()
        backend.release.set()
        service.request_stop()
        await asyncio.wait_for(task, 6)


async def eventually(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_loopback_clients_share_one_serial_backend_and_private_endpoint(tcp_paths):
    async with running(tcp_paths, queue_limit=32) as (_, backend):
        endpoint = read_endpoint(tcp_paths.endpoint)
        assert 1 <= endpoint.port <= 65535 and len(endpoint.secret) == 32
        assert_private_path(tcp_paths.endpoint)
        assert_private_path(tcp_paths.directory, directory=True)
        assert (await service_status(paths=tcp_paths))["running"]
        clients = [SharedTelegramBackend(paths=tcp_paths, autostart=False) for _ in range(20)]
        results = await asyncio.gather(*(c.get_message(chat_id=1, message_id=i+1) for i, c in enumerate(clients)))
        assert sorted(item.message_id for item in results) == list(range(1, 21))
        assert backend.maximum == 1
        with pytest.raises(ServiceBusyError):
            with profile_exclusive(paths=tcp_paths):
                pytest.fail("Second profile owner was accepted")
    assert backend.closed and not tcp_paths.endpoint.exists()
    assert tcp_paths.lock.exists(), "Lock files must persist across service lifetimes"


@pytest.mark.asyncio
async def test_loopback_stop_drains_active_work_before_unlock(tcp_paths):
    backend = FakeBackend()
    backend.release.clear()
    async with running(tcp_paths, backend) as (service, _):
        client = SharedTelegramBackend(paths=tcp_paths, autostart=False)
        active = asyncio.create_task(client.get_message(chat_id=1, message_id=1))
        await backend.entered.wait()
        stop = asyncio.create_task(stop_service(paths=tcp_paths))
        await eventually(service.stopping.is_set)
        assert not stop.done() and not backend.closed and service_lock_held(tcp_paths)
        backend.release.set()
        assert await stop == {"running": False, "stopped": True}
        await active
    assert backend.closed and not service_lock_held(tcp_paths)


@pytest.mark.asyncio
async def test_loopback_idle_stops_and_restart_rotates_secret(tcp_paths):
    async with running(tcp_paths, idle_timeout=0.04) as (_, backend):
        original = read_endpoint(tcp_paths.endpoint)
        await eventually(lambda: not service_lock_held(tcp_paths))
        assert backend.closed
    async with running(tcp_paths):
        assert read_endpoint(tcp_paths.endpoint).secret != original.secret


@pytest.mark.asyncio
async def test_unauthenticated_control_or_oversized_bytes_never_dispatch(tcp_paths, monkeypatch):
    async with running(tcp_paths) as (service, backend):
        endpoint = read_endpoint(tcp_paths.endpoint)
        responses = []
        original_respond = service._respond
        async def record_response(*args):
            responses.append(args)
            await original_respond(*args)
        monkeypatch.setattr(service, "_respond", record_response)
        for payload in (b'{"operation":"stop"}', b"x" * (MAX_REQUEST_BYTES + 1024)):
            reader, writer = await asyncio.open_connection(HOST, endpoint.port)
            try:
                await reader.readexactly(len(MAGIC) + 32)
                writer.write(payload + b"x" * 40)
                await writer.drain()
                # Rejecting a bad handshake with unread attacker bytes can
                # close TCP with RST instead of FIN/EOF, depending on the OS.
                with contextlib.suppress(ConnectionResetError):
                    assert await asyncio.wait_for(reader.read(128), 2) == b""
            finally:
                writer.close()
                with contextlib.suppress(ConnectionResetError):
                    await asyncio.wait_for(writer.wait_closed(), 2)
        await eventually(lambda: not service.connections)
        assert not service.stopping.is_set() and not backend.calls and not responses


@pytest.mark.asyncio
async def test_wrong_client_secret_is_rejected_before_rpc(tcp_paths):
    async with running(tcp_paths) as (service, backend):
        endpoint = read_endpoint(tcp_paths.endpoint)
        reader, writer = await asyncio.open_connection(HOST, endpoint.port)
        try:
            with pytest.raises(ServiceProtocolError, match="authentication"):
                await authenticate_client(reader, writer, secrets.token_bytes(32))
        finally:
            writer.close()
            await writer.wait_closed()
        assert not service.stopping.is_set() and not backend.calls


@pytest.mark.asyncio
async def test_unknown_listener_gets_no_client_proof_or_telegram_payload(tcp_paths):
    captured = asyncio.get_running_loop().create_future()
    secret = secrets.token_bytes(32)
    async def impostor(reader, writer):
        writer.write(MAGIC + secrets.token_bytes(32))
        await writer.drain()
        hello = await reader.readexactly(len(MAGIC) + 32)
        writer.write(b"x" * 32)  # Does not know the shared secret.
        await writer.drain()
        remaining = await reader.read()
        captured.set_result((hello, remaining))
        writer.close()
        await writer.wait_closed()
    server = await asyncio.start_server(impostor, HOST, 0)
    try:
        with profile_exclusive(paths=tcp_paths):
            publish_endpoint(tcp_paths.endpoint, Endpoint(server.sockets[0].getsockname()[1], secret))
            with pytest.raises(ServiceProtocolError, match="authentication"):
                await _request(tcp_paths, "get_message", {"chat_id": 1, "message_id": 1}, timeout=2)
            hello, remaining = await asyncio.wait_for(captured, 2)
        assert len(hello) == len(MAGIC) + 32 and remaining == b""
        assert secret not in hello
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_unresponsive_handshake_is_bounded(tcp_paths, monkeypatch):
    from telegram_search_mcp import windows_transport
    monkeypatch.setattr(windows_transport, "HANDSHAKE_TIMEOUT", 0.05)
    async with running(tcp_paths) as (service, _):
        endpoint = read_endpoint(tcp_paths.endpoint)
        reader, writer = await asyncio.open_connection(HOST, endpoint.port)
        await reader.readexactly(len(MAGIC) + 32)
        assert await asyncio.wait_for(reader.read(1), 1) == b""
        await eventually(lambda: not service.connections)
        writer.close()
        await writer.wait_closed()
        assert (await service_status(paths=tcp_paths))["running"]


@pytest.mark.parametrize("change", [
    {"host": "0.0.0.0"}, {"host": "localhost"}, {"host": "192.0.2.1"},
    {"port": True}, {"port": 0}, {"port": 65536}, {"secret": "x" * 64},
    {"secret": "00" * 31}, {"extra": 1},
])
def test_endpoint_validation_cannot_redirect_client(tcp_paths, change):
    ensure_private_dir(tcp_paths.directory)
    value = {"transport": "tcp-hmac-v1", "host": HOST, "port": 12345, "secret": "00" * 32, **change}
    fd = open_private_file(tcp_paths.endpoint)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream)
    with pytest.raises(ServiceError, match="endpoint"):
        read_endpoint(tcp_paths.endpoint)


async def process_client(paths):
    child = await asyncio.create_subprocess_exec(sys.executable, str(Path(__file__).resolve()),
        "client", str(paths.directory), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await asyncio.wait_for(child.communicate(), 20)
    assert child.returncode == 0, stderr.decode()
    return json.loads(stdout)


@pytest.mark.asyncio
async def test_concurrent_process_startup_and_crash_recovery_keep_lock_file(tcp_paths):
    try:
        results = await asyncio.gather(*(process_client(tcp_paths) for _ in range(5)))
        pids = {result["pid"] for result in results}
        assert len(pids) == 1 and all(result["maximum"] == 1 for result in results)
        original = read_endpoint(tcp_paths.endpoint)
        lock_identity = tcp_paths.lock.stat().st_ino
        # Only our fake fixture's PID is ever terminated. No service.signal
        # handlers are registered by fixture_main, so this simulates a crash.
        os.kill(pids.pop(), signal.SIGTERM)
        await eventually(lambda: not service_lock_held(tcp_paths))
        assert tcp_paths.endpoint.exists()
        assert await service_status(paths=tcp_paths) == {"running": False, "profile_busy": False}
        restarted = await process_client(tcp_paths)
        assert restarted["maximum"] == 1
        assert read_endpoint(tcp_paths.endpoint).secret != original.secret
        assert tcp_paths.lock.stat().st_ino == lock_identity
        assert (tcp_paths.directory.parent / "spawn-count").read_text().splitlines() == ["spawn", "spawn"]
    finally:
        await stop_service(paths=tcp_paths)


@pytest.mark.asyncio
async def test_old_loopback_service_is_drained_before_version_switch(tcp_paths):
    async with running(tcp_paths) as (service, backend):
        original_status = service.status
        service.status = lambda: {**original_status(), "version": "0.0.1"}
        assert await service_client._compatible_service("default", tcp_paths) is None
        assert backend.closed and not service_lock_held(tcp_paths)


@pytest.mark.skipif(os.name != "nt", reason="Native Windows socket exclusivity")
@pytest.mark.asyncio
async def test_native_windows_listener_cannot_be_rebound_by_another_socket(tcp_paths):
    import socket
    async with running(tcp_paths):
        endpoint = read_endpoint(tcp_paths.endpoint)
        impostor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            impostor.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            with pytest.raises(OSError):
                impostor.bind((HOST, endpoint.port))
        finally:
            impostor.close()


@pytest.mark.skipif(os.name != "nt", reason="Native Windows install path semantics")
def test_native_windows_current_pointer_resolves_new_python_without_symlinks(tcp_paths):
    from telegram_search_mcp import launchers
    root = tcp_paths.directory.parent / "install"
    ensure_private_dir(root)
    def write(path, text):
        fd = open_private_file(path)
        os.ftruncate(fd, 0)
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
    write(root / launchers.ROOT_MARKER, "telegram-search-mcp\n")
    previous = root / "version-0.9.0-aaaaaaaaaaaa"
    current = root / "version-0.9.1-bbbbbbbbbbbb"
    for version in (previous, current):
        ensure_private_dir(version / ".venv" / "Scripts")
        write(version / launchers.VERSION_MARKER, "telegram-search-mcp\n")
    write(root / "current.json", json.dumps({"version": current.name}))
    assert launchers.current_version(root) == current
    assert launchers.installed_root(str(previous / ".venv" / "Scripts" / "python.exe")) == root
    for escaped in ("../outside", str(current), "version-0.9.1-bbbbbbbbbbbb/child"):
        write(root / "current.json", json.dumps({"version": escaped}))
        with pytest.raises(RuntimeError, match="outside"):
            launchers.current_version(root)


def spawn_fake(profile, paths):
    # ensure_service holds startup.lock while calling this fixture.
    fd = open_private_file(paths.directory.parent / "spawn-count")
    try:
        os.lseek(fd, 0, os.SEEK_END)
        os.write(fd, b"spawn\n")
    finally:
        os.close(fd)
    options = ({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True})
    child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "daemon", str(paths.directory)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
    service_client._CHILDREN.append(child)
    return child


async def fixture_main():
    mode, directory = sys.argv[1:3]
    paths = ServicePaths(Path(directory), transport="tcp")
    if mode == "daemon":
        await LocalService(FakeBackend(), paths=paths, idle_timeout=5).run()
    else:
        service_client._spawn = spawn_fake
        await service_client.ensure_service(paths=paths)
        backend = SharedTelegramBackend(paths=paths, autostart=False)
        message = await backend.get_message(chat_id=1, message_id=1)
        print(message.text, flush=True)


if __name__ == "__main__":
    asyncio.run(fixture_main())
