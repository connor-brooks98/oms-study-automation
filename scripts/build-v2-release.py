import hashlib
import subprocess
import zipfile
from pathlib import Path

HOTFIX_FILES = (
    "src/oms_hub/app.py",
    "src/oms_hub/config.py",
    "src/oms_hub/ingestion/repository.py",
    "src/oms_hub/ingestion/worker.py",
    "src/oms_hub/llm/__init__.py",
    "src/oms_hub/llm/anthropic.py",
    "src/oms_hub/llm/domain.py",
    "src/oms_hub/llm/gemini.py",
    "src/oms_hub/llm/openai.py",
    "src/oms_hub/llm/provider.py",
    "src/oms_hub/llm/repository.py",
    "src/oms_hub/llm/service.py",
    "src/oms_hub/migrations.py",
    "src/oms_hub/models.py",
    "src/oms_hub/transcripts/cleaner.py",
    "src/oms_hub/transcripts/pipeline.py",
    "src/oms_hub/web/llm_schemas.py",
    "src/oms_hub/web/settings_routes.py",
    "src/oms_hub/web/static/app.css",
    "src/oms_hub/web/static/settings.js",
    "src/oms_hub/web/templates/settings.html",
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
    _write_archive(root, hotfix, HOTFIX_FILES)
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
            if path and _allowed(path)
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
            "__pycache__",
            "dist",
        }
        for part in path.parts
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
    built = build_releases(project_root, project_root / "dist", "20260726")
    for artifact in built:
        print(artifact)
        print(artifact.with_suffix(artifact.suffix + ".sha256"))
