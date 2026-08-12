"""Isolated launcher for an actual-process A0 rehearsal.

This file intentionally imports only the standard library until the requested
implementation checkout and interpreter have been verified.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import sysconfig
from pathlib import Path
from uuid import UUID


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
    parser.add_argument("--isolated-launch", action="store_true", help=argparse.SUPPRESS)
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
    trusted_python = args.trusted_python.resolve()
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
    paths: list[str] = []
    for key in ("purelib", "platlib"):
        value = sysconfig.get_paths().get(key)
        if value is not None and Path(value).is_dir() and value not in paths:
            paths.append(value)
    return paths


def _reexec_isolated(trusted_python: Path) -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    os.execve(
        str(trusted_python),
        [
            str(trusted_python),
            "-I",
            "-S",
            str(Path(__file__).resolve()),
            "--isolated-launch",
            *sys.argv[1:],
        ],
        environment,
    )


def main() -> int:
    args = _parser().parse_args()
    source, trusted_python = _verify_implementation_identity(args)
    if not args.isolated_launch:
        _reexec_isolated(trusted_python)
    if not (sys.flags.isolated and sys.flags.no_site and sys.flags.ignore_environment):
        raise RuntimeError("rehearsal launcher must run with trusted Python -I -S")
    sys.path[:0] = [str(source), *_trusted_dependency_paths()]
    from oms_hub.anki.rehearsal.process import ProcessRehearsal, RehearsalRequest

    request = vars(args).copy()
    request.pop("isolated_launch")
    result = ProcessRehearsal(RehearsalRequest(**request)).run()
    print(f"job_id={result.job_id} overlay={result.overlay} evidence={result.evidence_zip}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
