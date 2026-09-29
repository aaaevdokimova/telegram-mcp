from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("release_builder", ROOT / "scripts" / "release.py")
assert SPEC is not None and SPEC.loader is not None
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


@pytest.fixture
def source(tmp_path: Path) -> Path:
    project = tmp_path / "source"
    for name in release.REQUIRED_FILES:
        target = project / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# Public release source\n")
    (project / "pyproject.toml").write_text('[project]\nname="telegram-search-mcp"\nversion="0.4.0"\n')
    (project / "src/telegram_search_mcp/__init__.py").write_text('__version__ = "0.4.0"\n')
    (project / release.WINDOWS_ASSETS_SOURCE).write_bytes((ROOT / release.WINDOWS_ASSETS_SOURCE).read_bytes())
    return project


def test_archive_is_deterministic_and_has_complete_inventory(source: Path, tmp_path: Path) -> None:
    first = release.build(source, tmp_path / "first")
    for target in source.rglob("*"):
        os.utime(target, (1800000000, 1800000000))
        if target.is_file():
            target.chmod(0o600)
    second = release.build(source, tmp_path / "second")
    assert first.read_bytes() == second.read_bytes()
    assert first.name == "telegram-mcp-macos-v0.4.0.zip"
    manifest = release.verify_archive(first)
    assert {item["path"] for item in manifest["files"]} == release.REQUIRED_FILES | release.DERIVED_FILES
    assert first.with_suffix(".zip.sha256").read_text() == f"{hashlib.sha256(first.read_bytes()).hexdigest()}  {first.name}\n"
    assert json.loads(first.with_suffix(".manifest.json").read_text()) == manifest
    with zipfile.ZipFile(first) as archive:
        installer = archive.getinfo(f"{release.PACKAGE_ROOT}/install-macos.command")
        assert stat.S_IMODE(installer.external_attr >> 16) == 0o755
        assert all(item.date_time == (1980, 1, 1, 0, 0, 0) for item in archive.infolist())


def test_only_generated_environments_are_excluded(source: Path) -> None:
    for dirname in release.IGNORED_DIRS:
        target = source / dirname / "private.txt"
        target.parent.mkdir()
        target.write_text("Never distribute this")
    assert set(release.inventory(source)) == release.REQUIRED_FILES


@pytest.mark.parametrize("name", [
    ".env", ".env.local", "policy.json", "profiles/default/policy.json", "session.db",
    "runtime/secret.py", "src/telegram_search_mcp/token.json", "notes.txt",
])
def test_runtime_and_unlisted_files_refuse_build(source: Path, name: str) -> None:
    target = source / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("sensitive")
    with pytest.raises(release.ReleaseError):
        release.inventory(source)


@pytest.mark.parametrize("content", [
    "/" + "Users/example/private-project",
    "-----BEGIN " + "PRIVATE KEY-----",
    "ghp_" + "a" * 40,
    "123456789:" + "a" * 35,
    'api_hash = "' + "a" * 32 + '"',
    "binary\x00content",
])
def test_content_guard_refuses_secrets_without_echoing_them(source: Path, content: str) -> None:
    (source / "README.md").write_text(content)
    with pytest.raises(release.ReleaseError) as error:
        release.inventory(source)
    assert content not in str(error.value)


@pytest.mark.parametrize("directory", [False, True])
def test_symlinks_cannot_import_external_data(source: Path, tmp_path: Path, directory: bool) -> None:
    external = tmp_path / "external"
    if directory:
        external.mkdir()
        (external / "test_private.py").write_text("secret")
        (source / "tests").symlink_to(external, target_is_directory=True)
    else:
        external.write_text("secret")
        (source / "CHANGELOG.md").symlink_to(external)
    with pytest.raises(release.ReleaseError, match="[Ss]ymlink|regular"):
        release.inventory(source)


def test_archive_tampering_is_detected(source: Path, tmp_path: Path) -> None:
    original = release.build(source, tmp_path / "dist")
    payload = release.archive_payload(release.inventory(source))
    payload[release.MANIFEST_NAME] = release.manifest_bytes(payload)
    payload["README.md"] = b"Replaced by a different document\n"
    modified = tmp_path / "tampered.zip"
    release.write_zip(modified, payload)
    with pytest.raises(release.ReleaseError, match="manifest"):
        release.verify_archive(modified)
    assert release.verify_archive(original)["version"] == "0.4.0"


def test_extracted_bundle_can_run_inventory_and_build(source: Path, tmp_path: Path) -> None:
    original = release.build(source, tmp_path / "first")
    with zipfile.ZipFile(original) as archive:
        archive.extractall(tmp_path / "unpacked")
    extracted = tmp_path / "unpacked" / release.PACKAGE_ROOT
    assert release.inventory(extracted) == release.inventory(source)
    rebuilt = release.build(extracted, tmp_path / "second")
    assert original.read_bytes() == rebuilt.read_bytes()
    (extracted / "README.md").write_text("changed since release")
    with pytest.raises(release.ReleaseError, match="manifest"):
        release.inventory(extracted)


