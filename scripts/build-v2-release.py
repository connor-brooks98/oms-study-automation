import hashlib
import subprocess
import zipfile
from pathlib import Path

HOTFIX_FILES = (
    ".env.example",
    "README.md",
    "docs/operations/quiz-builder.md",
    "pyproject.toml",
    "scripts/evaluate_anydoc_corpus.py",
)


def build_releases(
    root: Path,
    output_dir: Path,
    release_date: str,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    hotfix = output_dir / (
        f"Study-Hub-V2-Multi-Provider-Hotfix-{release_date}.zip"
    )
    source = output_dir / f"Study-Hub-V2-Source-{release_date}.zip"
    source_files = _source_files(root)
    runtime_files = {
        path for path in source_files if path.startswith("src/oms_hub/")
    }
    hotfix_files = tuple(sorted(set(HOTFIX_FILES) | runtime_files))
    _write_archive(root, hotfix, hotfix_files)
    _write_archive(root, source, source_files)
    _write_checksum(hotfix)
    _write_checksum(source)
    return hotfix, source


def _source_files(root: Path) -> tuple[str, ...]:
    result = subprocess.run(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    files = tuple(
        sorted(
            path
            for path in result.stdout.splitlines()
            if path and _allowed(path) and (root / path).is_file()
        )
    )
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
    root: Path,
    destination: Path,
    relative_paths: tuple[str, ...],
) -> None:
    with zipfile.ZipFile(
        destination,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for relative in sorted(relative_paths):
            if not _allowed(relative):
                raise ValueError(f"release path is not allowed: {relative}")
            source = root / relative
            if not source.is_file():
                raise FileNotFoundError(source)
            info = zipfile.ZipInfo(
                relative,
                date_time=(2026, 7, 26, 0, 0, 0),
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, source.read_bytes())


def _write_checksum(path: Path) -> None:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_suffix(path.suffix + ".sha256").write_text(
        f"{digest}  {path.name}\n",
        encoding="ascii",
    )


if __name__ == "__main__":
    project_root = Path(__file__).resolve().parents[1]
    built = build_releases(project_root, project_root / "dist", "20260728")
    for artifact in built:
        print(artifact)
        print(artifact.with_suffix(artifact.suffix + ".sha256"))
