from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

from oms_hub.providers.gemini.evidence import failure_record

PREFLIGHT_KEYS = {
    "status",
    "source_revision_hash",
    "document_types",
    "page_count",
    "slide_count",
    "provider_operation_states",
    "byte_usage",
    "warnings",
}
LIVE_KEYS = PREFLIGHT_KEYS | {
    "citation_resolution_rate",
    "duration_ms",
    "token_usage",
    "transient_attempts",
    "failure_class",
}


def corrected_blocked_record() -> dict[str, object]:
    return failure_record(
        None,
        FixtureFailure("synthetic blocked"),
        failure_stage="prior_state_check",
        states=[],
        cleanup_outcome="unknown",
        reconciliation_outcome="unknown",
    ).model_dump(mode="json")


class FixtureFailure(RuntimeError):
    pass


class _Scalar:
    def scalar_one_or_none(self) -> int:
        return 29


class _Connection:
    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, _statement: object) -> _Scalar:
        return _Scalar()


class _Engine:
    def connect(self) -> _Connection:
        return _Connection()


class _Database:
    engine = _Engine()

    def __init__(self, close_failure: bool) -> None:
        self._close_failure = close_failure

    def close(self) -> None:
        if self._close_failure:
            raise FixtureFailure("synthetic_close_failed")
        return None


class _Smoke:
    def __init__(self, mode: str) -> None:
        self._mode = mode

    def run_private_shadow_preflight(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "status": "ready",
            "source_revision_hash": "a" * 64,
            "document_types": ["markdown"],
            "page_count": 1,
            "slide_count": 1,
            "provider_operation_states": ["prior_operator_state_empty"],
            "byte_usage": {"index_inputs": 1},
            "warnings": [],
        }

    async def run_authorized_private_shadow(
        self, *_args: object, failure_evidence: dict[str, object], **_kwargs: object
    ) -> dict[str, object]:
        if self._mode == "corrected":
            failure_evidence.update(corrected_blocked_record())
        raise FixtureFailure("synthetic_blocked")


def _load_entrypoint(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("task_2_8_composition_entrypoint", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("entrypoint_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _reviewed(mode: str, database_path: Path) -> SimpleNamespace:
    def remove_tree(path: Path) -> None:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()

    def cleanup(scratch: Path) -> None:
        if mode == "cleanup_failure":
            raise FixtureFailure("synthetic_cleanup_failed")
        for child in tuple(scratch.iterdir()):
            remove_tree(child)

    return SimpleNamespace(
        OperatorFailure=FixtureFailure,
        stat=stat,
        make_url=lambda _url: SimpleNamespace(database=str(database_path)),
        _validate_scratch=lambda _scratch: None,
        _approved_hashes=lambda _project: ("a" * 64, "b" * 64),
        _runtime_configuration=lambda: ("sqlite:///fixture.db", database_path.parent),
        backup_sqlite_database=lambda _source, backup: backup.write_bytes(b"fixture"),
        Database=lambda _url: _Database(mode == "close_failure"),
        text=lambda statement: statement,
        ArtifactService=lambda *_args, **_kwargs: object(),
        _select_revision=lambda *_args: SimpleNamespace(id=1),
        _load_smoke=lambda _project: _Smoke(mode),
        _remove_tree=remove_tree,
        _cleanup=cleanup,
        _PREFLIGHT_KEYS=PREFLIGHT_KEYS,
        _LIVE_KEYS=LIVE_KEYS,
        _blocked=lambda warning: {
            "status": "blocked",
            "provider_operation_states": ["private_shadow_failed"],
            "warnings": [warning],
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--entrypoint", type=Path, required=True)
    parser.add_argument(
        "--mode",
        choices=("corrected", "fallback", "close_failure", "cleanup_failure"),
        required=True,
    )
    args = parser.parse_args()
    module = _load_entrypoint(args.entrypoint)
    with tempfile.TemporaryDirectory(prefix="task28-entrypoint-fixture-") as temporary:
        root = Path(temporary)
        scratch = root / "scratch"
        project = root / "project"
        scratch.mkdir()
        project.mkdir()
        shutil.copytree(Path(__file__).resolve().parents[2] / "src", project / "src")
        database = root / "fixture.db"
        database.write_bytes(b"fixture")
        entrypoint = cast(Any, module)
        entrypoint._load_reviewed_operator = lambda: _reviewed(args.mode, database)
        os.environ["OMS_TASK28_PRIVATE_SCRATCH"] = str(scratch)
        os.environ["OMS_TASK28_PRIVATE_PROJECT"] = str(project)
        os.environ["RUN_PRIVATE_GEMINI_SHADOW"] = "1"
        return int(entrypoint.main())


if __name__ == "__main__":
    raise SystemExit(main())
