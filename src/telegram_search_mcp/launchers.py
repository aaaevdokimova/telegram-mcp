"""Stable entry points resolve an immutable installed version before starting Python."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
import shlex
import stat
import sys

from .platform_support import IS_WINDOWS, assert_private_path, open_owned_readonly

ROOT_MARKER = ".telegram-search-install-root"
VERSION_MARKER = ".telegram-search-install"
LAUNCHER_NAME = "launch-mcp.ps1" if IS_WINDOWS else "launch-mcp.command"


def validate_root(root: Path) -> None:
    if not root.is_absolute() or root.resolve() != root:
        raise RuntimeError("Installation root must be an absolute path without symlinks")
    if IS_WINDOWS:
        assert_private_path(root, directory=True)
        if _read_private_text(root / ROOT_MARKER) != "telegram-search-mcp\n":
            raise RuntimeError("Unrecognized installation root")
        return
    info = root.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise RuntimeError("Installation root is not safely owned")
    marker = root / ROOT_MARKER
    if marker.is_symlink() or not marker.is_file() or marker.read_text() != "telegram-search-mcp\n":
        raise RuntimeError("Unrecognized installation root")


def current_version(root: Path) -> Path:
    validate_root(root)
    if IS_WINDOWS:
        try:
            pointer = json.loads(_read_private_text(root / "current.json"))
        except (OSError, ValueError) as exc:
            raise RuntimeError("The installation has no valid current version") from exc
        if (not isinstance(pointer, dict) or set(pointer) != {"version"}
                or not isinstance(pointer["version"], str)
                or not re.fullmatch(r"version-[0-9]+\.[0-9]+\.[0-9]+-[a-zA-Z0-9]+", pointer["version"])):
            raise RuntimeError("current points outside a managed installation")
        version = root / pointer["version"]
        assert_private_path(version, directory=True)
        if _read_private_text(version / VERSION_MARKER) != "telegram-search-mcp\n":
            raise RuntimeError("current points outside a managed installation")
        return version
    link = root / "current"
    if not link.is_symlink():
        raise RuntimeError("The installation has no current version")
    version = link.resolve(strict=True)
    marker = version / VERSION_MARKER
    if version.parent != root or marker.is_symlink() or not marker.is_file() or marker.read_text() != "telegram-search-mcp\n":
        raise RuntimeError("current points outside a managed installation")
    return version


def _read_private_text(path: Path) -> str:
    fd = open_owned_readonly(path, private=True)
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        text = stream.read(16 * 1024 + 1)
    if len(text) > 16 * 1024:
        raise RuntimeError("Invalid installation metadata")
    return text


def launcher_text(root: Path, module: str) -> str:
    if IS_WINDOWS:
        from .windows_install import windows_launcher_text
        return windows_launcher_text(root, module)
    from .registration import real_home
    from .gemini_config import SAFE_LANG, SAFE_PATH

    # Resolve current before exec, so a later update cannot change sys.path under
    # an already running Python process. Never run through current/.venv/python.
    return (
        "#!/bin/sh\nset -eu\n"
        f"root={shlex.quote(str(root))}\n"
        'version="$(cd "$root/current" && pwd -P)"\n'
        '[ "$(dirname "$version")" = "$root" ] || exit 1\n'
        '[ -f "$version/.telegram-search-install" ] || exit 1\n'
        + "exec /usr/bin/env -i "
        + shlex.join([f"HOME={real_home()}", f"PATH={SAFE_PATH}", f"LANG={SAFE_LANG}"])
        + f' "$version/.venv/bin/python" -I -m {shlex.quote(module)} "$@"\n'
    )


def validate_launcher(root: Path) -> None:
    validate_root(root)
    path = root / LAUNCHER_NAME
    if IS_WINDOWS:
        if _read_private_text(path) != launcher_text(root, "telegram_search_mcp.server"):
            raise RuntimeError("The stable MCP launcher was modified")
        return
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
        raise RuntimeError("Unsafe stable MCP launcher")
    if path.read_text() != launcher_text(root, "telegram_search_mcp.server"):
        raise RuntimeError("The stable MCP launcher was modified")


def installed_root(executable: str | None = None) -> Path | None:
    path = Path(executable or sys.executable)
    version = path.parent.parent.parent
    root = version.parent
    expected = "Scripts" if IS_WINDOWS else "bin"
    if path.parent.name != expected or path.parent.parent.name != ".venv" or not (root / ROOT_MARKER).is_file():
        return None
    validate_root(root)
    if not (version / VERSION_MARKER).is_file():
        return None
    return root


def service_python() -> str:
    root = installed_root()
    relative = ".venv/Scripts/python.exe" if IS_WINDOWS else ".venv/bin/python"
    return str(current_version(root) / relative) if root else sys.executable
