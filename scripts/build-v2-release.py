import hashlib
import subprocess
import zipfile
from pathlib import Path

HOTFIX_FILES = (
    ".env.example",
    "README.md",
    "pyproject.toml",
    "src/oms_hub/app.py",
    "src/oms_hub/cli.py",
    "src/oms_hub/config.py",
    "src/oms_hub/ingestion/domain.py",
    "src/oms_hub/ingestion/repository.py",
    "src/oms_hub/ingestion/service.py",
    "src/oms_hub/ingestion/staging.py",
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
    "src/oms_hub/routing.py",
    "src/oms_hub/study_generation/__init__.py",
    "src/oms_hub/study_generation/domain.py",
    "src/oms_hub/study_generation/native_quiz.py",
    "src/oms_hub/study_generation/notebook.py",
    "src/oms_hub/study_generation/notebook_connection.py",
    "src/oms_hub/study_generation/outline.py",
    "src/oms_hub/study_generation/prompts.py",
    "src/oms_hub/study_generation/repository.py",
    "src/oms_hub/study_generation/service.py",
    "src/oms_hub/study_generation/worker.py",
    "src/oms_hub/transcripts/cleaner.py",
    "src/oms_hub/transcripts/pipeline.py",
    "src/oms_hub/web/llm_schemas.py",
    "src/oms_hub/web/artifact_routes.py",
    "src/oms_hub/web/generation_routes.py",
    "src/oms_hub/web/public_quiz_routes.py",
    "src/oms_hub/web/settings_routes.py",
    "src/oms_hub/web/upload_routes.py",
    "src/oms_hub/web/static/app.css",
    "src/oms_hub/web/static/public_quiz.css",
    "src/oms_hub/web/static/public_quiz.js",
    "src/oms_hub/web/static/public_quiz_library.css",
    "src/oms_hub/web/static/public_quiz_library.js",
    "src/oms_hub/web/static/settings.js",
    "src/oms_hub/web/static/lecture.js",
    "src/oms_hub/web/static/uploads.js",
    "src/oms_hub/web/templates/artifact_text.html",
    "src/oms_hub/web/templates/settings.html",
    "src/oms_hub/web/templates/lecture.html",
    "src/oms_hub/web/templates/public_quiz.html",
    "src/oms_hub/web/templates/public_quiz_library.html",
    "src/oms_hub/web/templates/uploads.html",
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
