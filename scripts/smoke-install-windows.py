#!/usr/bin/env python3
"""Exercise a real Windows archive installation without Telegram authorization.

All installation, plugin and marketplace files live in a disposable directory.
Only MCP initialization/discovery and the local sending preference are exercised;
no tool is called and no Telegram profile or Credential Manager secret is opened.
Requires native Windows x64, Python 3.13, PowerShell and uv.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import zipfile


MCP_DISCOVERY_CHECK = """
import asyncio
import json
from pathlib import Path
import sys
from mcp import Client
from mcp.client.stdio import stdio_client, StdioServerParameters

async def main():
    entry = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))['mcpServers']
    assert len(entry) == 1, 'Expected exactly one installed Telegram MCP server'
    server = next(iter(entry.values()))
    parameters = StdioServerParameters(
        command=server['command'],
        args=server.get('args', []),
        env=server.get('env'),
    )
    async with Client(stdio_client(parameters)) as client:
        result = await client.list_tools()
        expected = {
            'telegram_search_messages', 'telegram_get_message',
            'telegram_get_context', 'telegram_get_media',
            'telegram_list_voice_messages', 'telegram_transcribe_voice',
            'telegram_list_chats', 'telegram_get_chat_history',
            'telegram_search_chat_messages', 'telegram_download_file',
            'telegram_get_message_thread', 'telegram_get_chat_draft',
            'telegram_get_scheduled_messages',
            'telegram_search_public_posts',
        }
        sending = sys.argv[2] == 'sending'
        if sending:
            expected.update({
                'telegram_prepare_message', 'telegram_send_message',
                'telegram_get_send_status', 'telegram_set_chat_draft',
            })
        assert {tool.name for tool in result.tools} == expected
        assert len(result.tools) == (18 if sending else 14)
        writes = {
            'telegram_prepare_message', 'telegram_send_message',
            'telegram_transcribe_voice', 'telegram_download_file',
            'telegram_set_chat_draft',
            'telegram_search_public_posts',
        }
        for tool in result.tools:
            assert tool.annotations is not None
            assert tool.annotations.read_only_hint is (tool.name not in writes)
            assert tool.annotations.destructive_hint is (tool.name == 'telegram_set_chat_draft')
            if tool.name == 'telegram_search_public_posts':
                assert tool.annotations.idempotent_hint is False

# Listing tools must not start the Telegram service or open a Telegram profile.
asyncio.run(main())
print('PASS: installed Windows plugin stdio discovery and annotations; ' + sys.argv[2])
"""

NATIVE_RUNTIME_CHECK = """
import sys
from telegram_search_mcp import __version__
from telegram_search_mcp.native_runtime import bundled_candidates, verify

