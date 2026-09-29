"""Private loopback transport for Windows' default Proactor event loop.

The endpoint is an owner-only file, never an environment variable. Every service
lifetime rotates its secret. A fixed-size, mutually authenticated handshake
proves possession before either side sends any Telegram/control request. The
secret itself never crosses the socket, including when a stale port is reused.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
from dataclasses import dataclass

from .platform_support import open_owned_readonly, open_private_file, replace_private_file
from .wire import ServiceError, ServiceProtocolError

HOST = "127.0.0.1"
MAGIC = b"TGSMCPW1"
NONCE_BYTES = 32
ENDPOINT_LIMIT = 1024
HANDSHAKE_TIMEOUT = 5.0


@dataclass(frozen=True)
class Endpoint:
    port: int
    secret: bytes


def read_endpoint(path: Path) -> Endpoint:
    fd = open_owned_readonly(path, private=True)
    try:
        data = os.read(fd, ENDPOINT_LIMIT + 1)
    finally:
        os.close(fd)
    try:
        value = json.loads(data)
        if (len(data) > ENDPOINT_LIMIT or not isinstance(value, dict)
                or set(value) != {"transport", "host", "port", "secret"}
                or value["transport"] != "tcp-hmac-v1" or value["host"] != HOST
                or type(value["port"]) is not int or not 1 <= value["port"] <= 65535
                or not isinstance(value["secret"], str) or len(value["secret"]) != 64):
            raise ValueError()
        secret = bytes.fromhex(value["secret"])
        if len(secret) != NONCE_BYTES:
            raise ValueError()
        return Endpoint(value["port"], secret)
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise ServiceError("Invalid private Telegram service endpoint") from exc


def publish_endpoint(path: Path, endpoint: Endpoint) -> None:
    # Reject unexpected files before replacement, including reparse points and
    # files whose owner/ACL permits another account to replace the endpoint.
    try:
        existing = open_owned_readonly(path, private=True)
    except FileNotFoundError:
        pass
    else:
        os.close(existing)
    temporary = path.with_name(path.name + "." + secrets.token_hex(12) + ".tmp")
    fd = open_private_file(temporary, exclusive=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"transport": "tcp-hmac-v1", "host": HOST,
                       "port": endpoint.port, "secret": endpoint.secret.hex()}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        replace_private_file(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def remove_endpoint(path: Path, expected: Endpoint) -> None:
    try:
        current = read_endpoint(path)
    except FileNotFoundError:
        return
    if current == expected:
        path.unlink()


def listener_socket() -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # Windows otherwise permits another local process to bind a listening
        # port with SO_REUSEADDR. Exclusive binding closes that spoofing avenue.
        if os.name == "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind((HOST, 0))
        listener.listen(40)
        listener.setblocking(False)
        return listener
    except BaseException:
        listener.close()
        raise


def _proof(secret: bytes, role: bytes, server_nonce: bytes, client_nonce: bytes) -> bytes:
    return hmac.digest(secret, MAGIC + role + server_nonce + client_nonce, hashlib.sha256)


async def authenticate_server(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, secret: bytes) -> None:
    """Server proves its identity first, then authenticates the client."""
    async with asyncio.timeout(HANDSHAKE_TIMEOUT):
        server_nonce = secrets.token_bytes(NONCE_BYTES)
        writer.write(MAGIC + server_nonce)
        await writer.drain()
        hello = await reader.readexactly(len(MAGIC) + NONCE_BYTES)
        if hello[:len(MAGIC)] != MAGIC:
            raise ServiceProtocolError("Private service authentication failed")
        client_nonce = hello[len(MAGIC):]
        writer.write(_proof(secret, b"server", server_nonce, client_nonce))
        await writer.drain()
        supplied = await reader.readexactly(NONCE_BYTES)
        if not hmac.compare_digest(supplied, _proof(secret, b"client", server_nonce, client_nonce)):
            raise ServiceProtocolError("Private service authentication failed")


async def authenticate_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, secret: bytes) -> None:
    """Do not send a client proof or RPC to an unauthenticated listener."""
    async with asyncio.timeout(HANDSHAKE_TIMEOUT):
        hello = await reader.readexactly(len(MAGIC) + NONCE_BYTES)
        if hello[:len(MAGIC)] != MAGIC:
            raise ServiceProtocolError("Private service authentication failed")
        server_nonce = hello[len(MAGIC):]
        client_nonce = secrets.token_bytes(NONCE_BYTES)
        writer.write(MAGIC + client_nonce)
        await writer.drain()
        supplied = await reader.readexactly(NONCE_BYTES)
        if not hmac.compare_digest(supplied, _proof(secret, b"server", server_nonce, client_nonce)):
            raise ServiceProtocolError("Private service authentication failed")
        writer.write(_proof(secret, b"client", server_nonce, client_nonce))
        await writer.drain()
