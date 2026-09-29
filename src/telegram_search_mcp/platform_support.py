"""Native private storage and locks on macOS/POSIX and Windows.

Windows security is enforced with a protected DACL, not chmod/getuid emulation.
New objects grant access only to the current token user and LocalSystem. Existing
objects fail closed on unexpected ownership, ACEs, links or reparse points.
The Windows-only pywin32 dependency is imported lazily.
"""
from __future__ import annotations

import errno
import os
from pathlib import Path
import stat
import sys
import uuid

IS_WINDOWS = sys.platform == "win32"


def trusted_home() -> Path:
    """Resolve the account's actual home, independent of inherited environment."""
    if IS_WINDOWS:
        from win32com.shell import shell, shellcon
        return Path(shell.SHGetFolderPath(0, shellcon.CSIDL_PROFILE, None, 0))
    import pwd
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def local_data_dir() -> Path:
    """OS-resolved per-user local data; never trust LOCALAPPDATA/HOME input."""
    if IS_WINDOWS:
        from win32com.shell import shell, shellcon
        return Path(shell.SHGetFolderPath(0, shellcon.CSIDL_LOCAL_APPDATA, None, 0))
    return trusted_home() / "Library" / "Application Support"


def _user_sid():
    import win32api
    import win32con
    import win32security
    with win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY) as token:
        return win32security.GetTokenInformation(token, win32security.TokenUser)[0]


def _security_attributes():
    import pywintypes
    import win32security
    sid = _user_sid()
    text = win32security.ConvertSidToStringSid(sid)
    descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        f"O:{text}D:P(A;OICI;FA;;;{text})(A;OICI;FA;;;SY)",
        win32security.SDDL_REVISION_1,
    )
    attributes = pywintypes.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = descriptor
    attributes.bInheritHandle = False
    return attributes


def reject_links(path: Path) -> None:
    """Reject symlinks, junctions and every reparse point in an absolute path."""
    if not path.is_absolute() or ".." in path.parts:
        raise RuntimeError("Profile paths must be absolute and must not traverse symlinks")
    if IS_WINDOWS and (str(path).startswith("\\\\") or any(":" in part for part in path.parts[1:])):
        raise RuntimeError("Runtime paths must be local and must not select alternate data streams")
    for component in (*reversed(path.parents), path):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise RuntimeError(f"Refusing symlink or reparse point: {component}")


def _check_windows_handle(handle, *, directory: bool, private: bool, allow_system_owner: bool = False) -> None:
    import win32con
    import win32file
    import win32security
    info = win32file.GetFileInformationByHandle(handle)
    if info[0] & win32con.FILE_ATTRIBUTE_REPARSE_POINT:
        raise RuntimeError("Refusing symlink or reparse point")
    if bool(info[0] & win32con.FILE_ATTRIBUTE_DIRECTORY) != directory:
        raise RuntimeError("Expected a private directory" if directory else "Expected a private regular file")
    if not directory and (win32file.GetFileType(handle) != win32file.FILE_TYPE_DISK or info[7] != 1):
        raise RuntimeError("Expected a private regular file without hard links")
    security = win32security.GetSecurityInfo(handle, win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION)
    current = _user_sid()
    owner = win32security.ConvertSidToStringSid(security.GetSecurityDescriptorOwner())
    safe_owners = {win32security.ConvertSidToStringSid(current)}
    if allow_system_owner:
        safe_owners.update({"S-1-5-18", "S-1-5-32-544"})
    if owner not in safe_owners:
        raise RuntimeError("Profile data must be owned by the current user")
    if not private:
        return
    dacl = security.GetSecurityDescriptorDacl()
    if dacl is None:
        raise RuntimeError("Profile data must have a private Windows ACL")
    allowed = {win32security.ConvertSidToStringSid(current), "S-1-5-18"}
    user_allowed = False
    for index in range(dacl.GetAceCount()):
        ace = dacl.GetAce(index)
        # Unknown/object/callback ACE forms fail closed; we only create plain ACEs.
        if len(ace) != 3 or ace[0][0] != win32security.ACCESS_ALLOWED_ACE_TYPE:
            raise RuntimeError("Profile data has an unsupported Windows ACL")
        principal = win32security.ConvertSidToStringSid(ace[2])
        if principal not in allowed:
            raise RuntimeError("Profile data must be private to the current user")
        if principal == win32security.ConvertSidToStringSid(current) and not ace[0][1] & win32con.INHERIT_ONLY_ACE:
            user_allowed = True
    if not user_allowed:
        raise RuntimeError("Profile data has no current-user access")


