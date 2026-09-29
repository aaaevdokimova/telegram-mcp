from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace
import pytest

from telegram_search_mcp import keychain, profile_binding

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="macOS Keychain adapter; Credential Manager has separate tests")


@pytest.fixture(autouse=True)
def isolated_profile_binding(monkeypatch, tmp_path):
    # Never inspect the developer's migrated Keychain namespace in unit tests.
    monkeypatch.setattr(profile_binding, "shared_root", lambda: tmp_path / "shared")


def test_set_secret_uses_detached_stdin_and_never_argv(monkeypatch) -> None:
    captured = {}

    def fake_run(arguments, **kwargs):
        captured["arguments"] = arguments
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    keychain.set_secret("api_hash", "sensitive-value")

    arguments = captured["arguments"]
    kwargs = captured["kwargs"]
    assert arguments[-1] == "-w"
    assert "sensitive-value" not in arguments
    assert kwargs["input"] == "sensitive-value\nsensitive-value\n"
    assert kwargs["start_new_session"] is True
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert keychain.SERVICE == "local.unofficial-telegram-search-mcp-shared"
    assert keychain.SERVICE in arguments


def test_get_secret_does_not_probe_other_product_namespaces(monkeypatch) -> None:
    captured = []

    def fake_run(arguments, **kwargs):
        captured.append(arguments)
        return SimpleNamespace(returncode=44, stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert keychain.get_secret("api_hash") is None
    assert len(captured) == 1
    assert keychain.SERVICE in captured[0]
