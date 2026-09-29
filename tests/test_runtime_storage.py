"""Native private storage for durable operations, with synthetic sessions only."""
from __future__ import annotations

import os
import uuid

import pytest

from telegram_search_mcp.chat_drafts import SetDraftRequest, set_draft, version
from telegram_search_mcp.downloads import export_file
from telegram_search_mcp.outgoing import Outbox
from telegram_search_mcp.platform_support import assert_private_path, ensure_private_dir, open_private_file
from telegram_search_mcp.speech import transcribe


class Session:
    user_id = 42

    def __init__(self):
        self.dispatched = []

    def get_chat(self, chat_id, timeout=30):
        return {"@type": "chat", "id": chat_id, "title": "Synthetic", "type": {"@type": "chatTypePrivate"}, "draft_message": None}

    def request(self, request, timeout=30):
        kind = request["@type"]
        if kind == "getMessage":
            return {"@type": "message", "chat_id": request["chat_id"], "id": request["message_id"],
                    "content": {"@type": "messageVoiceNote", "voice_note": {"duration": 1, "speech_recognition_result": None}}}
        if kind == "getMessageProperties":
            return {"can_recognize_speech": True}
        if kind in {"setChatDraftMessage", "recognizeSpeech", "sendMessage"}:
            self.dispatched.append(kind)
            raise TimeoutError("Synthetic timeout; no Telegram request occurred")
        raise AssertionError(request)


def test_draft_dispatch_marker_survives_process_restart(tmp_path):
    root = tmp_path.resolve() / "drafts"
    request = SetDraftRequest(chat_id=123, operation_id=uuid.uuid4().hex, text="synthetic", expected_version=version(None))
    first, restarted = Session(), Session()
    assert set_draft(first, request, root)["status"] == "unknown"
    assert set_draft(restarted, request, root)["status"] == "unknown"
    assert first.dispatched == ["setChatDraftMessage"]
    assert restarted.dispatched == []
    assert_private_path(root / (request.operation_id + ".json"))


def test_speech_dispatch_marker_survives_process_restart(tmp_path):
    root = tmp_path.resolve() / "recognition"
    first, restarted = Session(), Session()
    assert transcribe(first, root, chat_id=123, message_id=10, wait_seconds=0)["status"] == "pending"
    assert transcribe(restarted, root, chat_id=123, message_id=10, wait_seconds=0)["status"] == "pending"
    assert first.dispatched == ["recognizeSpeech"]
    assert restarted.dispatched == []
    assert_private_path(root / "123_10.json")


def test_outbox_preserves_uncertain_dispatch_with_private_snapshot(tmp_path):
    root = tmp_path.resolve() / "source"
    ensure_private_dir(root)
    source = root / "attachment.txt"
    fd = open_private_file(source, exclusive=True)
    os.write(fd, b"synthetic attachment")
    os.close(fd)
    outbox_path = tmp_path.resolve() / "outbox"
    first = Session()
    box = Outbox(outbox_path, first.user_id)
    draft_id = uuid.uuid4().hex
    box.prepare(first, draft_id=draft_id, recipient="123", text="synthetic", file_path=str(source))
    assert_private_path(outbox_path / draft_id / "attachment.txt")
    assert box.send(first, draft_id=draft_id)["status"] == "unknown"
    restarted = Session()
    assert Outbox(outbox_path, restarted.user_id).send(restarted, draft_id=draft_id)["status"] == "unknown"
    assert first.dispatched == ["sendMessage"]
    assert restarted.dispatched == []


@pytest.mark.parametrize("filename", ["CON", "NUL.txt", "com1.exe", "LPT².doc", "bad<>name?.txt"])
def test_export_windows_unsafe_names_are_usable_private_files(tmp_path, filename):
    root = tmp_path.resolve() / "cache"
    ensure_private_dir(root)
    source = root / "download"
    fd = open_private_file(source, exclusive=True)
    os.write(fd, b"synthetic bytes")
    os.close(fd)
    result, size, digest = export_file(source, root, tmp_path.resolve() / "downloads", filename, 100)
    assert result.read_bytes() == b"synthetic bytes"
    assert size == len(b"synthetic bytes")
    assert len(digest) == 64
    assert not any(char in result.name for char in '<>:"|?*')
    assert result.name != filename
    assert_private_path(result)
