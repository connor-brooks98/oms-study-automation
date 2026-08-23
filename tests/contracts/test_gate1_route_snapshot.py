from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

import yaml  # type: ignore[import-untyped]

from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.features import FeatureFlag

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_COMMIT = "86bf2a7de75c5496955f978d97d3bbae075c9fef"
BASELINE_ROUTE_SNAPSHOT = (
    REPO_ROOT / "artifacts/acceptance/grounded-learning/baseline/route-snapshot-v1.json"
)
MANIFEST = REPO_ROOT / "docs/superpowers/plans/study-hub-parallel-workstream-manifest.yaml"
GATE_RECORD = REPO_ROOT / "artifacts/acceptance/grounded-learning/gate-1.json"
DESIGN = REPO_ROOT / (
    "docs/superpowers/specs/2026-08-20-study-hub-grounded-adaptive-learning-design.md"
)
PLAN = REPO_ROOT / "docs/superpowers/plans/2026-08-20-study-hub-grounded-adaptive-learning.md"
REPO_MAP = REPO_ROOT / "artifacts/implementation/repo-map-v1.json"
SCHEMAS = REPO_ROOT / "schemas"
FIXTURES = REPO_ROOT / "tests/fixtures/grounded_learning"

NEW_FLAGS = tuple(
    flag for flag in FeatureFlag if flag is not FeatureFlag.LEGACY_NOTEBOOKLM_GENERATION
)
EXPECTED_COMPLETED = ("0.1", "0.2", "0.3", "0.4", "0.5", "0.6")
EXPECTED_READY = (
    "1.1",
    "2.1",
    "3.1",
    "4.1",
    "5.1",
    "5.3",
    "6.3",
    "6.10",
    "6.13",
    "7.1",
    "7.5",
    "8.1",
    "8.2",
    "9.1",
    "9.2",
    "9.5",
)
EXPECTED_WORKSTREAMS = {
    "sol1": ("sol1/source-trust", "1.1"),
    "sol2": ("sol2/gemini-indexing", "2.1"),
    "sol3": ("sol3/ask-backend", "3.1"),
    "sol4": ("sol4/ask-frontend", "4.1"),
    "sol5": ("sol5/board-questions", "5.1", "5.3"),
    "sol6": ("sol6/adaptive-mastery", "6.3"),
    "sol7": ("sol7/anki-runway", "7.1", "7.5"),
    "sol8": ("sol8/journal-evidence", "8.1", "8.2"),
    "sol9": ("sol9/evals-observability-release", "9.1", "9.2", "9.5"),
    "sol10": ("sol10/practice-modes", "6.10", "6.13"),
}
EXPECTED_VERIFICATION_RESULTS = {
    "environment_fingerprint": "pass",
    "route_snapshot": "pass",
    "contracts_providers_features": "pass",
    "settings_generation": "pass",
    "schema_reproducibility": "pass",
    "ruff": "pass",
    "mypy_source": "pass",
    "mypy_phase0_tests": "baseline_exception",
    "javascript": "pass",
    "broad_python": "baseline_exception",
    "native_windows": "baseline_exception",
    "manifest_validation": "pass",
    "gate_json_validation": "pass",
}
SHA256_RE = r"^[0-9a-f]{64}$"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _iter_route_objects(routes: Iterable[Any]) -> Iterable[Any]:
    for route in routes:
        effective_route_contexts = getattr(route, "effective_route_contexts", None)
        if effective_route_contexts is None:
            yield route
        else:
            yield from effective_route_contexts()


def _normalize_routes(routes: Iterable[Any]) -> list[dict[str, object]]:
    normalized = [
        {
            "name": getattr(route, "name", None) or "",
            "path": route.path,
            "methods": sorted(getattr(route, "methods", None) or ()),
        }
        for route in _iter_route_objects(routes)
    ]
    return sorted(
        normalized,
        key=lambda route: (
            cast(str, route["path"]),
            cast(str, route["name"]),
            tuple(cast(list[str], route["methods"])),
        ),
    )


