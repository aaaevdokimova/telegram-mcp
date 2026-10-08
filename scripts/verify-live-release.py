#!/usr/bin/env python3
"""Accept an installed release using an already authorized, owner-selected chat.

Run only after the owner explicitly approves live acceptance for this chat, using
the current installed Python with -I. No login, installation, registration,
sending, drafts, transcription, public search or read acknowledgements are run.
Normal Telegram reads may start the installed shared service and populate its
local cache. A mismatched running service is rejected, never restarted here.
Only counts and release/service checks are reported; chat content stays in memory.
--output is the only explicit report-file write.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import re
import sys


class AcceptanceError(RuntimeError):
    """A source-owned, content-free acceptance failure."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AcceptanceError(message)


def check_install(root: Path, version: str, revision: str) -> Path:
    import telegram_search_mcp
    from telegram_search_mcp.launchers import current_version, validate_launcher

    require(bool(sys.flags.isolated), "Run the current installed Python with -I")
    current = current_version(root)
    require(Path(sys.prefix).resolve() == current / ".venv", "Python is not from the current installation")
    receipt = json.loads((root / "installation.json").read_text())
    require(receipt.get("version") == version, "Installation receipt version mismatch")
    require(receipt.get("revision") == revision, "Installation receipt revision mismatch")
    require(telegram_search_mcp.__version__ == version, "Imported package version mismatch")
    source_revision = current / "SOURCE_REVISION"
    if source_revision.exists():
        require(source_revision.read_text().strip() == revision, "Installed source revision mismatch")
    # Also reject a source checkout or a stale wheel injected into this Python.
    for name in ("__init__", "server", "service", "service_client", "navigation", "tdlib_backend", "diagnostics"):
        module_name = "telegram_search_mcp" + ("." + name if name != "__init__" else "")
        spec = importlib.util.find_spec(module_name)
        require(spec is not None and spec.origin is not None, "Installed module is missing")
        origin = Path(spec.origin).resolve()
        require(origin.is_relative_to(current), "Imported module is outside the current installation")
        source = current / "src" / "telegram_search_mcp" / (name + ".py")
        require(hashlib.sha256(origin.read_bytes()).digest() == hashlib.sha256(source.read_bytes()).digest(),
                "Installed module differs from the current source")
    validate_launcher(root)
    return current


async def check_service(version: str, *, optional: bool = False) -> dict:
    from telegram_search_mcp.service_client import service_status

    status = await service_status(connect=False)
    if not status.get("running") and optional:
        require(not status.get("profile_busy"), "Telegram profile is busy without an available service")
        return {"running": False}
    require(status.get("running") is True and not status.get("stopping"), "Shared service is not available")
    require(status.get("version") == version, "Shared service version mismatch; activate the release first")
    return {"running": True, "version": status["version"], "protocol": status.get("protocol")}


async def call(client, tool: str, params: dict) -> dict:
    result = await client.call_tool(tool, params)
    require(not result.is_error, "MCP read returned an error")
    require(isinstance(result.structured_content, dict), "MCP read returned no structured result")
    return result.structured_content


def message_ids(rows: list, chat_id: int) -> list[int]:
    require(all(isinstance(row, dict) and row.get("chat_id") == chat_id
                and type(row.get("message_id")) is int and 0 < row["message_id"] < 2**53
                for row in rows), "Read returned invalid or unrelated messages")
    ids = [row["message_id"] for row in rows]
    require(len(ids) == len(set(ids)), "Read returned duplicate messages")
    return ids


async def pages(client, tool: str, params: dict, maximum: int) -> tuple[list, dict]:
    rows, cursors, counts = [], set(), []
    cursor = None
    for _ in range(maximum):
        page = await call(client, tool, {**params, "cursor": cursor})
        items = page.get("items")
        require(isinstance(items, list) and len(items) <= params["limit"], "Read exceeded its page limit")
        rows.extend(items)
        counts.append(len(items))
        ids = message_ids(rows, params["chat_id"])
        require(ids == sorted(ids, reverse=True), "Read order is not descending")
        cursor = page.get("next_cursor")
        if cursor is None:
            break
        require(isinstance(cursor, str) and cursor and cursor not in cursors, "Pagination cursor did not advance")
        cursors.add(cursor)
    return rows, {"page_counts": counts, "messages": len(rows), "has_more": cursor is not None,
                  "unique": True, "descending": True}