assert sys.platform == 'win32'
assert sys.version_info[:2] == (3, 13)
assert __version__ == sys.argv[1]
candidates = bundled_candidates()
assert candidates, 'The Windows installation must include its pinned TDLib wheel'
verify(candidates[0])
print('PASS: installed Python/package version and native TDLib version/commit')
"""


def active_version(install: Path) -> Path:
    pointer = json.loads((install / "current.json").read_text(encoding="utf-8"))
    name = pointer["version"]
    assert isinstance(name, str) and name not in {"", ".", ".."}
    assert Path(name).name == name, "Activation must point at a direct child"
    version = (install / name).resolve(strict=True)
    assert version.parent == install, "Activation must stay inside the installation"
    assert (version / ".venv" / "Scripts" / "python.exe").is_file()
    return version


def plugin_configuration(marketplace: Path, install: Path, original: dict) -> Path:
    actual = json.loads(marketplace.read_text(encoding="utf-8"))
    for key, value in original.items():
        if key != "plugins":
            assert actual[key] == value, "Installation changed unrelated marketplace metadata"
    assert original["plugins"][0] in actual["plugins"], "Installation lost an existing plugin"
    entries = [entry for entry in actual["plugins"] if entry.get("name") == "telegram-mcp-work"]
    assert len(entries) == 1, "Installation must register Telegram exactly once"
    source = entries[0]["source"]
    relative = source["path"] if isinstance(source, dict) else source
    marketplace_root = marketplace.parent.parent.parent
    plugin = (marketplace_root / relative).resolve(strict=True)
    assert plugin.is_relative_to(marketplace_root), "Smoke plugin must remain disposable"
    assert (plugin / ".codex-plugin" / "plugin.json").is_file()
    configuration = plugin / ".mcp.json"
    servers = json.loads(configuration.read_text(encoding="utf-8"))["mcpServers"]
    assert len(servers) == 1
    entry = next(iter(servers.values()))
    assert Path(entry["command"]).name.lower() in {"powershell.exe", "pwsh.exe"}
    arguments = entry["args"]
    file_argument = next(index for index, value in enumerate(arguments) if value.lower() == "-file")
    assert Path(arguments[file_argument + 1]).resolve(strict=True) == install / "launch-mcp.ps1"
    return configuration


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    if platform.system() != "Windows" or platform.machine().lower() not in {"amd64", "x86_64"}:
        parser.error("The actual installer smoke test requires native Windows x64")
    from telegram_search_mcp.windows_install import powershell_path
    powershell = powershell_path()
    if not powershell.is_file() or shutil.which("uv") is None:
        parser.error("System Windows PowerShell 5.1 and uv must already be installed")
    spec = importlib.util.spec_from_file_location("release", Path(__file__).with_name("release.py"))
    assert spec is not None and spec.loader is not None
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)
    manifest = release.verify_archive(args.archive)

    # Exercise native PowerShell argument/JSON encoding for ordinary user paths.
    with tempfile.TemporaryDirectory(prefix="telegram windows install check ") as temporary:
        root = Path(temporary).resolve()
        with zipfile.ZipFile(args.archive) as archive:
            archive.extractall(root / "исходники source")
        source = root / "исходники source" / manifest["archive_root"]
        install = root / "приложение with spaces"
        marketplace_root = root / "пользователь home"
        marketplace = marketplace_root / ".agents" / "plugins" / "marketplace.json"
        marketplace.parent.mkdir(parents=True)
        original = {
            "name": "windows-install-smoke",
            "interface": {"displayName": "Preserve this marketplace title"},
            "plugins": [{
                "name": "preserved-smoke-plugin",
                "source": {"source": "local", "path": "./plugins/preserved-smoke-plugin"},
                "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                "category": "Productivity",
            }],
        }
        preserved = marketplace_root / "plugins" / "preserved-smoke-plugin"
        preserved.mkdir(parents=True)
        (preserved / "plugin.json").write_text(json.dumps({"name": "preserved-smoke-plugin"}), encoding="utf-8")
        marketplace.write_text(json.dumps(original), encoding="utf-8")
        command = [
            str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
            str(source / "install-windows.ps1"), "-PrepareOnly",
            "-InstallDir", str(install), "-MarketplacePath", str(marketplace),
        ]
        # Explicit disposable destinations; never override the user's home or credentials.
        subprocess.run(command, check=True, timeout=600)
        first = active_version(install)
        first_python = first / ".venv" / "Scripts" / "python.exe"
        subprocess.run([str(first_python), "-I", "-m", "telegram_search_mcp.cli", "--help"], check=True, timeout=30)
        subprocess.run([str(first_python), "-I", "-c", NATIVE_RUNTIME_CHECK, manifest["version"]], check=True, timeout=30)
        configuration = plugin_configuration(marketplace, install, original)
        subprocess.run([str(first_python), "-I", "-c", MCP_DISCOVERY_CHECK, str(configuration), "default"], check=True, timeout=60)
        subprocess.run([str(first_python), "-I", "-m", "telegram_search_mcp.cli", "sending", "on"], check=True, timeout=30)
        configuration = plugin_configuration(marketplace, install, original)
        subprocess.run([str(first_python), "-I", "-c", MCP_DISCOVERY_CHECK, str(configuration), "sending"], check=True, timeout=60)
        configuration_before = configuration.read_bytes()
        subprocess.run(command, check=True, timeout=600)
        second = active_version(install)
        assert second != first, "Reinstallation must activate a new immutable version"
        assert first_python.is_file(), "Reinstallation must preserve the former interpreter"
        second_python = second / ".venv" / "Scripts" / "python.exe"
        configuration = plugin_configuration(marketplace, install, original)
        assert configuration.read_bytes() == configuration_before, "Reinstallation changed the stable plugin launcher"
        subprocess.run([str(second_python), "-I", "-c", NATIVE_RUNTIME_CHECK, manifest["version"]], check=True, timeout=30)
        subprocess.run([str(second_python), "-I", "-c", MCP_DISCOVERY_CHECK, str(configuration), "sending"], check=True, timeout=60)
        subprocess.run([str(second_python), "-I", "-m", "telegram_search_mcp.cli", "sending", "off"], check=True, timeout=30)
        configuration = plugin_configuration(marketplace, install, original)
        subprocess.run([str(second_python), "-I", "-c", MCP_DISCOVERY_CHECK, str(configuration), "default"], check=True, timeout=60)
        print("PASS: verified archive, isolated native Windows installation and immutable update, marketplace and sending preference preservation, pinned TDLib, CLI and installed plugin discovery. No Telegram authorization or tool invocation performed.")


if __name__ == "__main__":
    main()
