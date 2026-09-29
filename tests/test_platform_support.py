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
                    {"attributes": 0x400}, {"links": 2}, {"acl": ACL([((5, 0), 1, "current")])},
                    {"acl": ACL([((0, 0x13), 0x1F01FF, "S-1-3-4")])}):
        state.update(original)
        state.update(changed)
        with pytest.raises(RuntimeError):
            platform._check_windows_handle(1, directory=False, private=True)


@pytest.mark.parametrize("fail", [False, True])
def test_windows_token_handle_without_context_manager_is_closed(monkeypatch, fail):
    class Handle:
        closed = False
        def Close(self):
            self.closed = True
    token = Handle()  # pywin32 311 PyHANDLE deliberately has no __enter__/__exit__.
    def token_information(handle, kind):
        assert handle is token
        if fail:
            raise RuntimeError("synthetic token failure")
        return ("current-sid", 0)
    monkeypatch.setitem(sys.modules, "win32api", SimpleNamespace(GetCurrentProcess=lambda: 1))
    monkeypatch.setitem(sys.modules, "win32con", SimpleNamespace(TOKEN_QUERY=8))
    monkeypatch.setitem(sys.modules, "win32security", SimpleNamespace(
        OpenProcessToken=lambda *args: token, GetTokenInformation=token_information, TokenUser=1))
    if fail:
        with pytest.raises(RuntimeError, match="synthetic token failure"):
            platform._user_sid()
    else:
        assert platform._user_sid() == "current-sid"
    assert token.closed


@pytest.mark.parametrize("check", ["assert_private_path", "assert_safe_path"])
@pytest.mark.parametrize("fail", [False, True])
def test_windows_file_handle_without_context_manager_is_closed(monkeypatch, tmp_path, check, fail):
    class Handle:
        closed = False
        def Close(self):
            self.closed = True
    handle = Handle()
    def inspect(*args, **kwargs):
        if fail:
            raise RuntimeError("synthetic ACL failure")
    descriptor = SimpleNamespace(GetSecurityDescriptorDacl=lambda: SimpleNamespace(GetAceCount=lambda: 0))
    monkeypatch.setattr(platform, "IS_WINDOWS", True)
    monkeypatch.setattr(platform, "_windows_open", lambda *args, **kwargs: handle)
    monkeypatch.setattr(platform, "_check_windows_handle", inspect)
    monkeypatch.setattr(platform, "_user_sid", lambda: "current-sid")
    monkeypatch.setitem(sys.modules, "win32security", SimpleNamespace(
        GetSecurityInfo=lambda *args: descriptor, SE_FILE_OBJECT=1, DACL_SECURITY_INFORMATION=4,
        ConvertSidToStringSid=lambda sid: sid))
    if fail:
        with pytest.raises(RuntimeError, match="synthetic ACL failure"):
            getattr(platform, check)(tmp_path.resolve())
    else:
        getattr(platform, check)(tmp_path.resolve())
    assert handle.closed


def test_windows_open_uses_file_module_reparse_constant(monkeypatch, tmp_path):
    """pywin32 311 exports OPEN_REPARSE_POINT in win32file, not win32con."""
    con = SimpleNamespace(GENERIC_READ=0x80000000, GENERIC_WRITE=0x40000000, READ_CONTROL=0x20000,
        FILE_SHARE_READ=1, FILE_SHARE_WRITE=2, FILE_SHARE_DELETE=4, CREATE_NEW=1, OPEN_ALWAYS=4,
        OPEN_EXISTING=3, FILE_FLAG_BACKUP_SEMANTICS=0x02000000)
    captured = []
    handle = object()
    def create_file(*args):
        captured.append(args)
        return handle
    monkeypatch.setitem(sys.modules, "win32con", con)
    monkeypatch.setitem(sys.modules, "win32file", SimpleNamespace(
        FILE_FLAG_OPEN_REPARSE_POINT=0x00200000, CreateFile=create_file))
    monkeypatch.setitem(sys.modules, "pywintypes", SimpleNamespace(error=Exception))
    assert platform._windows_open(tmp_path.resolve(), writable=False, directory=True) is handle
    assert captured[0][5] == 0x02200000


def test_windows_replace_uses_file_module_write_through_constant(monkeypatch, tmp_path):
    """pywin32 311 exports MOVEFILE_WRITE_THROUGH only from win32file."""
    captured = []
    monkeypatch.setattr(platform, "IS_WINDOWS", True)
    monkeypatch.setattr(platform, "assert_private_path", lambda *args, **kwargs: None)
    monkeypatch.setitem(sys.modules, "win32con", SimpleNamespace(MOVEFILE_REPLACE_EXISTING=1))
    monkeypatch.setitem(sys.modules, "win32file", SimpleNamespace(
        MOVEFILE_WRITE_THROUGH=8, MoveFileEx=lambda *args: captured.append(args)))
    source, target = tmp_path.resolve() / "source", tmp_path.resolve() / "target"
    platform.replace_private_file(source, target)
    assert captured == [(str(source), str(target), 9)]