def _route_diff(
    expected: list[dict[str, object]], actual: list[dict[str, object]]
) -> str:
    expected_by_identity = {(route["path"], route["name"]): route for route in expected}
    actual_by_identity = {(route["path"], route["name"]): route for route in actual}
    added = [
        actual_by_identity[key]
        for key in sorted(actual_by_identity.keys() - expected_by_identity)
    ]
    removed = [
        expected_by_identity[key]
        for key in sorted(expected_by_identity.keys() - actual_by_identity)
    ]
    changed = [
        {"expected": expected_by_identity[key], "actual": actual_by_identity[key]}
        for key in sorted(expected_by_identity.keys() & actual_by_identity)
        if expected_by_identity[key] != actual_by_identity[key]
    ]
    return json.dumps(
        {"added": added, "removed": removed, "changed": changed},
        indent=2,
        sort_keys=True,
    )


def test_feature_flags_off_keep_baseline_routes(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        allow_local_access=True,
        feature_flags=cast(Any, {flag.value: False for flag in NEW_FLAGS}),
    )
    assert all(not settings.feature_flags.is_enabled(flag) for flag in NEW_FLAGS)
    assert settings.feature_flags.is_enabled(FeatureFlag.LEGACY_NOTEBOOKLM_GENERATION)

    app = create_app(settings)
    try:
        actual = _normalize_routes(app.routes)
    finally:
        app.state.database.close()

    expected = json.loads(BASELINE_ROUTE_SNAPSHOT.read_text(encoding="utf-8"))
    assert actual == expected, _route_diff(expected, actual)


def _load_manifest() -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(MANIFEST.read_text(encoding="utf-8")))


def _load_base_manifest() -> dict[str, Any]:
    raw = subprocess.check_output(
        [
            "git",
            "show",
            f"{BASELINE_COMMIT}:docs/superpowers/plans/study-hub-parallel-workstream-manifest.yaml",
        ],
        cwd=REPO_ROOT,
        text=True,
    )
    return cast(dict[str, Any], yaml.safe_load(raw))


def _manifest_after_authorized_transition() -> dict[str, Any]:
    expected = _load_base_manifest()
    for task in expected["tasks"]:
        task["initial_state"] = (
            "complete"
            if task["id"] in EXPECTED_COMPLETED
            else "ready"
            if task["id"] in EXPECTED_READY
            else "blocked"
        )
    expected["gates"]["gate_1_shared_contracts"]["state"] = "open"
    return expected


def test_manifest_is_exactly_the_gate1_state_transition() -> None:
    manifest = _load_manifest()
    assert manifest == _manifest_after_authorized_transition()

    tasks = manifest["tasks"]
    task_by_id = {task["id"]: task for task in tasks}
    assert len(task_by_id) == len(tasks) == 90
    assert {task["initial_state"] for task in tasks} <= {"complete", "ready", "blocked"}

    dependencies = {
        task_id: set(task["depends_on"])
        for task_id, task in task_by_id.items()
    }
    assert all(
        dependency in task_by_id
        for values in dependencies.values()
        for dependency in values
    )

    resolved: set[str] = set()
    while unresolved := {
        task_id
        for task_id, values in dependencies.items()
        if task_id not in resolved and not values - resolved
    }:
        resolved.update(unresolved)
    assert resolved == set(task_by_id)

    complete = {task["id"] for task in tasks if task["initial_state"] == "complete"}
    ready = {task["id"] for task in tasks if task["initial_state"] == "ready"}
    blocked = {task["id"] for task in tasks if task["initial_state"] == "blocked"}
    assert complete == set(EXPECTED_COMPLETED)
    assert ready == set(EXPECTED_READY)
    assert len(blocked) == 68
    assert all(dependencies[task_id] <= complete for task_id in ready)
    assert [task_id for task_id in blocked if dependencies[task_id] <= complete] == []

    gates = manifest["gates"]
    assert gates["gate_1_shared_contracts"]["state"] == "open"
    assert all(
        gate["state"] == "blocked"
        for gate_id, gate in gates.items()
        if gate_id != "gate_1_shared_contracts"
    )


