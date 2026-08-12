import argparse
import hashlib
import json
import re
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path

HOTFIX_FILES = (
    ".env.example",
    "README.md",
    "docs/operations/quiz-builder.md",
    "pyproject.toml",
    "scripts/backup-sqlite.py",
    "scripts/accept-f28-restart.ps1",
    "scripts/evaluate_anydoc_corpus.py",
    "scripts/install-windows.ps1",
    "scripts/start-hub.ps1",
)
_RECOVERY_HOTFIX = "scripts/restart-hub-after-failure.ps1"
_FULL_COMMIT_SHA = re.compile(r"[0-9a-fA-F]{40}\Z")
_ZIP_TIMESTAMP = (2026, 7, 26, 0, 0, 0)
_MANIFEST_NAME = "RELEASE-MANIFEST.json"


@dataclass(frozen=True, slots=True)
class ReleaseTree:
    commit_sha: str
    tree_sha: str
    blobs: dict[str, bytes]


def build_releases(
    root: Path,
    output_dir: Path,
    release_date: str,
    commit_sha: str,
) -> tuple[Path, Path]:
    """Build deterministic release archives from one immutable Git commit.

    The caller must name the complete reviewed commit.  No file is ever read
    from the worktree, so a dirty checkout cannot affect the archive.
    """
    release_tree = _release_tree(root, commit_sha)
    output_dir.mkdir(parents=True, exist_ok=True)
    hotfix = output_dir / f"Study-Hub-V2-Multi-Provider-Hotfix-{release_date}.zip"
    source = output_dir / f"Study-Hub-V2-Source-{release_date}.zip"
    source_files = _source_files(release_tree)
    runtime_files = {
        path for path in source_files if path.startswith("src/oms_hub/")
    }
    # A release is built from the named immutable tree, never the dirty
    # worktree.  New fixed files become mandatory once the reviewed commit
    # contains them; older historical commits remain reproducible.
    missing_required = set(HOTFIX_FILES) - set(release_tree.blobs)
    if missing_required:
        raise ValueError(
            f"release tree is missing required hotfix files: {sorted(missing_required)}"
        )
    fixed_files = set(HOTFIX_FILES)
    if _RECOVERY_HOTFIX in release_tree.blobs:
        fixed_files.add(_RECOVERY_HOTFIX)
    elif any(
        b"restart-hub-after-failure.ps1" in payload
        for path, payload in release_tree.blobs.items()
        if path in {"scripts/install-windows.ps1", "scripts/accept-f28-restart.ps1"}
    ):
        raise ValueError("action-chain release tree references missing recovery wrapper")
    hotfix_files = tuple(sorted(fixed_files | runtime_files))
    _write_archive(release_tree, hotfix, hotfix_files)
    _write_archive(release_tree, source, source_files)
    _write_checksum(hotfix)
    _write_checksum(source)
    return hotfix, source


def _release_tree(root: Path, commit_sha: str) -> ReleaseTree:
    if not _FULL_COMMIT_SHA.fullmatch(commit_sha):
        raise ValueError("release commit must be an explicit full 40-hex SHA")
    resolved_commit = _git(root, "rev-parse", "--verify", f"{commit_sha}^{{commit}}")
    if resolved_commit.casefold() != commit_sha.casefold():
        raise ValueError("release commit did not resolve exactly to the requested SHA")
    tree_sha = _git(root, "rev-parse", "--verify", f"{resolved_commit}^{{tree}}")
    blobs: dict[str, bytes] = {}
    listing = _git_bytes(root, "ls-tree", "-r", "-z", tree_sha)
    for entry in listing.split(b"\0"):
        if not entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        mode, object_type, object_sha = metadata.decode("ascii").split(" ")
        path = raw_path.decode("utf-8")
        if mode == "120000":
            raise ValueError(f"release tree contains a symbolic link: {path}")
        if object_type != "blob" or mode not in {"100644", "100755"}:
            raise ValueError(f"release tree contains unsupported entry: {path}")
        if (
            path.startswith("/")
            or "\\" in path
            or any(part in {"", ".", ".."} for part in Path(path).parts)
        ):
            raise ValueError(f"release tree contains unsafe path: {path}")
        blobs[path] = _git_bytes(root, "cat-file", "blob", object_sha)
    if not blobs:
        raise ValueError("release commit tree has no blobs")
    return ReleaseTree(resolved_commit, tree_sha, blobs)


