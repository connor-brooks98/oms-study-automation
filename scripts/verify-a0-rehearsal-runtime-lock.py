"""Verify an already-provisioned A0 Windows runtime without package installation."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import re
import sys
from pathlib import Path
from typing import Any

_LOCK_NAME = "a0-rehearsal-windows-py312.lock.json"
_EXCLUDED_DISTRIBUTIONS = frozenset({"oms-study-automation"})


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")


def _normalized_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).casefold()


def _load_lock(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("runtime lock is unavailable or malformed") from exc
    if not isinstance(value, dict):
        raise RuntimeError("runtime lock is malformed")
    return value


def _installed_distributions(dependency_paths: list[Path]) -> list[dict[str, str]]:
    seen: set[str] = set()
    values: list[tuple[str, dict[str, str]]] = []
    for distribution in importlib.metadata.distributions(
        path=[str(path) for path in dependency_paths]
    ):
        name = distribution.metadata.get("Name")
        version = distribution.metadata.get("Version")
        if not isinstance(name, str) or not name or not isinstance(version, str) or not version:
            raise RuntimeError("installed distribution metadata is incomplete")
        normalized = _normalized_name(name)
        if normalized in _EXCLUDED_DISTRIBUTIONS:
            continue
        if normalized in seen:
            raise RuntimeError("installed distribution names are not unique")
        seen.add(normalized)
        values.append((normalized, {"name": name, "version": version}))
    return [value for _normalized, value in sorted(values)]


def _verify_runtime_lock(
    document: dict[str, Any],
    installed: list[dict[str, str]],
    *,
    python_version: str,
    implementation: str,
    cache_tag: str,
) -> None:
    if set(document) != {
        "cache_tag",
        "distribution_count",
        "distributions",
        "distributions_sha256",
        "implementation",
        "python_version",
        "schema_version",
    } or document.get("schema_version") != 1:
        raise RuntimeError("runtime lock is malformed")
    if (
        document.get("python_version") != python_version
        or document.get("implementation") != implementation
        or document.get("cache_tag") != cache_tag
    ):
        raise RuntimeError("runtime interpreter identity does not match lock")
    locked = document.get("distributions")
    if not isinstance(locked, list) or document.get("distribution_count") != len(locked):
        raise RuntimeError("runtime lock distribution count is invalid")
    digest = hashlib.sha256(_canonical_json(locked)).hexdigest()
    if document.get("distributions_sha256") != digest:
        raise RuntimeError("runtime lock distribution digest is invalid")
    if installed != locked:
        raise RuntimeError("runtime distribution closure does not match lock")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lock",
        type=Path,
        default=Path(__file__).resolve().with_name(_LOCK_NAME),
    )
    parser.add_argument("--dependency-path", action="append", required=True, type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    dependency_paths: list[Path] = []
    for supplied in args.dependency_path:
        if not supplied.is_absolute() or supplied.is_symlink() or not supplied.is_dir():
            raise RuntimeError("dependency path is unavailable or indirect")
        resolved = supplied.resolve(strict=True)
        if resolved not in dependency_paths:
            dependency_paths.append(resolved)
    document = _load_lock(args.lock.resolve(strict=True))
    installed = _installed_distributions(dependency_paths)
    _verify_runtime_lock(
        document,
        installed,
        python_version=".".join(str(value) for value in sys.version_info[:3]),
        implementation=sys.implementation.name,
        cache_tag=sys.implementation.cache_tag,
    )
    print(
        "RUNTIME_LOCK_OK "
        f"count={len(installed)} sha256={hashlib.sha256(_canonical_json(installed)).hexdigest()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
