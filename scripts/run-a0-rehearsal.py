"""Isolated launcher for an actual-process A0 rehearsal.

This file intentionally imports only the standard library until the requested
implementation checkout and interpreter have been verified.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import sysconfig
from pathlib import Path
from uuid import UUID

_ISOLATED_BOOTSTRAP = (
    "import json,runpy,sys;"
    "payload=json.loads(sys.stdin.buffer.read().decode('utf-8'));"
    "module=runpy.run_path(sys.argv[1],run_name='_a0_rehearsal_isolated');"
    "sys.exit(module['_isolated_main'](payload))"
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run an isolated actual-process A0 rehearsal")
    parser.add_argument("--capsule", required=True, type=Path)
    parser.add_argument("--overlay", required=True, type=Path)
    parser.add_argument("--mode", choices=("deterministic", "shadow"), required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--evidence-zip", required=True, type=Path)
    parser.add_argument("--failed-job-id", required=True, type=UUID)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--implementation-repository", required=True, type=Path)
    parser.add_argument("--expected-implementation-commit", required=True)
    parser.add_argument("--expected-implementation-tree", required=True)
    parser.add_argument("--trusted-python", required=True, type=Path)
    parser.add_argument("--shadow-egress-pins-json")
    parser.add_argument("--replay-supplement", type=Path)
    parser.add_argument("--expected-replay-supplement-manifest-sha256")
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    parser.add_argument("--run-goal", choices=("golden", "first_replay_miss"), default="golden")
    parser.add_argument(
        "--no-restart",
        dest="restart_after_durable_boundary",
        action="store_false",
        default=True,
        help="disable the durable-boundary restart (required for first_replay_miss)",
    )
    return parser


def _git_output(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise ValueError("implementation Git identity cannot be verified")
    return completed.stdout.strip()


def _verify_implementation_identity(args: argparse.Namespace) -> tuple[Path, Path]:
    repository = args.implementation_repository.resolve()
    # Keep the caller's launcher path distinct from its resolved base runtime:
    # POSIX virtualenv launchers are commonly symlinks, while their exact path is
    # part of the parent/child identity contract.
    trusted_python = args.trusted_python.absolute()
    if not repository.is_dir():
        raise ValueError("implementation repository is unavailable")
    if not trusted_python.is_file() or not os.access(trusted_python, os.X_OK):
        raise ValueError("trusted Python interpreter is unavailable or not executable")
    if _git_output(repository, "rev-parse", "HEAD") != args.expected_implementation_commit:
        raise ValueError("implementation commit does not match the supplied identity")
    if _git_output(repository, "rev-parse", "HEAD^{tree}") != args.expected_implementation_tree:
        raise ValueError("implementation tree does not match the supplied identity")
    if _git_output(repository, "status", "--porcelain"):
        raise ValueError("implementation repository must be clean")
    source = repository / "src"
    if not source.is_dir():
        raise ValueError("verified implementation source is unavailable")
    return source, trusted_python


def _trusted_dependency_paths() -> list[str]:
    return _canonical_dependency_paths(
        [value for key in ("purelib", "platlib") if (value := sysconfig.get_paths().get(key))]
    )


def _canonical_dependency_paths(values: object) -> list[str]:
    if not isinstance(values, list):
        raise ValueError("trusted dependency paths are malformed")
    paths: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError("trusted dependency path is malformed")
        candidate = Path(value)
        if not candidate.is_absolute() or candidate.is_symlink() or not candidate.is_dir():
            raise ValueError("trusted dependency path is unavailable or indirect")
        resolved = candidate.resolve(strict=True)
        if not resolved.is_dir() or resolved.is_symlink():
            raise ValueError("trusted dependency path is unavailable or indirect")
        canonical = str(resolved)
        if canonical not in paths:
            paths.append(canonical)
    if not paths:
        raise ValueError("trusted launcher has no dependency paths")
    return paths


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _attestation_transport(document: dict[str, object]) -> tuple[str, str]:
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )
    return base64.urlsafe_b64encode(encoded).decode("ascii"), hashlib.sha256(encoded).hexdigest()


def _decode_attestation(encoded: str | None, expected_sha256: str | None) -> dict[str, object]:
    if not encoded or not expected_sha256 or len(expected_sha256) != 64:
        raise ValueError("isolated launch attestation is unavailable")
    try:
        document = base64.b64decode(encoded.encode("ascii"), altchars=b"-_", validate=True)
        value = json.loads(document)
    except (UnicodeEncodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("isolated launch attestation is malformed") from exc
    if hashlib.sha256(document).hexdigest() != expected_sha256:
        raise ValueError("isolated launch attestation integrity check failed")
    if not isinstance(value, dict) or _attestation_transport(value)[1] != expected_sha256:
        raise ValueError("isolated launch attestation is not canonical")
    return value


def _capture_isolated_runtime(trusted_python: Path) -> tuple[Path, dict[str, object]]:
    launcher = trusted_python.absolute()
    if Path(sys.executable).absolute() != launcher:
        raise ValueError("trusted launcher does not match the running interpreter")
    base_runtime = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
    if not base_runtime.is_file() or not os.access(base_runtime, os.X_OK):
        raise ValueError("trusted base runtime is unavailable or not executable")
    return base_runtime, {
        "schema_version": 1,
        "parent_pid": os.getpid(),
        "launcher": str(launcher),
        "launcher_sha256": _sha256_file(launcher),
        "runtime": str(base_runtime),
        "runtime_sha256": _sha256_file(base_runtime),
        "runtime_version": sys.version,
        "runtime_implementation": sys.implementation.name,
        "runtime_cache_tag": sys.implementation.cache_tag,
        "dependency_paths": _trusted_dependency_paths(),
    }


def _validate_isolated_runtime(
    encoded_attestation: str | None,
    attestation_sha256: str | None,
    trusted_python: Path,
) -> list[str]:
    document = _decode_attestation(encoded_attestation, attestation_sha256)
    expected_keys = {
        "schema_version",
        "parent_pid",
        "launcher",
        "launcher_sha256",
        "runtime",
        "runtime_sha256",
        "runtime_version",
        "runtime_implementation",
        "runtime_cache_tag",
        "dependency_paths",
    }
    if set(document) != expected_keys or document["schema_version"] != 1:
        raise RuntimeError("isolated launch attestation is invalid")
    if type(document["parent_pid"]) is not int or os.getppid() != document["parent_pid"]:
        raise RuntimeError("isolated launch has no trusted parent origin")
    runtime = Path(str(document["runtime"])).resolve()
    if runtime != Path(sys.executable).resolve() or not runtime.is_file():
        raise RuntimeError("isolated runtime does not match the launched interpreter")
    if _sha256_file(runtime) != document["runtime_sha256"]:
        raise RuntimeError("isolated runtime changed before launch")
    launcher = Path(str(document["launcher"])).absolute()
    if (
        trusted_python.absolute() != launcher
        or trusted_python.resolve() != launcher.resolve()
        or _sha256_file(trusted_python) != document["launcher_sha256"]
    ):
        raise RuntimeError("trusted launcher changed before isolated launch")
    if (
        not isinstance(document["runtime_version"], str)
        or sys.version != document["runtime_version"]
    ):
        raise RuntimeError("isolated runtime version does not match the trusted launcher")
    if (
        document["runtime_implementation"] != sys.implementation.name
        or document["runtime_cache_tag"] != sys.implementation.cache_tag
    ):
        raise RuntimeError("isolated runtime build does not match the trusted launcher")
    paths = _canonical_dependency_paths(document["dependency_paths"])
    if document["dependency_paths"] != paths:
        raise RuntimeError("isolated dependency paths are not canonical")
    return paths


def _reexec_isolated(trusted_python: Path) -> int:
    runtime, attestation = _capture_isolated_runtime(trusted_python)
    encoded_attestation, attestation_sha256 = _attestation_transport(attestation)
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    payload = json.dumps(
        {
            "attestation_b64": encoded_attestation,
            "attestation_sha256": attestation_sha256,
            "arguments": sys.argv[1:],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    # A direct subprocess avoids Windows venv-launcher process-replacement crashes;
    # its exact status is returned to the original launcher without a shell.
    completed = subprocess.run(
        [
            str(runtime),
            "-I",
            "-S",
            "-c",
            _ISOLATED_BOOTSTRAP,
            str(Path(__file__).resolve()),
        ],
        env=environment,
        input=payload,
        check=False,
    )
    return completed.returncode


def _isolated_main(payload: object) -> int:
    if not isinstance(payload, dict) or set(payload) != {
        "attestation_b64",
        "attestation_sha256",
        "arguments",
    }:
        raise RuntimeError("isolated launch payload is invalid")
    arguments = payload["arguments"]
    if not isinstance(arguments, list) or not all(
        isinstance(argument, str) for argument in arguments
    ):
        raise RuntimeError("isolated launch arguments are invalid")
    args = _parser().parse_args(arguments)
    source, trusted_python = _verify_implementation_identity(args)
    if not (sys.flags.isolated and sys.flags.no_site and sys.flags.ignore_environment):
        raise RuntimeError("rehearsal launcher must run with trusted Python -I -S")
    dependency_paths = _validate_isolated_runtime(
        payload["attestation_b64"], payload["attestation_sha256"], trusted_python
    )
    sys.path[:0] = [str(source), *dependency_paths]
    from oms_hub.anki.rehearsal.process import (
        ProcessRehearsal,
        RehearsalRequest,
    )

    request = vars(args).copy()
    request["trusted_dependency_paths"] = tuple(Path(value) for value in dependency_paths)
    result = ProcessRehearsal(RehearsalRequest(**request)).run()
    print(
        f"job_id={result.job_id} overlay={result.overlay} evidence={result.evidence_zip} "
        f"run_goal={result.run_goal} outcome={result.outcome}"
    )
    return 0


def main() -> int:
    args = _parser().parse_args()
    _source, trusted_python = _verify_implementation_identity(args)
    return _reexec_isolated(trusted_python)


if __name__ == "__main__":
    raise SystemExit(main())
