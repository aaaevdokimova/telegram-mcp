"""The pinned Windows wheel uses delvewheel-renamed libraries and dependencies."""
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from telegram_search_mcp import native_runtime as runtime, tdjson


def test_windows_wheel_discovers_renamed_tdjson_not_its_dependencies(monkeypatch, tmp_path):
    names = [
        "tdjson/tdjson_ext.cp313-win_amd64.pyd",
        "tdjson.libs/tdjson-cbd1f691ae43f84f8b4aa1cfd85846c0.dll",
        "tdjson.libs/libcrypto-1_1-x64-f9d4d39bbd61cb511b591e9e162c5d0f.dll",
        "tdjson.libs/libssl-1_1-x64-411e91fdbf1e42775826e67392ae29f1.dll",
        "tdjson.libs/msvcp140-a4c2229bdc2a2a630acdc095b4d86008.dll",
        "tdjson.libs/zlib1-93e9243a44c29200eeacaf9658efe255.dll",
    ]
    package = SimpleNamespace(version=runtime.VERSION,
                              files=[PurePosixPath(name) for name in names],
                              locate_file=lambda item: tmp_path / str(item))
    monkeypatch.setattr(runtime, "distribution", lambda name: package)
    assert runtime.bundled_candidates() == [tmp_path / names[1]]


def test_windows_loader_retains_one_dependency_directory_handle(monkeypatch, tmp_path):
    library = tmp_path / "tdjson.libs" / "tdjson.dll"
    calls = []
    handle = SimpleNamespace(close=lambda: calls.append("closed"))
    loaded = object()
    monkeypatch.setattr(runtime, "IS_WINDOWS", True)
    monkeypatch.setattr(runtime, "_dll_directories", {})
    monkeypatch.setattr(runtime.os, "add_dll_directory",
                        lambda path: calls.append(("directory", path)) or handle, raising=False)
    monkeypatch.setattr(runtime.ctypes, "CDLL", lambda path: calls.append(("load", path)) or loaded)
    assert runtime.load_library(library) is loaded
    assert runtime.load_library(library) is loaded
    assert runtime._dll_directories == {library.parent: handle}
    assert calls == [("directory", str(library.parent)), ("load", str(library)), ("load", str(library))]


def test_windows_loader_closes_new_directory_when_library_fails(monkeypatch, tmp_path):
    calls = []
    handle = SimpleNamespace(close=lambda: calls.append("closed"))
    monkeypatch.setattr(runtime, "IS_WINDOWS", True)
    monkeypatch.setattr(runtime, "_dll_directories", {})
    monkeypatch.setattr(runtime.os, "add_dll_directory", lambda path: handle, raising=False)

    def failed(path):
        raise OSError("invalid native library")

    monkeypatch.setattr(runtime.ctypes, "CDLL", failed)
    with pytest.raises(OSError, match="invalid native"):
        runtime.load_library(tmp_path / "wrong.dll")
    assert calls == ["closed"]
    assert runtime._dll_directories == {}


def test_transport_uses_the_same_dependency_loader_as_verification(monkeypatch, tmp_path):
    library = tmp_path / "tdjson.dll"
    loaded = object()
    calls = []
    monkeypatch.setattr(tdjson, "tdjson_library_candidates", lambda explicit: (library,))
    monkeypatch.setattr(runtime, "load_library", lambda path: calls.append(path) or loaded)
    assert tdjson.CtypesTdJsonTransport._load_library(None) == (library, loaded)
    assert calls == [library]
