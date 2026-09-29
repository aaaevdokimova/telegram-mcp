"""Private, atomic client configuration edits with recoverable backups."""
from __future__ import annotations

import os
import stat
import tempfile
import uuid
from pathlib import Path


def validate_path(path: Path) -> None:
    if os.name == "nt":
        from .platform_support import assert_safe_path
        if not path.is_absolute():
            raise RuntimeError("Configuration path must be absolute")
        ancestor = path.parent
        while not ancestor.exists():
            ancestor = ancestor.parent
        assert_safe_path(ancestor, directory=True)
        if path.exists() or path.is_symlink():
            assert_safe_path(path)
        return
    if not path.is_absolute():
        raise RuntimeError("Configuration path must be absolute")
    if path.is_symlink() or path.parent.is_symlink():
        raise RuntimeError("Refusing symlinked configuration or directory")
    ancestor = path.parent
    while not ancestor.exists():
        ancestor = ancestor.parent
    info = ancestor.stat()
    if not ancestor.is_dir() or info.st_uid not in (os.getuid(), 0):
        raise RuntimeError("Configuration directory is not safely owned")
    if stat.S_IMODE(info.st_mode) & 0o022 and not info.st_mode & stat.S_ISVTX:
        raise RuntimeError("Configuration directory is group- or world-writable")
    if path.exists():
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError("Configuration must be a regular, non-hard-linked file")
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise RuntimeError("Configuration is not privately owned or is writable by others")


def read_source(path: Path) -> bytes | None:
    validate_path(path)
    return path.read_bytes() if path.exists() else None


def _temporary(parent: Path, prefix: str) -> tuple[int, str]:
    if os.name == "nt":
        from .platform_support import open_private_file
        path = parent / (prefix + uuid.uuid4().hex)
        return open_private_file(path, exclusive=True), str(path)
    return tempfile.mkstemp(prefix=prefix, dir=parent)


def atomic_write(path: Path, content: bytes, *, expected: bytes | None) -> Path | None:
    """Compare with the inspected source, then keep a private byte-exact backup."""
    if read_source(path) != expected:
        raise RuntimeError("Configuration changed during registration; retry safely")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup = None
    if expected is not None:
        descriptor, name = _temporary(path.parent, path.name + ".telegram-search-backup-")
        backup = Path(name)
        if os.name == "nt":
            from .platform_support import secure_file
            secure_file(backup)
        with os.fdopen(descriptor, "wb") as output:
            output.write(expected)
            output.flush()
            os.fsync(output.fileno())
    descriptor, name = _temporary(path.parent, ".telegram-search-")
    temporary = Path(name)
    if os.name == "nt":
        from .platform_support import secure_file
        secure_file(temporary)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return backup
