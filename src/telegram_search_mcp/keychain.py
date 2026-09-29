"""Native credential stores: macOS Keychain and Windows Credential Manager."""

from __future__ import annotations

import getpass
import subprocess

from .profile_binding import credential_services
from .platform_support import IS_WINDOWS

SERVICE = "local.unofficial-telegram-search-mcp-shared"
LEGACY_SERVICES: tuple[str, ...] = ()
SECURITY = "/usr/bin/security"


class KeychainError(RuntimeError):
    pass


def _account(profile: str, secret_name: str) -> str:
    return f"{profile}:{secret_name}"


def get_secret(secret_name: str, profile: str = "default") -> str | None:
    if IS_WINDOWS:
        return _windows_get(secret_name, profile)
    for service in credential_services():
        result = subprocess.run(
            [
                SECURITY,
                "find-generic-password",
                "-s",
                service,
                "-a",
                _account(profile, secret_name),
                "-w",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 44:
            continue
        if result.returncode != 0:
            raise KeychainError(
                "Unable to read Telegram Search credential from Keychain"
            )
        return result.stdout.rstrip("\n")
    return None


def set_secret(secret_name: str, value: str, profile: str = "default") -> None:
    if not value:
        raise ValueError("Refusing to store an empty secret")
    if IS_WINDOWS:
        _windows_set(secret_name, value, profile)
        return
    # A bare final -w prompts twice. Detaching the child from the controlling
    # terminal makes `security` read both values from our private stdin pipe,
    # rather than opening /dev/tty. The secret never enters argv or a file.
    result = subprocess.run(
        [
            SECURITY,
            "add-generic-password",
            "-U",
            "-s",
            credential_services()[0],
            "-a",
            _account(profile, secret_name),
            "-l",
            f"Unofficial Telegram Search MCP ({profile}/{secret_name})",
            "-w",
        ],
        input=f"{value}\n{value}\n",
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    if result.returncode != 0:
        raise KeychainError("Unable to store Telegram Search credential in Keychain")


def delete_secret(secret_name: str, profile: str = "default") -> bool:
    if IS_WINDOWS:
        return _windows_delete(secret_name, profile)
    deleted = False
    for service in credential_services():
        result = subprocess.run(
            [
                SECURITY,
                "delete-generic-password",
                "-s",
                service,
                "-a",
                _account(profile, secret_name),
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode == 44:
            continue
        if result.returncode != 0:
            raise KeychainError(
                "Unable to delete Telegram Search credential from Keychain"
            )
        deleted = True
    return deleted


def prompt_and_store_api_hash(profile: str = "default") -> None:
    store = "Windows Credential Manager" if IS_WINDOWS else "macOS Keychain"
    value = getpass.getpass(f"Telegram api_hash (stored only in {store}): ").strip()
    if len(value) < 16:
        raise ValueError("api_hash looks too short")
    set_secret("api_hash", value, profile)


def _windows_get(secret_name: str, profile: str) -> str | None:
    import pywintypes
    import win32cred
    for service in credential_services():
        try:
            credential = win32cred.CredRead(f"{service}:{_account(profile, secret_name)}", win32cred.CRED_TYPE_GENERIC, 0)
        except pywintypes.error as exc:
            if getattr(exc, "winerror", None) == 1168:
                continue
            raise KeychainError("Unable to read Telegram Search credential from Windows Credential Manager") from None
        try:
            return bytes(credential["CredentialBlob"]).decode("utf-8")
        except (KeyError, TypeError, UnicodeError):
            raise KeychainError("Invalid Telegram Search credential in Windows Credential Manager") from None
    return None


def _windows_set(secret_name: str, value: str, profile: str) -> None:
    import pywintypes
    import win32cred
    try:
        win32cred.CredWrite({
            "Type": win32cred.CRED_TYPE_GENERIC,
            "TargetName": f"{credential_services()[0]}:{_account(profile, secret_name)}",
            "CredentialBlob": value.encode("utf-8"),
            "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE,
            "UserName": _account(profile, secret_name),
            "Comment": "Unofficial Telegram Search MCP",
        }, 0)
    except pywintypes.error:
        raise KeychainError("Unable to store Telegram Search credential in Windows Credential Manager") from None


def _windows_delete(secret_name: str, profile: str) -> bool:
    import pywintypes
    import win32cred
    deleted = False
    for service in credential_services():
        try:
            win32cred.CredDelete(f"{service}:{_account(profile, secret_name)}", win32cred.CRED_TYPE_GENERIC, 0)
        except pywintypes.error as exc:
            if getattr(exc, "winerror", None) == 1168:
                continue
            raise KeychainError("Unable to delete Telegram Search credential from Windows Credential Manager") from None
        deleted = True
    return deleted