def _windows_open(path: Path, *, writable: bool, create: bool = False, exclusive: bool = False, directory: bool = False):
    import pywintypes
    import win32con
    import win32file
    reject_links(path)
    try:
        return win32file.CreateFile(str(path),
        (win32con.GENERIC_READ | win32con.GENERIC_WRITE if writable else win32con.GENERIC_READ) | win32con.READ_CONTROL,
        win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE | win32con.FILE_SHARE_DELETE,
        _security_attributes() if create else None,
        win32con.CREATE_NEW if exclusive else win32con.OPEN_ALWAYS if create else win32con.OPEN_EXISTING,
        win32con.FILE_FLAG_OPEN_REPARSE_POINT | (win32con.FILE_FLAG_BACKUP_SEMANTICS if directory else 0), None)
    except pywintypes.error as exc:
        if getattr(exc, "winerror", None) in {2, 3}:
            raise FileNotFoundError(errno.ENOENT, "File not found", str(path)) from exc
        if getattr(exc, "winerror", None) in {80, 183}:
            raise FileExistsError(errno.EEXIST, "File already exists", str(path)) from exc
        raise OSError(errno.EACCES if exc.winerror == 5 else errno.EIO, "Unable to open local file", str(path)) from exc


def assert_private_path(path: Path, *, directory: bool = False) -> None:
    reject_links(path)
    if IS_WINDOWS:
        with _windows_open(path, writable=False, directory=directory) as handle:
            _check_windows_handle(handle, directory=directory, private=True)
        return
    info = path.lstat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError("Profile data must be private to the current user")
    if directory:
        if not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("Expected a private profile directory")
    elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise RuntimeError("Expected a private regular profile file")



def assert_safe_path(path: Path, *, directory: bool = False) -> None:
    """Validate owned local config: broad reads allowed, untrusted writes denied.

    Windows permits the current user, LocalSystem and built-in Administrators to
    write configuration. Runtime secrets must use assert_private_path instead.
    """
    reject_links(path)
    if not IS_WINDOWS:
        info = path.lstat()
        expected_type = stat.S_ISDIR if directory else stat.S_ISREG
        if info.st_uid != os.getuid() or info.st_mode & 0o022 or not expected_type(info.st_mode) or (not directory and info.st_nlink != 1):
            raise RuntimeError("Config must be owned by the current user and not writable by other users")
        return
    import win32security
    with _windows_open(path, writable=False, directory=directory) as handle:
        _check_windows_handle(handle, directory=directory, private=False, allow_system_owner=True)
        descriptor = win32security.GetSecurityInfo(handle, win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION)
        dacl = descriptor.GetSecurityDescriptorDacl()
        if dacl is None:
            raise RuntimeError("Config must have an explicit safe Windows ACL")
        allowed_writers = {win32security.ConvertSidToStringSid(_user_sid()), "S-1-5-18", "S-1-5-32-544"}
        write_access = 0x40000000 | 0x10000000 | 0x000D0156
        for index in range(dacl.GetAceCount()):
            ace = dacl.GetAce(index)
            if len(ace) != 3 or ace[0][0] not in {win32security.ACCESS_ALLOWED_ACE_TYPE, win32security.ACCESS_DENIED_ACE_TYPE}:
                raise RuntimeError("Config has an unsupported Windows ACL")
            if ace[0][0] == win32security.ACCESS_ALLOWED_ACE_TYPE and ace[1] & write_access and win32security.ConvertSidToStringSid(ace[2]) not in allowed_writers:
                raise RuntimeError("Config must not be writable by other users")

def ensure_private_dir(path: Path) -> None:
    reject_links(path)
    if IS_WINDOWS:
        import pywintypes
        import win32file
        missing = []
        ancestor = path
        while not ancestor.exists():
            missing.append(ancestor)
            ancestor = ancestor.parent
        for directory in reversed(missing):
            try:
                win32file.CreateDirectory(str(directory), _security_attributes())
            except pywintypes.error as exc:
                if getattr(exc, "winerror", None) != 183:
                    raise
            assert_private_path(directory, directory=True)
        assert_private_path(path, directory=True)
        return
    old_umask = os.umask(0o077)
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    finally:
        os.umask(old_umask)
    reject_links(path)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError(f"Runtime directory is not owned by current user: {path}")
    path.chmod(0o700)


