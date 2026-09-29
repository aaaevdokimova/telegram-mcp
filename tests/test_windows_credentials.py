"""Mock all Credential Manager calls; never access a real credential store."""
from types import SimpleNamespace
import subprocess
import sys

import pytest

from telegram_search_mcp import keychain


@pytest.fixture
def credentials(monkeypatch):
    class WinError(Exception):
        def __init__(self, code):
            self.winerror = code
    stored = {}
    def read(target, kind, flags):
        if target not in stored:
            raise WinError(1168)
        return stored[target]
    def write(value, flags):
        stored[value["TargetName"]] = value
    def delete(target, kind, flags):
        if target not in stored:
            raise WinError(1168)
        del stored[target]
    api = SimpleNamespace(CRED_TYPE_GENERIC=1, CRED_PERSIST_LOCAL_MACHINE=2, CredRead=read, CredWrite=write, CredDelete=delete)
    monkeypatch.setitem(sys.modules, "pywintypes", SimpleNamespace(error=WinError))
    monkeypatch.setitem(sys.modules, "win32cred", api)
    monkeypatch.setattr(keychain, "IS_WINDOWS", True)
    monkeypatch.setattr(keychain, "credential_services", lambda: (keychain.SERVICE,))
    def subprocess_forbidden(*args, **kwargs):
        raise AssertionError("Credentials must not enter a subprocess")
    monkeypatch.setattr(subprocess, "run", subprocess_forbidden)
    return api, stored, WinError


def test_windows_credentials_roundtrip_separate_profiles(credentials):
    api, stored, _ = credentials
    assert keychain.get_secret("api_hash") is None
    keychain.set_secret("api_hash", "synthetic-ü-value", "one")
    keychain.set_secret("api_hash", "different-value", "two")
    assert keychain.get_secret("api_hash", "one") == "synthetic-ü-value"
    assert keychain.get_secret("api_hash", "two") == "different-value"
    assert keychain.get_secret("api_hash") is None
    assert all(entry["Persist"] == api.CRED_PERSIST_LOCAL_MACHINE for entry in stored.values())
    assert all(isinstance(entry["CredentialBlob"], bytes) for entry in stored.values())
    assert keychain.delete_secret("api_hash", "one") is True
    assert keychain.delete_secret("api_hash", "one") is False
    assert keychain.get_secret("api_hash", "two") == "different-value"


def test_windows_credential_api_error_is_not_misreported_as_missing(credentials):
    api, _, error = credentials
    def denied(*args):
        raise error(5)
    api.CredRead = denied
    with pytest.raises(keychain.KeychainError, match="Unable to read"):
        keychain.get_secret("api_hash")
    api.CredWrite = denied
    with pytest.raises(keychain.KeychainError, match="Unable to store"):
        keychain.set_secret("api_hash", "secret-value")
    api.CredDelete = denied
    with pytest.raises(keychain.KeychainError, match="Unable to delete"):
        keychain.delete_secret("api_hash")


def test_windows_credentials_reject_empty_or_corrupt_values(credentials):
    _, stored, _ = credentials
    with pytest.raises(ValueError, match="empty"):
        keychain.set_secret("api_hash", "")
    stored[f"{keychain.SERVICE}:default:api_hash"] = {"CredentialBlob": b"\xff"}
    with pytest.raises(keychain.KeychainError, match="Invalid"):
        keychain.get_secret("api_hash")