@pytest.mark.parametrize("principal,flags,mask,directory,accepted", [
    ("current", 0, 0x1F01FF, True, True),
    ("S-1-5-18", 0, 0x1F01FF, True, True),
    ("S-1-5-32-544", 0, 0x1F01FF, True, True),
    ("S-1-1-0", 0, 0x120089, True, True),  # broad read-only config access
    ("S-1-3-4", 0x13, 0x1F01FF, True, True),  # inherited OWNER RIGHTS: verified owner
    ("S-1-3-4", 0, 0x1F01FF, False, True),
    ("S-1-3-0", 0x0B, 0x1F01FF, True, True),  # CREATOR_OWNER OI/CI/IO template
    ("S-1-3-0", 0x03, 0x1F01FF, True, False),  # effective unknown owner placeholder
    ("S-1-3-0", 0x0B, 0x1F01FF, False, False),
    ("S-1-1-0", 0x0B, 0x1F01FF, True, False),  # broad inherited child writes
    ("S-1-5-32-545", 0, 0x000004, True, False),  # Users create-subdirectory access
    ("S-1-1-0", 0, 0x000002, False, False),
])
def test_windows_config_acl_handles_creator_owner_template_only(monkeypatch, tmp_path,
        principal, flags, mask, directory, accepted):
    handle = SimpleNamespace(Close=lambda: None)
    dacl = SimpleNamespace(GetAceCount=lambda: 1, GetAce=lambda index: ((0, flags), mask, principal))
    descriptor = SimpleNamespace(GetSecurityDescriptorDacl=lambda: dacl)
    monkeypatch.setattr(platform, "IS_WINDOWS", True)
    monkeypatch.setattr(platform, "_windows_open", lambda *args, **kwargs: handle)
    monkeypatch.setattr(platform, "_check_windows_handle", lambda *args, **kwargs: None)
    monkeypatch.setattr(platform, "_user_sid", lambda: "current")
    monkeypatch.setitem(sys.modules, "win32security", SimpleNamespace(
        GetSecurityInfo=lambda *args: descriptor, SE_FILE_OBJECT=1, DACL_SECURITY_INFORMATION=4,
        ACCESS_ALLOWED_ACE_TYPE=0, ACCESS_DENIED_ACE_TYPE=1, ConvertSidToStringSid=lambda sid: sid))
    if accepted:
        platform.assert_safe_path(tmp_path.resolve(), directory=directory)
    else:
        with pytest.raises(RuntimeError, match="writable by other users") as exc:
            platform.assert_safe_path(tmp_path.resolve(), directory=directory)
        assert f"SID={principal}" in str(exc.value)


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows owner-relative config ACL")
@pytest.mark.parametrize("principal,flags", [("S-1-3-0", 0x0B), ("S-1-3-4", 0x03)])
def test_windows_creator_owner_template_allows_config_but_not_private_runtime(private_area, principal, flags):
    import win32security
    from telegram_search_mcp.config_io import atomic_write
    dacl = win32security.GetNamedSecurityInfo(str(private_area), win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION).GetSecurityDescriptorDacl()
    dacl.AddAccessAllowedAceEx(win32security.ACL_REVISION, flags, 0x1F01FF,
        win32security.ConvertStringSidToSid(principal))
    win32security.SetNamedSecurityInfo(str(private_area), win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None, None, dacl, None)
    platform.assert_safe_path(private_area, directory=True)
    with pytest.raises(RuntimeError, match="private"):
        platform.assert_private_path(private_area, directory=True)
    target = private_area / "marketplace.json"
    assert atomic_write(target, b'{"synthetic": true}\n', expected=None) is None
    platform.assert_private_path(target)


@pytest.mark.skipif(sys.platform != "win32", reason="Native ordinary Windows temporary config ACL")
def test_windows_ordinary_temporary_config_directory_is_safe(tmp_path):
    from telegram_search_mcp.config_io import atomic_write, read_source
    # Intentionally ordinary mkdir/write_text: a pre-existing marketplace inherits
    # Windows defaults, unlike private runtime objects created by our native API.
    directory = tmp_path.resolve() / "ordinary config" / ".agents" / "plugins"
    directory.mkdir(parents=True)
    target = directory / "marketplace.json"
    original = b'{"synthetic": "preserved"}\n'
    target.write_bytes(original)
    platform.assert_safe_path(directory, directory=True)
    platform.assert_safe_path(target)
    assert read_source(target) == original
    backup = atomic_write(target, b'{"synthetic": "updated"}\n', expected=original)
    assert backup.read_bytes() == original
    platform.assert_private_path(backup)
    platform.assert_private_path(target)