def open_private_file(path: Path, *, exclusive: bool = False) -> int:
    """Open/create one private regular file read/write, never follow a link."""
    reject_links(path)
    if IS_WINDOWS:
        import msvcrt
        handle = _windows_open(path, writable=True, create=True, exclusive=exclusive)
        try:
            _check_windows_handle(handle, directory=False, private=True)
            fd = msvcrt.open_osfhandle(int(handle), os.O_RDWR | os.O_BINARY)
            handle.Detach()
            return fd
        except BaseException:
            handle.Close()
            raise
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    if exclusive:
        flags |= os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise RuntimeError("Private files must be regular, owned by the current user, and not hard linked")
        os.fchmod(fd, 0o600)
        return fd
    except BaseException:
        os.close(fd)
        raise



def private_temp_file(directory: Path, *, prefix: str = ".tmp-") -> tuple[int, str]:
    """Create a temporary file born with a private DACL, including in config dirs."""
    for _ in range(128):
        path = directory / (prefix + uuid.uuid4().hex)
        try:
            return open_private_file(path, exclusive=True), str(path)
        except FileExistsError:
            continue
    raise FileExistsError("Unable to allocate a unique private temporary file")

def open_owned_readonly(path: Path, *, private: bool = False) -> int:
    """Open an owned ordinary file; optionally require private permissions."""
    reject_links(path)
    if IS_WINDOWS:
        import msvcrt
        handle = _windows_open(path, writable=False)
        try:
            _check_windows_handle(handle, directory=False, private=private)
            fd = msvcrt.open_osfhandle(int(handle), os.O_RDONLY | os.O_BINARY)
            handle.Detach()
            return fd
        except BaseException:
            handle.Close()
            raise
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise RuntimeError("Expected a regular file owned by the current user without hard links")
        if private and info.st_mode & 0o077:
            raise RuntimeError("Profile file permissions must be 0600")
        return fd
    except BaseException:
        os.close(fd)
        raise


def secure_file(path: Path) -> None:
    """Set/check private permissions on an existing owned regular file."""
    if IS_WINDOWS:
        # Our files are born under a private parent or with a private DACL. Do
        # not silently launder an untrusted pre-existing ACL into a trusted one.
        assert_private_path(path)
    else:
        reject_links(path)
        if path.stat().st_uid != os.getuid():
            raise RuntimeError("Profile file is not owned by current user")
        path.chmod(0o600)
        assert_private_path(path)


def acquire_file_lock(fd: int, *, blocking: bool = False) -> None:
    """Take the per-file exclusive lock; fd close releases it on either OS."""
    if IS_WINDOWS:
        import msvcrt
        import pywintypes
        import win32con
        import win32file
        flags = win32con.LOCKFILE_EXCLUSIVE_LOCK
        if not blocking:
            flags |= win32con.LOCKFILE_FAIL_IMMEDIATELY
        try:
            win32file.LockFileEx(msvcrt.get_osfhandle(fd), flags, 1, 0, pywintypes.OVERLAPPED())
        except pywintypes.error as exc:
            if getattr(exc, "winerror", None) in {33, 158}:
                raise BlockingIOError(errno.EAGAIN, "File is locked by another process") from exc
            raise
        return
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))


def release_file_lock(fd: int) -> None:
    if IS_WINDOWS:
        import msvcrt
        import pywintypes
        import win32file
        win32file.UnlockFileEx(msvcrt.get_osfhandle(fd), 1, 0, pywintypes.OVERLAPPED())
        return
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_UN)


def replace_private_file(source: Path, target: Path) -> None:
    """Publish a flushed private file, using Windows write-through rename."""
    assert_private_path(source)
    reject_links(target)
    if target.exists():
        assert_private_path(target)
    if IS_WINDOWS:
        import win32con
        import win32file
        win32file.MoveFileEx(str(source), str(target), win32con.MOVEFILE_REPLACE_EXISTING | win32con.MOVEFILE_WRITE_THROUGH)
    else:
        os.replace(source, target)


def fsync_directory(path: Path) -> None:
    """Persist POSIX directory changes; Windows uses write-through replacement."""
    if IS_WINDOWS:
        assert_private_path(path, directory=True)
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
