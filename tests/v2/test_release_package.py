import hashlib
import importlib.util
import json
import secrets
import subprocess
import zipfile
from pathlib import Path

import pytest


def load_builder():
    path = Path(__file__).parents[2] / "scripts" / "build-v2-release.py"
    spec = importlib.util.spec_from_file_location("build_v2_release", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_builder_creates_secret_safe_hotfix_and_source_archives(tmp_path):
    builder = load_builder()
    root = Path(__file__).parents[2]
    commit = subprocess.check_output(
        ["git", "-C", root, "rev-parse", "HEAD"],
        text=True,
    ).strip()

    hotfix, source = builder.build_releases(root, tmp_path, "20260726", commit)

    with zipfile.ZipFile(hotfix) as archive:
        hotfix_names = set(archive.namelist())
    with zipfile.ZipFile(source) as archive:
        source_names = set(archive.namelist())

    assert "src/oms_hub/llm/service.py" in hotfix_names
    assert "src/oms_hub/web/static/settings.js" in hotfix_names
    assert "src/oms_hub/migrations.py" in hotfix_names
    assert "src/oms_hub/ingestion/domain.py" in hotfix_names
    assert "src/oms_hub/ingestion/service.py" in hotfix_names
    assert "src/oms_hub/ingestion/staging.py" in hotfix_names
    assert "src/oms_hub/web/upload_routes.py" in hotfix_names
    assert "src/oms_hub/web/artifact_routes.py" in hotfix_names
    assert "src/oms_hub/web/static/uploads.js" in hotfix_names
    assert "src/oms_hub/web/templates/uploads.html" in hotfix_names
    assert "src/oms_hub/web/templates/artifact_text.html" in hotfix_names
    assert "scripts/backup-sqlite.py" in hotfix_names
    assert "scripts/install-windows.ps1" in hotfix_names
    assert "scripts/start-hub.ps1" in hotfix_names
    assert "scripts/accept-f28-restart.ps1" in hotfix_names
    assert "scripts/restart-hub-after-failure.ps1" in builder._RECOVERY_HOTFIX
    assert "pyproject.toml" in source_names
    assert "src/oms_hub/app.py" in source_names
    assert "tests/v2/test_llm_settings_routes.py" in source_names
    for names in (hotfix_names, source_names):
        lowered = {name.casefold() for name in names}
        assert ".env" not in lowered
        assert not any(name.endswith(("hub.db", ".pyc")) for name in lowered)
        assert not any("__pycache__" in name for name in lowered)
        assert not any("gpt key" in name for name in lowered)

    with zipfile.ZipFile(source) as archive:
        manifest = json.loads(archive.read("RELEASE-MANIFEST.json"))
    assert manifest["commit_sha"] == commit
    assert manifest["tree_sha"] == subprocess.check_output(
        ["git", "-C", root, "rev-parse", f"{commit}^{{tree}}"],
        text=True,
    ).strip()
    assert all(len(entry["sha256"]) == 64 for entry in manifest["files"])


def test_release_builder_is_deterministic_and_excludes_untracked_files(tmp_path):
    builder = load_builder()
    root = Path(__file__).parents[2]
    commit = subprocess.check_output(
        ["git", "-C", root, "rev-parse", "HEAD"],
        text=True,
    ).strip()
    planted = root / "arbitrary-untracked-release-secret.bin"
    planted_payload = b"secret-like untracked payload: " + secrets.token_bytes(32)
    try:
        first = builder.build_releases(root, tmp_path / "first", "20260726", commit)
        planted.write_bytes(planted_payload)
        assert planted.exists()
        second = builder.build_releases(root, tmp_path / "second", "20260726", commit)
    finally:
        planted.unlink(missing_ok=True)

    for first_path, second_path in zip(first, second, strict=True):
        assert hashlib.sha256(first_path.read_bytes()).digest() == hashlib.sha256(
            second_path.read_bytes()
        ).digest()
        with zipfile.ZipFile(first_path) as archive:
            assert planted.name not in archive.namelist()
            assert all(
                planted_payload not in archive.read(member)
                for member in archive.namelist()
            )
        with zipfile.ZipFile(second_path) as archive:
            assert planted.name not in archive.namelist()
            assert all(
                planted_payload not in archive.read(member)
                for member in archive.namelist()
            )


def test_release_builder_rejects_missing_legacy_hotfix_member(tmp_path, monkeypatch):
    builder = load_builder()
    legacy = builder.HOTFIX_FILES[0]
    release_tree = builder.ReleaseTree(
        commit_sha="a" * 40,
        tree_sha="b" * 40,
        blobs={path: b"x" for path in builder.HOTFIX_FILES if path != legacy},
    )
    monkeypatch.setattr(builder, "_release_tree", lambda _root, _commit: release_tree)

    with pytest.raises(ValueError, match="required hotfix files"):
        builder.build_releases(tmp_path, tmp_path / "output", "20260811", "a" * 40)


def test_release_builder_requires_referenced_recovery_wrapper_and_packages_it(
    tmp_path, monkeypatch
):
    builder = load_builder()
    blobs = {path: b"x" for path in builder.HOTFIX_FILES}
    blobs["scripts/install-windows.ps1"] = b"restart-hub-after-failure.ps1"
    missing = builder.ReleaseTree("a" * 40, "b" * 40, blobs)
    monkeypatch.setattr(builder, "_release_tree", lambda _root, _commit: missing)
    with pytest.raises(ValueError, match="references missing recovery wrapper"):
        builder.build_releases(tmp_path, tmp_path / "missing", "20260811", "a" * 40)

    blobs[builder._RECOVERY_HOTFIX] = b"recovery"
    present = builder.ReleaseTree("a" * 40, "b" * 40, blobs)
    monkeypatch.setattr(builder, "_release_tree", lambda _root, _commit: present)
    hotfix, _ = builder.build_releases(tmp_path, tmp_path / "present", "20260811", "a" * 40)
    with zipfile.ZipFile(hotfix) as archive:
        assert builder._RECOVERY_HOTFIX in archive.namelist()


@pytest.mark.parametrize("commit", ["62c6d5f", "HEAD", "not-a-commit", "f" * 40])
def test_release_builder_rejects_nonexact_or_unresolved_commit(tmp_path, commit):
    builder = load_builder()
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        builder.build_releases(Path(__file__).parents[2], tmp_path, "20260726", commit)


def test_release_builder_rejects_symlink_entries_in_the_selected_tree(tmp_path):
    builder = load_builder()
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", repository], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", repository, "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", repository, "config", "user.name", "Release Test"],
        check=True,
    )
    blob = subprocess.check_output(
        ["git", "-C", repository, "hash-object", "-w", "--stdin"],
        input="tracked\n",
        text=True,
    ).strip()
    tree = subprocess.check_output(
        ["git", "-C", repository, "mktree"],
        input=(
            f"100644 blob {blob}\ttracked.txt\n"
            f"120000 blob {blob}\tlinked.txt\n"
        ),
        text=True,
    ).strip()
    commit = subprocess.check_output(
        ["git", "-C", repository, "commit-tree", tree, "-m", "symlink fixture"],
        text=True,
    ).strip()

    with pytest.raises(ValueError, match="symbolic link"):
        builder.build_releases(repository, tmp_path / "output", "20260726", commit)