def test_gate_record_matches_manifest_and_all_accepted_hashes() -> None:
    gate = cast(dict[str, Any], json.loads(GATE_RECORD.read_text(encoding="utf-8")))
    assert gate["schema_version"] == 1
    assert gate["gate"] == "gate_1_shared_contracts"
    assert gate["state"] == "open"
    assert gate["integration"] == {
        "branch": "integration/studyhub-grounded-learning-v1",
        "integration_ref": "integration/studyhub-grounded-learning-v1",
        "integration_sha": "SELF",
        "integration_tree_sha": "SELF_TREE",
        "self_reference_resolution": (
            "Resolve SELF and SELF_TREE from the commit containing this record."
        ),
    }
    assert gate["tasks"] == {
        "completed": list(EXPECTED_COMPLETED),
        "ready": list(EXPECTED_READY),
    }
    assert gate["contract_tag"] == {
        "name": "studyhub-grounded-contracts-v1",
        "annotated_tag_object": "a2636bbeb84d2143685c3555e9a3f74ccb8380d0",
        "peeled_target": "60a5f3ec873f982bca14d3507d719eb9927a8f1a",
    }

    checksum_paths = {
        "repo_map": REPO_MAP,
        "design": DESIGN,
        "plan": PLAN,
        "manifest": MANIFEST,
        "route_snapshot": BASELINE_ROUTE_SNAPSHOT,
    }
    assert gate["checksums"] == {
        name: _sha256(path) for name, path in checksum_paths.items()
    }
    assert all(
        isinstance(value, str) and re.fullmatch(SHA256_RE, value)
        for value in gate["checksums"].values()
    )

    schema_paths = {
        f"{name}-v1.json": SCHEMAS / f"{name}-v1.json"
        for name in ("knowledge", "ask", "question", "mastery", "practice", "journal")
    }
    assert gate["schemas"] == {name: _sha256(path) for name, path in schema_paths.items()}
    fixture_paths = {
        "lecture-13-normalized.md": FIXTURES / "course/lecture-13-normalized.md",
        "lecture-13-pages.json": FIXTURES / "course/lecture-13-pages.json",
        "article-1-normalized.md": FIXTURES / "literature/article-1-normalized.md",
        "README.md": FIXTURES / "README.md",
    }
    assert gate["fixtures"] == {name: _sha256(path) for name, path in fixture_paths.items()}

    verification = gate["verification"]
    assert {entry["name"] for entry in verification} == set(EXPECTED_VERIFICATION_RESULTS)
    assert {
        entry["name"]: entry["result"] for entry in verification
    } == EXPECTED_VERIFICATION_RESULTS
    assert all(entry["evidence"] == "docs/implementation/handoffs/0.6.md" for entry in verification)
    assert gate["known_baseline_exceptions"] == [
        {
            "id": "windows_native_acceptance",
            "status": "outstanding",
            "detail": "No native Windows pass is claimed.",
        },
        {
            "id": "pymupdf_macos_cpython312",
            "status": "inherited",
            "detail": (
                "CPython 3.12.13 and PyMuPDF 1.28.2 reproduce signal 11 "
                "in the recorded baseline path."
            ),
        },
        {
            "id": "phase0_test_typing",
            "status": "inherited",
            "detail": (
                "The frozen tests/features/test_flags.py has 13 strict-mypy errors; "
                "Task 0.6 does not edit or suppress that file."
            ),
        },
    ]

    workstreams = gate["workstreams"]
    assert [entry["id"] for entry in workstreams] == [f"sol{number}" for number in range(1, 11)]
    for entry in workstreams:
        workstream_id = entry["id"]
        branch_and_tasks = EXPECTED_WORKSTREAMS[workstream_id]
        assert entry == {
            "id": workstream_id,
            "branch": branch_and_tasks[0],
            "worktree": str(
                Path("/Users/connor/Developer/worktrees/oms-study-automation-grounded-learning")
                / workstream_id
            ),
            "ready_tasks": list(branch_and_tasks[1:]),
        }

    allowed_absolute_paths = {
        entry["worktree"] for entry in workstreams
    }
    for value in gate.values():
        if isinstance(value, str) and value.startswith("/"):
            assert value in allowed_absolute_paths