@pytest.mark.parametrize("name", ["../outside.md", "src/../../outside.py", "/absolute.md", "a\\b.md"])
def test_archive_path_traversal_is_rejected(source: Path, tmp_path: Path, name: str) -> None:
    payload = release.archive_payload(release.inventory(source))
    payload[release.MANIFEST_NAME] = release.manifest_bytes(payload)
    payload[name] = b"untrusted"
    archive = tmp_path / "unsafe.zip"
    release.write_zip(archive, payload)
    with pytest.raises(release.ReleaseError):
        release.verify_archive(archive)


def test_version_mismatch_refuses_release(source: Path) -> None:
    (source / "src/telegram_search_mcp/__init__.py").write_text('__version__ = "0.3.0"\n')
    with pytest.raises(release.ReleaseError, match="version"):
        release.inventory(source)


def test_real_repository_passes_allowlist_and_data_guard() -> None:
    payload = release.inventory(ROOT)
    assert "src/telegram_search_mcp/server.py" in payload
    assert "tests/test_packaging.py" in payload


def test_git_source_omits_derived_files_required_by_the_old_updater() -> None:
    if (ROOT / ".git").exists():
        assert not any((ROOT / name).exists() for name in release.DERIVED_FILES)
    assert release.DERIVED_FILES.isdisjoint(release.inventory(ROOT))


def test_generated_windows_assets_match_source_and_package_version(source, tmp_path):
    archive_path = release.build(source, tmp_path / "dist")
    expected = release.windows_assets(release.inventory(source))
    with zipfile.ZipFile(archive_path) as archive:
        for name, content in expected.items():
            assert archive.read(release.PACKAGE_ROOT + "/" + name) == content
    manifest = json.loads(expected["plugins/telegram-mcp-work/.codex-plugin/plugin.json"])
    assert manifest["version"] == "0.4.0"


@pytest.mark.parametrize("change", ["modified", "missing"])
def test_even_a_rehashed_archive_cannot_change_derived_assets(source, tmp_path, change):
    payload = release.archive_payload(release.inventory(source))
    if change == "modified":
        payload["install-windows.ps1"] += b"Write-Output 'unexpected'\n"
    else:
        payload.pop("install-windows.ps1")
    payload[release.MANIFEST_NAME] = release.manifest_bytes(payload)
    archive_path = tmp_path / "modified.zip"
    release.write_zip(archive_path, payload)
    with pytest.raises(release.ReleaseError, match="Generated Windows assets"):
        release.verify_archive(archive_path)


def test_windows_asset_source_is_parsed_without_executing_code(source):
    (source / release.WINDOWS_ASSETS_SOURCE).write_text("raise RuntimeError('must never execute')\n")
    with pytest.raises(release.ReleaseError, match="Invalid reviewed Windows assets"):
        release.inventory(source)


def test_legacy_smoke_snapshot_is_exactly_the_git_source_inventory(source, tmp_path):
    spec = importlib.util.spec_from_file_location("legacy_smoke", ROOT / "scripts/smoke-upgrade-06.py")
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    archive_path = release.build(source, tmp_path / "dist")
    with zipfile.ZipFile(io.BytesIO(smoke.codeload_snapshot(archive_path, release))) as archive:
        actual = {item.filename.split("/", 1)[1]: archive.read(item) for item in archive.infolist()}
    assert actual == release.inventory(source)


@pytest.mark.parametrize("license_state", ["matching", "missing", "changed"])
def test_wheel_requires_the_repository_license(source: Path, tmp_path: Path, license_state: str) -> None:
    wheel_path = tmp_path / "package.whl"
    metadata_root = "telegram_search_mcp-0.4.0.dist-info/"
    with zipfile.ZipFile(wheel_path, "w") as wheel:
        wheel.writestr("telegram_search_mcp/__init__.py", (source / "src/telegram_search_mcp/__init__.py").read_bytes())
        wheel.writestr(metadata_root + "METADATA", "Name: telegram-search-mcp\nVersion: 0.4.0\n")
        if license_state != "missing":
            license_data = (source / "LICENSE").read_bytes() if license_state == "matching" else b"Replaced license\n"
            wheel.writestr(metadata_root + "licenses/LICENSE", license_data)
    if license_state == "matching":
        assert release.verify_wheel(wheel_path, source) == 3
    else:
        with pytest.raises(release.ReleaseError, match="license|LICENSE"):
            release.verify_wheel(wheel_path, source)
