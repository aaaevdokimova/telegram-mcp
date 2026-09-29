"""All files stay under pytest temporary directories; no real profiles/secrets."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from telegram_search_mcp import platform_support as platform


@pytest.fixture
def private_area(tmp_path):
    path = tmp_path.resolve() / "private"
    platform.ensure_private_dir(path)
    return path


def test_private_files_and_directory_permissions(private_area):
    target = private_area / "state.json"
    descriptor = platform.open_private_file(target, exclusive=True)
    try:
        os.write(descriptor, b"synthetic state")
    finally:
        os.close(descriptor)
    platform.assert_private_path(private_area, directory=True)
    platform.assert_private_path(target)
    descriptor = platform.open_owned_readonly(target, private=True)
    try:
        assert os.read(descriptor, 100) == b"synthetic state"
    finally:
        os.close(descriptor)
    with pytest.raises(FileExistsError):
        platform.open_private_file(target, exclusive=True)


def test_lock_excludes_another_open_and_releases_on_close(private_area):
    path = private_area / "profile.lock"
    first = platform.open_private_file(path)
    second = platform.open_private_file(path)
    try:
        platform.acquire_file_lock(first)
        with pytest.raises(BlockingIOError):
            platform.acquire_file_lock(second)
        os.close(first)
        first = -1
        platform.acquire_file_lock(second)
        platform.release_file_lock(second)
    finally:
        if first >= 0:
            os.close(first)
        os.close(second)


def test_lock_excludes_another_process(private_area):
    path = private_area / "profile.lock"
    fd = platform.open_private_file(path)
    try:
        platform.acquire_file_lock(fd)
        result = subprocess.run([sys.executable, "-c", """
import os, sys
from pathlib import Path
from telegram_search_mcp.platform_support import open_private_file, acquire_file_lock
fd = open_private_file(Path(sys.argv[1]))
try:
    try:
        acquire_file_lock(fd)
    except BlockingIOError:
        sys.exit(42)
finally:
    os.close(fd)
""", str(path)], capture_output=True, text=True, timeout=15)
        assert result.returncode == 42, result.stderr
    finally:
        os.close(fd)


def test_hard_link_cannot_be_a_private_file(private_area):
    original = private_area / "original"
    original.write_text("synthetic")
    target = private_area / "hardlink"
    os.link(original, target)
    with pytest.raises(RuntimeError, match="regular|hard link"):
        platform.open_private_file(target)
    with pytest.raises(RuntimeError, match="regular|hard link"):
        platform.open_owned_readonly(target)


def test_symlink_is_refused_without_touching_target(private_area):
    target = private_area / "original"
    target.write_text("keep")
    link = private_area / "linked"
    try:
        link.symlink_to(target)
    except OSError as exc:
        if sys.platform == "win32" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege unavailable; junction test covers reparse rejection")
        raise
    with pytest.raises(RuntimeError, match="symlink|reparse"):
        platform.open_private_file(link)
    assert target.read_text() == "keep"


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows junction/ACL test")
def test_windows_junction_and_public_acl_are_refused(private_area):
    import win32security
    link = private_area / "junction"
    destination = private_area / "destination"
    platform.ensure_private_dir(destination)
    # mklink /J is a local directory junction and does not require admin rights.
    result = subprocess.run([os.environ["COMSPEC"], "/d", "/c", "mklink", "/J", str(link), str(destination)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    try:
        with pytest.raises(RuntimeError, match="reparse"):
            platform.ensure_private_dir(link)
    finally:
        link.rmdir()
    fd = platform.open_private_file(private_area / "file")
    os.close(fd)
    descriptor = win32security.GetNamedSecurityInfo(str(private_area / "file"), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION)
    dacl = descriptor.GetSecurityDescriptorDacl()
    dacl.AddAccessAllowedAce(win32security.ACL_REVISION, 0x120089, win32security.ConvertStringSidToSid("S-1-1-0"))
    win32security.SetNamedSecurityInfo(str(private_area / "file"), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION, None, None, dacl, None)
    with pytest.raises(RuntimeError, match="private"):
        platform.assert_private_path(private_area / "file")
    platform.assert_safe_path(private_area / "file")  # world-readable config is allowed


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows trusted folder API")
def test_windows_runtime_roots_ignore_environment(monkeypatch):
    before = platform.local_data_dir(), platform.trusted_home()
    for name in ("LOCALAPPDATA", "APPDATA", "USERPROFILE", "HOME"):
        monkeypatch.setenv(name, r"Z:\untrusted-workspace")
    assert (platform.local_data_dir(), platform.trusted_home()) == before


def test_windows_acl_validation_fails_closed(monkeypatch):
    """Exercise the ACL decision on every OS; native tests verify API wiring."""
    class ACL:
        def __init__(self, entries):
            self.entries = entries
        def GetAceCount(self):
            return len(self.entries)
        def GetAce(self, index):
            return self.entries[index]
    state = {"owner": "current", "acl": ACL([((0, 0), 0x1F01FF, "current"), ((0, 0), 0x1F01FF, "S-1-5-18")]), "attributes": 0, "links": 1}
    descriptor = SimpleNamespace(GetSecurityDescriptorOwner=lambda: state["owner"], GetSecurityDescriptorDacl=lambda: state["acl"])
    security = SimpleNamespace(SE_FILE_OBJECT=1, OWNER_SECURITY_INFORMATION=1, DACL_SECURITY_INFORMATION=4,
        ACCESS_ALLOWED_ACE_TYPE=0, ConvertSidToStringSid=lambda sid: sid, GetSecurityInfo=lambda *args: descriptor)
    file = SimpleNamespace(FILE_TYPE_DISK=1, GetFileType=lambda handle: 1,
        GetFileInformationByHandle=lambda handle: (state["attributes"], None, None, None, 0, 0, 1, state["links"], 0, 0))
    con = SimpleNamespace(FILE_ATTRIBUTE_REPARSE_POINT=0x400, FILE_ATTRIBUTE_DIRECTORY=0x10, INHERIT_ONLY_ACE=8)
    monkeypatch.setitem(sys.modules, "win32security", security)
    monkeypatch.setitem(sys.modules, "win32file", file)
    monkeypatch.setitem(sys.modules, "win32con", con)
    monkeypatch.setattr(platform, "_user_sid", lambda: "current")
    platform._check_windows_handle(1, directory=False, private=True)
    original = dict(state)
    for changed in ({"owner": "another"}, {"acl": None}, {"acl": ACL([((0, 0), 1, "S-1-1-0")])},
                    {"attributes": 0x400}, {"links": 2}, {"acl": ACL([((5, 0), 1, "current")])}):
        state.update(original)
        state.update(changed)
        with pytest.raises(RuntimeError):
            platform._check_windows_handle(1, directory=False, private=True)