async def read_checks(client, chat_id: int, query: str | None, maximum: int) -> dict:
    # A fixed exclusive bound prevents newly arriving messages shifting page one.
    bound = datetime.now(timezone.utc).isoformat(timespec="seconds")
    params = {"chat_id": chat_id, "date_to": bound, "limit": 20}
    rows, history = await pages(client, "telegram_get_chat_history", params, maximum)
    require(len(rows) > 20, "Select a chat with more than twenty accessible messages to test the second page")
    single, _ = await pages(client, "telegram_get_chat_history", {**params, "limit": 1}, 20)
    require(message_ids(single, chat_id) == message_ids(rows[:20], chat_id),
            "Changing the history page size changed the first twenty messages")
    history["page_size_invariant"] = True
    anchor = next((row for row in rows if row.get("content_type") == "messageText"), None)
    require(anchor is not None, "Select a chat with an accessible text message to test context")
    context = await call(client, "telegram_get_context", {
        "chat_id": chat_id, "message_id": anchor["message_id"], "before": 2, "after": 2})
    context_rows = context.get("messages")
    require(isinstance(context_rows, list), "Context returned invalid messages")
    ids = message_ids(context_rows, chat_id)
    mid = anchor["message_id"]
    require(mid in ids and len(ids) <= 5 and ids == sorted(ids)
            and sum(value < mid for value in ids) <= 2 and sum(value > mid for value in ids) <= 2,
            "Context did not preserve its anchor, bounds or message order")
    report = {"history": history, "context": {"messages": len(ids), "anchor_present": True,
                                              "bounded": True, "ascending": True}}
    if query is not None:
        _, report["search"] = await pages(client, "telegram_search_chat_messages", {**params, "query": query}, maximum)
    return report


async def verify(args) -> dict:
    from mcp import Client
    from mcp.client.stdio import stdio_client, StdioServerParameters
    from telegram_search_mcp.launchers import LAUNCHER_NAME

    current = check_install(args.install_root, args.version, args.revision)
    await check_service(args.version, optional=True)
    launcher = args.install_root / LAUNCHER_NAME
    if os.name == "nt":
        command, arguments = "powershell.exe", ["-NoProfile", "-NonInteractive", "-File", str(launcher)]
    else:
        command, arguments = "/bin/sh", [str(launcher)]
    parameters = StdioServerParameters(command=command, args=arguments)
    with open(os.devnull, "w") as errors:
        async with Client(stdio_client(parameters, errlog=errors)) as client:
            require(client.server_info is not None and client.server_info.version == args.version,
                    "MCP proxy version mismatch")
            # Discovery does not connect TDLib; the first authorized read does.
            await client.list_tools()
            checks = await read_checks(client, args.chat_id, args.query, args.pages)
            service = await check_service(args.version)
    # Catch an update racing the bounded acceptance run.
    require(check_install(args.install_root, args.version, args.revision) == current,
            "Current installation changed during acceptance")
    return {"status": "passed", "version": args.version, "revision": args.revision,
            "installed_source_matches": True, "stdio_proxy_matches": True, "service": service, **checks}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-root", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--chat-id", required=True, type=int)
    parser.add_argument("--query")
    parser.add_argument("--pages", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if (not args.install_root.is_absolute() or not re.fullmatch(r"\d+\.\d+\.\d+", args.version)
            or not re.fullmatch(r"[0-9a-f]{40}", args.revision) or not args.chat_id
            or not 2 <= args.pages <= 20 or (args.query is not None and not 2 <= len(args.query.strip()) <= 200)):
        parser.error("Use an absolute install root, semantic version, full lowercase revision, nonzero chat ID, 2..20 pages and an optional 2..200 character query")
    logging.disable(logging.CRITICAL)
    try:
        report = asyncio.run(verify(args))
    except Exception as exc:
        report = {"status": "failed", "error_type": type(exc).__name__}
        if isinstance(exc, AcceptanceError):
            report["check"] = str(exc)
    rendered = json.dumps(report, indent=2) + "\n"
    if args.output is not None:
        args.output.write_text(rendered)
    print(rendered, end="")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
