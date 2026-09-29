"""Native Windows installation and local ChatGPT Work plugin registration.

Program versions are immutable. The source archive never contains a profile;
credentials and the TDLib session are created only by the owner's local auth.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

from . import __version__
from .config_io import atomic_write, read_source
from .launchers import ROOT_MARKER, VERSION_MARKER, current_version, validate_root
from .platform_support import (
    acquire_file_lock, assert_private_path, ensure_private_dir, local_data_dir,
    open_private_file, trusted_home,
)

PLUGIN_NAME = "telegram-mcp-work"
RECEIPT = "windows-install.json"


def _json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _write_marker(path: Path) -> None:
    fd = open_private_file(path, exclusive=True)
    with os.fdopen(fd, "wb") as output:
        output.write(b"telegram-search-mcp\n")
        output.flush()
        os.fsync(output.fileno())


def powershell_path() -> Path:
    import win32api
    return Path(win32api.GetWindowsDirectory()) / "System32/WindowsPowerShell/v1.0/powershell.exe"


def windows_launcher_text(root: Path, module: str) -> str:
    if module not in {"telegram_search_mcp.server", "telegram_search_mcp.cli"}:
        raise ValueError("Unsupported launcher module")
    # The root is derived from this script's location. No interpolation of a
    # user path into PowerShell code and no execution through a mutable symlink.
    return '''$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
# Keep native pipes UTF-8 when Windows PowerShell captures their output.
$utf8 = [System.Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$root = $PSScriptRoot
$pointer = Get-Content -LiteralPath (Join-Path $root 'current.json') -Raw | ConvertFrom-Json
$name = [string]$pointer.version
if ($name -notmatch '^version-[0-9]+\\.[0-9]+\\.[0-9]+-[0-9a-f]{12}$') { throw 'Invalid installed version' }
$version = Join-Path $root $name
if ((Get-Item -LiteralPath $version).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Refusing redirected installation' }
if ((Get-Content -LiteralPath (Join-Path $version '.telegram-search-install') -Raw) -ne "telegram-search-mcp`n") { throw 'Invalid installation marker' }
$python = Join-Path $version '.venv\\Scripts\\python.exe'
# Remove workspace injection variables before starting native libraries.
foreach ($key in @('PYTHONPATH','PYTHONHOME','TDJSON_LIBRARY','TGSEARCH_DATA_DIR')) { Remove-Item "Env:$key" -ErrorAction SilentlyContinue }
& $python -I -X utf8 -m ''' + module + ''' @args
exit $LASTEXITCODE
'''


@contextmanager
def install_lock(root: Path):
    fd = open_private_file(root / "windows-install.lock")
    try:
        try:
            acquire_file_lock(fd)
        except BlockingIOError as exc:
            raise RuntimeError("Another Windows installation/settings update is running") from exc
        yield
    finally:
        os.close(fd)


def marketplace_entry() -> dict:
    return {"name": PLUGIN_NAME,
            "source": {"source": "local", "path": "./plugins/" + PLUGIN_NAME},
            "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
            "category": "Productivity"}


def merge_marketplace(before: bytes | None, *, owned_entry: dict | None = None) -> bytes:
    data = json.loads(before) if before is not None else {
        "name": "personal", "interface": {"displayName": "Personal"}, "plugins": []}
    if (not isinstance(data, dict) or not isinstance(data.get("name"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]+", data["name"])
            or not isinstance(data.get("plugins"), list)):
        raise RuntimeError("Invalid local plugin marketplace; repair it before installing")
    matches = [item for item in data["plugins"] if isinstance(item, dict) and item.get("name") == PLUGIN_NAME]
    if matches:
        if len(matches) != 1 or owned_entry is None or matches[0] != owned_entry:
            raise RuntimeError("Telegram plugin entry already exists or was edited; it was not overwritten")
        return before
    data["plugins"].append(marketplace_entry())
    return _json(data)


def _plugin_files(source: Path, root: Path, marketplace: Path) -> dict[Path, bytes]:
    if marketplace.name != "marketplace.json" or marketplace.parent.name != "plugins" or marketplace.parent.parent.name != ".agents":
        raise ValueError("Marketplace path must end in .agents/plugins/marketplace.json")
    directory = marketplace.parents[2] / "plugins" / PLUGIN_NAME
    release = _release_module(source)
    # Raw Git snapshots intentionally omit generated JSON/PowerShell files so
    # deployed macOS 0.6.1 updaters can still accept the source tree unchanged.
    assets = release.windows_assets(release.inventory(source))
    manifest = assets["plugins/" + PLUGIN_NAME + "/.codex-plugin/plugin.json"]
    config = {"mcpServers": {"telegram": {
        "command": str(powershell_path()),
        "args": ["-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(root / "launch-mcp.ps1")],
    }}}
    return {directory / ".codex-plugin/plugin.json": manifest,
            directory / ".mcp.json": _json(config)}


def _unchanged_registration(receipt: dict, marketplace: Path) -> bool:
    if receipt.get("marketplace") != str(marketplace):
        return False
    before = read_source(marketplace)
    if before is None:
        return False
    data = json.loads(before)
    if receipt.get("entry") not in data.get("plugins", []):
        return False
    for name, digest in receipt.get("plugin_files", {}).items():
        content = read_source(Path(name))
        if content is None or hashlib.sha256(content).hexdigest() != digest:
            return False
    return bool(receipt.get("plugin_files"))


def _release_module(source: Path):
    spec = importlib.util.spec_from_file_location("telegram_release", source / "scripts/release.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("Release inventory helper is missing")
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)
    return release


def _copy_source(source: Path, destination: Path) -> None:
    release = _release_module(source)
    # Complete allowlist/data audit before copying any program file.
    payload = release.inventory(source)
    for name, content in payload.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def install(source: Path, root: Path, marketplace: Path, uv: Path) -> dict:
    if sys.platform != "win32":
        raise RuntimeError("This installer requires native Windows x64")
    import platform
    if platform.machine().lower() not in {"amd64", "x86_64"}:
        raise RuntimeError("This release requires Windows x64; native ARM64 is not supported")
    # Do not resolve away reparse points before validating them.
    root = root.absolute()
    if root.exists():
        validate_root(root)
    else:
        ensure_private_dir(root)
        _write_marker(root / ROOT_MARKER)
    with install_lock(root):
        receipt_before = read_source(root / RECEIPT)
        previous = json.loads(receipt_before) if receipt_before else {}
        preserve_registration = bool(previous) and not _unchanged_registration(previous, marketplace)
        before_market = read_source(marketplace)
        plugin_files = {} if preserve_registration else _plugin_files(source, root, marketplace)
        after_market = before_market if preserve_registration else merge_marketplace(
            before_market, owned_entry=previous.get("entry"))
        if not previous:
            for target in plugin_files:
                if target.exists():
                    raise RuntimeError("A Telegram plugin directory already exists; existing files were preserved")
        version = root / f"version-{__version__}-{uuid.uuid4().hex[:12]}"
        ensure_private_dir(version)
        _copy_source(source, version)
        _write_marker(version / VERSION_MARKER)
        # uv verifies the pinned dependency hashes. -I is used for every runtime
        # entry point; the source tree is installed as a non-editable wheel.
        environment = {k: v for k, v in os.environ.items()
                       if not k.upper().startswith("UV_") and k.upper() not in {"VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME"}}
        subprocess.run([str(uv), "sync", "--frozen", "--no-dev", "--no-editable", "--no-config", "--managed-python",
                        "--python", "3.13", "--project", str(version)], check=True, env=environment)
        python = version / ".venv/Scripts/python.exe"
        subprocess.run([str(python), "-I", "-X", "utf8", "-c",
                        "from telegram_search_mcp.native_runtime import bundled_candidates,verify; "
                        "verify(bundled_candidates()[0]); from telegram_search_mcp.server import create_server"], check=True)
        subprocess.run([str(python), "-I", "-X", "utf8", "-m", "telegram_search_mcp.cli", "--help"], check=True, stdout=subprocess.DEVNULL)
        writes = {
            root / "launch-mcp.ps1": windows_launcher_text(root, "telegram_search_mcp.server").encode(),
            root / "tgsearch.ps1": windows_launcher_text(root, "telegram_search_mcp.cli").encode(),
            **plugin_files,
        }
        if not preserve_registration:
            writes[marketplace] = after_market
        receipt = {"platform": "windows", "version": __version__, "automatic_updates": False,
                   "marketplace": str(marketplace), "entry": marketplace_entry(),
                   "plugin_files": {str(p): hashlib.sha256(b).hexdigest() for p, b in plugin_files.items()}}
        if preserve_registration:
            receipt.update({key: previous.get(key) for key in ("marketplace", "entry", "plugin_files")})
        writes[root / RECEIPT] = _json(receipt)
        writes[root / "current.json"] = _json({"version": version.name})
        snapshots = {path: read_source(path) for path in writes}
        changed = []
        try:
            for path, content in writes.items():
                if content != snapshots[path]:
                    atomic_write(path, content, expected=snapshots[path])
                    changed.append(path)
        except BaseException:
            for path in reversed(changed):
                if snapshots[path] is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_write(path, snapshots[path], expected=read_source(path))
            raise
        return {"root": str(root), "python": str(python), "version": __version__,
                "plugin_registration": "preserved_user_changes" if preserve_registration else "available_in_personal_marketplace"}


def set_windows_sending(root: Path, enabled: bool) -> None:
    validate_root(root)
    with install_lock(root):
        path = root / "sending.json"
        atomic_write(path, _json({"enabled": bool(enabled)}), expected=read_source(path))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--uv", type=Path, required=True)
    parser.add_argument("--install-dir", type=Path)
    parser.add_argument("--marketplace-path", type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("Run install-windows.ps1 on Windows")
    root = args.install_dir or local_data_dir() / "Programs/TelegramMCP"
    marketplace = args.marketplace_path or trusted_home() / ".agents/plugins/marketplace.json"
    result = install(args.source.resolve(), root, marketplace.absolute(), args.uv.resolve())
    print(json.dumps(result, indent=2))
    if not args.prepare_only:
        ready = subprocess.run([result["python"], "-I", "-X", "utf8", "-m", "telegram_search_mcp.cli", "doctor"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if ready.returncode != 0:
            subprocess.run([result["python"], "-I", "-X", "utf8", "-m", "telegram_search_mcp.cli", "auth"], check=True)
        subprocess.run([result["python"], "-I", "-X", "utf8", "-m", "telegram_search_mcp.cli", "doctor", "--connect"], check=True)
    print("Restart ChatGPT. In Work, open Plugins, choose Personal, and install Telegram MCP. Start a new chat.")


if __name__ == "__main__":
    main()
