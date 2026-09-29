"""Private runtime paths for the local Telegram bridge."""

from __future__ import annotations

from pathlib import Path

from .platform_support import ensure_private_dir

APP_DIR_NAME = "TelegramSearchMCPShared"


def app_root() -> Path:
    # Gemini CLI inherits variables from a trusted workspace's .env file.
    # Runtime storage must therefore never be redirected by ambient input.
    from .profile_binding import data_root
    return data_root()


def profile_root(profile: str = "default") -> Path:
    if not profile or not profile.replace("-", "").replace("_", "").isalnum():
        raise ValueError("Invalid profile name")
    return app_root() / "profiles" / profile


def policy_path(profile: str = "default") -> Path:
    return profile_root(profile) / "policy.json"


def database_dir(profile: str = "default") -> Path:
    return profile_root(profile) / "db"


def files_dir(profile: str = "default") -> Path:
    return profile_root(profile) / "files"


def lock_path(profile: str = "default") -> Path:
    return profile_root(profile) / "tdlib.lock"


def ensure_runtime_layout(profile: str = "default") -> None:
    ensure_private_dir(app_root())
    ensure_private_dir(app_root() / "profiles")
    ensure_private_dir(profile_root(profile))
    ensure_private_dir(database_dir(profile))
    ensure_private_dir(files_dir(profile))