def _git(root: Path, *arguments: str) -> str:
    return _git_bytes(root, *arguments).decode("ascii").strip()


def _git_bytes(root: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return result.stdout


def _source_files(release_tree: ReleaseTree) -> tuple[str, ...]:
    files = tuple(sorted(path for path in release_tree.blobs if _allowed(path)))
    if not files:
        raise ValueError("source release has no files")
    return files


def _allowed(relative: str) -> bool:
    path = Path(relative)
    lowered = relative.casefold()
    if path.name == ".env" or path.suffix.casefold() in {".db", ".pyc"}:
        return False
    if any(
        part.casefold()
        in {
            ".git",
            ".mypy_cache",
            ".pytest_cache",
            ".ruff_cache",
            ".venv",
            ".superpowers",
            "__pycache__",
            "browser-profile",
            "dist",
        }
        for part in path.parts
    ):
        return False
    forbidden_names = {
        "notebooklm-storage.json",
        "oauth-client.json",
        "storage-state.json",
        "storage_state.json",
        "token.json",
        "trace.zip",
    }
    if path.name.casefold() in forbidden_names:
        return False
    if (
        path.suffix.casefold() == ".pdf"
        and any(part.casefold() in {"artifacts", "lecture outlines"} for part in path.parts)
    ):
        return False
    return "gpt key" not in lowered


def _write_archive(
    release_tree: ReleaseTree,
    destination: Path,
    relative_paths: tuple[str, ...],
) -> None:
    payloads: dict[str, bytes] = {}
    for relative in sorted(relative_paths):
        if not _allowed(relative):
            raise ValueError(f"release path is not allowed: {relative}")
        try:
            payloads[relative] = release_tree.blobs[relative]
        except KeyError as error:
            raise ValueError(f"release path is absent from commit tree: {relative}") from error
    payloads[_MANIFEST_NAME] = _manifest_bytes(release_tree, payloads)
    with zipfile.ZipFile(
        destination,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=True,
    ) as archive:
        for relative in sorted(payloads):
            info = zipfile.ZipInfo(relative, date_time=_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            info.create_system = 3
            archive.writestr(
                info,
                payloads[relative],
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            )


def _manifest_bytes(release_tree: ReleaseTree, payloads: dict[str, bytes]) -> bytes:
    manifest = {
        "commit_sha": release_tree.commit_sha,
        "files": [
            {"path": path, "sha256": hashlib.sha256(payload).hexdigest()}
            for path, payload in sorted(payloads.items())
        ],
        "tree_sha": release_tree.tree_sha,
    }
    return (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _write_checksum(path: Path) -> None:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_suffix(path.suffix + ".sha256").write_text(
        f"{digest}  {path.name}\n",
        encoding="ascii",
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build immutable Study Hub release archives")
    parser.add_argument("--commit", required=True, help="reviewed full 40-hex commit SHA")
    parser.add_argument("--output-dir", type=Path, default=Path("dist"))
    parser.add_argument("--release-date", default="20260728")
    return parser.parse_args()


if __name__ == "__main__":
    arguments = _parse_args()
    project_root = Path(__file__).resolve().parents[1]
    built = build_releases(
        project_root,
        arguments.output_dir,
        arguments.release_date,
        arguments.commit,
    )
    for artifact in built:
        print(artifact)
        print(artifact.with_suffix(artifact.suffix + ".sha256"))
